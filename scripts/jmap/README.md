# Fastmail JMAP Scripts

Python equivalents of the AppleScript email commands, targeting Fastmail via JMAP.

## Why this exists

The AppleScript layer in `scripts/applescript/` is macOS + Mail.app specific. These scripts keep the same core workflow (fetch, triage, draft-only replies, delete/move) but run against Fastmail directly.

## Requirements

- uv (`brew install uv` or https://docs.astral.sh/uv/)
- Python 3.10+ (uv will manage this automatically)
- Fastmail API token with JMAP permissions

Generate API token in Fastmail:
- Settings → Privacy & Security → Integrations → New API Token

## Config

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

Canonical behavior reference: [`docs/jmap-automation-reference.md`](../../docs/jmap-automation-reference.md).

## Codex setup (for intelligent triage)

Subscription auth (recommended):

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
Use `--no-codex` if you want rule-only triage.

## Primitive commands

```bash
uv run scripts/jmap/get_mailboxes.py
uv run scripts/jmap/fetch_emails.py "Fastmail" "INBOX" 10
uv run scripts/jmap/fetch_all_emails.py "Fastmail" "INBOX" 50 7
uv run scripts/jmap/fetch_email_by_id.py "Fastmail" "INBOX" "Mabc123"
uv run scripts/jmap/fetch_sent.py "Fastmail" "Sent" 20
uv run scripts/jmap/create_draft.py "Mabc123" "Thanks, I will send this Friday."
uv run scripts/jmap/create_followup_draft.py "Just checking in" "person@example.com" "Re: Topic" "Original text" "2026-02-26"
uv run scripts/jmap/delete_email.py "Fastmail" "INBOX" "Mabc123"
```

## Automated triage pipeline

Tiny launcher:

```bash
./scripts/jmap/run.sh           # one apply cycle (Codex)
./scripts/jmap/run.sh dry       # one dry-run cycle
./scripts/jmap/run.sh daemon    # continuous loop
./scripts/jmap/run.sh rules     # rule-only apply cycle
./scripts/jmap/run.sh reset-status # reset triage status to triaged
```

Dry-run one cycle (Codex triage + no drafts created):

```bash
uv run scripts/jmap/triage_cycle.py
```

Apply mode (Codex triage + auto-create drafts in Drafts mailbox):

```bash
uv run scripts/jmap/triage_cycle.py --apply
```

Continuous mode:

```bash
uv run scripts/jmap/triage_cycle.py --apply --loop-seconds 900
# or
uv run scripts/jmap/daemon.py
./scripts/jmap/run.sh reset-status --state-db ~/.config/email-triage/triage.db
``` 

Rule-only fallback mode (no Codex API calls):

```bash
uv run scripts/jmap/triage_cycle.py --apply --no-codex
```

State is persisted in SQLite (`automation.state_db`) so already-drafted emails are skipped by default.
VIP senders are also persisted there in a `vip_senders` table. Manage them with:

```bash
uv run scripts/jmap/triage_cycle.py --vip-list
uv run scripts/jmap/triage_cycle.py --vip-add "your-boss@example.com" --vip-add "client-alert@example.com"
uv run scripts/jmap/triage_cycle.py --vip-remove "old-contact@example.com"
uv run scripts/jmap/triage_cycle.py --state-db ~/.config/email-triage/triage.db --vip-list
```

Blocked senders for auto-draft suppression are persisted in the same DB (`draft_blocked_senders`). Manage them with:

```bash
uv run scripts/jmap/triage_cycle.py --draft-block-list
uv run scripts/jmap/triage_cycle.py --draft-block-add "noreply@example.com" --draft-block-add "alerts@example.com"
uv run scripts/jmap/triage_cycle.py --draft-block-remove "alerts@example.com"
uv run scripts/jmap/triage_cycle.py --state-db ~/.config/email-triage/triage.db --draft-block-list
```

Low and medium-priority emails are auto-archived to the configured Archive mailbox (`mail.archive_mailbox`) when
`automation.auto_archive_priorities` includes them.

Emails addressed to any `mail.sender_emails` identity (in either `To` or `Cc`) are treated as high priority by the
rule-based classifier.

`triage.vip_frequency_threshold` (in config) controls automatic VIP promotion:
- when a sender is classified as high priority and the sender already has at least this many prior high-priority emails, they are added to `vip_senders` with source `auto_frequency`.

If a launcher/apply run reports `errors=1`, inspect the exact failure:

```bash
uv run scripts/jmap/triage_cycle.py --apply --limit 1 --reprocess
sqlite3 ~/.config/email-triage/triage.db \
  'select email_id,status,error,draft_id,updated_at from triage_state order by updated_at desc limit 5;'
```

## Output compatibility

Mail-fetching scripts retain the same marker style as AppleScript:

- `EMAIL_START` / `EMAIL_END`
- `ID`, `SUBJECT`, `FROM` / `TO`, `DATE`, `CONTENT`
- `fetch_all_emails.py` also emits `READ:true|false`

This makes it easy to reuse existing Claude instructions and triage parsing logic.
