"""Thin wrapper around the Anthropic SDK: prompt caching, server-side fallback, cost ledger, monthly limit.

Every call has a fixed part (system blocks, cached) and a variable part (one CV, posting or skill pair). Only the
last fixed block carries `cache_control`, so all calls of a run share one cached prefix.

Answers come back as JSON (structured output with the schema of a draft model). The program books the usage first
and validates the JSON itself afterwards: a truncated or invalid answer leaves the entry open but is still paid for,
so it must be in the ledger. The single and the batch path share `read_answer`.

Every single call is streamed and only the final message is used: the SDK refuses a request that is not streamed
above about 21,000 max_tokens, and step 1 needs more room (its thinking counts as output). Batches are not streamed.
A stream that breaks after message_start is paid too (the API has read the input): it is booked with the message as
far as it came. The ledger then knows its input and cache tokens, but the output only as far as it was reported: the
final count comes with the last events of the stream. A Ctrl-C in a single call is booked in the same way.

call_many sends several calls of one step at the same time (step 3b). Only the API request runs in a worker thread;
the budget check, the booking and the reading of each answer stay in the calling thread, because a SQLite connection
and so the ledger must not be shared between threads.
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache
from typing import Protocol

import anthropic
import httpx2
from pydantic import BaseModel, ValidationError

from skillpose.core.config import Price, Settings

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_NO_VALUE_STOPS = ("refusal", "max_tokens")


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int

    @classmethod
    def from_api(cls, u) -> "Usage":
        def n(name: str) -> int:
            return int(getattr(u, name, 0) or 0)
        return cls(n("input_tokens"), n("output_tokens"), n("cache_creation_input_tokens"),
                   n("cache_read_input_tokens"))


@dataclass
class CallResult[T: BaseModel]:
    value: T | None
    usage: Usage | None
    cost_usd: float
    model: str
    error: str | None
    stop_reason: str | None


@dataclass(frozen=True)
class Stopped:
    """A paid answer without a value because the model stopped early: at the token limit of its step ("max_tokens";
    the thinking counts too) or with a refusal ("refusal"). str() is the stop reason, as logs and stored errors had
    it before."""
    reason: str
    max_tokens: int      # the limit of the step
    cost_usd: float      # what the attempt cost; it is booked like any answer

    def __str__(self) -> str:
        return self.reason


def stopped(result: CallResult, max_tokens: int) -> Stopped | None:
    """The early stop of a call without value (max_tokens: the limit of its step), or None: a value, an invalid answer
    or an API error."""
    if result.value is None and result.stop_reason in _NO_VALUE_STOPS:
        return Stopped(result.stop_reason, max_tokens, result.cost_usd)
    return None


class CostLedger(Protocol):
    def month_total_usd(self) -> float: ...

    def record(self, step: str, model: str, usage: Usage, cost_usd: float, batch: bool) -> None: ...


class BudgetExceeded(RuntimeError):
    pass


def cost_usd(usage: Usage, price: Price, batch: bool = False) -> float:
    total = (usage.input_tokens * price.input + usage.cache_write_tokens * price.cache_write
             + usage.cache_read_tokens * price.cache_read + usage.output_tokens * price.output) / 1_000_000
    return total * 0.5 if batch else total


def fixed_blocks(texts: list[str]) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": t} for t in texts]
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def uses_fallback(model: str) -> bool:
    """Server-side fallback exists for Opus and Sonnet on the Claude API, not for Haiku."""
    return model.startswith(("claude-opus", "claude-sonnet"))


def price_for(settings: Settings, answered_by: str, requested: str) -> Price | None:
    price = settings.prices.get(answered_by)
    if price is None:
        log.warning("no price for %s; booking with the price of %s", answered_by, requested)
        price = settings.prices.get(requested)
    return price


@cache
def json_format(schema: type[BaseModel]) -> dict:
    """output_config.format for a draft model, in the form the API accepts."""
    return {"type": "json_schema", "schema": anthropic.transform_schema(schema)}


def answer_text(message) -> str:
    """The first text block of the model that answered. After a server-side fallback, content keeps the output of
    the model that declined, then a `fallback` block, then the answer of the fallback model: so the text comes from
    after the last fallback block."""
    content = list(message.content)
    begin = max((i + 1 for i, b in enumerate(content) if getattr(b, "type", None) == "fallback"), default=0)
    return next((b.text for b in content[begin:] if getattr(b, "type", None) == "text"), "")


def read_answer[T: BaseModel](message, schema: type[T]) -> tuple[T | None, str | None]:
    """The validated value of an answer, or None and the reason (stop reason or "invalid answer")."""
    stop = getattr(message, "stop_reason", None)
    if stop in _NO_VALUE_STOPS:
        return None, stop
    text = answer_text(message)
    try:
        return schema.model_validate_json(text), None
    except ValidationError as e:
        log.warning("invalid %s answer (%d errors)", schema.__name__, e.error_count())
        return None, "invalid answer"


def log_call(step: str, model: str, usage: Usage | None, usd: float, how: str) -> None:
    """One line per booked API call: what it cost, never what it said. how: "<seconds> s, single" or "<seconds> s,
    batch <id>" (a batch result: the time the whole batch took)."""
    if usage is None:
        log.info("api call %s: %s, no usage, %s", step, model, how)
        return
    log.info("api call %s: %s, input %d, output %d, cache write %d, cache read %d, %.4f USD, %s", step, model,
             usage.input_tokens, usage.output_tokens, usage.cache_write_tokens, usage.cache_read_tokens, usd, how)


