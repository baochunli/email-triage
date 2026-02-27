#!/usr/bin/env python3
"""Fetch read + unread emails from a mailbox via JMAP."""

from __future__ import annotations

import argparse

from common import (
    JMAPClient,
    JMAPError,
    escape_field,
    extract_text_content,
    find_mailbox,
    format_address_list,
    list_mailboxes,
    load_config,
    mailbox_role_hint,
    query_emails,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch all emails from a JMAP mailbox")
    parser.add_argument("account_name", nargs="?", help="Optional account name (compatibility arg)")
    parser.add_argument("mailbox_name", nargs="?", help="Mailbox name (default from config)")
    parser.add_argument("limit", nargs="?", type=int, default=50, help="Number of emails to fetch")
    parser.add_argument(
        "days_back",
        nargs="?",
        type=int,
        default=0,
        help="Optional days-back filter (0 means no date filter)",
    )
    parser.add_argument("--config", help="Path to config file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, _ = load_config(args.config)
    client = JMAPClient(config)

    mailbox_name = args.mailbox_name or config["mail"].get("mailbox")
    mailboxes = list_mailboxes(client)
    mailbox = find_mailbox(
        mailboxes,
        mailbox_name=mailbox_name,
        role=mailbox_role_hint(mailbox_name),
    )

    emails = query_emails(
        client,
        mailbox_id=mailbox["id"],
        limit=max(1, args.limit),
        unread_only=False,
        days_back=max(0, args.days_back),
    )

    for email in emails:
        msg_id = email.get("id")
        subject = escape_field(email.get("subject") or "")
        sender = escape_field(format_address_list(email.get("from") or []))
        received = escape_field(email.get("receivedAt") or "")
        is_read = "$seen" in (email.get("keywords") or {})
        content = escape_field(extract_text_content(email))

        print("EMAIL_START")
        print(f"ID:{msg_id}")
        print(f"SUBJECT:{subject}")
        print(f"FROM:{sender}")
        print(f"DATE:{received}")
        print(f"READ:{str(is_read).lower()}")
        print(f"CONTENT:{content}")
        print("EMAIL_END")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
