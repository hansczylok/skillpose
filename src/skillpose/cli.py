"""Command line: skillpose analyze | vocab | eval | demo build | demo site | web | export."""

import argparse
import logging
import os
import shlex
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import anthropic
import httpx2

from skillpose.core.config import Settings, load_settings
from skillpose.core.llm import LLM, BudgetExceeded, Stopped, estimate_usd
from skillpose.core.models import NOT_COUNTED, JobMatch, JobProfile, RoleReport, SkillProposal
from skillpose.core.prompts import PromptError, active_prompts, use_prompts
from skillpose.core.textio import CvTextError, read_cv, read_postings
from skillpose.core.vocabulary import Vocabulary, proposals_of
from skillpose.demo import DemoBuildError, build_demo, build_estimate
from skillpose.evaluation import STEPS as EVAL_STEPS
from skillpose.evaluation import check_request, read_expected, run_eval
from skillpose.export import ExportError, build_export
from skillpose.logs import console, file_log, log_path
from skillpose.pipeline import (
    Run,
    analyze_postings,
    attach_client,
    collect_batches,
    ensure_profile,
    estimate,
    ingest_postings,
    limit_message,
    market,
    merge_proposals,
    open_run,
    plan_postings,
    refresh_matches,
    rematch,
)
from skillpose.stability import StabilityPlan, run_stability, stability_plan
from skillpose.store.db import StabilityStore, Store
from skillpose.web.security import bind_error, host_names, hosts_error

DEFAULT_DB = Path("data/skillpose.sqlite")
GOLDSET_DB = Path("data/goldset.sqlite")  # gold-set results (eval and demo) stay out of the own database
STABILITY_DB = Path("data/stability.sqlite")  # the runs of eval --repeat: never in the gold-set store or the demo
PROMPTS_HELP = "folder with cv.md, job.md, relation.md and path.md (default: SKILLPOSE_PROMPTS, else the examples)"
LOG_HELP = "log file, or off for none (default: SKILLPOSE_LOG, else skillpose.log in the folder of --db)"

log = logging.getLogger(__name__)


def load_env_file(path: Path) -> None:
    """Set KEY=VALUE lines as environment variables. Values are never printed."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip("\"'")


def apply_env(settings: Settings) -> Settings:
    """A copy of the settings with what the environment sets: MONTHLY_LIMIT_USD (US dollars)."""
    update = {}
    if value := os.environ.get("MONTHLY_LIMIT_USD", "").strip():
        try:
            update["monthly_limit_usd"] = float(value)
        except ValueError:
            raise ValueError(f"MONTHLY_LIMIT_USD must be a number, not {value!r}") from None
    return settings.model_copy(update=update, deep=True)


def _settings(args) -> Settings:
    """Settings and prompt set of analyze, eval, demo build and web; a prompt folder that cannot be loaded raises
    PromptError (main exits 2), so no command runs with other prompts than the chosen ones."""
    if args.env_file:
        load_env_file(args.env_file)
    use_prompts(args.prompts or os.environ.get("SKILLPOSE_PROMPTS"))
    return apply_env(load_settings(args.settings))


def _announce_prompts() -> None:
    """The prompt set comes first, before any estimate or question about money: the user sees which prompts a run
    would pay for."""
    print(f"prompt set: {active_prompts().label}")


def _client(client_factory: Callable[[], object] | None):
    return client_factory() if client_factory is not None else anthropic.Anthropic()


NO_KEY_LINE = "no API key: set ANTHROPIC_API_KEY or pass --env-file"


def _has_key() -> bool:
    """A key in the environment (an --env-file has set it by now); a blank one counts as none."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _key_missing(client_factory: Callable[[], object] | None) -> bool:
    """Whether a command that pays has no way to make its client: no key (_has_key) and no factory (the tests give
    one). Without the check the SDK raises a TypeError at the first call, after the command has opened and written its
    database."""
    return client_factory is None and not _has_key()


def _batch_line(done: int, total: int) -> str:
    return f"{done} of {total} postings analyzed, {total - done} stay open"


