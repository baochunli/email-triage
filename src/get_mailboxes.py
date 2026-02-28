#!/usr/bin/env python3
"""List Fastmail/JMAP mailboxes with unread counts."""

from __future__ import annotations

import argparse

from common import JMAPClient, JMAPError, load_config, list_mailboxes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List JMAP mailboxes")
    parser.add_argument("account_name", nargs="?", help="Optional account label for output")
    parser.add_argument("--config", help="Path to config file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    config, _ = load_config(args.config)
    client = JMAPClient(config)
    session = client.session()

    account_label = args.account_name or config["mail"].get("account")
    if not account_label:
        account_id = client.account_id
        account_label = (session.get("accounts") or {}).get(account_id or "", {}).get("name")

    print(f"ACCOUNT:{account_label or 'Fastmail'}")
    for mailbox in list_mailboxes(client):
        name = mailbox.get("name") or ""
        unread = mailbox.get("unreadEmails") or 0
        print(f"  MAILBOX:{name}|{unread}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
