# JMAP Automation Reference (Source of Truth)

This is the canonical reference for Fastmail JMAP automation behavior.

If command behavior changes, update this file together with:
- `run.sh` (launcher help text)
- `src/triage_cycle.py --help`
- `src/daemon.py --help`

## Auth modes (Codex)

Configured under `ai.codex.auth_mode` in `config.yaml`:

- `subscription` (default): uses local Codex CLI login (`codex login`)
- `api_key`: uses `OPENAI_API_KEY` / `CODEX_API_KEY` (or explicit `ai.codex.api_key`)
- `auto`: uses API key if present, otherwise subscription

Reasoning effort is configured under `ai.codex.reasoning_effort`:

- Supports model-dependent values such as `minimal`, `low`, `medium`, `high`, `none`, and `xhigh`.
- Omitted values are passed through to defaults.

## Recommended startup

```bash
brew install uv   # one-time
codex login
./run.sh
```

## Launcher modes

```bash
./run.sh once          # default: one apply cycle (Codex)
./run.sh dry           # one dry-run cycle
./run.sh daemon        # continuous apply loop
./run.sh daemon-dry    # continuous dry-run loop
./run.sh rules         # rule-only one apply cycle
./run.sh rules-daemon  # continuous rule-only apply loop
./run.sh reset-status  # reset triage state status to triaged
```

## Direct commands

```bash
uv run src/triage_cycle.py
uv run src/triage_cycle.py --vip-list
uv run src/triage_cycle.py --vip-add important-contact@example.com
uv run src/triage_cycle.py --vip-remove no-longer-vip@example.com
uv run src/triage_cycle.py --draft-block-list
uv run src/triage_cycle.py --draft-block-add noreply@example.com
uv run src/triage_cycle.py --draft-block-remove noreply@example.com
uv run src/triage_cycle.py --vip-list --state-db /path/to/triage.db
uv run src/triage_cycle.py --draft-block-list --state-db /path/to/triage.db
uv run src/triage_cycle.py --apply
uv run src/triage_cycle.py --apply --no-codex
uv run src/daemon.py
uv run src/daemon.py --dry-run
uv run src/daemon.py --no-codex
./run.sh reset-status
```

## Drafts guarantee

In apply mode (`--apply`, or launcher `once`/`daemon`), generated replies are created as JMAP drafts in the configured Drafts mailbox (`mail.drafts_mailbox`, default: `Drafts`).
Senders listed in the DB table `draft_blocked_senders` are excluded from auto-generated draft replies.

Low and medium-priority emails are auto-archived to the configured Archive mailbox (`mail.archive_mailbox`, default: `Archive`) when included in `automation.auto_archive_priorities`.

## State and safety

- Triage state DB: `automation.state_db` (default `~/.config/email-triage/triage.db`)
- `reset-status` sets all existing `triage_state` rows back to `status='triaged'` for rescan/reprocess workflows.
- VIP senders are stored in the `vip_senders` table in the same DB and used as rule-level high-priority overrides
- Emails addressed to any configured `mail.sender_emails` identity (in `To` or `Cc`) are treated as high priority by rule-based triage
- Draft suppression senders are stored in the `draft_blocked_senders` table in the same DB
- `triage.vip_frequency_threshold` (if set > 0) auto-promotes senders to VIP after that many high-priority emails (counts from triage history)
- Existing drafted emails are skipped by default (unless `--reprocess`)
- On cycle errors, DB rollback is attempted before next cycle
- If Codex fails and `automation.codex_fallback_to_rules: true`, triage falls back to rule-based behavior

## Troubleshooting cycle errors

If launcher output shows `errors=1`, run one explicit cycle and inspect state rows:

```bash
uv run src/triage_cycle.py --apply --limit 1 --reprocess
sqlite3 ~/.config/email-triage/triage.db \
  'select email_id,status,error,draft_id,updated_at from triage_state order by updated_at desc limit 5;'
```
