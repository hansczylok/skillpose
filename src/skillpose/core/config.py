"""Settings: models per step, prices, rule thresholds. Defaults live in defaults.yaml next to this file."""

from datetime import date
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class StepModel(BaseModel):
    model: str
    effort: Literal["low", "medium", "high"]
    max_tokens: int = Field(gt=0, le=64000)  # requests are streamed (LLM._send), so more than the SDK allows unstreamed


class Price(BaseModel):
    """US dollars per million tokens."""
    input: float
    output: float
    cache_write: float
    cache_read: float


class Rules(BaseModel):
    stale_years: int = 8
    tier1_coverage: float = 0.8
    tier2_max_weeks: int = 26
    years_gap_margin: float = 2.0
    effort_weeks: dict[str, int] = {"weeks": 4, "months": 12, "certificate": 8}
    role_tier_share: float = 0.4
    min_jobs_per_role: int = 1
    conditions_default_travel_share: int = Field(default=10, ge=0, le=100)


class Estimates(BaseModel):
    """What the estimate before a run assumes about step 1, from measured runs (the numbers are in defaults.yaml)."""
    cv_chars_per_token: float = Field(default=2.0, gt=0)  # characters of a CV per input token
    cv_out_per_in: float = Field(default=4.0, gt=0)       # output tokens (thinking included) per input token
    cv_out_floor: int = Field(default=6000, ge=0)         # the least output of step 1
    cv_fixed_tokens: int = Field(default=12000, ge=0)     # the fixed part (system blocks) of step 1


class Stability(BaseModel):
    """The run-to-run variance measured with `skillpose eval --repeat` (source "cv" of its JSON lines): the note on
    the profile page and the market view."""
    level_same: float = Field(ge=0, le=1)       # of the skills found in two runs of a CV: the share with the same level
    role_tier_same: float = Field(ge=0, le=1)   # of the role families: the share with the same tier in two runs
    runs: int = Field(ge=2)                     # runs per CV
    min_jobs: int = Field(ge=1)                 # postings a role needed for a tier when it was measured
    # measured with the rules of matches and tiers and the step-2 prompt before this day
    rules_before: date | None = None


class Settings(BaseModel):
    steps: dict[str, StepModel]
    prices: dict[str, Price]
    estimates: Estimates = Field(default_factory=Estimates)
    stability: Stability | None = None  # None: not measured, and the pages show no note
    rules: Rules
    role_families: list[str]
    batch_threshold: int = 20
    batch_single_first: int = Field(default=1, ge=0)  # from batch_threshold: this many single calls first
    batch_groups: int = Field(default=3, ge=1)        # the rest as this many Message Batches
    relation_workers: int = Field(default=4, ge=1)    # step 3b: at most this many calls at the same time
    monthly_limit_usd: float = 5.0
    confirm_above_usd: float = 0.5
    max_posting_chars: int = 30000


WHOLE_BLOCKS = ("stability",)  # one measurement: a settings file that gives it replaces it, never mixes with it


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key not in WHOLE_BLOCKS:
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(override: Path | None = None) -> Settings:
    data = yaml.safe_load(resources.files("skillpose.core").joinpath("defaults.yaml").read_text(encoding="utf-8"))
    if override is not None:
        data = _merge(data, yaml.safe_load(Path(override).read_text(encoding="utf-8")) or {})
    return Settings.model_validate(data)
