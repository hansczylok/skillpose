"""Keeps private data out of the public repository.

The patterns describe general forms only; the author's own addresses never appear in this file.
"""

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from skillpose.core.config import load_settings
from skillpose.core.prompts import load_prompts
from skillpose.core.vocabulary import Vocabulary
from skillpose.demo import DemoBundle, demo_data_dir
from skillpose.evaluation import match_metrics, read_expected
from skillpose.pipeline import split_groups

ROOT = Path(__file__).parent.parent
# Read once, when this module is imported: the autouse fixture of conftest.py removes the variable from every test.
PRIVATE_PROMPTS = os.environ.get("SKILLPOSE_PROMPTS", "").strip()
# Private IPv4 addresses (RFC 1918: 10.x, 172.16-31.x, 192.168.x) of any network: a tracked file names none. The
# loopback and any-address (127.0.0.1, 0.0.0.0) of the documented setup are not private ranges.
PRIVATE_ADDRESS = (r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
                   r"|192\.168(?:\.\d{1,3}){2})(?!\.?\d)")
PATTERNS = [r"@gmail\.com", r"[A-Za-z]:[\\/]Users[\\/]", PRIVATE_ADDRESS, r"sk-ant-[a-z0-9]"]
needs_git = pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout"
)


def tracked_files() -> list[Path]:
    # -z keeps unusual file names intact; quoted names would not exist on disk and would be skipped silently.
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [ROOT / name for name in listing.decode("utf-8").split("\0") if name]


@needs_git
def test_no_private_data_in_tracked_files():
    hits = []
    for path in tracked_files():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{path.relative_to(ROOT)}: {pattern}" for pattern in PATTERNS if re.search(pattern, text)]
    assert hits == []


def test_the_private_address_pattern_finds_the_private_ranges_only():
    # built from numbers: this file must not contain an address itself
    def address(*parts: int) -> str:
        return ".".join(map(str, parts))

    for parts in [(10, 0, 0, 1), (10, 255, 3, 4), (172, 16, 0, 1), (172, 31, 9, 9), (192, 168, 1, 20)]:
        assert re.search(PRIVATE_ADDRESS, f"VPN {address(*parts)}:8000, ok"), parts
        assert re.search(PRIVATE_ADDRESS, f"{address(*parts)}."), parts   # end of a sentence
    for parts in [(127, 0, 0, 1), (0, 0, 0, 0), (8, 8, 8, 8), (172, 15, 0, 1), (172, 32, 0, 1), (192, 169, 1, 1),
                  (110, 1, 2, 3), (1, 10, 1, 2, 3), (10, 1, 2, 3, 4), (10, 1, 2, 3456)]:   # also versions, not hosts
        assert not re.search(PRIVATE_ADDRESS, f"x {address(*parts)} y"), parts


@needs_git
def test_no_env_file_tracked():
    names = {path.name for path in tracked_files()}
    assert ".env" not in names and ".env.example" in names
    assert [name for name in names if name.startswith(".env.") and name != ".env.example"] == []


@needs_git
@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production", "deploy/.env.vps"])
def test_env_files_are_git_ignored(name):
    assert subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=ROOT).returncode == 0, name


@needs_git
def test_the_env_example_is_not_git_ignored():
    assert subprocess.run(["git", "check-ignore", "-q", "--no-index", ".env.example"], cwd=ROOT).returncode != 0


def test_downloaded_exports_are_git_ignored_by_their_pattern():
    # an export holds content of a CV; one downloaded into the repository by mistake stays out
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "skillpose-*.md" in lines and "/data/" in lines
    # the briefs and diffs of the working process hold local paths; .git/info/exclude is local only,
    # so a re-clone or another machine could sweep them in with `git add -A`
    assert "/.superpowers/" in lines and ".claude/" in lines
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "skillpose export --cv 1 --lang en --out data/analysis.md" in readme


@needs_git
@pytest.mark.parametrize("name", ["skillpose-Maria-Berger-2026-10-24.md", "docs/skillpose-cv-2026-10-24.md",
                                  "data/analysis.md"])
