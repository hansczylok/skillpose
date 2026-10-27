"""Step 2 for many postings at once through the Message Batches API (half price, results within minutes to hours).

Server-side fallbacks are not allowed in batches, so the schema goes in as plain JSON schema and the program
validates each answer itself. Results arrive in any order and are matched by custom_id (the text hash).
"""

import logging
import time
from collections.abc import Callable

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from skillpose.core.config import Settings
from skillpose.core.extract_job import job_from_draft, job_request_parts
from skillpose.core.llm import (
    BudgetExceeded,
    CostLedger,
    Usage,
    check_budget,
    cost_usd,
    estimate_usd,
    fixed_blocks,
    json_format,
    log_call,
    price_for,
    read_answer,
)
from skillpose.core.models import JobDraft, JobProfile
from skillpose.core.vocabulary import Vocabulary

log = logging.getLogger(__name__)


def _request(custom_id: str, text: str, settings: Settings, snapshot: str, lang: str) -> Request:
    cfg = settings.steps["job"]
    fixed, variable = job_request_parts(text, snapshot, settings.role_families, lang)
    params = MessageCreateParamsNonStreaming(
        model=cfg.model, max_tokens=cfg.max_tokens, system=fixed_blocks(fixed),
        messages=[{"role": "user", "content": variable}],
        output_config={"format": json_format(JobDraft), "effort": cfg.effort})
    return Request(custom_id=custom_id, params=params)


def submit_jobs_batch(client, settings: Settings, ledger: CostLedger, texts: dict[str, str], snapshot: str,
                      lang: str, *, reserved_usd: float = 0.0) -> str:
    """Submit one batch and return its id. The caller stores the id, so an interrupted run can collect it later
    instead of paying twice. reserved_usd: the estimate of the batches this run submitted before this one; the
    ledger books them only when they are collected."""
    check_budget(settings, ledger)
    # one batch is one payment: it must fit into the rest of the monthly limit as a whole
    estimate = estimate_usd(settings, "job", len(texts), batch=True)
    if ledger.month_total_usd() + reserved_usd + estimate > settings.monthly_limit_usd:
        raise BudgetExceeded(f"batch of {len(texts)} postings (~{estimate:.2f} USD) does not fit into the monthly "
                             f"limit of {settings.monthly_limit_usd:.2f} USD")
    batch = client.messages.batches.create(
        requests=[_request(h, text, settings, snapshot, lang) for h, text in texts.items()])
    log.info("batch %s submitted with %d postings", batch.id, len(texts))
    return batch.id


def batch_seconds(batch) -> float | None:
    """How long a batch took from its submission to its end, from a status answer (created_at, ended_at); None while
    it runs or when the answer lacks the times."""
    created, ended = getattr(batch, "created_at", None), getattr(batch, "ended_at", None)
    if created is None or ended is None:
        return None
    return max(0.0, (ended - created).total_seconds())


def collect_jobs_batch(client, settings: Settings, ledger: CostLedger, batch_id: str, texts: dict[str, str],
                       vocab: Vocabulary, *, poll_seconds: float = 30.0,
                       sleep: Callable[[float], None] = time.sleep) -> dict[str, JobProfile | None]:
    """Wait for a batch, then read_jobs_batch."""
    while (batch := client.messages.batches.retrieve(batch_id)).processing_status != "ended":
        sleep(poll_seconds)
    return read_jobs_batch(client, settings, ledger, batch_id, texts, vocab, seconds=batch_seconds(batch))


def read_jobs_batch(client, settings: Settings, ledger: CostLedger, batch_id: str, texts: dict[str, str],
                    vocab: Vocabulary, *, seconds: float | None = None) -> dict[str, JobProfile | None]:
    """The results of a batch that has ended: book every answer at half price and return a JobProfile (or None) per
    text hash. seconds: how long the batch took (batch_seconds); a result has no duration of its own, so the log
    line of each result names this one."""
    took = "duration unknown" if seconds is None else f"{seconds:.1f} s"
    requested = settings.steps["job"].model
    out: dict[str, JobProfile | None] = dict.fromkeys(texts)
    for item in client.messages.batches.results(batch_id):
        if item.custom_id not in texts:
            continue
        if item.result.type != "succeeded":
            log.warning("batch item %s: %s", item.custom_id, item.result.type)
            continue
        message = item.result.message
        if getattr(message, "usage", None) is not None:
            usage = Usage.from_api(message.usage)
            model = getattr(message, "model", None) or requested
            price = price_for(settings, model, requested)
            usd = cost_usd(usage, price, batch=True) if price else 0.0
            ledger.record("job", model, usage, usd, True)
            log_call("job", model, usage, usd, f"{took}, batch {batch_id}")
        draft, error = read_answer(message, JobDraft)
        if draft is None:
            log.warning("batch item %s stays open: %s", item.custom_id, error)
            continue
        out[item.custom_id] = job_from_draft(draft, texts[item.custom_id], vocab, settings.role_families)
    return out


def extract_jobs_batch(client, settings: Settings, ledger: CostLedger, texts: dict[str, str], vocab: Vocabulary,
                       snapshot: str, lang: str, *, poll_seconds: float = 30.0,
                       sleep: Callable[[float], None] = time.sleep) -> dict[str, JobProfile | None]:
    batch_id = submit_jobs_batch(client, settings, ledger, texts, snapshot, lang)
    return collect_jobs_batch(client, settings, ledger, batch_id, texts, vocab, poll_seconds=poll_seconds,
                              sleep=sleep)
