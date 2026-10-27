import json
import logging
import re

import anthropic
import httpx2
import pytest
from fakes import BrokenStream, Dummy, FakeClient, FakeLedger, answer, api_error, ok, refusal, started

from skillpose.core.config import Price, load_settings
from skillpose.core.llm import LLM, BudgetExceeded, CallResult, Stopped, Usage, cost_usd, estimate_usd, stopped


def test_fixed_part_identical_and_cached_only_at_end():
    client = FakeClient(answers=[ok(Dummy(x=1)), ok(Dummy(x=2))])
    llm = LLM(client, load_settings(), FakeLedger())
    llm.call("job", ["SYSTEM", "VOCAB"], "<posting>A</posting>", Dummy)
    llm.call("job", ["SYSTEM", "VOCAB"], "<posting>B</posting>", Dummy)
    s1, s2 = (c.kwargs["system"] for c in client.calls)
    assert s1 == s2 and "cache_control" in s1[-1] and all("cache_control" not in b for b in s1[:-1])
    assert client.calls[0].kwargs["messages"] != client.calls[1].kwargs["messages"]


def test_opus_uses_fallback_haiku_not():
    client = FakeClient(answers=[ok(Dummy(x=1)), ok(Dummy(x=1))])
    llm = LLM(client, load_settings(), FakeLedger())
    llm.call("cv", ["S"], "v", Dummy)
    llm.call("relation", ["S"], "v", Dummy)  # relation runs on Haiku 5.5
    assert client.calls[0].path == "beta.messages.stream" and client.calls[0].kwargs["fallbacks"] == "default"
    assert client.calls[0].kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert client.calls[1].path == "messages.stream" and "fallbacks" not in client.calls[1].kwargs
    config = client.calls[1].kwargs["output_config"]
    assert config["effort"] == "low" and config["format"]["type"] == "json_schema"
    assert config["format"]["schema"]["title"] == "Dummy"


def test_every_call_is_streamed_and_its_stream_closed():
    # above about 21,000 max_tokens the SDK refuses a request that is not streamed; step 1 needs more
    client = FakeClient(answers=[ok(Dummy(x=1)), ok(Dummy(x=2)), ok(Dummy(x=3))])
    llm = LLM(client, load_settings(), FakeLedger())
    results = [llm.call(step, ["S"], "v", Dummy) for step in ("cv", "job", "relation")]
    assert [r.value for r in results] == [Dummy(x=1), Dummy(x=2), Dummy(x=3)]
    assert [c.path for c in client.calls] == ["beta.messages.stream", "beta.messages.stream", "messages.stream"]
    assert len(client.streams) == 3 and all(s.closed for s in client.streams)
    assert client.calls[0].kwargs["max_tokens"] == 32000


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _start(model: str, usage: Usage) -> list[dict]:
    """message_start (input and cache tokens, output 1), then the start of the thinking."""
    start_usage = {"input_tokens": usage.input_tokens, "output_tokens": 1,
                   "cache_creation_input_tokens": usage.cache_write_tokens,
                   "cache_read_input_tokens": usage.cache_read_tokens}
    return [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                              "content": [], "stop_reason": None, "stop_sequence": None,
                                              "usage": start_usage}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "",
                                                                      "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "..."}},
    ]


def _text(index: int, text: str) -> list[dict]:
    return [{"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": index}]


def _end(stop_reason: str, usage: Usage) -> list[dict]:
    return [{"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
             "usage": {"output_tokens": usage.output_tokens}},
            {"type": "message_stop"}]


def _streamed_answer(model: str, text: str, stop_reason: str, usage: Usage) -> bytes:
    """The server-sent events of one answer: thinking, then the text, then the stop reason and the usage."""
    return _sse([*_start(model, usage), {"type": "content_block_stop", "index": 0}, *_text(1, text),
                 *_end(stop_reason, usage)])