def check_budget(settings: Settings, ledger: CostLedger) -> None:
    if ledger.month_total_usd() >= settings.monthly_limit_usd:
        raise BudgetExceeded(f"monthly limit of {settings.monthly_limit_usd:.2f} USD reached")


def estimate_usd(settings: Settings, step: str, n: int, *, avg_in: int = 2500, avg_out: int = 1000,
                 fixed_tokens: int = 5000, batch: bool = False) -> float:
    """Rough cost of n calls of one step: one cache write and n cache reads of the fixed part, plus the variable
    input and the output of each call."""
    if n <= 0:
        return 0.0
    price = settings.prices[settings.steps[step].model]
    usage = Usage(input_tokens=n * avg_in, output_tokens=n * avg_out, cache_write_tokens=fixed_tokens,
                  cache_read_tokens=n * fixed_tokens)
    return cost_usd(usage, price, batch=batch)


def estimate_cv_usd(settings: Settings, n_chars: int) -> float:
    """Rough cost of step 1 for a CV of n_chars characters. The output of the step (the thinking counts) grows with
    the CV: cv_out_per_in x its input tokens, at least cv_out_floor and never more than the max_tokens of the step.
    The numbers are in the `estimates` block of defaults.yaml."""
    e = settings.estimates
    tokens_in = round(n_chars / e.cv_chars_per_token)
    tokens_out = min(settings.steps["cv"].max_tokens, max(e.cv_out_floor, round(e.cv_out_per_in * tokens_in)))
    return estimate_usd(settings, "cv", 1, avg_in=tokens_in, avg_out=tokens_out, fixed_tokens=e.cv_fixed_tokens)


@dataclass
class _Request:
    step: str
    model: str
    kwargs: dict
    schema: type[BaseModel]


@dataclass
class _Sent:
    response: object | None
    error: anthropic.APIError | httpx2.HTTPError | KeyboardInterrupt | None
    seconds: float
    partial: object | None = None  # a stream that broke after message_start: the message as far as it came


def _snapshot(answer) -> object | None:
    """The message of a stream as far as it came, or None: no stream, or no message_start yet (the SDK asserts that
    message_start came before it gives the snapshot)."""
    if answer is None:
        return None
    try:
        return answer.current_message_snapshot
    except AssertionError:
        return None


