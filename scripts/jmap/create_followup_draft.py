#!/usr/bin/env python3
"""Create a follow-up draft using supplied details."""

from __future__ import annotations

import argparse

from common import JMAPClient, JMAPError, create_draft, ensure_reply_subject, load_config, parse_csv_addresses, quote_lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a follow-up draft")
    parser.add_argument("reply_content", help="Reply text (use \\n for line breaks)")
    parser.add_argument("recipient_emails", help="Comma-separated email addresses")
    parser.add_argument("subject", help="Original subject")
    parser.add_argument("original_content", help="Original message content (use \\n for line breaks)")
    parser.add_argument("date_sent_str", help="Date string for quote header")
    parser.add_argument("account_name", nargs="?", help="Optional account name (compatibility arg)")
    parser.add_argument("--config", help="Path to config file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, _ = load_config(args.config)
    client = JMAPClient(config)

    reply_content = args.reply_content.replace("\\n", "\n")
    original_content = args.original_content.replace("\\n", "\n")

    subject = ensure_reply_subject(args.subject)
    quote_header = f"On {args.date_sent_str}, you wrote:"
    full_body = f"{reply_content}\n\n{quote_header}\n\n{quote_lines(original_content)}"

    recipients = parse_csv_addresses(args.recipient_emails)
    if not recipients:
        raise JMAPError("No valid recipient addresses were provided")

    draft_id = create_draft(
        client,
        to=recipients,
        subject=subject,
        body=full_body,
    )

    print(f"SUCCESS:Follow-up draft saved as {draft_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
