# Evaluation guide

## Separate the evidence types

| Suite | Purpose | External requests |
| --- | --- | --- |
| `tests/` | Application behavior, ownership, SQL restrictions, clarification, and report integrity | Disabled |
| `evaluate_credit.py` | 120 fixed metric cases compared with independent Python/Decimal calculations | Disabled |
| `live_smoke.py --local-contract` | Seven open queries under two scopes, using scripted model responses and real MCP/SQL | Disabled |
| `verify_benchmark_gold.py` | Independent CSV calculations for the 40 frozen query references | Disabled |
| `evaluate_live.py` | Real model queries under retrieved or full authorized schema | Explicit opt-in |
| `evaluate_behaviors.py` | Real model interaction, security, and report scenarios | Explicit opt-in |

Passing a scripted or offline suite does not measure model accuracy. The benchmark is project-authored, not a blind holdout or public leaderboard.

## Inputs and local replay

`questions.json` contains the 40 question texts. `cases.json` freezes reference SQL, expected results, ordering, and precision rules. `behavior_rubric.json` defines checks for eight interaction, six security, and six report scenarios. Reference answers are used for evaluation, not passed into the application prompts.

Generate the default synthetic dataset first. From `backend`:

```sh
.venv-local/bin/python seed_demo_data.py
.venv-local/bin/python scripts/evaluate_credit.py
.venv-local/bin/python scripts/live_smoke.py --local-contract
PYTHONPATH=. .venv-local/bin/python scripts/verify_benchmark_gold.py
```

The public package excludes historical provider responses, token traces, session databases, and local report archives. Newly generated evaluation outputs are ignored by Git. Existing helpers for normalization, security evidence, and SQL counterexamples operate on a matching local run directory; they do not ship with completed historical runs.

## Optional live evaluation

Configure provider endpoints and credentials in the private `.env` file, set `CREDIT_DEMO_MODE=false`, and explicitly enable `CREDIT_EXTERNAL_MODEL_ENABLED=true`. The following commands send questions, schema metadata, context, and authorized synthetic results to the configured services and consume provider quota:

```sh
.venv-local/bin/python scripts/evaluate_live.py --allow-external-model --run rag_trial --variant rag --workers 2
.venv-local/bin/python scripts/evaluate_live.py --allow-external-model --run full_schema_trial --variant full_schema --workers 2
.venv-local/bin/python scripts/evaluate_behaviors.py --allow-external-model --run behavior_trial --workers 2
```

Keep distinct run names for independent trials. Resume only when the runner's code, input, and configuration hashes match. A complete comparison is 60 designed scenarios plus 40 repeated query scenarios under full schema: 100 task attempts, not 100 independent questions.

## Interpretation

SQL execution, numerical agreement, and accepted business semantics are separate outcomes. Display normalization may align equivalent IDs/labels and ordering, but must not change financial values or precision. Necessary clarification must be exercised, not assumed from the presence of an interrupt branch. A timeout is neither a successful refusal nor a correct report.

The retained aggregate baseline and its limitations are documented in [validation](../../../docs/validation.md). It was recorded on 2026-10-07 and was not rerun during publication preparation.