def _cv_failure(reason: str | Stopped | None) -> str:
    """Why step 1 gave no profile. An answer that stopped early was paid: the line says what happened, the token
    limit and the cost of the attempt."""
    if not isinstance(reason, Stopped):
        return str(reason)
    usd = f"{reason.cost_usd:.{2 if reason.cost_usd >= 0.01 else 4}f}"
    if reason.reason == "max_tokens":
        return (f"the answer was too long and was cut off (limit {reason.max_tokens:,} tokens). Cost of the attempt: "
                f"{usd} USD. Try again or shorten the CV.")
    return f"Claude refused the analysis. Cost of the attempt: {usd} USD. Check the text and try again."


def _print_still_open(store: Store, hashes: list[str]) -> None:
    """Why each of these postings is still open (a refusal, no result in the batch, a lost batch)."""
    tried = set(hashes)
    for row in store.postings():
        if row.hash in tried and row.status == "open" and row.error:
            print(f"posting {row.hash} stays open: {row.error}")


def _top_gaps(jm: JobMatch, vocab: Vocabulary, proposed: dict[str, SkillProposal] | None = None, n: int = 3) -> str:
    """The first n unmet requirements; a skill the vocabulary does not know yet (proposed in this run, merged only
    at its end) shows the name Claude proposed, else its id. A condition that is not met is no gap that can be
    learned: it shows as a condition."""
    if not jm.matches:
        return "no requirements extracted"
    unmet = [m for m in jm.matches if m.status not in ("met", *NOT_COUNTED)]
    unmet.sort(key=lambda m: (m.requirement.importance != "must", m.status != "gap"))
    names = []
    for m in unmet[:n]:
        skill, proposal = vocab.get(m.requirement.skill_id), (proposed or {}).get(m.requirement.skill_id)
        label = skill.name_en if skill else proposal.name if proposal else m.requirement.skill_id
        what = "condition: not met" if m.requirement.kind == "condition" else m.gap_type or m.status
        names.append(f"{label} ({what})")
    return ", ".join(names) or "-"


def _print_table(rows: list[tuple[JobProfile, JobMatch]], vocab: Vocabulary) -> None:
    print(f"{'title':40} | {'role_family':24} | tier | must % | top gaps")
    print("-" * 110)
    for jp, jm in sorted(rows, key=lambda r: (r[1].tier, -r[1].must_coverage, r[0].title)):
        print(f"{jp.title[:40]:40} | {jp.role_family[:24]:24} | {jm.tier:^4} | {jm.must_coverage:6.0%} | "
              f"{_top_gaps(jm, vocab, proposals_of(jp))}")


def _print_role_families(reports: list[RoleReport]) -> None:
    if not reports:
        return
    print("Role families (tier, postings, median must coverage):")
    for r in reports:
        if r.tier is None:
            print(f"  {r.role_family:24}  too little data ({r.postings} postings)")
        else:
            print(f"  {r.role_family:24}  tier {r.tier}  {r.postings:>5} postings  {r.median_coverage * 100:>3.0f} %")


def _analyze(args, client_factory) -> int:
    settings = _settings(args)
    _announce_prompts()
    if _key_missing(client_factory):
        print(NO_KEY_LINE)
        return 2
    run_start = datetime.now(UTC)
    try:
        cv_text = read_cv(args.cv)
    except CvTextError as e:
        print(f"CV {args.cv}: {e}")
        return 2
    with Store(args.db) as store:
        run = open_run(settings, store, args.lang, now=run_start)
        return _analyze_cv(args, client_factory, run, cv_text, run_start)


