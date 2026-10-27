# Step 2 model choice – Phase 1 measurement (2026-10-20)

Gold set: 15 invented postings (German and English), effort `low`, prompt caching on, single calls (no batch).
Full reports: `2026-10-phase1-haiku.md` (all steps) and `2026-10-phase1-sonnet.md` (step 2 only).

| metric (mean of 15) | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| role_family_ok | 1.00 | 1.00 |
| seniority_ok | 1.00 | 1.00 |
| must_recall | 0.98 | 1.00 |
| nice_recall | 1.00 | 1.00 |
| importance_accuracy | 1.00 | 1.00 |
| must_precision | 0.98 | 0.96 |
| cost per posting | 0.049 US cents | 0.99 US cents |
| cache read share | 97 % | 91 % |
| projected 1,000 postings (single calls) | ≈ 0.49 USD | ≈ 9.9 USD |
| projected 1,000 postings (batch, half price) | ≈ 0.25 USD | ≈ 4.9 USD |

Haiku's two deviations, checked one by one:
- `03_senior_sdet_vienna`: "testing REST APIs" mapped to `rest_api` instead of `api_testing` – a defensible reading.
- `15_senior_ml_engineer`: a second run extracted all six must requirements; the miss was run-to-run variance.

Sonnet adds a few extra must requirements (precision 0.96) at twenty times the price.

Step 1 (CV, Opus 5.5, effort medium): recall 1.00, no trap hits, evidence strength 1.00, all quotes verified;
9.5 US cents per CV, cache read share 63 % over three CVs.

Caveat: the gold set is small and its postings are clean; real postings are messier. The real test is the owner's
own posting base in phase 3.

**Decision (owner, 2026-10-20):** Sonnet 5.5 for step 2 (`defaults.yaml`); Haiku 5.5 stays the documented
low-cost override.

**Note (2026-10-22):** after this measurement the Sonnet 5.5 cache-read price in `defaults.yaml` was corrected from
0.20 to 0.10 USD per million tokens (see `2026-10-phase2-summary.md`). The table above shows the figures as booked at
the time. At the corrected price Sonnet 5.5 costs 0.88 US cents per posting (≈ 8.8 USD per 1,000 postings, ≈ 4.4 USD
as a batch; 1 : 18 against Haiku 5.5).
