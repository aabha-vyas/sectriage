# Experiment: prompt v2 ("report each finding as soon as it is verified")

Decision (2026-10-27): **prompt v1 stays the default. v2 is not merged into `main`.** The v2 prompt
lives only on git tag `prompt-v2-experiment` (tip of branch `feat/prompt-v2`). The v1 baselines in
`evals/baselines/` are unchanged, and no v2 baselines were created.

## Question

The v1 step-budget sweep on `ledger_api` showed that the v1 agent reported its findings in a late
burst (median first report at step 13 to 14 in the 30-step runs), so at small budgets a run could be
cut off before the burst. v2 adds one instruction to `SYSTEM_PROMPT` (item 4 in "How to work") and
bumps `PROMPT_VERSION` to `"v2"`:

> Report each finding as soon as you have verified it, before you read anything else. Do not hold
> verified findings back to report them together at the end: your step budget is limited, and a
> finding that is not reported when the run ends is lost. Each `report_finding` call uses one step.

`WRAP_UP_MESSAGE`, `NUDGE_MESSAGE`, the loop and the tools are unchanged.

## Setup

| setting | value |
|---|---|
| model | `claude-haiku-5-5`, `price_verified_on` 2026-10-24 |
| runs | N=5 per cell, budgets `--max-steps` 6, 10, 15, 30, `--max-cost-usd` 0.05 per run |
| targets | `ledger_api` (5 planted bugs; **tuned**: v2 was written from its v1 sweep) and `notes_portal` (5 planted bugs; **untuned** hold-out) |
| arms | `ledger_api` v1 at `abdee28`, v2 at `e9bd3e9`; `notes_portal` v1 at `869a68f` (separate worktree), v2 at `cda8804` |
| tools / sandbox | `read_file`, `search_code` / none (host read-only), identical in both arms |

Within each v1/v2 pair the files agree on model, target, tools, sandbox, `max_steps`, `max_cost_usd`
and `price_verified_on`. They differ in `prompt_version` and `git_sha` (and `tuned` for `ledger_api`,
which is not a gate field).

This is a side-by-side report, **not a gate result**: v1 and v2 summaries are not comparable through
`evals.baseline` (`prompt_version` is a setup field).

## Pre-registered success criteria

Written before the v2 numbers existed. The original text had four bullets; the second one holds two
thresholds and is split here, giving five.

1. The median first-report step moves earlier at every budget.
2. Mean recall is at least v1 at 6, 10 and 15 steps.
3. Mean recall is at least v1 + 0.2 (one extra bug on average) at 6 and 10 steps.
4. At 30 steps: recall >= 0.85 (v1 1.00 minus the 0.15 gate tolerance), precision >= 0.90, mean cost
   within +25% of v1, and 5/5 clean runs.
5. fp-free runs do not fall below 5/5 at any budget.

## Results

Cells are v1 / v2. Clean runs are 5/5 in every cell for both prompts. Precision is the mean over runs
that reported at least one finding.

### ledger_api (tuned)

| steps | recall | precision | fp / run | fp-free | mean cost usd | mean steps | median 1st report |
|---|---|---|---|---|---|---|---|
| 6 | 0.20 / 0.20 | 1.00 / 1.00 | 0.00 / 0.00 | 5/5 / 5/5 | 0.0014 / 0.0011 | 6.0 / 6.0 | 5 / 5 |
| 10 | 0.36 / 0.36 | 1.00 / 1.00 | 0.00 / 0.00 | 5/5 / 5/5 | 0.0021 / 0.0018 | 10.0 / 9.6 | 9 / 6 |
| 15 | 0.44 / 0.72 | 1.00 / 0.93 | 0.00 / 0.20 | 5/5 / 4/5 | 0.0033 / 0.0032 | 15.0 / 14.8 | 14 / 5 |
| 30 | 1.00 / 1.00 | 1.00 / 1.00 | 0.00 / 0.00 | 5/5 / 5/5 | 0.0045 / 0.0042 | 18.4 / 18.8 | 14 / 5 |

### notes_portal (untuned hold-out)

| steps | recall | precision | fp / run | fp-free | mean cost usd | mean steps | median 1st report |
|---|---|---|---|---|---|---|---|
| 6 | 0.28 / 0.24 | 1.00 / 1.00 | 0.00 / 0.00 | 5/5 / 5/5 | 0.0015 / 0.0011 | 6.0 / 5.6 | 5 / 5 |
| 10 | 0.36 / 0.36 | 1.00 / 1.00 | 0.00 / 0.00 | 5/5 / 5/5 | 0.0023 / 0.0020 | 10.0 / 10.0 | 9 / 9 |
| 15 | 0.40 / 0.76 | 1.00 / 1.00 | 0.00 / 0.00 | 5/5 / 5/5 | 0.0034 / 0.0036 | 15.0 / 14.6 | 14 / 7 |
| 30 | 0.92 / 0.84 | 1.00 / 1.00 | 0.00 / 0.00 | 5/5 / 5/5 | 0.0044 / 0.0040 | 17.8 / 16.6 | 13 / 9 |