def _analyze_cv(args, client_factory, run: Run, cv_text: str, run_start: datetime) -> int:
    store, settings = run.store, run.settings
    hashes, skipped = ingest_postings(run, read_postings(args.jobs))
    for message in skipped:
        print(message)
    plan = plan_postings(run, hashes, no_batch=args.no_batch)

    cv_id = store.find_cv(cv_text)
    needs_profile = cv_id is None or store.profile(cv_id) is None
    cost = estimate(run, cv_text=cv_text if needs_profile else None, plan=plan)
    if (message := limit_message(run, cost)) is not None:
        print(message)
        return 4
    if cost > settings.confirm_above_usd and not args.yes:
        print(f"estimated cost {cost:.2f} USD is above {settings.confirm_above_usd:.2f} USD "
              f"({len(plan.open)} new postings) - run again with --yes")
        return 3

    run = attach_client(run, _client(client_factory))
    rc = 0
    try:
        if plan.pending:
            for batch_id, batch_hashes in plan.pending:
                print(f"collecting batch {batch_id} of an earlier run ({len(batch_hashes)} postings) ...")
            print(_batch_line(collect_batches(run), sum(len(h) for _, h in plan.pending)))
            _print_still_open(store, [h for _, batch_hashes in plan.pending for h in batch_hashes])
        if cv_id is None:
            cv_id = store.save_cv(Path(args.cv).name, cv_text)
        profile, error = ensure_profile(run, cv_id)
        if profile is None:
            print(f"CV analysis failed: {_cv_failure(error)}")
            return 5

        outcome = analyze_postings(run, plan, wait=False)  # the batches go out first, then the single calls
        if plan.batch:
            for batch_id, group in zip(outcome.batch_ids, plan.groups, strict=True):
                print(f"batch {batch_id} with {len(group)} postings submitted (half price)")
            if plan.single:
                print(f"first result: {outcome.done} of {len(plan.single)} postings analyzed at once")
                _print_table(rematch(run, cv_id, plan.single), run.vocab)
            print("waiting for the batches - if this run is interrupted, the next run collects them")
            done = collect_batches(run, collected=lambda batch_id, analyzed, total: print(
                f"batch {batch_id} collected: {_batch_line(analyzed, total)}"))
            print(_batch_line(done, plan.batched))
        _print_still_open(store, plan.open)

        _print_table(rematch(run, cv_id, hashes), run.vocab)
        refresh_matches(run)  # the market view counts every stored match of the CV, not only those of this run
        _print_role_families(market(store, settings, cv_id))
    except BudgetExceeded as e:
        wanted = set(hashes) - {h for _, batch_hashes in store.pending_batches() for h in batch_hashes}
        left = len([h for h in store.open_postings() if h in wanted])  # a batch that went out is not open
        print(f"{e}: monthly limit reached - {left} postings left open; the next run continues")
        rc = 4
    finally:
        merge_proposals(run, [cv_id] if cv_id is not None else [], hashes)

    summary = store.cost_summary(since=run_start)
    print(f"Cost this run: {summary.usd:.4f} USD, cache read share: {summary.cache_read_share:.0%}, "
          f"month: {store.month_total_usd():.2f} / {settings.monthly_limit_usd:.2f} USD")
    return rc


def _vocab(args, client_factory) -> int:
    with Store(args.db) as store:
        if args.accept:
            store.accept_skills(args.accept)
            print(f"accepted: {', '.join(args.accept)}")
        if args.reject:
            store.reject_skills(args.reject)
            print(f"rejected: {', '.join(args.reject)}")
        if args.new:
            for skill in store.added_skills(reviewed=False):
                print(f"{skill.id}\t{skill.name_en}\t{skill.category}")
    return 0


