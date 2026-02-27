#!/usr/bin/env python3
"""Create a reply draft in Fastmail via JMAP."""

from __future__ import annotations

import argparse

from common import (
    JMAPClient,
    JMAPError,
    create_reply_draft_from_email,
    get_email_by_id,
    load_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a reply draft from an existing email")
    parser.add_argument("message_id", help="JMAP email id for the original message")
    parser.add_argument("reply_content", help="Reply text (use \\n for line breaks)")
    parser.add_argument("account_name", nargs="?", help="Optional account name (compatibility arg)")
    parser.add_argument("mailbox_name", nargs="?", help="Optional mailbox name (compatibility arg)")
    parser.add_argument(
        "--reply-all",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include original To/Cc recipients (default: true)",
    )
    parser.add_argument("--config", help="Path to config file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, _ = load_config(args.config)
    client = JMAPClient(config)

    reply_content = args.reply_content.replace("\\n", "\n")
    original = get_email_by_id(client, args.message_id)

    draft_id = create_reply_draft_from_email(
        client,
        original_email=original,
        reply_content=reply_content,
        reply_all=args.reply_all,
    )

    print(f"SUCCESS:Draft saved for message ID {args.message_id} as draft {draft_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