Other counts: two `notes_portal` v2 runs at 6 steps reported nothing (zero findings, stopped at step
5). Runs that finished with steps unused: `notes_portal` v2 9 of 20, v1 5 of 20; `ledger_api` v2 8 of
20, v1 5 of 20. The v1 runs are all at 30 steps.

## Verdicts

| # | criterion | ledger_api | notes_portal |
|---|---|---|---|
| 1 | median first report earlier at every budget | not met (equal at 6: 5 vs 5) | not met (equal at 6 and 10: 5 vs 5, 9 vs 9) |
| 2 | recall >= v1 at 6, 10, 15 | met | not met (6 steps: 0.24 vs 0.28) |
| 3 | recall >= v1 + 0.2 at 6, 10 | not met (+0.00, +0.00) | not met (-0.04, +0.00) |
| 4 | at 30: recall, precision, cost, clean | met (1.00, 1.00, cost ratio 0.95, 5/5) | not met (recall 0.84 < 0.85; precision 1.00, cost ratio 0.89, 5/5 hold) |
| 5 | fp-free 5/5 at every budget | not met (4/5 at 15 steps) | met |
| | **met** | **2 of 5** | **1 of 5** |

Cost ratios are v2 mean cost over v1 mean cost at 30 steps, from the unrounded means in the result
files. The `ledger_api` false positive at 15 steps is one run (`ledger_api-49c36748`): 2 TP, 1 FP, 3
FN. It is the only false positive in all 40 v2 runs.

## What the data shows

- Both targets show the same shape: recall rises at 15 steps (0.44 to 0.72, 0.40 to 0.76), is
  unchanged at 10, and is within 0.04 at 6. The median first report moves earlier at 15 and 30 steps
  on both targets.
- At 30 steps `ledger_api` scores 1.00 with both prompts. On `notes_portal`, v1 found 23 of 25 bugs
  across five runs and v2 found 21 of 25, a gap of two bugs in total; N=5 cannot separate that from
  noise, but criterion 4 is missed by 0.01.
- v1 reports its first finding at a median of step 13 to 14 at 30 steps, while its 30-step runs end
  after 17.8 to 18.4 steps on average, and all ten finished with steps unused. The wrap-up note fires
  with 2 steps left, so none of those runs saw it: the late first report is not caused by the note.
- N=5 per cell. `ledger_api` is tuned, so its numbers are not a held-out test.

## Reproducing

- Code: `git checkout prompt-v2-experiment` (the v2 prompt, the fingerprint row and the v2-text test).
  `git diff e9bd3e9 cda8804 -- src/agent` is empty, so both v2 arms ran the same agent code.
- The v1 arms need `main` (`PROMPT_VERSION = "v1"`).
- Command shape of every arm (live, needs `.env`; never export the key):

  ```
  uv run --env-file .env python -m evals.run --target <t> --repeat 5 \
    --max-steps <N> --max-cost-usd 0.05 --budget-usd <0.50|1.00> \
    --save evals/results/<t>-<v1|v2>-steps<NN>.json
  ```

  `--budget-usd` was 0.50 for `ledger_api` and 1.00 for `notes_portal`. `--save` overwrites silently,
  so check that the path does not exist first.
- The `notes_portal` v1 arm ran in a separate worktree detached at `869a68f`, with its own
  `logs/evals`.

## Files

- `evals/results/ledger_api-steps*.json` (v1), `ledger_api-v2-steps*.json`,
  `notes_portal-v1-steps*.json`, `notes_portal-v2-steps*.json`: the 16 sweep summaries.
- `docs/experiments/prompt-v2/runs.json`: one row per run (80 rows) with the scores from those files
  plus `first_report_step`, the step of the first `report_finding` event in the run's events log.
  The raw events logs are gitignored (`logs/evals/`), so this file is the only record of that number.
  A run with no such event has `null`.
- `docs/experiments/prompt-v2/compare.py`: `python -I docs/experiments/prompt-v2/compare.py` prints
  the tables above and the criteria verdicts from `runs.json` alone. Its means match the aggregates
  stored in the 16 result files.

## Not done

- No v2 baselines (`evals/baselines/v2/`). The follow-up was dropped with the decision to keep v1.
- v3 (a different wrap-up trigger) was only a contingency in the plan and was not run.
