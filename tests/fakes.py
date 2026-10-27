"""Test doubles for the Anthropic client and the cost ledger. No test outside the `live` mark calls the API."""

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
from pydantic import BaseModel

from skillpose.core.config import load_settings
from skillpose.core.llm import CallResult, Usage
from skillpose.core.matching import match_fingerprint
from skillpose.core.models import (
    CandidateSkillDraft,
    CvDraft,
    EvidenceDraft,
    JobDraft,
    PathDraft,
    PathStepDraft,
    RelatedSkill,
    RelationAnswer,
    RequirementDraft,
)
from skillpose.core.paths import GAPS_HEADER

# A CV that holds the quote of cv_draft(), so its evidence is verified.
CV = ("Maria Berger - QA Engineer. 2016-2026 Acme Testing GmbH: Built a Selenium test automation framework in Java "
      "that cut regression time from 5 days to 4 hours. Wrote API tests with Postman. Skills: Docker (basic).")


class Dummy(BaseModel):
    x: int


def prompt_folder(folder: Path, **texts: str) -> Path:
    """A prompt folder with the four files; a text not given is "<NAME> PROMPT". Written as bytes: no newline added."""
    folder.mkdir(parents=True)
    for name in ("cv", "job", "relation", "path"):
        (folder / f"{name}.md").write_bytes(texts.get(name, f"{name.upper()} PROMPT").encode("utf-8"))
    return folder


def _api_usage(u: Usage) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                           cache_creation_input_tokens=u.cache_write_tokens,
                           cache_read_input_tokens=u.cache_read_tokens)


def answer(text: str, stop_reason: str = "end_turn", usage: Usage | None = None,
           model: str | None = None) -> SimpleNamespace:
    """A Messages API response with thinking first and the JSON answer as text block, like the real API."""
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason, model=model,
                           usage=_api_usage(usage or Usage(1000, 500, 0, 0)))


def ok(value: BaseModel, usage: Usage | None = None, model: str | None = None) -> SimpleNamespace:
    return answer(value.model_dump_json(), usage=usage, model=model)


def refusal() -> SimpleNamespace:
    return SimpleNamespace(content=[], stop_reason="refusal", model=None, usage=_api_usage(Usage(500, 0, 0, 0)))


def api_error(status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(f"status {status}", response=httpx2.Response(status, request=request), body=None)


def not_found() -> anthropic.NotFoundError:
    """What the API answers for the id of a batch that this key cannot see (another key or workspace)."""
    request = httpx2.Request("GET", "https://api.anthropic.com/v1/messages/batches/batch_x")
    return anthropic.NotFoundError("Error code: 404", response=httpx2.Response(404, request=request), body=None)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx2.Request("GET", "https://api.anthropic.com/v1/messages/batches"))


# what the SDK raises for an ended batch without results_url (its results are gone after 29 days)
NO_RESULTS = anthropic.AnthropicError("No `results_url` for the given batch; Has it finished processing? ended")


@dataclass
class Call:
    path: str
    kwargs: dict


@dataclass
class BrokenStream:
    """A scripted answer whose stream breaks after it began: get_final_message() raises error. started is the message
    as far as it came, with the usage of message_start (input and cache tokens, output 1); None: it broke before.
    error may also be a KeyboardInterrupt (Ctrl-C while the stream is read)."""
    error: BaseException
    started: SimpleNamespace | None = None


def started(usage: Usage, stop_reason: str | None = None) -> SimpleNamespace:
    """The message of a stream right after message_start: no content, no stop reason, the start usage. With a stop
    reason: after message_delta, which carries the stop reason and the final output count."""
    return SimpleNamespace(content=[], stop_reason=stop_reason, model=None, usage=_api_usage(usage))


class _StreamedAnswer:
    """What entering a stream gives: get_final_message() and current_message_snapshot, which fails like the SDK's
    (an assert) before message_start."""

    def __init__(self, message):
        self._message = message

    def get_final_message(self):
        if isinstance(self._message, BrokenStream):
            raise self._message.error
        return self._message

    @property
    def current_message_snapshot(self):
        snapshot = self._message.started if isinstance(self._message, BrokenStream) else self._message
        assert snapshot is not None
        return snapshot


class FakeStream:
    """What messages.stream returns: a context manager. The request goes out when it is entered (as in the SDK, so an
    API error is raised there); get_final_message() returns the scripted message. Leaving it closes the stream."""

    def __init__(self, send: Callable[[], object]):
        self._send = send
        self.closed = False

    def __enter__(self) -> _StreamedAnswer:
        return _StreamedAnswer(self._send())

    def __exit__(self, *exc) -> None:
        self.closed = True


