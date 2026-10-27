# skillpose eval --repeat - 2026-10-24 18:08 UTC

Prompt set: private:3f3eb394

Step 1: claude-opus-5-5 (effort medium); step 2: claude-sonnet-5-5 (effort low); language: auto

Run-to-run variance: the same input analyzed several times, each time with a new call. Every number compares all pairs of runs of the same input (n runs give n × (n − 1) / 2 pairs). The first run of an input is its stored result of `eval --save` (base), if the model of the step made it.

## Runs

| input | runs | pairs | days (UTC) |
|---|---|---|---|
| CV maria_berger | 5 | 10 | 2026-10-21 (base), 2026-10-24 (4) |
| CV daniel_novak | 5 | 10 | 2026-10-21 (base), 2026-10-24 (4) |
| CV sabine_huber | 5 | 10 | 2026-10-21 (base), 2026-10-24 (4) |
| postings (30) | 2 | 1 | 2026-10-21 (base), 2026-10-24 (1) |

Calls without a result in this command: 0 (paid and in the ledger, but no run).

## The same CV again

The postings stay at their first run.

| metric | maria_berger | daniel_novak | sabine_huber | all |
|---|---|---|---|---|
| skill set (mean Jaccard) | 0.98 (mean of 10) | 1.00 (mean of 10) | 0.90 (mean of 10) | 0.96 (mean of 30) |
| level the same | 0.90 (225 of 251) | 0.97 (204 of 210) | 0.94 (149 of 159) | 0.93 (578 of 620) |
| level at most one step apart | 1.00 (251 of 251) | 1.00 (210 of 210) | 1.00 (159 of 159) | 1.00 (620 of 620) |
| strongest evidence the same | 0.90 (225 of 251) | 0.97 (204 of 210) | 1.00 (159 of 159) | 0.95 (588 of 620) |
| requirement status the same | 1.00 (1874 of 1880) | 1.00 (1880 of 1880) | 0.97 (1832 of 1880) | 0.99 (5586 of 5640) |
| posting tier the same | 1.00 (300 of 300) | 1.00 (300 of 300) | 0.99 (296 of 300) | 1.00 (896 of 900) |
| role tier the same | 1.00 (40 of 40) | 1.00 (40 of 40) | 1.00 (40 of 40) | 1.00 (120 of 120) |
| heaviest gap the same | 1.00 (40 of 40) | 1.00 (40 of 40) | 1.00 (40 of 40) | 1.00 (120 of 120) |

## The same postings again

Each CV stays at its first run.

| metric | maria_berger | daniel_novak | sabine_huber | all |
|---|---|---|---|---|
| requirement status the same | 1.00 (187 of 187) | 0.99 (185 of 187) | 0.99 (186 of 187) | 0.99 (558 of 561) |
| posting tier the same | 1.00 (30 of 30) | 1.00 (30 of 30) | 1.00 (30 of 30) | 1.00 (90 of 90) |
| role tier the same | 1.00 (4 of 4) | 1.00 (4 of 4) | 1.00 (4 of 4) | 1.00 (12 of 12) |
| heaviest gap the same | 1.00 (4 of 4) | 1.00 (4 of 4) | 1.00 (4 of 4) | 1.00 (12 of 12) |

The postings alone, without a CV:

| metric | all |
|---|---|
| requirement set (mean Jaccard) | 1.00 (mean of 30) |
| role family of the posting the same | 1.00 (30 of 30) |

## How the numbers are computed

A cell shows the value, then the comparisons it counts: "0.88 (380 of 432)" or "0.95 (mean of 10)". The column all counts the comparisons of every CV together.

### The same CV again

- **skill set (mean Jaccard)**: J(A, B) = |A ∩ B| / |A ∪ B| for the skill ids A and B of the two runs (1 for two empty sets); the mean of J over the pairs.
- **level the same**: of the skills found in both runs, the share with the same level: the sum of the skills with the same level over the sum of |A ∩ B|, over all pairs.
- **level at most one step apart**: as above, but a level one step away counts too (basic < working < strong < expert).
- **strongest evidence the same**: of the skills found in both runs, the share whose strongest evidence (outcome > activity > claim, or none) is the same.
- **requirement status the same**: every requirement of every posting (in its first run) matched against both CV runs: the share with the same status (met, partial, gap).
- **posting tier the same**: every posting matched against both CV runs: the share with the same tier.
- **role tier the same**: every role family with a tier in at least one of the two runs: the share with the same tier (no tier counts as a value of its own).
- **heaviest gap the same**: the same role families: the share whose heaviest gap (the first gap of the market view) is the same skill (no gap counts as a value of its own).

### The same postings again

- **requirement set (mean Jaccard)**: J of the required skill ids of two runs of one posting; the mean over the pairs and the postings that both runs have. It needs no CV.
- **role family of the posting the same**: of the postings that both runs have, the share with the same role family. It needs no CV.
- **requirement status the same**: the requirements that both runs of a posting have (the same skill id; a skill asked twice counts in its order), matched against the first run of the CV: the share with the same status.
- **posting tier the same**: every posting that both runs have, matched against the first run of the CV: the share with the same tier.
- **role tier the same**: as for the CV runs, over the postings that both runs have.
- **heaviest gap the same**: as for the CV runs, over the postings that both runs have.

## Summary (JSON lines)

One line per metric, over all CVs.

```jsonl
{"source": "cv", "metric": "skill_jaccard", "value": 0.959, "pairs": 30, "runs": {"maria_berger": 5, "daniel_novak": 5, "sabine_huber": 5}, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "cv", "metric": "level_same", "value": 0.9323, "same": 578, "total": 620, "runs": {"maria_berger": 5, "daniel_novak": 5, "sabine_huber": 5}, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "cv", "metric": "level_within_one", "value": 1.0, "same": 620, "total": 620, "runs": {"maria_berger": 5, "daniel_novak": 5, "sabine_huber": 5}, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "cv", "metric": "evidence_same", "value": 0.9484, "same": 588, "total": 620, "runs": {"maria_berger": 5, "daniel_novak": 5, "sabine_huber": 5}, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "cv", "metric": "status_same", "value": 0.9904, "same": 5586, "total": 5640, "runs": {"maria_berger": 5, "daniel_novak": 5, "sabine_huber": 5}, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "cv", "metric": "posting_tier_same", "value": 0.9956, "same": 896, "total": 900, "runs": {"maria_berger": 5, "daniel_novak": 5, "sabine_huber": 5}, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "cv", "metric": "role_tier_same", "value": 1.0, "same": 120, "total": 120, "runs": {"maria_berger": 5, "daniel_novak": 5, "sabine_huber": 5}, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "cv", "metric": "main_gap_same", "value": 1.0, "same": 120, "total": 120, "runs": {"maria_berger": 5, "daniel_novak": 5, "sabine_huber": 5}, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "postings", "metric": "requirement_jaccard", "value": 0.9952, "pairs": 30, "runs": 2, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "postings", "metric": "role_family_same", "value": 1.0, "same": 30, "total": 30, "runs": 2, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "postings", "metric": "status_same", "value": 0.9947, "same": 558, "total": 561, "runs": 2, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "postings", "metric": "posting_tier_same", "value": 1.0, "same": 90, "total": 90, "runs": 2, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "postings", "metric": "role_tier_same", "value": 1.0, "same": 12, "total": 12, "runs": 2, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
{"source": "postings", "metric": "main_gap_same", "value": 1.0, "same": 12, "total": 12, "runs": 2, "first_run": "2026-10-21", "last_run": "2026-10-24", "prompt_set": "private:3f3eb394", "created": "2026-10-24 18:08 UTC"}
```