def test_an_export_in_the_repository_is_git_ignored(name):
    assert subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=ROOT).returncode == 0, name


def test_readme_has_a_stability_section_after_the_eval_results():
    # the run-to-run spread belongs in the README next to the results
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    assert raw.index("\n## Eval results\n") < raw.index("\n## Stability\n") < raw.index("\n## Cost per analysis\n")
    section = _section(raw, "## Stability")
    for part in ("`skillpose eval --repeat N`", "`--repeat-jobs M`", "`data/stability.sqlite`",
                 "the author's private prompts", "single levels and single postings can change between two runs",
                 "The runs accumulate per configuration", "the same command again makes only the missing runs",
                 "| role family of a posting | 100 % |", "evals/results/2026-10-stability.md"):
        assert part in section, part
    summary = " ".join(raw[raw.index("## Kurzfassung (Deutsch)"):].split())
    assert "Wiederholte Analysen streuen" in summary


def test_readme_does_not_say_that_an_interrupted_measurement_pays_nothing_twice():
    # a call interrupted mid-answer is paid but gives no run, so the next command makes it again
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(raw, "## Stability")
    assert ("after an interruption it continues: nothing that was finished is paid twice, and a call interrupted "
            "mid-answer is paid once and made again") in section
    assert "pays nothing twice" not in section


def test_the_monthly_limit_is_said_to_count_one_database_file():
    # the own database, data/goldset.sqlite and data/stability.sqlite each have a ledger of their own
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    row = next(" ".join(line.split()) for line in raw.splitlines() if line.startswith("| `MONTHLY_LIMIT_USD`"))
    assert ("it counts the costs of one database file (`eval`, `eval --repeat` and `demo build` have ledgers of their "
            "own)") in row
    assert "the monthly limit counts only the costs of that file" in _section(raw, "## Stability")


def test_readme_has_no_placeholder_left_and_states_the_measured_numbers():
    # the numbers of the stability section come from the JSON lines of the report, which is tracked
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "{{" not in raw   # a template placeholder; "}}" is legitimate (the YAML example of a step model)
    section = _section(raw, "## Stability")
    for part in ("Measured in October 2026", "each of the 3 CVs in 5 runs, the 30 postings in 2 runs",
                 "| level of a skill found in both runs | 93.2 % (at most one level apart: 100 %) |",
                 "| status of a requirement: CV again / postings again | 99.0 % / 99.5 % |",
                 "| tier of a posting: CV again / postings again | 99.6 % / 100 % |",
                 "stable in 264 of 264 cases", "about 88 % of the levels the same, 9 of about 75 differing"):
        assert part in section, part
    summary = " ".join(raw[raw.index("## Kurzfassung (Deutsch)"):].split())
    # the 100 % of the role tiers was measured when a role needed 5 postings; the default is 1 now
    assert ("93,2 % der Niveaus und 100 % der Rollen-Stufen gleich (bei Rollen mit mindestens 5 Inseraten, "
            "der damaligen Vorgabe), gemessen mit 5 Läufen je Lebenslauf") in summary
    report = (ROOT / "evals/results/2026-10-stability.md").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in report.splitlines() if line.startswith('{"source"')]
    cv = {line["metric"]: line for line in lines if line["source"] == "cv"}
    assert (cv["level_same"]["same"], cv["level_same"]["total"]) == (578, 620)
    assert cv["role_tier_same"]["value"] == 1.0


EVAL_REPORT = ROOT / "evals/results/2026-10-25-rules-and-step2.md"


def _report_table(step: str, header: str) -> dict[str, list[float]]:
    """The rows of the table that starts with `header` in the section of one step of the eval report of 2026-10-25
    (first cell -> the numbers of the other cells)."""
    report = EVAL_REPORT.read_text(encoding="utf-8")
    part = report[report.index(f"## Step {step} - "):]
    table = part[part.index(header):].split("\n\n")[0]
    cells = [[c.strip() for c in line.strip("|").split("|")] for line in table.splitlines()[2:]]
    return {row[0]: [float(c) for c in row[1:]] for row in cells}


