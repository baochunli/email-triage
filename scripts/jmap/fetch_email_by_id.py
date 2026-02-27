#!/usr/bin/env python3
"""Fetch a single email by JMAP id."""

from __future__ import annotations

import argparse

from common import (
    JMAPClient,
    JMAPError,
    escape_field,
    extract_text_content,
    format_address_list,
    get_email_by_id,
    load_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch a single email by JMAP id")
    parser.add_argument(
        "positionals",
        nargs="+",
        help="Either <message_id> or <account_name> <mailbox_name> <message_id>",
    )
    parser.add_argument("--config", help="Path to config file")
    args = parser.parse_args()

    if len(args.positionals) == 1:
        args.message_id = args.positionals[0]
    elif len(args.positionals) == 3:
        args.message_id = args.positionals[2]
    else:
        parser.error("Expected either 1 positional (<message_id>) or 3 positionals (<account> <mailbox> <message_id>)")
    return args


def main() -> int:
    args = parse_args()
    config, _ = load_config(args.config)
    client = JMAPClient(config)

    email = get_email_by_id(client, args.message_id)

    msg_id = email.get("id")
    subject = escape_field(email.get("subject") or "")
    sender = escape_field(format_address_list(email.get("from") or []))
    received = escape_field(email.get("receivedAt") or "")
    content = escape_field(extract_text_content(email))

    print("EMAIL_START")
    print(f"ID:{msg_id}")
    print(f"SUBJECT:{subject}")
    print(f"FROM:{sender}")
    print(f"DATE:{received}")
    print(f"CONTENT:{content}")
    print("EMAIL_END")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