def messages_api(create: Callable[..., object]) -> SimpleNamespace:
    """messages (or beta.messages) of a fake client: create, and stream, which runs the create of this namespace when
    the stream is entered - a test that replaces create changes both."""
    api = SimpleNamespace(create=create)
    api.stream = lambda **kw: FakeStream(lambda: api.create(**kw))
    return api


class FakeClient:
    """Answers messages.create / .stream and beta.messages.create / .stream from a queue and records every call with
    its path (e.g. "beta.messages.stream"); streams holds every stream it opened."""

    def __init__(self, answers: list):
        self.answers = list(answers)
        self.calls: list[Call] = []
        self.streams: list[FakeStream] = []
        self.messages = self._api("messages")
        self.beta = SimpleNamespace(messages=self._api("beta.messages"))

    def _api(self, prefix: str) -> SimpleNamespace:
        def stream(**kw) -> FakeStream:
            self.streams.append(FakeStream(lambda: self._answer(f"{prefix}.stream", kw)))
            return self.streams[-1]
        return SimpleNamespace(create=lambda **kw: self._answer(f"{prefix}.create", kw), stream=stream)

    def _answer(self, path: str, kwargs: dict):
        self.calls.append(Call(path, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeBatchClient:
    """messages.batches with create / retrieve / results. results: custom_id -> (type, json text or None).
    statuses: what retrieve answers, in turn - one list for every batch, or a list per batch id; an exception in the
    list is raised by that status request. The batches it creates
    are batch_1, batch_2, ...; results of one of them yields only its own requests. counts: batch id -> request
    counts (processing, succeeded, errored, canceled, expired; missing ones are 0) that retrieve returns; without an
    entry the answer has no request_counts. log: a list (e.g. ScriptedClient.calls) that gets a
    Call("batches.create", ...) per batch, so a test sees the order of batches and single calls.
    retrieve_error / results_error: raised by retrieve / results instead of an answer (results raises when it is
    called, before the first item, like the SDK); retrieve_error can also be a dict batch id -> error, for the
    status requests of these batches only. Each status answer has created_at, and an ended one ended_at, took seconds
    later (as the API answers)."""

    CREATED = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)

    def __init__(self, results: dict, statuses: list[str] | dict[str, list[str]], *,
                 retrieve_error: Exception | dict[str, Exception] | None = None,
                 results_error: Exception | None = None,
                 counts: dict[str, dict[str, int]] | None = None, log: list | None = None, took: float = 754.0):
        self._results = results
        self._statuses = {k: list(v) for k, v in statuses.items()} if isinstance(statuses, dict) else list(statuses)
        self.retrieve_error, self.results_error = retrieve_error, results_error
        self.counts = {} if counts is None else counts
        self.log = log
        self.took = took
        self.created: list[SimpleNamespace] = []          # every request of every batch
        self.batches: dict[str, list[str]] = {}           # batch id -> the custom ids of its requests
        self.retrieved: list[str] = []                    # the batch id of every status request
        self.sleeps = 0
        self.messages = SimpleNamespace(batches=SimpleNamespace(create=self._create, retrieve=self._retrieve,
                                                                results=self._results_of))

    def _create(self, requests):
        batch_id = f"batch_{len(self.batches) + 1}"
        self.created.extend(SimpleNamespace(**r) for r in requests)
        self.batches[batch_id] = [r["custom_id"] for r in requests]
        if self.log is not None:
            self.log.append(Call("batches.create", {"batch_id": batch_id, "requests": len(requests)}))
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    def _retrieve(self, batch_id):
        self.retrieved.append(batch_id)
        error = self.retrieve_error
        if isinstance(error, dict):
            error = error.get(batch_id)
        if error is not None:
            raise error
        statuses = self._statuses[batch_id] if isinstance(self._statuses, dict) else self._statuses
        status = statuses.pop(0)
        if isinstance(status, BaseException):  # one status request that fails (a lost connection, Ctrl-C)
            raise status
        batch = SimpleNamespace(id=batch_id, processing_status=status, created_at=self.CREATED,
                                ended_at=self.CREATED + timedelta(seconds=self.took) if status == "ended" else None)
        if batch_id in self.counts:
            zero = dict.fromkeys(("processing", "succeeded", "errored", "canceled", "expired"), 0)
            batch.request_counts = SimpleNamespace(**(zero | self.counts[batch_id]))
        return batch

    def _results_of(self, batch_id):
        if self.results_error is not None:
            raise self.results_error
        return self._items(self.batches.get(batch_id))

    def _items(self, own: list[str] | None):
        """own: the custom ids of a batch this fake created; None (a batch of an earlier run): every result."""
        for custom_id, (kind, text) in self._results.items():
            if own is not None and custom_id not in own:
                continue
            message = None
            if kind == "succeeded":
                message = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                                          model="claude-haiku-5-5", usage=_api_usage(Usage(2000, 800, 0, 5000)))
            yield SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type=kind, message=message))