def _demo_match_counts() -> tuple[tuple[int, int], tuple[int, int], dict[str, float]]:
    """(statuses right, statuses expected), (tiers right, tiers expected) and the status share per person, recomputed
    from the demo bundles with the expected values of the gold set: no API call."""
    vocab, rules = Vocabulary.load(), load_settings().rules
    expected = read_expected(ROOT / "evals/goldset")
    right = total = tiers_right = tiers_total = 0
    shares = {}
    for key, person in expected["cvs"].items():
        bundle = DemoBundle.model_validate_json((demo_data_dir() / f"{key}.json").read_bytes())
        by_hash = {m.text_hash: m for m in bundle.matches}
        matches = {Path(ref).stem: by_hash[p.hash] for p in bundle.postings for ref in p.refs}
        jobs = {Path(ref).stem: p.job_profile for p in bundle.postings for ref in p.refs}
        metrics = match_metrics(matches, jobs, person, vocab, rules)
        pairs = sum(len(statuses) for statuses in person["match_status"].values())
        right, total = right + round(metrics["match_status_accuracy"] * pairs), total + pairs
        tiers_right += round(metrics["role_tier_accuracy"] * len(person["role_tiers"]))
        tiers_total += len(person["role_tiers"])
        shares[key] = metrics["match_status_accuracy"]
    return (right, total), (tiers_right, tiers_total), shares


def test_readme_eval_table_has_the_numbers_of_the_report_of_2026_10_25():
    # every gold-set number of the README is a number of the report of 2026-10-25 (the rules of
    # 2026-10-25 and the step-2 prompt that marks soft skills and conditions); the stability numbers stay older
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(raw, "## Eval results")
    assert section.startswith("Measured on 2026-10-25 on the gold set")
    assert "(evals/results/2026-10-25-rules-and-step2.md)" in section
    assert "new rules of matches and tiers" in section and "the new step-2 prompt" in section
    assert ("The earlier measurement of 2026-10-21 and 2026-10-22, made before these rules, is the summary "
            "[`2026-10-phase2-summary.md`](evals/results/2026-10-phase2-summary.md)") in section
    assert "only the [stability numbers](#stability) below are still from before 2026-10-25" in section
    rows = {line.split("|")[1].strip(): [c.strip() for c in line.strip("|").split("|")]
            for line in raw.splitlines() if line.startswith(("| 1 CV", "| 2 posting", "| 3 match", "| 4 role"))}
    cv = {k: v[0] for k, v in _report_table("cv", "| metric | mean |").items()}
    job = {k: v[0] for k, v in _report_table("job", "| metric | mean |").items()}
    match = {k: v[0] for k, v in _report_table("match", "| metric | mean |").items()}
    assert rows["1 CV → profile"][3] == (f"recall {cv['recall']:.2f}, trap hits {cv['trap_hits']:.0f}, strength "
                                         f"{cv['strength_accuracy']:.2f}, verified quotes {cv['verified_share']:.2f}")
    assert rows["2 posting → requirements"][3] == f"{job['must_recall']:.2f} / {job['must_precision']:.2f}"
    assert (job["role_family_ok"], job["seniority_ok"], job["nice_recall"], job["importance_accuracy"]) == (1.0,) * 4
    (right, total), (tiers_right, tiers_total), shares = _demo_match_counts()
    assert (right, total) == (31, 34) and rows["3 match status"][3] == "31 / 34"
    assert (tiers_right, tiers_total) == (12, 12) and rows["4 role tier"][3].startswith("12 / 12 (")
    # the demo bundles are the run that the report measured: the counts give its mean and its items
    assert f"{right / total:.2f}" == f"{match['match_status_accuracy']:.2f}" and match["role_tier_accuracy"] == 1.0
    items = _report_table("match", "| item |")
    assert {key: f"{share:.2f}" for key, share in shares.items()} == {key: f"{v[0]:.2f}" for key, v in items.items()}
    # the two postings in which Sonnet added a must requirement are the "2 postings" of the bullet (precision below 1)
    added = [name for name, numbers in _report_table("job", "| item |").items() if numbers[-1] < 1]
    assert added == ["03_senior_sdet_vienna", "12_it_business_analyst"]
    assert "it added a must requirement in 2 postings (precision 0.99)" in section
    assert "Sonnet 5.5 cost 0.81 US cents per posting" in section   # 0.2445 USD for 30 postings


