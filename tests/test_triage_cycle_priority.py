import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "jmap"))

import triage_cycle  # noqa: E402


class PriorityRulesTests(unittest.TestCase):
    def test_sent_to_configured_sender_email_is_high_priority_even_with_cc(self) -> None:
        email = {
            "from": [{"email": "noreply@updates.example.com", "name": "Updates"}],
            "to": [
                {"email": "me@example.com", "name": "Me"},
            ],
            "cc": [
                {"email": "teammate@example.com", "name": "Teammate"},
            ],
            "subject": "Weekly digest",
            "preview": "FYI",
        }
        config = {
            "mail": {
                "sender_emails": ["me@example.com", "me+alias@example.com"],
            },
            "triage": {
                "urgent_keywords": [],
            },
        }

        priority, actionable, reason, _ = triage_cycle.classify_priority(email, config, vip_senders=set())

        self.assertEqual(priority, "high")
        self.assertFalse(actionable)
        self.assertIn("sent to configured sender address", reason)

    def test_no_sender_identity_recipient_does_not_force_high_priority(self) -> None:
        email = {
            "from": [{"email": "updates@example.com", "name": "Updates"}],
            "to": [{"email": "other@example.com", "name": "Other"}],
            "cc": [{"email": "teammate@example.com", "name": "Teammate"}],
            "subject": "Weekly digest",
            "preview": "FYI",
        }
        config = {
            "mail": {
                "sender_emails": ["me@example.com"],
            },
            "triage": {
                "urgent_keywords": [],
            },
        }

        priority, actionable, reason, _ = triage_cycle.classify_priority(email, config, vip_senders=set())

        self.assertEqual(priority, "low")
        self.assertFalse(actionable)
        self.assertNotIn("sent to configured sender address", reason)

    def test_sender_identities_split_comma_delimited_list_entries(self) -> None:
        config = {
            "mail": {
                "sender_emails": ["me@example.com, me+alias@example.com"],
            },
        }

        identities = triage_cycle.configured_sender_identities(config)

        self.assertIn("me@example.com", identities)
        self.assertIn("me+alias@example.com", identities)

    def test_draft_self_check_remains_to_only_not_cc(self) -> None:
        email = {
            "to": [{"email": "other@example.com", "name": "Other"}],
            "cc": [{"email": "me@example.com", "name": "Me"}],
        }
        config = {
            "mail": {
                "sender_emails": ["me@example.com"],
            },
        }

        is_to_self = triage_cycle._is_drafted_to_self(email=email, config=config, client=object())  # type: ignore[arg-type]

        self.assertFalse(is_to_self)


if __name__ == "__main__":
    unittest.main()
