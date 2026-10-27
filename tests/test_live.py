"""Tests against the real Claude API. They cost money and run only with `pytest -m live`.

Set ANTHROPIC_API_KEY, or SKILLPOSE_ENV_FILE to a file with ANTHROPIC_API_KEY=... .
"""

import os
from pathlib import Path

import anthropic
import pytest

from skillpose.cli import load_env_file
from skillpose.core.batch import extract_jobs_batch
from skillpose.core.config import load_settings
from skillpose.core.extract_job import extract_job
from skillpose.core.llm import LLM
from skillpose.core.models import CandidateProfile, CandidateSkill, Evidence, GapStat, RoleReport, Station, text_hash
from skillpose.core.paths import build_path
from skillpose.core.vocabulary import Vocabulary
from skillpose.store.db import Store

POSTINGS = Path(__file__).parent.parent / "evals" / "goldset" / "postings"

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    if os.environ.get("SKILLPOSE_ENV_FILE"):
        load_env_file(Path(os.environ["SKILLPOSE_ENV_FILE"]))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("no ANTHROPIC_API_KEY")
    return anthropic.Anthropic()


def _texts(n: int) -> list[str]:
    return [p.read_text(encoding="utf-8") for p in sorted(POSTINGS.glob("*.md"))[:n]]


def test_schemas_accepted_and_cache_used(client, tmp_path):
    settings, vocab = load_settings(), Vocabulary.load()
    llm = LLM(client, settings, Store(tmp_path / "live.sqlite"))
    snapshot = vocab.snapshot()
    results = [extract_job(llm, t, vocab, snapshot, settings.role_families, "en", settings.max_posting_chars)
               for t in _texts(2)]
    (jp1, r1), (jp2, r2) = results
    assert jp1 is not None and jp2 is not None, (r1.error, r2.error)
    assert r1.usage.cache_write_tokens > 0 or r1.usage.cache_read_tokens > 0
    assert r2.usage.cache_read_tokens > 0


def test_batch_roundtrip(client, tmp_path):
    settings, vocab = load_settings(), Vocabulary.load()
    texts = {text_hash(t): t for t in _texts(2)}
    out = extract_jobs_batch(client, settings, Store(tmp_path / "live.sqlite"), texts, vocab, vocab.snapshot(), "en",
                             poll_seconds=15)
    assert all(v is not None for v in out.values())


def test_path_schema_accepted(client, tmp_path):
    settings, vocab = load_settings(), Vocabulary.load()
    llm = LLM(client, settings, Store(tmp_path / "live.sqlite"))
    evidence = Evidence(quote="Reports in SQL", context="Analyst", period_from=2021, period_to=None,
                        strength="activity", verified=True)
    profile = CandidateProfile(language="en", skills=[CandidateSkill(skill_id="sql", level="working", years=4,
                                                                     last_used=2026, evidence=[evidence])],
                               stations=[Station(title="Analyst", employer="Example Ltd", period_from=2021,
                                                 period_to=None)])
    report = RoleReport(role_family="data_engineering", postings=6, median_coverage=0.4, tier=2,
                        tier_shares={1: 0.0, 2: 0.5, 3: 0.5},
                        top_gaps=[GapStat(skill_id="airflow", weight=8.0, postings=4, gap_type="weeks"),
                                  GapStat(skill_id="aws", weight=6.0, postings=3, gap_type="months")])
    path, result = build_path(llm, profile, report, vocab, "en")
    assert path is not None, result.error
    assert 1 <= len(path.steps) <= 5
