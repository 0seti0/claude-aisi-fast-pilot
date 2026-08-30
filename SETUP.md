# Install and verify fast-pilot-v11

Do not make real model calls until every command in the verification block passes.

## 1. Install

Copy the bundle files into the repository root, replacing the empty placeholders, then run:

```bash
python -m pip install -r requirements.txt
```

## 2. Verify without API calls

```bash
python -m pytest -q
python run_pilot.py --validate-only
python run_pilot.py --dry-run
python score_pilot.py --raw-log runs/dry_run.jsonl --analysis-dir analysis/dry_run
```

Expected results:

- 23 tests pass.
- The validation gate passes.
- The dry run writes 12 episode records plus one run header.
- `analysis/dry_run/cross_model.csv` contains six data rows.

The dry run uses a local deterministic fake provider and needs no API keys.

## 3. Before a real run

Fill the exact dated model IDs and reasoning settings in `config.yaml`. Never use aliases.
Set `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in the environment, not in tracked files.
Commit the frozen artifacts and confirm the worktree state. Use a fresh `runs/raw.jsonl`;
never copy the dry-run log to that path.

The real command is deliberately deferred until configuration review:

```bash
python run_pilot.py
```
