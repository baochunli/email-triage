# Email Triage (Fastmail JMAP)

Python-powered automation for Fastmail email workflows using JMAP.

## Features

- Fastmail mailbox discovery (`get_mailboxes.py`)
- Fetch unread emails, date-limited emails, sent mail, and messages by ID
- Create draft-only replies and follow-up drafts
- Move/delete emails
- Codex-powered triage with rule-based fallback
- Stateful triage persistence with VIP sender and draft-block lists
- One-shot triage and daemon loops via `daemon.py` and `run.sh`

## Requirements

- uv (`brew install uv` or https://docs.astral.sh/uv/)
- Python 3.10+
- Fastmail API token with JMAP permissions

Generate API token in Fastmail:

- Settings → Privacy & Security → Integrations → New API Token

## Config

Copy the example config:

```bash
mkdir -p ~/.config/email-triage
cp examples/config.yaml.example ~/.config/email-triage/config.yaml
```

Then set `fastmail.api_token` in that file or export `FASTMAIL_API_TOKEN`.

Default config lookup order:

1. `--config /path/to/config.yaml`
2. `$EMAIL_TRIAGE_CONFIG`
3. `~/.config/email-triage/config.yaml`
4. `~/.config/email-triage/config.yml`
5. `~/.config/email-triage/config.json`
6. `~/.config/email-manager/config.yaml`
7. `~/.config/email-manager/config.yml`
8. `~/.config/email-manager/config.json`

Use the template in `examples/config.yaml.example`.

## Setup

```bash
# Install dependencies
uv sync

# Verify JMAP connectivity
uv run src/get_mailboxes.py
```

## Codex setup (for intelligent triage)

Recommended auth:

```bash
codex login
```

Optional API-key auth:

```bash
export OPENAI_API_KEY="your-key"
```

The triage pipeline uses `ai.backend: codex` and `ai.codex.model` from config.
`ai.codex.reasoning_effort` can optionally set the model's reasoning effort.
`ai.codex.auth_mode` defaults to `subscription`.
Use `--no-codex` to force rule-only behavior.

Canonical behavior reference: [`docs/jmap-automation-reference.md`](docs/jmap-automation-reference.md).

## Primitive commands

```bash
uv run src/get_mailboxes.py
uv run src/fetch_emails.py "Fastmail" "INBOX" 10
uv run src/fetch_all_emails.py "Fastmail" "INBOX" 50 7
uv run src/fetch_email_by_id.py "Fastmail" "INBOX" "Mabc123"
uv run src/fetch_sent.py "Fastmail" "Sent" 20
uv run src/create_draft.py "Mabc123" "Thanks, I will send this Friday."
uv run src/create_followup_draft.py "Just checking in" "person@example.com" "Re: Topic" "Original text" "2026-02-26"
uv run src/delete_email.py "Fastmail" "INBOX" "Mabc123"
```

Output marker compatibility is preserved:

- `EMAIL_START` / `EMAIL_END`
- `ID`, `SUBJECT`, `FROM`, `TO`, `DATE`, `CONTENT`
- `fetch_all_emails.py` also emits `READ:true|false`

This keeps existing parsing and instruction workflows compatible.

## Automated triage pipeline

Tiny launcher:

```bash
./run.sh           # one apply cycle (Codex)
./run.sh dry       # one dry-run cycle
./run.sh daemon    # continuous loop
./run.sh rules     # rule-only apply cycle
./run.sh reset-status # reset triage status to triaged
```

Dry-run one cycle (Codex triage, no drafts):

```bash
uv run src/triage_cycle.py
```

Apply mode (Codex triage + auto-create drafts in Drafts mailbox):

```bash
uv run src/triage_cycle.py --apply
```

Continuous mode:

```bash
uv run src/triage_cycle.py --apply --loop-seconds 900
# or
uv run src/daemon.py
./run.sh reset-status --state-db ~/.config/email-triage/triage.db
```

Rule-only fallback mode:

```bash
uv run src/triage_cycle.py --apply --no-codex
```

### VIP and draft-block management

VIP senders are managed through the triage DB:

```bash
uv run src/triage_cycle.py --vip-list
uv run src/triage_cycle.py --vip-add "your-boss@example.com" --vip-add "client-alert@example.com"
uv run src/triage_cycle.py --vip-remove "old-contact@example.com"
uv run src/triage_cycle.py --state-db ~/.config/email-triage/triage.db --vip-list
```

Draft suppression list commands:

```bash
uv run src/triage_cycle.py --draft-block-list
uv run src/triage_cycle.py --draft-block-add "noreply@example.com" --draft-block-add "alerts@example.com"
uv run src/triage_cycle.py --draft-block-remove "alerts@example.com"
uv run src/triage_cycle.py --state-db ~/.config/email-triage/triage.db --draft-block-list
```

State is persisted in SQLite (`automation.state_db`) so already-drafted emails are skipped by default.
`triage.vip_frequency_threshold` controls automatic VIP promotion when a high-priority sender reaches the threshold.
`mail.sender_emails` matching in either `To` or `Cc` marks an email as high priority in rule-based classification.
Low/medium-priority emails are auto-archived when configured in `automation.auto_archive_priorities`.

If the launcher/apply cycle reports `errors=1`, re-run one email and inspect DB rows:

```bash
uv run src/triage_cycle.py --apply --limit 1 --reprocess
sqlite3 ~/.config/email-triage/triage.db \
  'select email_id,status,error,draft_id,updated_at from triage_state order by updated_at desc limit 5;'
```

## Documentation

- [`docs/jmap-fastmail-setup.md`](docs/jmap-fastmail-setup.md) (setup and first run)
- [`docs/jmap-automation-reference.md`](docs/jmap-automation-reference.md) (command/reference matrix and runbook)

## Notes

- Default state DB: `~/.config/email-triage/triage.db`.
- Draft creation remains non-destructive; drafted messages are not sent automatically.