def test_readme_limits_and_german_summary_say_when_the_gold_set_numbers_were_measured():
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    limits = _section(raw, "## Known limits")
    assert ("The gold-set numbers were measured on 2026-10-25 with the new rules of matches and tiers and the new "
            "step-2 prompt; the stability numbers were measured before 2026-10-25.") in limits
    assert "The gold-set and stability numbers were measured with the rules" not in limits
    summary = " ".join(raw[raw.index("## Kurzfassung (Deutsch)"):].split())
    assert ("Die Zahlen des Goldsatzes wurden am 25.10.2026 mit den neuen Regeln für Abgleich und Stufen und dem neuen "
            "Prompt von Schritt 2 gemessen; nur die Zahlen der Streuung entstanden davor") in summary
    assert "Im Goldsatz stimmen 31 von 34 Abgleichen und 12 von 12 Stufen" in summary
    assert "Die Zahlen des Goldsatzes und der Streuung entstanden" not in summary


def test_readme_cost_table_is_the_run_of_2026_10_25():
    # steps 1, 2 and 5 from the rows that the run of 2026-10-25 added to the ledger (the demo build wrote
    # each path in both languages); step 3b is the run of 2026-10-22, the answers of which the new run reused
    ledger = json.loads((demo_data_dir() / "costs.json").read_text(encoding="utf-8"))["rows"]
    latest = [r for r in ledger if r["ts"].startswith("2026-10-25")]
    by_step = {step: [r for r in latest if r["step"] == step] for step in ("cv", "job", "relation", "path")}
    assert {step: len(rows) for step, rows in by_step.items()} == {"cv": 3, "job": 30, "relation": 7, "path": 12}
    usd = {step: sum(r["usd"] for r in rows) for step, rows in by_step.items()}
    figure = {"1 CV": usd["cv"] / 3, "2 postings (30)": usd["job"], "5 paths (2)": usd["path"] / 3 / 2}  # 3 persons
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    for row, value in figure.items():
        assert re.search(rf"\| {re.escape(row)} \| [^|]+ \| {value:.3f} USD", raw), (row, value)
    assert f"{usd['relation']:.3f} USD in all" in raw          # the 7 new pairs of 3b: 0.002
    total = sum(figure.values()) + 0.0207                       # 3b of 2026-10-22: 0.0622 USD for 3 persons
    assert f"**≈ {total:.2f} USD**" in raw and "≈ 0.43 USD" in raw
    section = _section(raw, "## Cost per analysis")
    assert "from the cost ledger of the gold-set run of 2026-10-25" in section
    assert "Step 3b is from 2026-10-22" in section and "asked only 7 new pairs (0.002 USD in all)" in section
    assert "etwa 0,43 USD, mit Batches" in " ".join(raw[raw.index("## Kurzfassung (Deutsch)"):].split())


def test_goldset_has_no_real_brand_word():
    assert "Telekom" not in (ROOT / "evals/goldset/cvs/maria_berger.md").read_text(encoding="utf-8")


def test_readme_head_is_non_commercial():
    head = "\n".join((ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:12])
    assert "non-commercial" in head and not re.search(r"(?i)services|consulting|solutions", head)


def test_readme_has_a_german_summary():
    assert "## Kurzfassung (Deutsch)" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_explains_the_three_tiers_in_a_table_after_the_market_view():
    # is tier 1 or 3 better? The pages and the README must say it
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("## What it does"):text.index("## Screenshots")]
    after_market = section[section.index("**Market view.**"):section.index("**Development paths.**")]
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in after_market.splitlines() if line.strip().startswith("|")]
    assert [r[:2] for r in rows[:1]] == [["Tier", "Word"]] and set(rows[1][0]) <= set("-: ")
    assert [r[:2] for r in rows[2:]] == [["1", "fits"], ["2", "learnable"], ["3", "big gap"]]
    assert "1 is best" in " ".join(after_market.split()) and "How it works" in " ".join(after_market.split())
    assert "the best tier (the lowest number) that at least 40 % of its postings reach" in " ".join(text.split())
    summary = text[text.index("## Kurzfassung (Deutsch)"):]
    assert "Stufe 1 passt, 2 heißt Lücken lernbar, 3 große Lücke." in " ".join(summary.split())


