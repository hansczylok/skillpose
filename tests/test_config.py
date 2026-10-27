import pytest
from pydantic import ValidationError

from skillpose.core.config import load_settings


def test_defaults_load():
    s = load_settings()
    assert s.steps["cv"].model == "claude-opus-5-5" and s.steps["cv"].effort == "medium"
    assert s.steps["job"].model == "claude-sonnet-5-5" and s.steps["job"].effort == "low"
    assert s.steps["relation"].model == "claude-haiku-5-5"
    assert s.monthly_limit_usd == 5.0 and s.confirm_above_usd == 0.5 and s.batch_threshold == 20
    assert s.rules.stale_years == 8 and s.rules.tier2_max_weeks == 26
    assert "other" in s.role_families and "test_automation" in s.role_families
    assert "claude-haiku-4-5" not in s.prices


def test_override_merges(tmp_path):
    p = tmp_path / "o.yaml"
    p.write_text("rules:\n  stale_years: 10\nsteps:\n  job:\n    model: claude-haiku-5-5\n")
    s = load_settings(p)
    assert s.rules.stale_years == 10 and s.rules.tier1_coverage == 0.8
    assert s.steps["job"].model == "claude-haiku-5-5" and s.steps["job"].effort == "low"


def test_fallback_models_have_prices():
    p = load_settings().prices
    assert p["claude-opus-5"].input == 5.0 and p["claude-opus-4-8"].cache_read == 0.50
    sonnet_5, sonnet_5_5 = p["claude-sonnet-5"], p["claude-sonnet-5-5"]
    assert (sonnet_5.input, sonnet_5.output, sonnet_5.cache_write) == (
        sonnet_5_5.input, sonnet_5_5.output, sonnet_5_5.cache_write)  # same prices, but 0.1 x input for cache reads


def test_cache_reads_cost_the_multiple_of_the_input_price_that_the_pricing_page_states():
    # pricing page, October 2026: 0.05 x input on Opus 5.5 and Sonnet 5.5, 0.1 x input on every other model here
    p = load_settings().prices
    for model, factor in [("claude-opus-5-5", 0.05), ("claude-sonnet-5-5", 0.05), ("claude-haiku-5-5", 0.1),
                          ("claude-sonnet-5", 0.1), ("claude-opus-5", 0.1), ("claude-opus-4-8", 0.1)]:
        assert p[model].cache_read == pytest.approx(factor * p[model].input), model
        assert p[model].cache_write == pytest.approx(1.25 * p[model].input), model


def test_max_tokens_of_each_step():
    # the CV step stopped at 16,000 output tokens; the calls are streamed now, so it has more room
    steps = load_settings().steps
    assert {name: step.max_tokens for name, step in steps.items()} == {
        "cv": 32000, "job": 8000, "relation": 2000, "path": 16000}


@pytest.mark.parametrize("value, ok", [(64000, True), (64001, False), (0, False)])
def test_max_tokens_up_to_64000_is_accepted(tmp_path, value, ok):
    p = tmp_path / "o.yaml"
    p.write_text(f"steps:\n  cv:\n    max_tokens: {value}\n")
    if ok:
        assert load_settings(p).steps["cv"].max_tokens == value
    else:
        with pytest.raises(ValidationError, match="max_tokens"):
            load_settings(p)


def test_the_estimate_of_the_cv_step_comes_from_measured_runs():
    e = load_settings().estimates
    assert (e.cv_chars_per_token, e.cv_out_per_in, e.cv_out_floor, e.cv_fixed_tokens) == (2.0, 4.0, 6000, 12000)


@pytest.mark.parametrize("values, ok", [
    ("cv_out_floor: 0", True), ("cv_out_floor: -1", False),
    ("cv_out_per_in: 0.5", True), ("cv_out_per_in: 0", False),
    ("cv_chars_per_token: 3", True), ("cv_chars_per_token: 0", False),
    ("cv_fixed_tokens: 0", True), ("cv_fixed_tokens: -5", False),
])
def test_estimate_settings_are_validated(tmp_path, values, ok):
    p = tmp_path / "o.yaml"
    p.write_text(f"estimates:\n  {values}\n")
    if ok:
        load_settings(p)
    else:
        with pytest.raises(ValidationError, match="greater than"):
            load_settings(p)


def test_from_the_batch_threshold_one_posting_goes_at_once_and_the_rest_in_three_batches():
    s = load_settings()
    assert s.batch_single_first == 1 and s.batch_groups == 3


@pytest.mark.parametrize("values, ok", [
    ("batch_single_first: 0\n", True), ("batch_single_first: -1\n", False),
    ("batch_groups: 1\n", True), ("batch_groups: 0\n", False),
])
def test_batch_split_settings_are_validated(tmp_path, values, ok):
    p = tmp_path / "o.yaml"
    p.write_text(values)
    if ok:
        load_settings(p)
    else:
        with pytest.raises(ValidationError, match="greater than or equal"):
            load_settings(p)


def test_relation_checks_run_four_at_once_by_default():
    assert load_settings().relation_workers == 4


@pytest.mark.parametrize("values, ok", [("relation_workers: 1\n", True), ("relation_workers: 0\n", False)])
def test_relation_workers_are_validated(tmp_path, values, ok):
    p = tmp_path / "o.yaml"
    p.write_text(values)
    if ok:
        assert load_settings(p).relation_workers == 1  # 1: one check after the other, as before
    else:
        with pytest.raises(ValidationError, match="greater than or equal"):
            load_settings(p)
