# Fastmail JMAP Setup Guide

Use this guide if you want the email automation workflow without Apple Mail + AppleScript.

Canonical behavior reference (for ongoing updates): [jmap-automation-reference.md](jmap-automation-reference.md).

## 0) Install uv

```bash
brew install uv
```

## 1) Create a Fastmail API token

In Fastmail:
1. Open **Settings**
2. Go to **Privacy & Security → Integrations**
3. Create **New API Token** with JMAP mail permissions

## 2) Configure this repo

Copy and edit the config:

```bash
mkdir -p ~/.config/email-triage
cp examples/config.yaml.example ~/.config/email-triage/config.yaml
```

Then set:

- `fastmail.api_token`
- mailbox names under `mail` if your account uses different names
- archive behavior in `automation.auto_archive_priorities` (default: `low`, `medium`)


## 2.5) Configure Codex access (for intelligent triage)

Subscription auth (recommended):

```bash
codex login
```

Optional API-key auth:

```bash
export OPENAI_API_KEY="your-key"
```

The automation pipeline uses Codex via `ai.backend: codex` in config, with optional
`ai.codex.reasoning_effort` for deeper or faster reasoning.

## 3) Verify connectivity

```bash
uv run scripts/get_mailboxes.py
```

Expected output starts with `ACCOUNT:` and mailbox lines.

## 4) Use JMAP scripts

Unread inbox emails:

```bash
uv run scripts/fetch_emails.py "Fastmail" "INBOX" 20
```

Create a reply draft:

```bash
uv run scripts/create_draft.py "<jmap-email-id>" "Thanks — I'll get this done by Friday."
```

Delete (move to Trash):

```bash
uv run scripts/delete_email.py "Fastmail" "INBOX" "<jmap-email-id>"
```

## 5) Run automated triage + drafting

Tiny launcher (recommended):

```bash
./scripts/run.sh
./scripts/run.sh dry
./scripts/run.sh daemon
./scripts/run.sh reset-status
```

Direct commands:

```bash
uv run scripts/triage_cycle.py
uv run scripts/triage_cycle.py --apply
uv run scripts/daemon.py
./scripts/run.sh reset-status --state-db ~/.config/email-triage/triage.db
```

Triage history and state are stored in SQLite at `automation.state_db` (default `~/.config/email-triage/triage.db`).

If you see launcher output like `errors=1`, run one explicit cycle and inspect the latest triage rows:

```bash
uv run scripts/triage_cycle.py --apply --limit 1 --reprocess
sqlite3 ~/.config/email-triage/triage.db \
  'select email_id,status,error,draft_id,updated_at from triage_state order by updated_at desc limit 5;'
```

## 6) Update your agent instructions

Replace AppleScript command examples with the new `uv run scripts/...` commands.

Recommended guidance:
- Always create drafts only (never send directly)
- Keep reply-all default behavior
- Include full previous thread content when drafting replies

## Notes

- JMAP email IDs are opaque strings (e.g. `Mabc123...`), not Mail.app integers.