def test_readme_tier_table_names_the_knockout_of_a_must_condition():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    table = text[text.index("| Tier | Word |"):text.index("**Development paths.**")]
    rows = [" ".join(line.split()) for line in table.splitlines() if line.strip().startswith(("| 1 |", "| 2 |"))]
    assert len(rows) == 2
    assert all("no must condition that your answers do not meet" in row for row in rows), rows


def test_readme_keeps_the_compliance_texts():
    # the purpose limits come before any instruction to run the app
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "not affiliated with or endorsed by Anthropic" in " ".join(text.splitlines()[:12])
    assert text.index("## Intended purpose and limits") < text.index("## Quick start")
    for heading in ("## Using your own Anthropic API key", "## Data protection", "## License"):
        assert heading in text


def test_readme_quick_start_creates_a_venv_with_the_supported_python():
    # a bare `python` is missing on many systems or is a newer version that pyproject refuses
    version = re.search(r'requires-python = ">=(3\.\d+),<', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))[1]
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"python{version} -m venv .venv" in text and f"py -{version} -m venv .venv" in text
    assert "python -m venv" not in text


def test_readme_says_that_a_set_password_is_checked_on_every_request():
    text = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "the password is checked on every request" in text
    assert "enforces the password only when" not in text


def test_readme_shows_the_logo_right_below_the_title():
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# skillpose"
    assert lines[2] == ('<img src="docs/img/logo.svg" alt="skillpose logo: two hands whose index fingers almost touch, '
                        'with a spark between them" width="240">')
    # grey hands: GitHub's light or dark theme can differ from the system setting, so the logo has no dark rule
    logo = (ROOT / "docs/img/logo.svg").read_text(encoding="utf-8")
    assert 'role="img"' in logo and "<title" in logo and "#6e7781" in logo and "#EF9F27" in logo
    assert "#1f2328" not in logo and "prefers-color-scheme" not in logo
    assert not re.search(r"<script|href=|\bdata:|\son[a-z]+\s*=", logo)   # an image that loads and runs nothing


def test_readme_links_and_screenshots_exist():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = {t.split("#")[0] for t in re.findall(r"\]\(([^)\s]+)\)", text) if not re.match(r"[a-z]+:|#", t)}
    assert {f"docs/img/{name}.png" for name in ("profile", "match", "market", "path")} <= targets
    for target in targets:
        assert (ROOT / target).exists(), target
        if target.endswith(".png"):
            assert (ROOT / target).stat().st_size <= 500 * 1024, target


def test_readme_marks_the_cost_figures_as_a_snapshot():
    # the table of the cost section is one measurement of October 2026; the app measures the reader's own average
    text = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    section = text[text.index("## Cost per analysis"):text.index("## Design decisions")]
    assert "snapshot of October 2026" in section
    assert "page shows the average cost per call, measured in your own installation (with your own key)" in section
    summary = text[text.index("## Kurzfassung (Deutsch)"):]
    assert ("Momentaufnahme Oktober 2026; die Seite „Kosten“ zeigt die durchschnittlichen Kosten je Aufruf, "
            "gemessen in der eigenen Installation (mit dem eigenen Schlüssel).") in summary
    prices = " ".join((ROOT / "src/skillpose/core/defaults.yaml").read_text(encoding="utf-8").split())
    assert "platform.claude.com/docs/en/about-claude/pricing" in prices and "October 2026" in prices


