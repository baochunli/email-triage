# Email Triage (Fastmail JMAP)

Python automation for Fastmail email workflows using JMAP.

This repository contains the JMAP side of the email-triage stack with no AppleScript or Claude Code plugin files.

## Features

- Fastmail mailbox discovery (`get_mailboxes.py`)
- Fetch unread emails, date-limited emails, sent mail, and messages by ID
- Create draft-only replies and follow-up drafts
- Move/delete emails
- Codex-powered triage with rule-based fallback
- Stateful triage persistence with VIP senders and draft-block lists
- One-shot triage/daemon loops with `daemon.py` and `run.sh`

## Getting Started

### 1) Install dependencies

```
uv sync
```

### 2) Configure Fastmail access

Copy the example config:

```bash
mkdir -p ~/.config/email-triage
cp examples/config.yaml.example ~/.config/email-triage/config.yaml
```

Then set `fastmail.api_token` in that file or export `FASTMAIL_API_TOKEN`.

### 3) Run a quick dry run

```bash
uv run scripts/jmap/get_mailboxes.py
uv run scripts/jmap/fetch_emails.py "Fastmail" "INBOX" 5
```

### 4) Run triage

```bash
uv run scripts/jmap/triage_cycle.py
uv run scripts/jmap/triage_cycle.py --apply
uv run scripts/jmap/daemon.py
```

## Documentation

- `scripts/jmap/README.md` (CLI usage and examples for each script)
- `docs/jmap-fastmail-setup.md` (complete Fastmail setup and first run)
- `docs/jmap-automation-reference.md` (command/reference matrix and runbook)

## Notes

- The default state DB is `~/.config/email-triage/triage.db`.
- Triaged emails are persisted in the local SQLite database for priority and follow-up history.
- Draft creation remains non-destructive; drafted messages are not sent automatically.
