#!/usr/bin/env python3
"""Fetch sent emails via JMAP."""

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
    parser = argparse.ArgumentParser(description="Fetch sent emails from a JMAP mailbox")
    parser.add_argument("account_name", nargs="?", help="Optional account name (compatibility arg)")
    parser.add_argument("mailbox_name", nargs="?", help="Mailbox name (defaults to config mail.sent_mailbox)")
    parser.add_argument("limit", nargs="?", type=int, default=20, help="Number of sent emails")
    parser.add_argument("--config", help="Path to config file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, _ = load_config(args.config)
    client = JMAPClient(config)

    mailbox_name = args.mailbox_name or config["mail"].get("sent_mailbox")
    mailboxes = list_mailboxes(client)
    mailbox = find_mailbox(
        mailboxes,
        mailbox_name=mailbox_name,
        role=mailbox_role_hint(mailbox_name) or "sent",
    )

    emails = query_emails(
        client,
        mailbox_id=mailbox["id"],
        limit=max(1, args.limit),
        unread_only=False,
    )

    for email in emails:
        msg_id = email.get("id")
        subject = escape_field(email.get("subject") or "")
        recipients = escape_field(format_address_list(email.get("to") or []))
        sent_date = escape_field(email.get("sentAt") or email.get("receivedAt") or "")
        content = escape_field(extract_text_content(email))

        print("EMAIL_START")
        print(f"ID:{msg_id}")
        print(f"SUBJECT:{subject}")
        print(f"TO:{recipients}")
        print(f"DATE:{sent_date}")
        print(f"CONTENT:{content}")
        print("EMAIL_END")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