def _eval(args, client_factory) -> int:
    settings = _settings(args)
    _announce_prompts()
    if args.job_model:
        settings.steps["job"].model = args.job_model
    if args.job_effort:
        settings.steps["job"].effort = args.job_effort
    if args.repeat is not None or args.repeat_jobs:
        return _eval_repeat(args, client_factory, settings)
    if _key_missing(client_factory):
        print(NO_KEY_LINE)
        return 2
    steps = set(EVAL_STEPS) if args.step == "all" else {args.step}
    goldset = Path(args.goldset)
    try:
        expected = read_expected(goldset)
        check_request(expected, steps, args.lang)
    except (OSError, ValueError) as e:
        print(f"gold set {goldset}: {e}")
        return 2
    n_cvs = len(expected.get("cvs") or {}) if "cv" in steps else 0
    n_jobs = len(expected.get("postings") or {}) if "job" in steps else 0
    estimate = estimate_usd(settings, "cv", n_cvs, avg_out=4000) + estimate_usd(settings, "job", n_jobs)
    if "match" in steps:  # step 3b: a few cheap calls per person
        estimate += estimate_usd(settings, "relation", n_cvs * 20, avg_in=300, avg_out=300, fixed_tokens=6000)
    with Store(args.db) as store:
        if (message := limit_message(open_run(settings, store, "en"), estimate)) is not None:  # only the ledger
            print(message)
            return 4
        if estimate > settings.confirm_above_usd and not args.yes:
            print(f"estimated cost {estimate:.2f} USD is above {settings.confirm_above_usd:.2f} USD - "
                  "run again with --yes")
            return 3
        llm = LLM(_client(client_factory), settings, store)
        try:
            report = run_eval(llm, goldset, Vocabulary.load(), settings, steps, args.lang,
                              store=store if args.save else None)
        except BudgetExceeded as e:
            print(f"{e}: eval stopped")
            return 4
    _write_report(args, report.to_markdown())
    return 0


def _write_report(args, markdown: str) -> None:
    """The file first, so that a console that cannot show a character never loses a paid report; the console gets
    UTF-8 (the stability report has formulas with ∩ and ×)."""
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(markdown, encoding="utf-8")
    _print_utf8(markdown + "\n")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _plan_text(plan: StabilityPlan, repeat: int, repeat_jobs: int) -> str:
    """What the command makes: the new runs to reach the targets and the relation checks at most."""
    parts = [f"{_plural(plan.cv_new, 'new CV run')} (target {repeat} per CV)"] if repeat else []
    if repeat_jobs:
        parts.append(f"{_plural(plan.jobs_missing, 'new run')} of the {plan.n_postings} postings"
                     + (" as a batch" if plan.batch else "") + f" (target {repeat_jobs})")
    parts.append(f"at most {_plural(plan.relation_calls, 'relation check')}")
    return " and ".join(parts) if len(parts) < 3 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _same_file(a: Path, b: Path) -> bool:
    a, b = Path(a), Path(b)
    if a.exists() and b.exists():
        return os.path.samefile(a, b)
    return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())


def _continue_line(args) -> str:
    """The command to continue a stopped measurement: the same one, which makes only what is missing."""
    return ("to continue, run the same command again (it makes only what is missing: nothing finished is paid twice, "
            "a call interrupted mid-answer is paid once and made again): "
            f"skillpose {shlex.join(args.argv)}")


