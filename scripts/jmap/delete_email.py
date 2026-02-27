#!/usr/bin/env python3
"""Move an email to trash via JMAP."""

from __future__ import annotations

import argparse

from common import JMAPClient, JMAPError, load_config, move_email_to_trash


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move an email to Trash")
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

    original = move_email_to_trash(client, args.message_id)
    subject = original.get("subject") or ""
    print(f"SUCCESS:Deleted email with ID {args.message_id} - {subject}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