def _step2_usd(model: str, plan: list[tuple[int, bool]]) -> float:
    """What step 2 costs for the given groups of postings (size, as a batch), from the rows of the demo ledger: each
    group writes the cached prefix once and reads it for each further posting, as estimate() counts it."""
    settings = load_settings()
    ledger = json.loads((ROOT / "src/skillpose/demo_data/costs.json").read_text(encoding="utf-8"))["rows"]
    rows = [r for r in ledger if r["step"] == "job" and r["model"] == model and not r["batch"]]
    # the table is the latest run of the gold set with this model (30 postings, one day); the ledger keeps the rows of
    # the earlier runs too, so the rows are picked by their date, not by their position
    latest = max(r["ts"][:10] for r in rows)
    rows = [r for r in rows if r["ts"][:10] == latest]
    assert len(rows) == 30, (model, latest)
    price = settings.prices[model]
    fixed = max(max(r["cache_write"], r["cache_read"]) for r in rows)
    per_posting = sum(r["input"] * price.input + r["output"] * price.output for r in rows) / len(rows) / 1e6
    total = 0.0
    for size, batch in plan:
        usd = (fixed * price.cache_write + (size - 1) * fixed * price.cache_read) / 1e6 + size * per_posting
        total += usd / 2 if batch else usd
    return total


def test_readme_batch_cost_counts_one_cache_write_per_group():
    # the default split (1 single call, the rest in batch_groups batches) writes the cached prefix
    # of step 2 once per group; the line of the cost section and the German summary must name that sum
    settings = load_settings()
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    table = dict(re.findall(r"^\| (1 CV|2 postings \(30\)|3b related skills|5 paths \(2\)) \| [^|]+ \| ([\d.]+) USD",
                            raw, re.M))
    others = sum(float(table[step]) for step in ("1 CV", "3b related skills", "5 paths (2)"))
    sonnet, haiku = settings.steps["job"].model, "claude-haiku-5-5"
    assert float(table["2 postings (30)"]) == pytest.approx(_step2_usd(sonnet, [(30, False)]), abs=0.001)
    first = settings.batch_single_first
    split = [(first, False)] * bool(first) + [(len(group), True)
                                              for group in split_groups(list(range(30 - first)), settings.batch_groups)]
    assert [size for size, _ in split] == [1, 10, 10, 9]
    with_split, one_batch = others + _step2_usd(sonnet, split), others + _step2_usd(sonnet, [(30, True)])
    text = " ".join(raw.split())
    section = text[text.index("## Cost per analysis"):text.index("## Design decisions")]
    assert f"the other 29 in three batches) it is ≈ {with_split:.2f} USD" in section
    assert f"with Haiku 5.5 for step 2 ≈ {others + _step2_usd(haiku, split):.2f} USD" in section
    assert (f"the first result at once costs ≈ {with_split - one_batch:.2f} USD more than one batch of all 30 "
            f"postings (≈ {one_batch:.2f} USD)") in section
    summary = text[text.index("## Kurzfassung (Deutsch)"):]
    assert f"mit Batches etwa {with_split:.2f} USD".replace(".", ",") in summary


def test_readme_and_package_name_the_author_without_an_address():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Author" in text and "Hans Czylok" in text
    assert "Idea, concept and requirements: Hans Czylok" in text and "Claude Code" in text
    assert text.index("## License") < text.index("## Author") < text.index("## Kurzfassung (Deutsch)")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["authors"] == [{"name": "Hans Czylok"}]


@needs_git
def test_old_name_is_gone():
    # the project was renamed before its first publication; built from two parts so that
    # this file does not match itself
    old_name = "skill" + "path"
    found = subprocess.run(["git", "grep", "-I", "-i", "-n", old_name], cwd=ROOT, capture_output=True)
    assert found.returncode == 1, found.stdout.decode("utf-8", errors="replace")[:2000]
    # and no file name: git grep reads only the contents
    assert [p for p in tracked_files() if old_name in p.relative_to(ROOT).as_posix().lower()] == []


