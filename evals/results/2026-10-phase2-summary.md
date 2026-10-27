# Phase 2 measurement – summary (2026-10-22)

Gold set: 3 invented CVs, 30 invented postings (German and English). Single calls, no batch, prompt caching on,
`--lang auto` (each CV in its own language, postings in English), rules of `defaults.yaml`, year 2026. All cost figures
come from the cost ledger.

**Ledger rebooked (2026-10-22).** The Sonnet 5.5 cache-read price in `defaults.yaml` was 0.20 USD per million tokens;
the pricing page says 0.05 x input, i.e. 0.10. After the correction the 31 Sonnet 5.5 rows of the gold-set ledger were
recomputed from their stored tokens (29 changed; step 2 0.3102 to 0.2765 USD, whole ledger 1.2624 to 1.2286 USD); all
other rows are untouched. The cost figures below use the corrected price. The report files `2026-10-phase1-*.md`,
`2026-10-phase2-all.md` and `2026-10-phase2-haiku.md` are the programs' output of the time and show the old price.

Two measurements of steps 3 and 4:
- **Before** (first measurement, 2026-10-21): `skillpose eval --step job --job-model claude-haiku-5-5`, `skillpose eval
  --step all --save`, then `skillpose demo build` (paths only). Full reports: `2026-10-phase2-haiku.md` and
  `2026-10-phase2-all.md`. Result: role_tier_accuracy 7/12, because Haiku 5.5 rated many step 3b relations too
  generously.
- **After** (second measurement, 2026-10-22): owner decision to sharpen the relation prompt and recompute only
  relations, matches and paths. The stored relations, matches and paths were deleted, the stored CV profiles and job
  profiles (steps 1 and 2) were kept, and `skillpose demo build` ran again.

The before and after numbers below are recomputed from the stored matches with `evaluation.match_metrics` (no new
`eval` run). The stored matches of the second run equal a recomputation from the stored relations. The report files are
from the first run.

## Step 2: Haiku 5.5 against Sonnet 5.5 (30 postings, effort low; not re-run)

| metric (mean of 30) | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| role_family_ok | 1.00 | 1.00 |
| seniority_ok | 1.00 | 1.00 |
| must_recall | 0.99 | 1.00 |
| nice_recall | 1.00 | 1.00 |
| importance_accuracy | 1.00 | 1.00 |
| must_precision | 0.99 | 0.97 |
| cost per posting | 0.055 US cents | 0.80 US cents |
| cache read share | 94 % | 94 % |
| projected 1,000 postings (single calls) | ≈ 0.55 USD | ≈ 8.05 USD |
| projected 1,000 postings (batch, half price) | ≈ 0.28 USD | ≈ 4.02 USD |

- Haiku: `03_senior_sdet_vienna` misses one must requirement and adds one (0.80/0.80), `16_senior_test_automation_steyr`
  adds one must requirement (precision 0.83). Its job profiles were not saved, so the cause is not checked; the
  pattern of 03 is the one of phase 1 (API testing read as `rest_api`).
