#!/usr/bin/env python3
"""Run continuous automated JMAP triage cycles."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from common import JMAPClient, JMAPError, load_config
from triage_cycle import normalize_automation_settings, open_state_db, print_summary, process_one_cycle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Continuous JMAP triage daemon")
    parser.add_argument("--config", help="Path to config file")
    parser.add_argument("--interval-seconds", type=int, help="Polling interval in seconds")
    parser.add_argument("--cycles", type=int, help="Optional number of cycles before exit")
    parser.add_argument("--limit", type=int, help="Override max emails per cycle")
    parser.add_argument("--reprocess", action="store_true", help="Reprocess emails even if already drafted")
    parser.add_argument("--json", action="store_true", help="Print cycle summaries as JSON")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Disable draft creation (default false; drafts are created)",
    )
    parser.add_argument(
        "--no-codex",
        action="store_true",
        help="Disable Codex intelligence and use rule-only triage",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, _ = load_config(args.config)
    automation = normalize_automation_settings(config)
    if args.no_codex:
        automation["use_codex"] = False

    interval = max(1, int(args.interval_seconds or automation.get("loop_interval_seconds", 900)))

    state_db = Path(str(automation.get("state_db", "~/.config/email-triage/triage.db"))).expanduser()
    conn = open_state_db(state_db)

    cycle = 0
    while True:
        cycle += 1
        try:
            client = JMAPClient(config)
            summary = process_one_cycle(
                client=client,
                config=config,
                automation=automation,
                state_conn=conn,
                apply_mode=not args.dry_run,
                limit_override=args.limit,
                reprocess=args.reprocess,
            )
            print_summary(summary, args.json)
        except Exception as exc:  # noqa: BLE001
            rollback_ok = True
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                rollback_ok = False

            if args.json:
                print(json.dumps({"error": str(exc), "cycle": cycle, "rolled_back": rollback_ok}, ensure_ascii=False))
            else:
                print(f"ERROR:{exc}")

        if args.cycles and cycle >= args.cycles:
            break
        time.sleep(interval)

    conn.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
