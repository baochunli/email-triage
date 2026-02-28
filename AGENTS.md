# Repository Guidelines

## Project Structure & Module Organization
This repository is a script-first Python project (no packaged `src/` module). Core automation lives in `src/`:
- `src/common.py`: shared JMAP client/config helpers.
- `src/triage_cycle.py`: main triage engine and CLI flow.
- other `src/*.py`: focused mailbox and draft operations.
- `src/run.sh`: convenience launcher for one-shot and daemon modes.

Tests live in `tests/` (currently `test_triage_cycle_priority.py`). Reference docs are in `docs/`, and starter configuration is in `examples/config.yaml.example`.

## Build, Test, and Development Commands
- `uv sync`: install/update dependencies from `pyproject.toml` and `uv.lock`.
- `uv run src/get_mailboxes.py`: verify Fastmail/JMAP connectivity.
- `uv run src/triage_cycle.py --apply --limit 10`: run one apply cycle on a small batch.
- `./src/run.sh dry | rules | daemon`: run dry, rule-only, or continuous modes.
- `uv run python -m unittest discover -s tests -p "test_*.py"`: run the regression test suite.

Run commands from repo root so relative imports and `--project` behavior stay consistent.

## Coding Style & Naming Conventions
Follow existing Python 3.10+ style:
- 4-space indentation, type hints, and concise docstrings for non-obvious behavior.
- Keep modules and scripts `snake_case`; prefer verb-led script names (`fetch_emails.py`, `delete_email.py`).
- Keep helper functions deterministic where possible (especially classification logic), and isolate network side effects to JMAP client paths.

## Testing Guidelines
Use `unittest` with files named `tests/test_*.py` and methods `test_*`. Add regression tests for every bug fix, especially priority/actionability logic and config normalization edge cases. Prefer fixtures/mocks over live API calls; tests should run offline and deterministically.

## Commit & Pull Request Guidelines
Current history uses short, sentence-style summaries (for example, `Revised the README.`). Keep commits focused and atomic. In PRs, include:
- what changed and why,
- commands run locally (tests + key script invocation),
- config/schema impacts (for example, `triage.db` fields or new config keys),
- sample CLI output when behavior changes.

## Security & Configuration Tips
Never commit API tokens or personal mail data. Store credentials in `~/.config/email-triage/config.yaml` or `FASTMAIL_API_TOKEN`, and keep local state DB files outside version control.