- Sonnet adds must requirements in four postings: `test_automation` in 02 ("Automatisierte Tests ... mit Python und
  pytest"), `test_management` and `mentoring` in 03, `banking` in 12 (taken from the company description) and
  `code_review` in 13. Most are defensible readings; `banking` is not.
- Phase 1 (15 postings): Haiku 0.049, Sonnet 0.88 US cents per posting (1 : 18, recomputed at the corrected price);
  now 1 : 15.

## Step 1: CVs (Opus 5.5, effort medium, 3 CVs; not re-run)

recall 1.00, trap_hits 0.00, strength_accuracy 1.00, verified_share 1.00 (as in phase 1).
Cost 0.374 USD, **12.5 US cents per CV** (phase 1: 9.5). Cache read share 32 % (phase 1: 63 %): the fixed part
contains the output language, and `--lang auto` analyzed the CVs in German, English and German, so the prefix was
written twice and read once. About two thirds of the cost is output (12,130 tokens).

## Steps 3 and 4: match status and role tiers, before and after

| person | match_status before | match_status after | role_tier before | role_tier after |
|---|---|---|---|---|
| maria_berger | 0.83 (10/12) | 0.92 (11/12) | 0.50 (2/4) | 1.00 (4/4) |
| daniel_novak | 0.82 (9/11) | 1.00 (11/11) | 1.00 (4/4) | 1.00 (4/4) |
| sabine_huber | 0.82 (9/11) | 0.82 (9/11) | 0.25 (1/4) | 1.00 (4/4) |
| all | 0.82 (28/34) | 0.91 (31/34) | 0.58 (7/12) | 1.00 (12/12) |

## Step 3b relation prompt: before and after

**What changed.** After the first measurement the relation prompt of step 3b was made stricter. The prompts used for
all measurements and for the demo are the author's private prompts; the repository ships simplified example prompts
(see the README, section "Prompts"). Also, ids that start with `bachelors_` now meet the generic "any university
degree" requirement.

| | before | after |
|---|---|---|
| relation pairs asked | 2,385 | 2,383 |
| close | 122 | 26 |
| distant | 260 | 125 |
| same | 5 | 1 |
| none | 1,998 | 2,231 |
| pairs that differ from the reference table | 355 (15 %) | 104 (4 %) |
| – related, reference says none | 323 | 91 |
| – reference pair answered weaker | 5 | 12 |
| – reference pair answered stronger | 27 | 1 |
| reference pairs answered as in the table (of 66 asked) | 34 | 53 |
| postings with another tier than the reference (of 90) | 30 | 4 |
| requirement statuses other than the reference (of 564) | 180 | 27 |

The reference is the relation table in the header of `evals/goldset/expected.yaml`, applied by the program's rules to
the stored profiles and job profiles (pairs not listed: none). It gives all 12 role tiers and all 34 statuses. Five of
its 71 pairs are never asked, because Opus extracted the target skill for that person. The two pairs asked only before
(`university_degree` with `bachelors_degree_in_economics` and `dax`) fell away with the degree rule.
The new prompt mostly errs on the strict side: 12 reference pairs answered weaker (7 close pairs as distant, 5 as
none), 1 stronger.

**Cost of the second run** (ledger rows after the first 273; the first run is in the cost table below):

| step | model | calls | cost | cache read share |
|---|---|---|---|---|
| 3b relations | Haiku 5.5 | 202 | 0.0622 USD (first run: 0.0508) | 99 % |
| 5 paths | Opus 5.5 | 6 | 0.1525 USD (first run: 0.1604) | 45 % |
| **total** | | 208 | **0.2147 USD** | |

Relations cost 22 % more: the prompt is longer and the answers have more output (71,285 tokens against 51,873).

**Independence limit.** The stricter prompt was written after the failures of the first run on this same gold set.
It names no pair of the table, and a test guards that where the private prompts are present. So 12/12 is a
re-measurement on the data the prompt was tuned on, not a held-out test. A held-out check needs new postings and CVs,
or a reference table by a second author.

**Time limit.** The expected statuses hold for the stored profiles of this run. One pair depends on the years that
Claude states for an open-ended period: sabine_huber 19 / `rpa` (3 years required). Her RPA work runs "2024 bis
heute", and the stored `years` of 1.0 is Claude's answer for it. The code (`core/extract_cv.py`) computes years from the
periods only when Claude gives none (in 2026 the periods would give 2.0), and Claude is not given the current date. A
later run may state more years and turn the pair into met instead of the expected partial.

## Role tiers before: deviations, cell by cell (first measurement, corrected)

With the stored profiles and the reference table the rules give all 12 expected tiers, so every deviation came from
step 3b answers. Of 2,385 pairs, 355 differed from the reference (see above). A "close" answer turns a gap into a
partial (4 weeks), a "distant" answer turns a 12-week gap into a 4-week gap. No calendar or staleness effect.

1. **maria_berger, data_analysis_bi: 3 expected, 2 actual.** Postings 06, 08, 23, 24 moved to tier 2. `power_bi` and
   `tableau <- sql` and `data_modeling <- sql` close (reference: none, none, distant); `excel`, `statistics` and
   `data_visualization <- sql` distant (none). Example 06: 24 instead of 40 weeks.
2. **maria_berger, data_engineering: 3, 2.** Postings 10, 26, 27 moved to tier 2. `azure_data_factory`, `databricks`,
   `etl` and `power_bi <- sql` close (reference: none, distant, distant, none); `spark` and `kafka <- java` close
   (none); distant answers for `airflow` (26) and `aws` (27) (none). In 27 the close `etl <- sql` turned the years gap
   (5 years) into a partial. Weeks: 10 16 instead of 32, 26 24 instead of 40, 27 20 instead of a years gap.
3. **sabine_huber, test_automation: 3, 2.** Postings 01, 16, 18 moved to tier 2. `test_automation <- power_automate`,
   `<- python` and `<- rpa` were all close (reference: none, none, distant), so the missing test automation (3 and 5
   years required) became a partial instead of a years gap; `selenium <- power_automate` close (reference: distant).
   In 18 also `api_testing <- python` close and distant answers for `jira` and `agile_scrum` (none): 20 instead of 44
   weeks. Posting 17 moved the other way (`typescript <- python` none, reference close): tier 3.
4. **sabine_huber, data_analysis_bi: 3, 2.** Postings 07 and 22 moved to tier 2. In 07 `sql_server` and `ssis` got
   distant answers (reference: none) and `data_warehousing <- python` close (none); resetting `sql_server` and `ssis` to
   the reference gives tier 3. In 22 the answers for `data_modeling` and `data_warehousing` (close, distant) carry it;
   resetting either to the reference gives tier 3. The extra skill `data_analysis` that Opus extracted is not the
   cause: without any of its answers 07 and 22 stay tier 2.
5. **sabine_huber, data_engineering: 3, 2** (2 of 5 tier 2 = 0.40 = `role_tier_share`). Postings 10 and 26 moved to
   tier 2: `databricks`, `spark` and `airflow <- python` close (reference: distant), `etl <- python` close (none),
   `sql`, `data_modeling` and `power_bi <- data_analysis` close (none), `azure_data_factory` distant (none). Weeks: 10
   24 instead of 32, 26 24 instead of 40.

Not caused by step 3b: posting 10 ("Gute Kenntnisse in Spark und Python") got `min_level` strong from Sonnet, a stronger
level than expected (25 as well). sabine_huber: Opus gives 1 year for Power Automate and RPA ("2024 bis
heute", expected 2); 19 requires 3 years of `rpa` and 20 requires 3 years of `uipath`, the shortfall of 2.0 is just
within `years_gap_margin`, so the status stays partial.

**Six wrong match statuses before:**

| person | posting | skill | expected | actual | cause |
|---|---|---|---|---|---|
| maria_berger | 03 | kubernetes | gap | partial | Opus extracted `ci_cd`; `kubernetes <- ci_cd` close |
| maria_berger | 22 | power_bi | gap | partial | `power_bi <- sql` close (reference: none) |
| daniel_novak | 08 | university_degree | met | gap | id `bachelors_degree_in_economics` was not accepted by the generic-degree rule (fixed before the second measurement, 2026-10-22) |
| daniel_novak | 10 | azure_data_factory | gap | partial | `azure_data_factory <- sql` and `<- sql_server` close (also `<- data_warehousing`; dropping that alone keeps it partial) |
| sabine_huber | 01 | selenium | gap | partial | `selenium <- power_automate` close (reference: distant), also `python` and `rpa` close |
| sabine_huber | 04 | uipath | partial | met | `uipath <- rpa` same (reference: none) |

## Deviations after (second measurement)

All 12 role tiers match. Three of 34 statuses differ, all from relation answers:

| person | posting | skill | expected | actual | cause |
|---|---|---|---|---|---|
| maria_berger | 02 | playwright | partial | gap | `playwright <- selenium` distant (reference: close); with close it is partial |
| sabine_huber | 03 | kubernetes | partial | gap | `kubernetes <- docker` distant (reference: close); a nice requirement, no tier effect |
| sabine_huber | 04 | uipath | partial | met | `uipath <- rpa` same (reference: none) and `uipath <- power_automate` distant (reference: close); only both fixed together give partial |

Four of 90 postings have another tier than the reference:
- maria_berger 05 (rpa_process_automation): `sharepoint <- confluence` none (reference: close), so 28 instead of 20 weeks,
  tier 3 instead of 2. The family now has 2 of 5 postings in tier 2, exactly `role_tier_share` 0.40 (reference: 3 of
  5). One more tier-3 posting would make the family tier 3.
- maria_berger 10 and daniel_novak 10 (data_engineering): extra distant answers for `azure_data_factory` (both) and
  `spark` (maria_berger) make tier 2 instead of 3. No effect on the family tier.
- sabine_huber 30 (data_science_ml, 2 postings, no family tier).

## Cost per complete analysis

One CV, 30 postings, relations and 2 paths; per-person shares are the mean of the three persons. Relations and paths
are from the second run.

| step | model | calls (gold set) | cost (gold set) | per analysis | cache read share |
|---|---|---|---|---|---|
| 1 CV | Opus 5.5 | 3 | 0.3741 USD | 0.125 USD | 32 % |
| 2 postings | Sonnet 5.5 | 30 | 0.2414 USD | 0.241 USD | 94 % |
| 3b relations | Haiku 5.5 | 202 | 0.0622 USD | 0.021 USD | 99 % |
| 5 paths | Opus 5.5 | 6 | 0.1525 USD | 0.051 USD | 45 % |
| **total** | | | | **≈ 0.44 USD** | |

- With step 2 as a batch (the default from 20 open postings): ≈ 0.32 USD. With Haiku for step 2: ≈ 0.21 USD.
- A path costs 2.5 US cents (first run 2.7); the second path of a person reads the cached profile summary of the first.
- Paid in this task: 1.2286 USD over 488 calls in the gold-set ledger (first run 0.8433 including the Haiku comparison
  of 0.0165, second run 0.2147, third run 0.0350: posting 05 read again after its invented employer was renamed, one
  Sonnet call, no relation or path calls, metrics unchanged; fourth run 0.1355: 6 paths in the second language after
  the owner's acceptance finding, so that the demo has every path in German and English, no other calls) plus
  ≈ 0.07 USD for the three live tests (estimate, separate ledgers): ≈ 1.30 USD, under the approval of 1.50 USD. The
  ledger total was 1.2624 USD before the rebooking above; the 0.07 USD estimate was not recomputed.

## Demo data

`src/skillpose/demo_data/`: three bundles (30 postings, 30 matches and 4 paths each: 2 role families, each path in
German and English) and `costs.json` (the whole ledger of the build store: 488 calls, 1.229 USD, four runs).
- Every path is in German and English (since the owner's acceptance test of 2026-10-22), so the paths page switches
  with the interface language. Profiles, quotes and posting texts stay in their original language.
- No `http://`, `https://` or `www.` in any path, and none anywhere in the bundles; no `@`.
- Every person has four role families with a tier: maria_berger 1/2/3/3, daniel_novak 1/2/2/3, sabine_huber 2/3/3/3.
  These are the expected tiers.
- Twelve paths with 4 or 5 steps each (in the language of the CV: maria 4 and 5, daniel 5 and 5, sabine 4 and 4; in
  the other language: maria 5 and 4, daniel 4 and 4, sabine 5 and 5); `total_weeks` equals the sum of the steps; every
  step closes only skills from the top gaps of its role.
- The path targets follow the tiers, so they are the targets of the expected tiers: maria_berger rpa_process_automation
  and data_analysis_bi, daniel_novak data_engineering and rpa_process_automation, sabine_huber rpa_process_automation
  and data_analysis_bi.

## Possible new thresholds (proposals for the owner's decision; nothing changed)

Proposal: change none. With the current values the second run gives 12/12 role tiers (and 31/34 statuses). Tried on the
stored relations of the second run, one value at a time:

| parameter (current) | tried | correct tiers | what changes |
|---|---|---|---|
| `role_tier_share` (0.4) | 0.30 / 0.33 | 10 | sabine test_automation and data_analysis_bi become 2 |
| | 0.34 to 0.40 | 12 | – |
| | 0.41 to 0.60 | 11 | maria rpa_process_automation becomes 3 |
| `tier2_max_weeks` (26) | 20 to 23 | 11 | daniel data_engineering becomes 3 |
| | 24 to 27 | 12 | – |
| | 28 / 30 | 11 | maria data_engineering becomes 2 |
| | 32 | 10 | and sabine data_engineering becomes 2 |
| `tier1_coverage` (0.8) | 0.70 to 0.90 | 12 | – |
| `stale_years` (8) | 5 to 12 | 12 | – |
| `years_gap_margin` (2.0) | 1.0 | 12 | 30/34 statuses (one more wrong) |
| | 3.0 | 11 | sabine test_automation becomes 2 |

The current values sit inside the window where all 12 hold, `role_tier_share` at its upper edge (maria rpa at exactly
0.40). Moving them would fit 12 cells of an invented gold set. Another lever, also only a proposal:
- A held-out check of step 3b (see the independence limit), for example a second author's relation table.

Caveat: the gold set is small and invented; the expected relations are one author's judgement, not ground truth.