def _sdk_llm(bodies: list, ledger: FakeLedger) -> tuple[LLM, list[httpx2.Request]]:
    """An LLM on the real SDK against a local transport (no request leaves the machine), with the SDK's own retries
    (max_retries 2): a request that went out twice shows in the returned list. The n-th request gets the n-th body
    (bytes, or an iterator of bytes that may break)."""
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=bodies[len(sent) - 1])

    client = anthropic.Anthropic(api_key="no-key", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return LLM(client, load_settings(), ledger), sent


def test_the_sdk_streams_each_request_and_the_final_message_is_booked_and_read():
    bodies = [_streamed_answer("claude-opus-5-5", '{"x": ', "max_tokens", Usage(4657, 32000, 0, 0)),
              _streamed_answer("claude-haiku-5-5", '{"x": 7}', "end_turn", Usage(300, 40, 0, 6000))]
    ledger = FakeLedger()
    llm, sent = _sdk_llm(bodies, ledger)
    cut, fine = llm.call("cv", ["S"], "v", Dummy), llm.call("relation", ["S"], "v", Dummy)

    opus, haiku = (json.loads(r.content) for r in sent)
    assert opus["stream"] is True and opus["max_tokens"] == 32000 and opus["fallbacks"] == "default"
    assert sent[0].headers["anthropic-beta"] == "server-side-fallback-2026-07-01" and sent[0].url.path == "/v1/messages"
    assert {r.headers["anthropic-version"] for r in sent} == {"2026-10-21"}  # the API version of anthropic 2.x
    assert opus["output_config"]["effort"] == "medium" and opus["output_config"]["format"]["type"] == "json_schema"
    assert haiku["stream"] is True and "fallbacks" not in haiku and "anthropic-beta" not in sent[1].headers
    assert (cut.value, cut.error, cut.stop_reason, cut.model) == (None, "max_tokens", "max_tokens", "claude-opus-5-5")
    assert cut.usage == Usage(4657, 32000, 0, 0) and cut.cost_usd == pytest.approx(4657 * 4e-6 + 32000 * 20e-6)
    assert fine.value == Dummy(x=7) and fine.usage == Usage(300, 40, 0, 6000)
    assert [r.cost_usd for r in ledger.records] == [cut.cost_usd, fine.cost_usd]


def test_a_stream_that_breaks_books_its_start_once_and_gives_an_open_result():
    # the SDK retries only the start of a stream: a connection lost later comes as an httpx error. The API has read
    # the input by then (message_start reported it), so the attempt is paid: it is booked once, with that usage
    ledger = FakeLedger()
    answers = [BrokenStream(httpx2.ReadError("peer closed connection"), started(Usage(4657, 1, 5000, 0))),
               BrokenStream(httpx2.ReadError("lost before the start")), ok(Dummy(x=1))]
    client = FakeClient(answers=answers)
    llm = LLM(client, load_settings(), ledger)
    r = llm.call("cv", ["S"], "v", Dummy)
    assert (r.value, r.error, r.stop_reason, r.model) == (None, "peer closed connection", None, "claude-opus-5-5")
    assert r.usage == Usage(4657, 1, 5000, 0) and r.cost_usd == pytest.approx((4657 * 4 + 5000 * 5 + 20) / 1e6)
    assert [(x.step, x.usage, x.cost_usd) for x in ledger.records] == [("cv", r.usage, r.cost_usd)]
    assert stopped(r, 32000) is None  # the message names the error, not a token limit
    early = llm.call("cv", ["S"], "v", Dummy)  # broke before message_start: nothing is known, nothing is booked
    assert (early.value, early.usage, early.cost_usd, early.error) == (None, None, 0.0, "lost before the start")
    assert len(ledger.records) == 1 and all(s.closed for s in client.streams)
    got: list[CallResult] = []
    llm.call_many("relation", ["S"], ["a"], Dummy, lambda i, result: got.append(result))  # the next call still goes
    assert got[0].value == Dummy(x=1)


def _lost(events: bytes, error: BaseException):
    """A response body that breaks after its first part."""
    yield events
    raise error


def test_a_stream_that_breaks_in_the_sdk_is_booked_once_with_the_usage_of_its_start(caplog):
    # the real SDK with its default retries (max_retries 2): the connection breaks after message_start, or the API
    # sends an error event in the stream (e.g. overloaded). Both are paid attempts; input and cache come from
    # message_start, the output so far is not the output up to the break
    begun = _start("claude-opus-5-5", Usage(4657, 0, 5000, 0))
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    lost = _lost(_sse(begun), httpx2.RemoteProtocolError("peer closed connection without a complete message body"))
    ledger = FakeLedger()
    llm, sent = _sdk_llm([lost, _sse([*begun, overloaded])], ledger)
    assert llm.client.max_retries == 2
    with caplog.at_level(logging.INFO, logger="skillpose"):
        broken, failed = llm.call("cv", ["S"], "v", Dummy), llm.call("cv", ["S"], "v", Dummy)
    assert len(sent) == 2  # each request went out once: the SDK does not repeat a stream that has begun
    assert broken.error.startswith("peer closed connection") and "overloaded_error" in failed.error
    start_cost = (4657 * 4 + 5000 * 5 + 1 * 20) / 1e6
    for r in (broken, failed):
        assert (r.value, r.stop_reason, r.model, r.usage) == (None, None, "claude-opus-5-5", Usage(4657, 1, 5000, 0))
        assert r.cost_usd == pytest.approx(start_cost)
    assert [(x.step, x.model, x.usage) for x in ledger.records] == [("cv", "claude-opus-5-5", broken.usage)] * 2
    assert sum(x.cost_usd for x in ledger.records) == pytest.approx(2 * start_cost)
    booked = [m for m in caplog.messages if m.startswith("api call cv")]
    assert len(booked) == 2 and all("output may be incomplete" in m and "unknown" not in m for m in booked), booked


def test_a_stream_that_breaks_after_the_final_output_count_says_so_in_the_log(caplog):
    # message_delta carries the stop reason and the final output count; only message_stop is missing: the booked
    # output is complete, and the log line must not call it unknown or incomplete
    usage = Usage(4657, 15694, 5000, 0)
    events = [*_start("claude-opus-5-5", usage), {"type": "content_block_stop", "index": 0}, *_text(1, '{"x": 7}'),
              _end("end_turn", usage)[0]]
    ledger = FakeLedger()
    llm, sent = _sdk_llm([_lost(_sse(events), httpx2.RemoteProtocolError("peer closed connection"))], ledger)
    with caplog.at_level(logging.INFO, logger="skillpose"):
        r = llm.call("cv", ["S"], "v", Dummy)
    assert len(sent) == 1 and r.value is None and r.error.startswith("peer closed connection")
    assert r.usage == usage and r.cost_usd == pytest.approx((4657 * 4 + 15694 * 20 + 5000 * 5) / 1e6)
    [line] = [m for m in caplog.messages if m.startswith("api call cv")]
    assert "output 15694" in line and "after the final output count" in line and "incomplete" not in line, line
    assert [x.usage for x in ledger.records] == [usage]


def test_a_decoding_error_in_the_stream_is_booked_like_a_lost_connection():
    # httpx2.DecodingError (a bad content encoding) is an HTTPError, but no TransportError
    assert issubclass(httpx2.DecodingError, httpx2.HTTPError) and not issubclass(httpx2.DecodingError,
                                                                                httpx2.TransportError)
    begun = _start("claude-opus-5-5", Usage(4657, 0, 5000, 0))
    ledger = FakeLedger()
    garbled = _lost(_sse(begun), httpx2.DecodingError("invalid gzip data"))
    llm, sent = _sdk_llm([garbled, _lost(b"", httpx2.DecodingError("garbled before the start"))], ledger)
    after, before = llm.call("cv", ["S"], "v", Dummy), llm.call("cv", ["S"], "v", Dummy)
    assert (after.value, after.error, after.usage) == (None, "invalid gzip data", Usage(4657, 1, 5000, 0))
    assert after.cost_usd == pytest.approx((4657 * 4 + 5000 * 5 + 1 * 20) / 1e6)
    assert (before.value, before.error, before.usage, before.cost_usd) == (None, "garbled before the start", None, 0.0)
    assert [x.usage for x in ledger.records] == [after.usage]  # the first was paid, the second never began


def test_ctrl_c_in_a_single_call_books_the_stream_so_far_and_raises_again():
    # call_many books its calls under way when Ctrl-C comes; a single call has to do the same
    ledger = FakeLedger()
    answers = [BrokenStream(KeyboardInterrupt(), started(Usage(4657, 1, 5000, 0))),
               BrokenStream(KeyboardInterrupt()), ok(Dummy(x=1))]
    client = FakeClient(answers=answers)
    llm = LLM(client, load_settings(), ledger)
    with pytest.raises(KeyboardInterrupt):
        llm.call("cv", ["S"], "v", Dummy)
    assert [(x.step, x.usage, x.cost_usd) for x in ledger.records] == [
        ("cv", Usage(4657, 1, 5000, 0), pytest.approx((4657 * 4 + 5000 * 5 + 1 * 20) / 1e6))]
    with pytest.raises(KeyboardInterrupt):  # before message_start nothing is known, nothing is booked
        llm.call("cv", ["S"], "v", Dummy)
    assert len(ledger.records) == 1 and all(s.closed for s in client.streams)
    assert llm.call("relation", ["S"], "v", Dummy).value == Dummy(x=1)  # the object is still usable


def test_ctrl_c_in_a_stream_of_the_sdk_books_the_start_usage():
    begun = _start("claude-opus-5-5", Usage(4657, 0, 5000, 0))
    ledger = FakeLedger()
    llm, sent = _sdk_llm([_lost(_sse(begun), KeyboardInterrupt())], ledger)
    with pytest.raises(KeyboardInterrupt):
        llm.call("cv", ["S"], "v", Dummy)
    assert len(sent) == 1 and [x.usage for x in ledger.records] == [Usage(4657, 1, 5000, 0)]


def test_after_a_fallback_in_the_answer_the_text_of_the_fallback_model_is_read():
    # content keeps the output of the model that declined, then a fallback block, then the answer of the fallback
    # model (SDK: BetaFallbackBlock); the first text block is the declined part
    fallback = {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
                "trigger": {"type": "refusal", "category": None}}
    usage = Usage(4657, 900, 0, 0)
    events = [*_start("claude-opus-5-5", usage), {"type": "content_block_stop", "index": 0}, *_text(1, '{"x": '),
              {"type": "content_block_start", "index": 2, "content_block": fallback},
              {"type": "content_block_stop", "index": 2}, *_text(3, '{"x": 3}'), *_end("end_turn", usage)]
    ledger = FakeLedger()
    llm, _ = _sdk_llm([_sse(events)], ledger)
    r = llm.call("cv", ["S"], "v", Dummy)
    assert (r.value, r.error, r.model) == (Dummy(x=3), None, "claude-opus-5")
    assert r.cost_usd == pytest.approx((4657 * 5 + 900 * 25) / 1e6)  # the price of the model that answered
    assert [x.model for x in ledger.records] == ["claude-opus-5"]


def test_truncated_answer_is_booked_and_left_open():
    ledger = FakeLedger()
    llm = LLM(FakeClient(answers=[answer('{"x": ', stop_reason="max_tokens")]), load_settings(), ledger)
    r = llm.call("cv", ["S"], "v", Dummy)
    assert r.value is None and r.error == "max_tokens" and len(ledger.records) == 1


def test_an_early_stop_names_the_limit_of_its_step_and_the_cost_of_the_attempt():
    ledger = FakeLedger()
    answers = [answer('{"x": ', stop_reason="max_tokens", usage=Usage(4657, 32000, 0, 0)), refusal(),
               answer('{"y": 1}'), api_error(529), ok(Dummy(x=1))]
    llm = LLM(FakeClient(answers=answers), load_settings(), ledger)
    cut, refused, invalid, failed, fine = (llm.call("cv", ["S"], "v", Dummy) for _ in range(len(answers)))
    assert stopped(cut, 32000) == Stopped("max_tokens", 32000, pytest.approx(0.658628))
    assert stopped(refused, 32000) == Stopped("refusal", 32000, pytest.approx(0.002))
    assert str(stopped(cut, 32000)) == "max_tokens" and str(stopped(refused, 32000)) == "refusal"
    assert stopped(invalid, 32000) is stopped(failed, 32000) is stopped(fine, 32000) is None
    assert len(ledger.records) == 4  # each answer is paid, the stopped ones too


def test_invalid_answer_is_booked_and_left_open():
    ledger = FakeLedger()
    llm = LLM(FakeClient(answers=[answer('{"y": "no x"}')]), load_settings(), ledger)
    r = llm.call("job", ["S"], "v", Dummy)
    assert r.value is None and r.error == "invalid answer" and len(ledger.records) == 1


def test_cost_with_cache_and_batch():
    p = Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01)
    u = Usage(input_tokens=2000, output_tokens=1000, cache_write_tokens=0, cache_read_tokens=5000)
    assert cost_usd(u, p) == pytest.approx(0.00075)
    assert cost_usd(u, p, batch=True) == pytest.approx(0.000375)