@needs_git
def test_no_commit_id_of_this_repository_is_cited_in_a_tracked_file():
    # the published history is squashed: an id in a file would name a commit that is not there; dates say it as well
    commits = subprocess.run(["git", "rev-list", "--all"], cwd=ROOT, capture_output=True, check=True).stdout.split()
    ids = [commit.decode("ascii") for commit in commits]
    found = subprocess.run(["git", "grep", "-I", "-n", "-o", "-w", "-E", "[0-9a-f]{7,40}"], cwd=ROOT,
                           capture_output=True)   # exit 1 when nothing looks like an id
    # any other code is an error of git grep: its output would be empty, and the test must not pass on that
    assert found.returncode in (0, 1), found.stderr.decode("utf-8", errors="replace")[:2000]
    hits = []
    for line in found.stdout.decode("utf-8", errors="replace").splitlines():
        path, number, word = line.split(":", 2)
        if any(commit.startswith(word) for commit in ids):   # an id may be cited short (7 to 40 characters)
            hits.append(f"{path}:{number}: {word}")
    assert hits == []


def _section(markdown: str, heading: str) -> str:
    """The text below a heading up to the next heading, white space collapsed."""
    start = markdown.index(f"\n{heading}\n") + len(heading) + 2
    end = markdown.find("\n#", start)
    return " ".join(markdown[start:end if end != -1 else None].split())


def test_name_and_logo_are_excluded_from_mit():
    # the MIT text stays untouched so that license detection keeps showing "MIT";
    # the README and the logo files carry the carve-out
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    readme = " ".join(raw.split())
    assert _section(raw, "## License").startswith("MIT, see [LICENSE](LICENSE); the name and the logo are excluded, "
                                                  "see [Name and logo](#name-and-logo).")
    section = _section(raw, "### Name and logo")
    assert ('The name "skillpose", the wordmark (also its inline copy in the page templates) and the logo and icon '
            "(also where screenshots show them) are not licensed under it: © 2026 Hans Czylok, all rights "
            "reserved.") in section
    # every logo file the section lists exists; a bare name lives in the folder of the path before it
    listed, folder = [], ""
    for name in re.findall(r"`([^`]+)`", section):
        folder = name.rpartition("/")[0] if "/" in name else folder
        listed.append(name if "/" in name else f"{folder}/{name}")
    assert listed and [name for name in listed if not (ROOT / name).is_file()] == []
    summary = readme[readme.index("## Kurzfassung (Deutsch)"):]
    assert "Name und Logo stehen nicht unter der MIT-Lizenz; den Code darf jeder nach MIT nutzen." in summary
    logos = sorted((ROOT / "src/skillpose/web/static").glob("*.svg")) + sorted((ROOT / "docs/img").glob("*.svg"))
    assert len(logos) == 4
    assert {logo.relative_to(ROOT).as_posix() for logo in logos} <= set(listed)
    for logo in logos:
        text = logo.read_text(encoding="utf-8")
        assert "Not licensed under the MIT License" in text, logo.name
        # the comment is the first child of the root element
        notice = r"<!-- © 2026 Hans Czylok\. Not licensed under the MIT License"
        assert re.match(rf"(?s)<svg[^>]*>\s*{notice}", text), logo.name
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    # no trademark symbols: the name is claimed by copyright and the README says so, not by a registration
    assert not re.search("[™®]", readme)


GERMAN_PROMPT_SENTENCE = (
    "Die Prompts im Repository sind vereinfachte Beispiele unter MIT; die Messungen und die Demo entstanden mit den "
    "privaten Prompts des Autors (nicht veröffentlicht, nicht lizenziert), mit den Beispielen sind die Ergebnisse "
    "schwächer, und eigene Prompts liest skillpose aus dem Ordner in `SKILLPOSE_PROMPTS` oder `--prompts`.")


def test_readme_says_that_the_prompts_are_examples():
    # the tuned prompts stay private, and the README says so openly
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    assert raw.index("\n## Prompts\n") < raw.index("\n## Design decisions\n")
    section = _section(raw, "## Prompts")
    for part in ("simplified examples", "MIT license", "private prompts", "not published and not licensed", "weaker",
                 "`SKILLPOSE_PROMPTS`", "`--prompts FOLDER`", "`cv.md`", "`job.md`", "`relation.md`", "`path.md`"):
        assert part in section, part
    assert GERMAN_PROMPT_SENTENCE in " ".join(raw[raw.index("## Kurzfassung (Deutsch)"):].split())