def _eval_repeat(args, client_factory, settings: Settings) -> int:
    """eval --repeat: the run-to-run variance (stability.py). The new runs and their costs go to --stability-db; the
    gold-set store of --db is only read (read-only), for the base run of each input. Every stop after the first call
    names the command that continues."""
    if args.save:
        print("--save does not go with --repeat: the repeated runs never go into the gold-set store")
        return 2
    if _same_file(args.stability_db, args.db) or _same_file(args.stability_db, DEFAULT_DB):
        print(f"--stability-db {args.stability_db} must be a file of its own, not --db or {DEFAULT_DB}")
        return 2
    repeat, repeat_jobs, goldset = args.repeat or 0, args.repeat_jobs, Path(args.goldset)
    with ExitStack() as stack:
        base, base_note = None, None  # the note comes after the estimate, which follows the prompt set
        if args.no_base:
            base_note = "--no-base: the stored results of `eval --save` do not count"
        elif not Path(args.db).is_file():
            base_note = (f"no stored result in {args.db} (`eval --save` stores them): only the runs in "
                         f"{args.stability_db} count")
        else:
            try:
                base = stack.enter_context(Store(args.db, read_only=True))
            except ValueError as e:
                print(e)
                return 2
        store = stack.enter_context(StabilityStore(args.stability_db))
        vocab = Vocabulary.load()
        try:
            plan = stability_plan(settings, goldset, store, base, vocab, repeat, repeat_jobs, args.lang)
        except (OSError, ValueError) as e:
            print(f"gold set {goldset}: {e}")
            return 2
        # The stability store is opened first: the plan needs it (what is stored decides whether any call is left), and
        # only a plan with a call needs a key. A report of stored runs needs none.
        if (plan.calls or store.pending()) and _key_missing(client_factory):
            print(NO_KEY_LINE)
            return 2
        what = _plan_text(plan, repeat, repeat_jobs)
        if (message := limit_message(open_run(settings, store, "en"), plan.usd)) is not None:  # only the ledger
            print(message)
            return 4
        if plan.usd > settings.confirm_above_usd and not args.yes:
            print(f"estimated cost {plan.usd:.2f} USD for {what}: above {settings.confirm_above_usd:.2f} USD - "
                  "run again with --yes")
            if base_note:
                print(base_note)
            return 3
        print(f"estimated cost {plan.usd:.2f} USD for {what} (an upper bound)")
        if base_note:
            print(base_note)
        booked_before = len(store.cost_rows())  # the ledger has seconds: a time would also count a row of just before
        try:
            # nothing to make and no batch to collect: the report of the stored runs needs no client and no key
            llm = LLM(_client(client_factory), settings, store) if plan.calls or store.pending() else None
            report = run_stability(llm, goldset, vocab, settings, store, base, repeat=repeat, repeat_jobs=repeat_jobs,
                                   lang=args.lang, say=print)
        except BudgetExceeded as e:
            print(f"{e}: stopped - the runs made so far are stored in {args.stability_db}")
            print(_continue_line(args))
            return 4
        except (anthropic.AnthropicError, httpx2.HTTPError) as e:
            print(f"stopped by an API error ({type(e).__name__}: {e}) - the runs made so far are stored in "
                  f"{args.stability_db}")
            print(_continue_line(args))
            return 5
        except KeyboardInterrupt:
            print(f"stopped - the runs made so far are stored in {args.stability_db}")
            print(_continue_line(args))
            raise
        booked = store.cost_rows()[booked_before:]
    _write_report(args, report.to_markdown())
    tokens = sum(r.input + r.cache_write + r.cache_read for r in booked)
    share = sum(r.cache_read for r in booked) / tokens if tokens else 0.0
    print(f"Cost this run: {sum(r.usd for r in booked):.4f} USD, cache read share: {share:.0%}")
    return 0


def _demo_build(args, client_factory) -> int:
    settings = _settings(args)
    _announce_prompts()
    if _key_missing(client_factory):
        print(NO_KEY_LINE)
        return 2
    run_start = datetime.now(UTC)
    with Store(args.db) as store:
        run = open_run(settings, store, "en", now=run_start)  # postings in English, as `eval` analyzes them
        try:
            cost = build_estimate(run, args.goldset, args.paths)
        except (OSError, ValueError) as e:
            print(f"gold set {args.goldset}: {e}")
            return 2
        if (message := limit_message(run, cost)) is not None:
            print(message)
            return 4
        # The paths are the one result that the prompt set keys: with the examples, the paths that the private prompts
        # made give another input and are made again, and paid for, over the stored ones. The note says only what is
        # always true (a stored path that the examples made is reused for free); the estimate stays an upper bound.
        note = ""
        if active_prompts().label == "example" and store.has_paths():
            note = " (paths made with other prompts are paid again)"
        if cost > settings.confirm_above_usd and not args.yes:
            print(f"estimated cost {cost:.2f} USD is above {settings.confirm_above_usd:.2f} USD - "
                  f"run again with --yes{note}")
            return 3
        if note:
            print(f"estimated cost {cost:.2f} USD{note}")
        run = attach_client(run, _client(client_factory))
        rc = 0
        try:
            for path in build_demo(run, args.goldset, args.out, n_paths=args.paths):
                print(f"wrote {path}")
        except BudgetExceeded as e:
            print(f"{e}: demo build stopped - what was paid for is stored; the next run continues")
            rc = 4
        except DemoBuildError as e:
            print(f"demo build failed: {e}")
            rc = 5
        summary = store.cost_summary(since=run_start)
        print(f"Cost this run: {summary.usd:.4f} USD, cache read share: {summary.cache_read_share:.0%}, "
              f"month: {store.month_total_usd():.2f} / {settings.monthly_limit_usd:.2f} USD")
    return rc