def test_call_books_cost_with_price_of_answering_model():
    ledger = FakeLedger()
    llm = LLM(FakeClient(answers=[ok(Dummy(x=1), usage=Usage(1000, 1000, 0, 0), model="claude-haiku-5-5")]),
              load_settings(), ledger)
    r = llm.call("job", ["S"], "v", Dummy)
    assert r.value == Dummy(x=1) and r.cost_usd == pytest.approx(0.0006)
    assert ledger.records[0].cost_usd == pytest.approx(0.0006) and ledger.records[0].batch is False


def test_unknown_fallback_model_is_priced_as_requested_model():
    ledger = FakeLedger()
    llm = LLM(FakeClient(answers=[ok(Dummy(x=1), usage=Usage(1000, 0, 0, 0), model="claude-opus-next")]),
              load_settings(), ledger)
    r = llm.call("cv", ["S"], "v", Dummy)
    assert r.model == "claude-opus-next" and r.cost_usd == pytest.approx(0.004)


def test_known_fallback_model_is_priced_with_its_own_price():
    ledger = FakeLedger()
    llm = LLM(FakeClient(answers=[ok(Dummy(x=1), usage=Usage(1000, 0, 0, 0), model="claude-opus-4-8")]),
              load_settings(), ledger)
    r = llm.call("cv", ["S"], "v", Dummy)
    assert r.model == "claude-opus-4-8" and r.cost_usd == pytest.approx(0.005)