class Concurrency:
    """Counts the calls that run at the same time: `with concurrency:` around each call; most holds the maximum."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = self.most = 0

    def __enter__(self):
        with self._lock:
            self.active += 1
            self.most = max(self.most, self.active)

    def __exit__(self, *exc):
        with self._lock:
            self.active -= 1


TARGET = re.compile(r"<target>\n(.*?)\n</target>", re.S)


class RelationClient:
    """Answers step 3b by the target of each call. answers: target -> (candidate, relation) pairs (no entry: none is
    related). delay: seconds per call, or target -> seconds (other targets: 0.01), so later calls may end first.
    fail: target -> the exception that its call raises. Records in events ("start" | "end", target) in the order they
    happen, the most calls at the same time (concurrency.most) and the threads the calls ran in."""

    def __init__(self, answers: dict[str, list[tuple[str, str]]] | None = None, *,
                 delay: float | dict[str, float] = 0.01, fail: dict[str, Exception] | None = None):
        self.answers, self.delay, self.fail = answers or {}, delay, fail or {}
        self.events: list[tuple[str, str]] = []
        self.threads: set[int] = set()
        self.concurrency = Concurrency()
        self.messages = messages_api(self._create)
        self.beta = SimpleNamespace(messages=messages_api(self._create))

    def usage(self, target: str) -> Usage:
        return Usage(300 + len(target), 40, 0, 6000)

    def _create(self, **kw):
        target = TARGET.search(kw["messages"][0]["content"]).group(1)
        with self.concurrency:
            self.events.append(("start", target))
            self.threads.add(threading.get_ident())
            time.sleep(self.delay.get(target, 0.01) if isinstance(self.delay, dict) else self.delay)
            self.events.append(("end", target))
            if target in self.fail:
                raise self.fail[target]
            related = [RelatedSkill(skill_id=c, relation=r, reason="") for c, r in self.answers.get(target, [])]
            return ok(RelationAnswer(related=related), usage=self.usage(target), model="claude-haiku-5-5")


def cv_draft() -> CvDraft:
    ev = EvidenceDraft(quote="Built a Selenium test automation framework in Java", context="Acme", period_from=2016,
                       period_to=None, strength="outcome")
    return CvDraft(language="en", stations=[], skills=[
        CandidateSkillDraft(skill_id="selenium", name="Selenium", category="tool", level="expert", years=10,
                            evidence=[ev]),
        CandidateSkillDraft(skill_id="java", name="Java", category="programming_language", level="strong", years=10,
                            evidence=[ev])])


def req(skill_id, importance="must"):
    return RequirementDraft(skill_id=skill_id, name=skill_id, category="tool", importance=importance, min_level=None,
                            min_years=None, kind="tool", quote=skill_id)


def _first_gap(report: str) -> list[str]:
    """The id of the first gap line in a <role_report> section, if any."""
    lines = report.splitlines()
    if GAPS_HEADER not in lines:
        return []
    rest = lines[lines.index(GAPS_HEADER) + 1:]
    return [rest[0].split(" | ")[0]] if rest and " | " in rest[0] else []


class ScriptedClient:
    """Answers each step by the schema it asks for; records calls as Call(schema title, kwargs).

    `messages` and `beta` are plain namespaces, so a test can add `messages.batches` of a FakeBatchClient.
    Step 3b: each relation call waits relation_delay seconds, and for relation_gate (an Event, at most 5 s) if there
    is one; relation_started is set by the first; relations.most is the most relation calls at the same time.
    queued: schema title -> answers given first, in turn (a draft model, a scripted message such as refusal(), or an
    exception, which the call raises: KeyboardInterrupt is a Ctrl-C); when they are used up, the step answers as
    above. So runs of the same input can differ, and a command can stop in the middle."""

    def __init__(self, extra_requirement: str | None = None, cv_usage: Usage | None = None, *,
                 relation_delay: float = 0.0, relation_gate: threading.Event | None = None,
                 queued: dict[str, list] | None = None):
        self.calls: list[Call] = []
        self.extra = extra_requirement
        self.cv_usage = cv_usage
        self.relation_delay, self.relation_gate = relation_delay, relation_gate
        self.relation_started = threading.Event()
        self.relations = Concurrency()
        self.queued = {schema: list(values) for schema, values in (queued or {}).items()}
        self.messages = messages_api(self._answer)
        self.beta = SimpleNamespace(messages=messages_api(self._answer))

    def _answer(self, **kw):
        schema = kw["output_config"]["format"]["schema"]["title"]
        self.calls.append(Call(schema, kw))
        if self.queued.get(schema):
            value = self.queued[schema].pop(0)
            if isinstance(value, BaseException):
                raise value
            return ok(value, usage=self.cv_usage if schema == "CvDraft" else None) if isinstance(value, BaseModel) \
                else value
        if schema == "CvDraft":
            return ok(cv_draft(), usage=self.cv_usage)
        if schema == "JobDraft":
            title = kw["messages"][0]["content"].splitlines()[1][:30]
            reqs = [req("selenium"), req("java"), req("kubernetes", "nice")]
            reqs += [req(self.extra)] if self.extra else []
            return ok(JobDraft(title=title, role_family="test_automation", seniority="mid", requirements=reqs))
        if schema == "PathDraft":
            step = PathStepDraft(activity="Build a small project", effort_weeks=4, proof="public repository",
                                 closes=_first_gap(kw["messages"][0]["content"]))
            return ok(PathDraft(rationale="Close the top gap first.", steps=[step]))
        with self.relations:
            self.relation_started.set()
            if self.relation_gate is not None:
                self.relation_gate.wait(5)
            time.sleep(self.relation_delay)
        return ok(RelationAnswer(related=[]))


def scripted_client():
    return ScriptedClient()


def current_fingerprint() -> str:
    """The fingerprint of a match made now by the default rules: a test that stores a made-up match with it says the
    match follows the rules, so the app and `skillpose export` do not recompute it."""
    return match_fingerprint(load_settings().rules, datetime.now(UTC).year)


def fail_if_called():
    raise AssertionError("the API client must not be created")


class FailingClient:
    """A client where no API call may happen (demo mode): every call through it - messages.stream,
    beta.messages.stream, messages.batches.*, anything else - raises and is recorded in calls, so a test sees the
    attempt even where a background task swallows the error."""

    def __init__(self, path: str = "client", calls: list[str] | None = None):
        self._path = path
        self.calls = [] if calls is None else calls

    def __getattr__(self, name: str) -> "FailingClient":
        if name.startswith("__"):
            raise AttributeError(name)
        return FailingClient(f"{self._path}.{name}", self.calls)

    def __call__(self, *args, **kwargs):
        self.calls.append(self._path)
        raise AssertionError("no API call in demo mode")


@dataclass
class FakeLedger:
    total: float = 0.0
    records: list = field(default_factory=list)

    def month_total_usd(self) -> float:
        return self.total + sum(r.cost_usd for r in self.records)

    def record(self, step, model, usage, cost_usd, batch) -> None:
        self.records.append(SimpleNamespace(step=step, model=model, usage=usage, cost_usd=cost_usd, batch=batch))


@dataclass
class LLMCall:
    step: str
    fixed: list[str]
    variable: str
    schema: type


class FakeLLM:
    """Stands in for LLM: returns one preset value (or a list of values, one per call) and records the arguments."""

    def __init__(self, value=None, error: str | None = None, values: list | None = None, model: str = "fake-model"):
        self.values = list(values) if values is not None else None
        self.value, self.error, self.model = value, error, model
        self.calls: list[LLMCall] = []

    def call(self, step, fixed, variable, schema) -> CallResult:
        self.calls.append(LLMCall(step, fixed, variable, schema))
        value = self.values.pop(0) if self.values is not None else self.value
        error = self.error if value is None else None
        return CallResult(value=value, usage=Usage(100, 50, 0, 0), cost_usd=0.0, model=self.model,
                          error=error or (None if value is not None else "no parsed output"),
                          stop_reason="end_turn" if value is not None else error)

    def call_many(self, step, fixed, variables, schema, on_result, *, workers=1) -> None:
        """Like LLM.call_many, one call after the other."""
        for i, variable in enumerate(variables):
            on_result(i, self.call(step, fixed, variable, schema))