def _demo_site(args, client_factory) -> int:
    """No key and no client: the site is rendered from the packaged bundles only."""
    try:
        from skillpose.site import DEFAULT_REPO_URL, SiteError, build_site
    except ImportError:
        print('the static site needs the web extra and the test client of the dev extra: '
              'pip install "skillpose[web,dev]"')
        return 2
    try:
        pages = build_site(args.out, args.repo_url or os.environ.get("SKILLPOSE_REPO_URL") or DEFAULT_REPO_URL)
    except SiteError as e:
        print(f"demo site failed: {e}")
        return 5
    print(f"wrote {pages} pages to {args.out} - open {args.out / 'index.html'}")
    return 0


def _web(args, client_factory) -> int:
    settings = _settings(args)
    has_key = _has_key()
    password = os.environ.get("APP_PASSWORD") or None
    extra = [h.strip() for h in os.environ.get("SKILLPOSE_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if (message := bind_error(args.host, password, has_key) or hosts_error(extra, password)) is not None:
        print(message)
        return 2
    try:
        import uvicorn

        from skillpose.web.app import WebConfig, create_app
    except ImportError:
        print('the web app needs the web extra: pip install "skillpose[web]"')
        return 2
    config = WebConfig(db_path=args.db, settings=settings,
                       client_factory=(client_factory or anthropic.Anthropic) if has_key else None,
                       demo_dir=args.demo_dir, password=password)
    config.allowed_hosts = list(dict.fromkeys([*config.allowed_hosts, *host_names(args.host), *extra]))
    _announce_prompts()
    app = create_app(config)
    uvicorn.run(app, host=args.host, port=args.port)
    tasks = app.state.skillpose.tasks  # the app has stopped and dropped the waiting tasks
    if tasks.running:
        print("waiting for the running task to end (its calls are booked) ...")
        tasks.join()
    return 0


def _print_utf8(text: str) -> None:
    """The text on stdout as UTF-8 whatever the console's code page is: a quote of a CV may hold any character."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


def _export(args, client_factory) -> int:
    """The Markdown export of the stored results: no key, no API call, no match written, no file created (a database
    that does not exist is an error, not a new empty file). Matches that other rules made are recomputed first for the
    CVs asked, offline and in a copy in memory. A database of an older layout is updated once when it is opened, as by
    every command. Messages go to stderr, so stdout holds only the document."""
    def refuse(message: str) -> int:
        print(message, file=sys.stderr)
        return 2

    if not Path(args.db).exists():
        return refuse(f"no database at {args.db}")
    settings = load_settings(args.settings)
    with Store(args.db) as store:
        ids = [args.cv] if args.cv is not None else sorted(cv.id for cv in store.cvs() if cv.analyzed)
        if not ids:
            return refuse(f"no analyzed CV in {args.db}")
        if args.cv is not None and args.cv not in {cv.id for cv in store.cvs()}:
            return refuse(f"CV {args.cv} does not exist in {args.db}")
        with store.in_memory_copy() as fresh:
            refresh_matches(open_run(settings, fresh, args.lang), ids)
            try:
                text = build_export(fresh, settings, ids, args.lang)
            except ExportError as e:
                return refuse(f"CV {e.cv_id} does not exist in {args.db}" if e.reason == "unknown"
                              else f"CV {e.cv_id} is not analyzed yet: nothing to export")
    if args.out is None:
        _print_utf8(text)
        return 0
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out} ({len(text.encode('utf-8'))} bytes): it contains content of your CV, keep it private")
    return 0


def _count(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {value}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skillpose", description="Evidence-based skill gaps from a CV.")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="analyze a CV against postings")
    a.add_argument("--cv", type=Path, required=True)
    a.add_argument("--jobs", type=Path, nargs="+", required=True,
                   help=".md/.txt files, directories, .json lists or .csv files with a text column")
    a.add_argument("--lang", choices=["de", "en"], default="de")
    a.add_argument("--db", type=Path, default=DEFAULT_DB)
    a.add_argument("--settings", type=Path)
    a.add_argument("--env-file", type=Path, help="file with ANTHROPIC_API_KEY=... (and MONTHLY_LIMIT_USD=...)")
    a.add_argument("--prompts", type=Path, help=PROMPTS_HELP)
    a.add_argument("--log-file", help=LOG_HELP)
    a.add_argument("--yes", action="store_true", help="run even if the estimated cost is above the threshold")
    a.add_argument("--no-batch", action="store_true", help="never use the Message Batches API")
    a.set_defaults(func=_analyze)

    v = sub.add_parser("vocab", help="review skills Claude proposed")
    v.add_argument("--new", action="store_true", help="list unreviewed skills")
    v.add_argument("--accept", nargs="+", metavar="ID")
    v.add_argument("--reject", nargs="+", metavar="ID", help="drop proposals for good: they stay out of the vocabulary")
    v.add_argument("--db", type=Path, default=DEFAULT_DB)
    v.set_defaults(func=_vocab)

    e = sub.add_parser("eval", help="measure the analysis against the gold set: extraction, match status and role "
                                    "tiers (paid, under 1 USD with the default models)")
    e.add_argument("--goldset", type=Path, default=Path("evals/goldset"))
    e.add_argument("--step", choices=["cv", "job", "all"], default="all",
                   help="all: CVs, postings and the match (needs both)")
    e.add_argument("--job-model", help="model for step 2, e.g. claude-sonnet-5-5")
    e.add_argument("--job-effort", choices=["low", "medium", "high"])
    e.add_argument("--lang", choices=["de", "en", "auto"], default="auto",
                   help="auto: each CV in its language from expected.yaml, postings in English")
    e.add_argument("--db", type=Path, default=GOLDSET_DB,
                   help="cost ledger of the evals; with --save also the results (read by `demo build`)")
    e.add_argument("--save", action="store_true",
                   help="store profiles, job profiles, relations and matches in --db, so `demo build` reuses them")
    e.add_argument("--settings", type=Path)
    e.add_argument("--env-file", type=Path, help="file with ANTHROPIC_API_KEY=... (and MONTHLY_LIMIT_USD=...)")
    e.add_argument("--prompts", type=Path, help=PROMPTS_HELP)
    e.add_argument("--log-file", help=LOG_HELP)
    e.add_argument("--out", type=Path, help="write the report as Markdown")
    e.add_argument("--yes", action="store_true")
    e.add_argument("--repeat", type=_count, metavar="N",
                   help="run-to-run variance: N new runs of every CV (new calls; only the missing ones are made, so "
                        "the same command continues), then all pairs of runs of the same input are compared (--step "
                        "and --save do not apply)")
    e.add_argument("--repeat-jobs", type=_count, default=0, metavar="M",
                   help="with --repeat: M new runs of the postings too (from batch_threshold as a batch)")
    e.add_argument("--stability-db", type=Path, default=STABILITY_DB,
                   help="the runs and the cost ledger of --repeat; --db is only read, for the first run of each input")
    e.add_argument("--no-base", action="store_true",
                   help="with --repeat: leave out the stored results of `eval --save` (made with other prompts)")
    e.set_defaults(func=_eval)

    d = sub.add_parser("demo", help="demo bundles of the web app, and the static demo site")
    demo_commands = d.add_subparsers(dest="demo_command", required=True)
    b = demo_commands.add_parser(
        "build", help="build the demo bundles from the gold set (pays only for what is not stored yet)")
    b.add_argument("--goldset", type=Path, default=Path("evals/goldset"))
    b.add_argument("--db", type=Path, default=GOLDSET_DB)
    b.add_argument("--out", type=Path, default=Path("src/skillpose/demo_data"))
    b.add_argument("--paths", type=_count, default=2,
                   help="development paths per person, each built in German and in English")
    b.add_argument("--settings", type=Path)
    b.add_argument("--env-file", type=Path, help="file with ANTHROPIC_API_KEY=... (and MONTHLY_LIMIT_USD=...)")
    b.add_argument("--prompts", type=Path, help=PROMPTS_HELP)
    b.add_argument("--log-file", help=LOG_HELP)
    b.add_argument("--yes", action="store_true", help="run even if the estimated cost is above the threshold")
    b.set_defaults(func=_demo_build)

    site = demo_commands.add_parser(
        "site", help="write the demo as static HTML for GitHub Pages "
                     "(needs the web and dev extras; no key, no API call)")
    site.add_argument("--out", type=Path, default=Path("site"), help="folder to write (default: site, git-ignored)")
    site.add_argument("--repo-url",
                      help="the repository that the README link of every page leads to "
                           "(default: SKILLPOSE_REPO_URL, else the project repository)")
    site.set_defaults(func=_demo_site)

    w = sub.add_parser("web", help="start the web app (needs the web extra)")
    w.add_argument("--host", default="127.0.0.1",
                   help="with an API key, other addresses need APP_PASSWORD; host names: SKILLPOSE_ALLOWED_HOSTS")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--db", type=Path, default=DEFAULT_DB)
    w.add_argument("--settings", type=Path)
    w.add_argument("--env-file", type=Path,
                   help="file with ANTHROPIC_API_KEY=... (and APP_PASSWORD=..., MONTHLY_LIMIT_USD=...)")
    w.add_argument("--prompts", type=Path, help=PROMPTS_HELP)
    w.add_argument("--log-file", help=LOG_HELP)
    w.add_argument("--demo-dir", type=Path,
                   help="folder with demo bundles (default: the packaged demo; used only without a key)")
    w.set_defaults(func=_web)

    x = sub.add_parser("export", help="write the analysis of a CV as Markdown, to talk it over (no key, no API call)")
    x.add_argument("--cv", type=int, metavar="ID", help="one CV (default: every analyzed CV, with a comparison)")
    x.add_argument("--db", type=Path, default=DEFAULT_DB)
    x.add_argument("--lang", choices=["de", "en"], default="de")
    x.add_argument("--out", type=Path, help="write the file here as UTF-8 (default: print it)")
    x.add_argument("--settings", type=Path, help="the settings that analyze and web use (they decide the tiers)")
    x.set_defaults(func=_export)
    return parser


def _log_file(args) -> Path | None:
    """The log of a command that may pay; SKILLPOSE_LOG may also come from its --env-file."""
    if not hasattr(args, "log_file"):
        return None  # vocab: nothing to log
    if args.env_file:
        load_env_file(args.env_file)
    return log_path(args.log_file if args.log_file is not None else os.environ.get("SKILLPOSE_LOG"), args.db)


def _command(args, client_factory) -> int:
    name = " ".join(filter(None, [args.command, getattr(args, "demo_command", None)]))
    started = time.monotonic()
    log.info("%s started", name)
    try:
        rc = args.func(args, client_factory)
    except KeyboardInterrupt:
        log.warning("%s stopped after %.1f s", name, time.monotonic() - started)
        raise
    except PromptError:
        log.info("%s ended with code 2 after %.1f s: prompt folder", name, time.monotonic() - started)
        raise
    except Exception as e:
        # the type only, no traceback and no text: the commands read CVs, postings and gold sets, and the error of a
        # parser quotes the file (pypdf: about 80 bytes of a damaged PDF; PyYAML: the line it stops at). The error
        # goes on to the console with its traceback.
        log.error("%s failed after %.1f s: %s", name, time.monotonic() - started, type(e).__name__)
        raise
    log.info("%s ended with code %d after %.1f s", name, rc, time.monotonic() - started)
    return rc


def main(argv: list[str] | None = None, client_factory: Callable[[], object] | None = None) -> int:
    console()
    args = build_parser().parse_args(argv)
    args.argv = list(sys.argv[1:] if argv is None else argv)  # the command as given: eval --repeat names it to continue
    try:
        with file_log(_log_file(args)):
            return _command(args, client_factory)
    except PromptError as e:
        print(e)
        return 2