def test_refusal_is_booked_but_has_no_value():
    ledger = FakeLedger()
    llm = LLM(FakeClient(answers=[refusal()]), load_settings(), ledger)
    r = llm.call("cv", ["S"], "v", Dummy)
    assert r.value is None and r.error == "refusal" and len(ledger.records) == 1


def test_budget_stops_before_call():
    client = FakeClient(answers=[])
    llm = LLM(client, load_settings(), FakeLedger(total=5.0))
    with pytest.raises(BudgetExceeded):
        llm.call("job", ["S"], "v", Dummy)
    assert client.calls == []


def test_api_error_returns_open_result():
    llm = LLM(FakeClient(answers=[api_error(529)]), load_settings(), FakeLedger())
    r = llm.call("job", ["S"], "v", Dummy)
    assert r.value is None and r.error and r.usage is None


def test_estimate_grows_with_count_and_halves_in_batch():
    s = load_settings()
    assert estimate_usd(s, "job", 0) == 0.0
    one, hundred = estimate_usd(s, "job", 1), estimate_usd(s, "job", 100)
    assert 0 < one < hundred
    assert estimate_usd(s, "job", 100, batch=True) == pytest.approx(hundred / 2)


def test_each_call_is_one_line_in_the_log_without_its_text(caplog):
    answers = [ok(Dummy(x=1), usage=Usage(1000, 200, 300, 4000), model="claude-sonnet-5-5"), api_error(529)]
    llm = LLM(FakeClient(answers=answers), load_settings(), FakeLedger())
    with caplog.at_level(logging.INFO, logger="skillpose"):
        llm.call("job", ["SYSTEM PROMPT"], "<posting>secret text</posting>", Dummy)
        llm.call("job", ["SYSTEM PROMPT"], "<posting>secret text</posting>", Dummy)
    line, failed = (r.getMessage() for r in caplog.records)
    assert re.fullmatch(r"api call job: claude-sonnet-5-5, input 1000, output 200, cache write 300, cache read 4000, "
                        r"0\.\d{4} USD, \d+\.\d s, single", line), line
    assert re.match(r"job call failed after \d+\.\d s: status 529", failed), failed
    assert "secret" not in caplog.text and "SYSTEM PROMPT" not in caplog.text