class LLM:
    def __init__(self, client, settings: Settings, ledger: CostLedger):
        self.client = client
        self.settings = settings
        self.ledger = ledger

    def check_budget(self) -> None:
        check_budget(self.settings, self.ledger)

    def call[T: BaseModel](self, step: str, fixed: list[str], variable: str, schema: type[T]) -> CallResult[T]:
        """A Ctrl-C while the answer comes is booked like a broken stream, then raised again."""
        self.check_budget()
        request = self._request(step, fixed, variable, schema)
        sent = self._send(request)
        result = self._finish(request, sent)
        if isinstance(sent.error, KeyboardInterrupt):
            raise sent.error
        return result

    def call_many[T: BaseModel](self, step: str, fixed: list[str], variables: list[str], schema: type[T],
                                on_result: Callable[[int, CallResult[T]], None], *, workers: int = 1) -> None:
        """One call per variable part, at most `workers` at the same time. on_result(index, result) runs in this
        thread, in the order of variables. The first call runs alone: it writes the cache that the others read.

        The monthly limit is checked here before each call is sent. When it is reached, no further call is sent, but
        up to workers - 1 calls may already be under way: they are booked and passed on before BudgetExceeded is
        raised. An error that is no API error (an API error or a broken stream gives a result without value, as in
        call) stops new calls in the same way and is raised after the calls under way; if on_result raises, those
        calls are only booked."""
        requests = [self._request(step, fixed, variable, schema) for variable in variables]
        if not requests:
            return
        workers = max(1, workers)
        pending: deque[tuple[int, _Request, Future[_Sent]]] = deque()
        stop: Exception | None = None
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"skillpose-{step}") as pool:
            def send(index: int) -> None:
                self.check_budget()
                pending.append((index, requests[index], pool.submit(self._send, requests[index])))

            try:
                send(0)
                sent_up_to = 1
                while pending:
                    # the call stays in pending until its answer is here: Ctrl-C comes in this wait, and the handler
                    # below must book this call too
                    index, request, future = pending[0]
                    try:
                        sent = future.result()
                    except Exception as e:  # not an API error: no new call, but the calls under way still count
                        pending.popleft()
                        if stop is not None:  # only one error can be raised: the others go to the log
                            log.error("%s call failed", step, exc_info=True)
                        stop = stop or e
                        continue
                    pending.popleft()
                    on_result(index, self._finish(request, sent))
                    while stop is None and sent_up_to < len(requests) and len(pending) < workers:
                        try:
                            send(sent_up_to)
                        except BudgetExceeded as e:
                            stop = e
                            break
                        sent_up_to += 1
            except BaseException:
                for _, request, future in pending:  # paid for: book them, whatever happened here
                    try:
                        self._finish(request, future.result())
                    except Exception:
                        log.error("%s call failed", step, exc_info=True)
                raise
        if stop is not None:
            raise stop

    def _request(self, step: str, fixed: list[str], variable: str, schema: type[BaseModel]) -> _Request:
        cfg = self.settings.steps[step]
        kwargs = dict(model=cfg.model, max_tokens=cfg.max_tokens, system=fixed_blocks(fixed),
                      messages=[{"role": "user", "content": variable}],
                      output_config={"format": json_format(schema), "effort": cfg.effort})
        return _Request(step=step, model=cfg.model, kwargs=kwargs, schema=schema)

    def _send(self, request: _Request) -> _Sent:
        """The API request alone; it touches neither the ledger nor the store, so it may run in a worker thread. The
        answer is streamed; the final message is the same Message that a request without streaming returns. The SDK
        retries only the start of a stream. A connection lost later comes as an httpx error, an error event in the
        stream (e.g. overloaded) as an APIStatusError; both count as an API error (any httpx error does, also a
        DecodingError). But after message_start the API has read the input and the attempt is paid: the message as far
        as it came goes along in `partial`, to be booked. A Ctrl-C comes only in the main thread, in a single call: it
        goes along in the same way, and `call` raises it again after the booking."""
        started = time.monotonic()
        answer = None
        try:
            if uses_fallback(request.model):
                stream = self.client.beta.messages.stream(**request.kwargs, betas=[FALLBACK_BETA],
                                                          fallbacks="default")
            else:
                stream = self.client.messages.stream(**request.kwargs)
            with stream as answer:
                response = answer.get_final_message()
        except (anthropic.APIError, httpx2.HTTPError, KeyboardInterrupt) as e:
            return _Sent(response=None, error=e, seconds=time.monotonic() - started, partial=_snapshot(answer))
        return _Sent(response=response, error=None, seconds=time.monotonic() - started)

    def _finish(self, request: _Request, sent: _Sent) -> CallResult:
        """Book the answer, then read it. A failed call has no answer; it is booked when its stream had begun
        (`partial`): with the input and cache tokens of message_start and the output reported so far. That is the
        paid output only if the stream broke after the final count (message_delta, which also sets the stop reason);
        before it, the paid output may be higher."""
        if sent.error is not None:
            reason = str(sent.error) or type(sent.error).__name__
            log.warning("%s call failed after %.1f s: %s", request.step, sent.seconds, reason)
            model, usage, cost = request.model, None, 0.0
            if sent.partial is not None:
                final = getattr(sent.partial, "stop_reason", None) is not None
                note = ("broken stream after the final output count, booked as reported" if final else
                        "broken stream: output may be incomplete, booked as reported so far")
                model, usage, cost = self._book(request, sent.partial, f"{sent.seconds:.1f} s, single, {note}")
            return CallResult(None, usage, cost, model, reason, None)
        model, usage, cost = self._book(request, sent.response, f"{sent.seconds:.1f} s, single")
        value, error = read_answer(sent.response, request.schema)
        return CallResult(value, usage, cost, model, error, getattr(sent.response, "stop_reason", None))

    def _book(self, request: _Request, message, how: str) -> tuple[str, Usage | None, float]:
        """Book the usage of a message in the ledger, with the price of the model that answered; one log line."""
        model = getattr(message, "model", None) or request.model
        usage, cost = None, 0.0
        if getattr(message, "usage", None) is not None:
            usage = Usage.from_api(message.usage)
            price = price_for(self.settings, model, request.model)
            cost = cost_usd(usage, price) if price else 0.0
            self.ledger.record(request.step, model, usage, cost, False)
        log_call(request.step, model, usage, cost, how)
        return model, usage, cost