def test_readme_says_above_the_eval_table_which_prompts_made_the_results():
    section = _section((ROOT / "README.md").read_text(encoding="utf-8"), "## Eval results")
    intro = section[:section.index("| step |")]
    assert ("All measurements and the demo were made with the author's private prompts, not with the example prompts "
            "of this repository (see [Prompts](#prompts))." in intro)
    assert section.count("All measurements and the demo were made") == 1   # no second copy among the bullets


def test_the_phase2_summary_does_not_describe_the_private_prompts_by_absence():
    # "the prompt has no rule for X" tells a reader what the private prompts leave out, and so what to add to them
    text = " ".join((ROOT / "evals/results/2026-10-phase2-summary.md").read_text(encoding="utf-8").split())
    for phrase in ("step-1 prompt has no", "prompt has no rule", "prompt gets a rule", "has no date"):
        assert phrase not in text, phrase
    assert "posting 10" in text and "a stronger level than expected" in text   # the finding itself stays


def test_readme_known_limits_say_that_only_paths_are_keyed_by_the_prompt_set():
    section = _section((ROOT / "README.md").read_text(encoding="utf-8"), "## Known limits")
    assert ("Stored profiles, requirements and relations are not keyed by the prompt set (only development paths are): "
            "after a change of prompts, delete the database file (or use a new one with `--db`) and analyze again to "
            "compare." in section)


def _sentences(text: str) -> set[str]:
    """The sentences and clauses (split after . : ; ? !) of at least 40 characters, white space collapsed and list
    dashes and end marks removed, so that a copy is found whatever its line breaks and its list style."""
    parts = re.split(r"(?<=[.:;?!])\s", " ".join(text.split()))
    return {p for p in (part.strip(" -").rstrip(".:;?!") for part in parts) if len(p) >= 40}


def _copies(sentences: set[str], text: str) -> list[str]:
    flat = " ".join(text.split())
    return sorted(s for s in sentences if s in flat)


def test_the_prompt_guard_finds_a_sentence_whatever_its_line_breaks():
    sentences = _sentences("Too short. - A sentence that is long enough for the guard;\n  short tail")
    assert sentences == {"A sentence that is long enough for the guard"}
    assert _copies(sentences, "x A sentence that is long\n    enough for the guard, y") == sorted(sentences)
    assert _copies(sentences, "A sentence that is long enough") == []


@needs_git
@pytest.mark.skipif(not PRIVATE_PROMPTS, reason="needs SKILLPOSE_PROMPTS, the folder of the private prompts")
def test_private_prompts_are_not_in_the_repository(tmp_path):
    # the tuned prompts never enter the repository or its squashed history
    private = load_prompts(Path(PRIVATE_PROMPTS))
    if private.label == "example":
        pytest.skip("SKILLPOSE_PROMPTS names the example prompts")
    sentences = set().union(*(_sentences(text) for text in (private.cv, private.job, private.relation, private.path)))
    assert len(sentences) > 20
    patterns = tmp_path / "sentences.txt"
    patterns.write_bytes("\n".join(sorted(sentences)).encode("utf-8"))  # LF only: a CR would end every pattern
    found = subprocess.run(["git", "grep", "-F", "-I", "-n", "-f", str(patterns)], cwd=ROOT, capture_output=True)
    assert found.returncode == 1, found.stdout.decode("utf-8", errors="replace")[:2000]
    # git grep reads line by line; a copy with other line breaks shows in the collapsed text of each file
    hits = [f"{path.relative_to(ROOT)}: {sentence[:60]}" for path in tracked_files() if path.is_file()
            for sentence in _copies(sentences, path.read_text(encoding="utf-8", errors="ignore"))]
    assert hits == []


def test_readme_ends_with_the_disclosure_and_names_the_real_addresses():
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    assert raw.rstrip().endswith("Offenlegung gemäß § 25 Abs. 5 MedienG: Medieninhaber: Hans Czylok, Ottensheim, "
                                 "Österreich. Privates, nicht-kommerzielles Lernprojekt.")
    assert "https://hansczylok.github.io/skillpose/" in raw and "<user>" not in raw and "OWNER" not in raw
