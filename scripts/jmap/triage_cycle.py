#!/usr/bin/env python3
"""Automated email triage cycle with optional auto-draft creation."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import error, request

try:  # support both `python scripts/jmap/triage_cycle.py` and package imports
    from common import (
        JMAPClient,
        JMAPError,
        create_reply_draft_from_email,
        move_email_to_archive,
        extract_text_content,
        find_mailbox,
        format_address,
        list_mailboxes,
        load_config,
        mailbox_role_hint,
        query_emails,
    )
except ImportError:  # pragma: no cover
    from .common import (
        JMAPClient,
        JMAPError,
        create_reply_draft_from_email,
        move_email_to_archive,
        extract_text_content,
        find_mailbox,
        format_address,
        list_mailboxes,
        load_config,
        mailbox_role_hint,
        query_emails,
    )

PRIORITY_ORDER = {"low": 1, "medium": 2, "high": 3}
VALID_PRIORITIES = set(PRIORITY_ORDER.keys())

ACTION_PATTERNS = [
    r"\bplease\b",
    r"\bcan you\b",
    r"\bcould you\b",
    r"\bwould you\b",
    r"\bneed you\b",
    r"\baction required\b",
    r"\blet me know\b",
    r"\bfollow up\b",
    r"\bdeadline\b",
    r"\basap\b",
    r"\beod\b",
]

LOW_SIGNAL_PATTERNS = [
    r"\bnewsletter\b",
    r"\bdigest\b",
    r"\bnotification\b",
    r"\bpromo\b",
    r"\bmarketing\b",
]

DEFAULT_STATE_DB = "~/.config/email-triage/triage.db"
VIP_SOURCE_CONFIG = "config"
VIP_SOURCE_MANUAL = "manual"
VIP_SOURCE_AUTO = "auto_frequency"


class TriageRuntimeError(RuntimeError):
    """Raised for runtime issues during triage."""


class CodexClientError(RuntimeError):
    """Raised when Codex API calls fail."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one or more automated JMAP triage cycles")
    parser.add_argument("--config", help="Path to config file")
    parser.add_argument(
        "--state-db",
        help=(
            "Override state DB path (also used by VIP/draft-block management flags without needing full config "
            "if desired)."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Create drafts for matching emails")
    parser.add_argument("--limit", type=int, help="Override max emails per cycle")
    parser.add_argument("--reprocess", action="store_true", help="Reprocess emails even if already drafted")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output")
    parser.add_argument(
        "--no-codex",
        action="store_true",
        help="Disable Codex intelligence and use rule-only triage",
    )
    parser.add_argument(
        "--loop-seconds",
        type=int,
        help="Run continuously with this delay between cycles",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        help="When looping, stop after this many cycles",
    )
    parser.add_argument("--vip-list", action="store_true", help="List VIP senders from DB and exit")
    parser.add_argument(
        "--vip-add",
        action="append",
        help="Add VIP sender email(s), repeat or comma-separate values",
    )
    parser.add_argument(
        "--vip-remove",
        action="append",
        help="Remove VIP sender email(s), repeat or comma-separate values",
    )
    parser.add_argument(
        "--draft-block-list",
        action="store_true",
        help="List sender emails blocked from auto-draft creation and exit",
    )
    parser.add_argument(
        "--draft-block-add",
        action="append",
        help="Add blocked sender email(s), repeat or comma-separate values",
    )
    parser.add_argument(
        "--draft-block-remove",
        action="append",
        help="Remove blocked sender email(s), repeat or comma-separate values",
    )
    return parser.parse_args()


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_vip_address(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""

    if normalized.startswith("mailto:"):
        normalized = normalized[len("mailto:"):]

    lt = normalized.rfind("<")
    gt = normalized.rfind(">")
    if lt != -1 and gt != -1 and gt > lt:
        normalized = normalized[lt + 1 : gt].strip()

    return normalized


def split_address_values(values: list[str] | None) -> list[str]:
    if not values:
        return []

    normalized: list[str] = []
    for raw in values:
        if not raw:
            continue
        for part in str(raw).split(","):
            part = normalize_vip_address(part)
            if part:
                normalized.append(part)
    return normalized


def configured_sender_identities(config: dict[str, Any]) -> set[str]:
    mail = dict(config.get("mail") or {})
    sender_emails = mail.get("sender_emails")
    identities: set[str] = set()

    if isinstance(sender_emails, str):
        raw_values = sender_emails.replace(";", ",").replace("\n", ",").split(",")
        for value in raw_values:
            normalized = normalize_vip_address(value)
            if normalized:
                identities.add(normalized)
        return identities

    if isinstance(sender_emails, (list, tuple, set)):
        for value in sender_emails:
            for part in str(value).replace(";", ",").replace("\n", ",").split(","):
                normalized = normalize_vip_address(part)
                if normalized:
                    identities.add(normalized)

    return identities


def email_targets_sender_identity(
    email: dict[str, Any],
    sender_identities: set[str],
    *,
    include_cc: bool = True,
) -> bool:
    if not sender_identities:
        return False

    recipients = list(email.get("to") or [])
    if include_cc:
        recipients += list(email.get("cc") or [])
    for person in recipients:
        if not isinstance(person, dict):
            continue
        recipient_email = normalize_vip_address((person or {}).get("email") or "")
        if recipient_email and recipient_email in sender_identities:
            return True
    return False


def normalize_automation_settings(config: dict[str, Any]) -> dict[str, Any]:
    automation = dict(config.get("automation") or {})
    automation.setdefault("max_emails_per_cycle", 20)
    automation.setdefault("auto_draft", True)
    automation.setdefault("auto_archive_low_priority", True)
    automation.setdefault("reply_all", True)
    automation.setdefault("draft_actionable_only", True)
    automation.setdefault("min_priority_for_draft", "high")
    automation.setdefault("state_db", "~/.config/email-triage/triage.db")
    automation.setdefault("loop_interval_seconds", 900)
    automation.setdefault("use_codex", True)
    automation.setdefault("codex_timeout_seconds", 60)
    automation.setdefault("codex_fallback_to_rules", True)
    automation.setdefault("codex_max_body_chars", 4000)

    auto_archive_priorities = automation.get("auto_archive_priorities")
    if auto_archive_priorities is None:
        if automation.get("auto_archive_low_priority", True):
            auto_archive_priorities = ["low", "medium"]
        else:
            auto_archive_priorities = []

    if isinstance(auto_archive_priorities, str):
        auto_archive_priorities = [auto_archive_priorities]

    if not isinstance(auto_archive_priorities, (list, tuple, set)):
        auto_archive_priorities = ["low", "medium"] if automation.get("auto_archive_low_priority", True) else []

    automation["auto_archive_priorities"] = sorted(
        {str(v).strip().lower() for v in auto_archive_priorities if str(v).strip().lower() in VALID_PRIORITIES}
    )

    return automation


def normalize_ai_settings(config: dict[str, Any]) -> dict[str, Any]:
    ai = dict(config.get("ai") or {})
    backend = str(ai.get("backend") or "codex").strip().lower()
    if backend != "codex":
        raise TriageRuntimeError(
            f"Unsupported ai.backend '{backend}'. This pipeline is Codex-only; set ai.backend: codex."
        )

    codex = dict(ai.get("codex") or {})
    model = str(codex.get("model") or "gpt-5-codex")
    reasoning_effort = codex.get("reasoning_effort")
    if reasoning_effort is None:
        reasoning_effort = codex.get("reasoning")
    if isinstance(reasoning_effort, str):
        reasoning_effort = reasoning_effort.strip().lower() or None
    auth_mode = str(codex.get("auth_mode") or "subscription").strip().lower()
    if auth_mode not in {"subscription", "api_key", "auto"}:
        raise TriageRuntimeError(
            "Invalid ai.codex.auth_mode. Use one of: subscription, api_key, auto."
        )

    api_key = (
        codex.get("api_key")
        or os.environ.get(str(codex.get("api_key_env") or "OPENAI_API_KEY"))
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("CODEX_API_KEY")
    )

    resolved_mode = auth_mode
    if auth_mode == "auto":
        resolved_mode = "api_key" if api_key else "subscription"

    settings: dict[str, Any] = {
        "model": model,
        "auth_mode": resolved_mode,
        "base_url": str(codex.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
    }

    if reasoning_effort:
        settings["reasoning_effort"] = reasoning_effort

    if resolved_mode == "api_key":
        if not api_key:
            raise TriageRuntimeError(
                "Missing Codex API key. Set ai.codex.api_key or OPENAI_API_KEY (or CODEX_API_KEY)."
            )
        settings["api_key"] = str(api_key)

    return settings


class CodexClient:
    """Minimal Codex client via OpenAI Responses API."""

    def __init__(self, settings: dict[str, Any], timeout_seconds: int = 60):
        self.model = settings["model"]
        self.reasoning_effort = settings.get("reasoning_effort")
        self.api_key = settings["api_key"]
        self.base_url = settings["base_url"]
        self.timeout_seconds = max(10, int(timeout_seconds))

    def triage_email(
        self,
        *,
        email_payload: dict[str, Any],
        rule_priority: str,
        rule_actionable: bool,
        rule_reason: str,
        fallback_reply: str,
    ) -> dict[str, Any]:
        system_prompt = (
            "You are an email triage assistant. "
            "Return STRICT JSON only, no markdown, no commentary. "
            "Decide priority and actionability, then draft a short professional reply."
        )
        user_payload = {
            "task": "Classify and draft response",
            "rules_baseline": {
                "priority": rule_priority,
                "actionable": rule_actionable,
                "reason": rule_reason,
            },
            "email": email_payload,
            "requirements": {
                "priority_values": ["high", "medium", "low"],
                "must_reply_text": True,
                "reply_style": "concise, professional, no AI-fluff",
            },
            "output_schema": {
                "priority": "high|medium|low",
                "actionable": "boolean",
                "reason": "short explanation",
                "summary": "one-sentence summary",
                "reply_text": "draft reply body text",
            },
            "fallback_reply": fallback_reply,
        }

        payload = {
            "model": self.model,
            "input": f"SYSTEM:\n{system_prompt}\n\nUSER:\n{json.dumps(user_payload, ensure_ascii=False)}",
        }
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        response = self._post_json(f"{self.base_url}/responses", payload)
        output_text = self._extract_output_text(response)
        if not output_text:
            raise CodexClientError("Codex returned no output text")
        parsed = parse_json_from_text(output_text)
        return normalize_codex_triage_result(parsed, fallback_reply)

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as res:
                raw = res.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CodexClientError(f"Codex HTTP {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise CodexClientError(f"Codex network error: {exc.reason}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexClientError(f"Codex returned invalid JSON: {raw[:500]}") from exc

        if not isinstance(parsed, dict):
            raise CodexClientError("Codex response was not a JSON object")
        return parsed

    @staticmethod
    def _extract_output_text(response: dict[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        pieces: list[str] = []
        for item in response.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                ctype = content.get("type")
                if ctype in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str) and text.strip():
                        pieces.append(text.strip())
        return "\n".join(pieces).strip()


def normalize_codex_triage_result(parsed: dict[str, Any], fallback_reply: str) -> dict[str, Any]:
    priority = str(parsed.get("priority") or "").strip().lower()
    if priority not in VALID_PRIORITIES:
        raise CodexClientError(f"Invalid priority from Codex: {priority!r}")

    actionable_raw = parsed.get("actionable")
    if isinstance(actionable_raw, bool):
        actionable = actionable_raw
    else:
        actionable = str(actionable_raw).strip().lower() in {"1", "true", "yes", "y"}

    reason = str(parsed.get("reason") or "").strip() or "Codex triage"
    summary = str(parsed.get("summary") or "").strip() or f"Email triaged by Codex ({priority})"
    reply_text = str(parsed.get("reply_text") or "").strip() or fallback_reply

    return {
        "priority": priority,
        "actionable": actionable,
        "reason": reason,
        "summary": summary,
        "reply_text": reply_text,
        "source": "codex",
    }


class CodexSubscriptionClient:
    """Codex client that uses local `codex exec` with ChatGPT subscription login."""

    def __init__(self, settings: dict[str, Any], timeout_seconds: int = 120):
        self.model = settings["model"]
        self.reasoning_effort = settings.get("reasoning_effort")
        self.timeout_seconds = max(20, int(timeout_seconds))
        self.codex_bin = shutil.which("codex")
        if not self.codex_bin:
            raise CodexClientError("`codex` CLI not found in PATH. Install Codex CLI or use api_key auth mode.")
        self._ensure_logged_in()

    def _ensure_logged_in(self) -> None:
        try:
            proc = subprocess.run(
                [self.codex_bin, "login", "status"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise CodexClientError(f"Unable to check Codex login status: {exc}") from exc

        status_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0 or "logged in" not in status_text.lower():
            raise CodexClientError(
                "Codex subscription login not found. Run `codex login` (ChatGPT sign-in) and retry."
            )

    def triage_email(
        self,
        *,
        email_payload: dict[str, Any],
        rule_priority: str,
        rule_actionable: bool,
        rule_reason: str,
        fallback_reply: str,
    ) -> dict[str, Any]:
        payload = {
            "task": "Classify and draft response",
            "rules_baseline": {
                "priority": rule_priority,
                "actionable": rule_actionable,
                "reason": rule_reason,
            },
            "email": email_payload,
            "requirements": {
                "priority_values": ["high", "medium", "low"],
                "must_reply_text": True,
                "reply_style": "concise, professional, no AI-fluff",
            },
            "output_schema": {
                "priority": "high|medium|low",
                "actionable": "boolean",
                "reason": "short explanation",
                "summary": "one-sentence summary",
                "reply_text": "draft reply body text",
            },
            "fallback_reply": fallback_reply,
        }

        prompt = (
            "You are an email triage assistant. "
            "Return STRICT JSON matching the schema, no markdown, no extra text.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )

        schema = {
            "type": "object",
            "required": ["priority", "actionable", "reason", "summary", "reply_text"],
            "properties": {
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "actionable": {"type": "boolean"},
                "reason": {"type": "string"},
                "summary": {"type": "string"},
                "reply_text": {"type": "string"},
            },
            "additionalProperties": False,
        }

        with tempfile.TemporaryDirectory(prefix="codex_triage_") as tmp_dir:
            schema_path = Path(tmp_dir) / "schema.json"
            out_path = Path(tmp_dir) / "response.txt"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            cmd = [
                self.codex_bin,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                self.model,
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "-o",
                str(out_path),
            ]
            if self.reasoning_effort:
                cmd.extend(["-c", f"reasoning.effort={json.dumps(self.reasoning_effort)}"])
            cmd.append("-")

            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexClientError(f"Codex CLI timed out after {self.timeout_seconds}s") from exc

            if proc.returncode != 0:
                out = (proc.stdout or "")[-500:]
                err = (proc.stderr or "")[-500:]
                raise CodexClientError(f"Codex CLI failed (code {proc.returncode}). stdout={out!r} stderr={err!r}")

            output_text = out_path.read_text(encoding="utf-8").strip() if out_path.exists() else ""
            if not output_text:
                output_text = (proc.stdout or "").strip()
            if not output_text:
                raise CodexClientError("Codex CLI returned empty response")

        parsed = parse_json_from_text(output_text)
        return normalize_codex_triage_result(parsed, fallback_reply)


def parse_json_from_text(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise CodexClientError(f"Could not find JSON object in Codex output: {text[:300]}")

    snippet = text[start : end + 1]
    try:
        parsed = json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise CodexClientError(f"Failed to parse JSON from Codex output: {snippet[:300]}") from exc

    if not isinstance(parsed, dict):
        raise CodexClientError("Parsed Codex output was not a JSON object")
    return parsed


def open_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_state (
          email_id TEXT PRIMARY KEY,
          subject TEXT,
          sender TEXT,
          sender_email TEXT,
          received_at TEXT,
          priority TEXT,
          actionable INTEGER NOT NULL,
          reason TEXT,
          summary TEXT,
          reply_text TEXT,
          drafted INTEGER NOT NULL DEFAULT 0,
          draft_id TEXT,
          status TEXT NOT NULL,
          error TEXT,
          raw_email TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_at TEXT NOT NULL,
          mode TEXT NOT NULL,
          emails_seen INTEGER NOT NULL,
          triaged_count INTEGER NOT NULL,
          drafted_count INTEGER NOT NULL,
          skipped_count INTEGER NOT NULL,
          error_count INTEGER NOT NULL,
          details_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vip_senders (
          email TEXT PRIMARY KEY,
          added_at TEXT NOT NULL,
          source TEXT NOT NULL,
          note TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS draft_blocked_senders (
          email TEXT PRIMARY KEY,
          added_at TEXT NOT NULL,
          source TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_vip_senders(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT email FROM vip_senders").fetchall()
    return {str(row["email"]).strip().lower() for row in rows if row["email"]}


def add_vip_sender(conn: sqlite3.Connection, email: str, source: str = VIP_SOURCE_MANUAL) -> bool:
    normalized = normalize_vip_address(email)
    if not normalized or "@" not in normalized:
        return False

    existing = conn.execute("SELECT 1 FROM vip_senders WHERE email = ?", (normalized,)).fetchone()
    if existing:
        return False

    conn.execute(
        """
        INSERT INTO vip_senders (email, added_at, source)
        VALUES (?, ?, ?)
        """,
        (normalized, utc_now_iso(), source),
    )
    return True


def remove_vip_sender(conn: sqlite3.Connection, email: str) -> bool:
    normalized = normalize_vip_address(email)
    if not normalized:
        return False

    cur = conn.execute("DELETE FROM vip_senders WHERE email = ?", (normalized,))
    return cur.rowcount > 0


def list_vip_senders(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT email FROM vip_senders ORDER BY email").fetchall()
    return [str(row["email"]) for row in rows if row["email"]]


def get_draft_blocked_senders(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT email FROM draft_blocked_senders").fetchall()
    return {str(row["email"]).strip().lower() for row in rows if row["email"]}


def add_draft_blocked_sender(conn: sqlite3.Connection, email: str, source: str = VIP_SOURCE_MANUAL) -> bool:
    normalized = normalize_vip_address(email)
    if not normalized or "@" not in normalized:
        return False

    existing = conn.execute("SELECT 1 FROM draft_blocked_senders WHERE email = ?", (normalized,)).fetchone()
    if existing:
        return False

    conn.execute(
        """
        INSERT INTO draft_blocked_senders (email, added_at, source)
        VALUES (?, ?, ?)
        """,
        (normalized, utc_now_iso(), source),
    )
    return True


def remove_draft_blocked_sender(conn: sqlite3.Connection, email: str) -> bool:
    normalized = normalize_vip_address(email)
    if not normalized:
        return False

    cur = conn.execute("DELETE FROM draft_blocked_senders WHERE email = ?", (normalized,))
    return cur.rowcount > 0


def list_draft_blocked_senders(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT email FROM draft_blocked_senders ORDER BY email").fetchall()
    return [str(row["email"]) for row in rows if row["email"]]


def seed_vip_senders_from_config(conn: sqlite3.Connection, config: dict[str, Any]) -> int:
    triage_cfg = dict(config.get("triage") or {})
    raw_vips = triage_cfg.get("vip_senders", [])
    if not raw_vips:
        return 0
    if not isinstance(raw_vips, (list, tuple, set)):
        return 0

    added = 0
    for raw in raw_vips:
        if add_vip_sender(conn, str(raw), source=VIP_SOURCE_CONFIG):
            added += 1
    return added


def get_vip_frequency_threshold(config: dict[str, Any]) -> int:
    triage_cfg = dict(config.get("triage") or {})
    raw_threshold = triage_cfg.get("vip_frequency_threshold", 0)
    try:
        threshold = int(raw_threshold)
    except (TypeError, ValueError):
        return 0

    return max(0, threshold)


def count_high_priority_emails_for_sender(state_conn: sqlite3.Connection, sender_email: str) -> int:
    row = state_conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM triage_state
        WHERE sender_email = ? AND priority = 'high'
        """,
        (sender_email,),
    ).fetchone()
    if not row:
        return 0
    return int(row["count"] or 0)


def maybe_auto_promote_vip_from_high_frequency(
    state_conn: sqlite3.Connection,
    config: dict[str, Any],
    sender_email: str,
    previous_priority: str | None,
    current_priority: str,
) -> bool:
    threshold = get_vip_frequency_threshold(config)
    if threshold <= 0:
        return False

    normalized_sender = normalize_vip_address(sender_email)
    if not normalized_sender or "@" not in normalized_sender:
        return False
    if current_priority != "high":
        return False
    if str(previous_priority).lower() == "high":
        return False

    current_count = count_high_priority_emails_for_sender(state_conn, normalized_sender)
    if current_count + 1 < threshold:
        return False

    note = f"auto-promoted after {current_count + 1} high-priority emails"
    if normalized_sender in get_vip_senders(state_conn):
        return False

    state_conn.execute(
        """
        INSERT INTO vip_senders (email, added_at, source, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
          added_at = excluded.added_at,
          source = excluded.source,
          note = excluded.note
        """,
        (normalized_sender, utc_now_iso(), VIP_SOURCE_AUTO, note),
    )
    return True


def get_state_row(conn: sqlite3.Connection, email_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM triage_state WHERE email_id = ?", (email_id,))
    return cur.fetchone()


def upsert_state_row(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO triage_state (
          email_id, subject, sender, sender_email, received_at,
          priority, actionable, reason, summary, reply_text,
          drafted, draft_id, status, error, raw_email,
          first_seen_at, last_seen_at, updated_at
        ) VALUES (
          :email_id, :subject, :sender, :sender_email, :received_at,
          :priority, :actionable, :reason, :summary, :reply_text,
          :drafted, :draft_id, :status, :error, :raw_email,
          :first_seen_at, :last_seen_at, :updated_at
        )
        ON CONFLICT(email_id) DO UPDATE SET
          subject=excluded.subject,
          sender=excluded.sender,
          sender_email=excluded.sender_email,
          received_at=excluded.received_at,
          priority=excluded.priority,
          actionable=excluded.actionable,
          reason=excluded.reason,
          summary=excluded.summary,
          reply_text=excluded.reply_text,
          drafted=excluded.drafted,
          draft_id=excluded.draft_id,
          status=excluded.status,
          error=excluded.error,
          raw_email=excluded.raw_email,
          last_seen_at=excluded.last_seen_at,
          updated_at=excluded.updated_at
        """,
        payload,
    )


def record_run(conn: sqlite3.Connection, summary: dict[str, Any], apply_mode: bool) -> None:
    conn.execute(
        """
        INSERT INTO triage_runs (
          run_at, mode, emails_seen, triaged_count, drafted_count,
          skipped_count, error_count, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            summary["run_at"],
            "apply" if apply_mode else "dry-run",
            summary["emails_seen"],
            summary["triaged_count"],
            summary["drafted_count"],
            summary["skipped_count"],
            summary["error_count"],
            json.dumps(summary, ensure_ascii=False),
        ),
    )


def classify_priority(
    email: dict[str, Any],
    config: dict[str, Any],
    vip_senders: set[str],
) -> tuple[str, bool, str, str]:
    triage_cfg = dict(config.get("triage") or {})
    urgent_keywords = [str(k).strip().lower() for k in triage_cfg.get("urgent_keywords", []) if str(k).strip()]

    sender_person = (email.get("from") or [{}])[0]
    sender_email = str((sender_person or {}).get("email") or "").strip().lower()
    sender_display = format_address(sender_person or {})

    subject = str(email.get("subject") or "")
    body = extract_text_content(email)
    combined = f"{subject}\n{body}".lower()

    reasons: list[str] = []

    is_vip = sender_email in vip_senders if sender_email else False
    if is_vip:
        reasons.append("VIP sender")

    sender_identities = configured_sender_identities(config)
    sent_to_sender_identity = email_targets_sender_identity(email, sender_identities)
    if sent_to_sender_identity:
        reasons.append("sent to configured sender address")

    keyword_hits = [kw for kw in urgent_keywords if kw and kw in combined]
    if keyword_hits:
        reasons.append("urgent keywords: " + ", ".join(keyword_hits[:3]))

    actionable = False
    if "?" in combined:
        actionable = True
    if any(re.search(pattern, combined) for pattern in ACTION_PATTERNS):
        actionable = True
    if actionable:
        reasons.append("contains request/question language")

    low_signal = False
    if sender_email and ("noreply" in sender_email or "no-reply" in sender_email or "notification" in sender_email):
        low_signal = True
    if any(re.search(pattern, combined) for pattern in LOW_SIGNAL_PATTERNS):
        low_signal = True
    if low_signal:
        reasons.append("low-signal/newsletter indicators")

    if is_vip or keyword_hits or sent_to_sender_identity:
        priority = "high"
    elif actionable and not low_signal:
        priority = "medium"
    else:
        priority = "low"

    summary = f"From {sender_display or sender_email or 'unknown sender'} about '{subject or '(no subject)'}'"
    reason = "; ".join(dict.fromkeys(reasons)) if reasons else "default low-priority classification"
    return priority, actionable, reason, summary


def compose_auto_reply(email: dict[str, Any], priority: str, config: dict[str, Any]) -> str:
    subject = str(email.get("subject") or "your message").strip()

    if priority == "high":
        first_line = f"Thanks for your email about \"{subject}\". I received this and I’m prioritizing it now."
        second_line = "I’ll follow up shortly with a full response."
    elif priority == "medium":
        first_line = f"Thanks for the note about \"{subject}\". I received it and will review it shortly."
        second_line = "I’ll send a full response after I’ve gone through the details."
    else:
        first_line = f"Thanks for sharing this update about \"{subject}\"."
        second_line = "I’ve received it and will follow up if anything is needed from my side."

    body = first_line + "\n\n" + second_line
    body = append_drafting_signature(body, config)
    return body


def append_drafting_signature(reply_text: str, config: dict[str, Any] | None) -> str:
    drafting = dict(config.get("drafting") or {}) if isinstance(config, dict) else {}
    signature = str(drafting.get("signature") or "").strip()
    if not signature:
        return reply_text

    normalized_reply = str(reply_text or "").rstrip()
    if not normalized_reply:
        return signature

    normalized_signature = signature.strip()
    body_without_signature = _strip_trailing_signature(normalized_reply)
    if body_without_signature.endswith(normalized_signature):
        return body_without_signature

    if body_without_signature == "":
        return normalized_signature
    return body_without_signature.rstrip() + "\n\n" + normalized_signature


def _strip_trailing_signature(text: str) -> str:
    lines = str(text).splitlines()
    if not lines:
        return text

    # Drop explicit signature separators used by AI/clients.
    for idx in range(len(lines) - 1, -1, -1):
        candidate = lines[idx].strip()
        if candidate == "":
            continue
        if candidate == "--" or re.match(r"^--\s*$", candidate):
            return "\n".join(lines[:idx]).rstrip()

    signature_markers = [
        "regards",
        "best",
        "sincerely",
        "thanks",
        "thank you",
        "cheers",
        "best regards",
        "kind regards",
        "with appreciation",
        "sent from",
        "best,",
        "regards,",
        "sincerely,",
        "thanks,",
        "thank you,",
        "cheers,",
    ]

    for idx in range(len(lines) - 1, -1, -1):
        lower = lines[idx].strip().lower()
        if any(lower.startswith(marker) for marker in signature_markers):
            while idx > 0 and lines[idx - 1].strip():
                idx -= 1
            return "\n".join(lines[:idx]).rstrip()

    return text


def build_email_payload_for_codex(email: dict[str, Any], max_body_chars: int) -> dict[str, Any]:
    sender = (email.get("from") or [{}])[0]
    to_list = email.get("to") or []
    cc_list = email.get("cc") or []
    body = extract_text_content(email)
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n\n[truncated]"

    return {
        "id": email.get("id"),
        "subject": email.get("subject") or "",
        "from": format_address(sender),
        "from_email": (sender or {}).get("email") or "",
        "to": [format_address(p) for p in to_list if isinstance(p, dict)],
        "cc": [format_address(p) for p in cc_list if isinstance(p, dict)],
        "received_at": email.get("receivedAt") or email.get("sentAt") or "",
        "preview": email.get("preview") or "",
        "body": body,
    }


def apply_codex_intelligence(
    *,
    email: dict[str, Any],
    config: dict[str, Any],
    automation: dict[str, Any],
    codex_client: CodexClient | None,
    rule_priority: str,
    rule_actionable: bool,
    rule_reason: str,
    rule_summary: str,
    rule_reply: str,
) -> tuple[str, bool, str, str, str, str]:
    if not codex_client:
        return (
            rule_priority,
            rule_actionable,
            f"[rules] {rule_reason}",
            rule_summary,
            append_drafting_signature(rule_reply, config),
            "rules",
        )

    payload = build_email_payload_for_codex(email, int(automation.get("codex_max_body_chars", 4000)))
    try:
        result = codex_client.triage_email(
            email_payload=payload,
            rule_priority=rule_priority,
            rule_actionable=rule_actionable,
            rule_reason=rule_reason,
            fallback_reply=rule_reply,
        )
        return (
            result["priority"],
            bool(result["actionable"]),
            f"[codex] {result['reason']}",
            str(result["summary"]),
            append_drafting_signature(str(result["reply_text"]), config),
            "codex",
        )
    except Exception as exc:  # noqa: BLE001
        if automation.get("codex_fallback_to_rules", True):
            return (
                rule_priority,
                rule_actionable,
                f"[rules-fallback] {rule_reason}; codex_error={exc}",
                rule_summary,
                append_drafting_signature(rule_reply, config),
                "rules_fallback",
            )
        raise


def _is_drafted_to_self(
    *,
    email: dict[str, Any],
    config: dict[str, Any],
    client: JMAPClient,
) -> bool:
    to_people = list(email.get("to") or [])
    if not to_people:
        return False

    identities = configured_sender_identities(config)

    if not identities:
        try:
            session = client.session()
            account = (session.get("accounts") or {}).get(client.account_id or "", {})
            account_email = str(
                (account or {}).get("email")
                or (account or {}).get("emailAddress")
                or ""
            ).strip().lower()
            if account_email and "@" in account_email:
                identities.add(account_email)
        except Exception:
            pass

    if not identities:
        return False

    return email_targets_sender_identity(email, identities, include_cc=False)


def should_create_draft(
    *,
    apply_mode: bool,
    automation: dict[str, Any],
    blocked_sender_emails: set[str],
    priority: str,
    actionable: bool,
    has_existing_draft: bool,
    sender_email: str,
    email: dict[str, Any],
    client: JMAPClient,
    config: dict[str, Any],
) -> bool:
    if not apply_mode:
        return False
    if not automation.get("auto_draft", True):
        return False
    if sender_email and sender_email in blocked_sender_emails:
        return False
    if has_existing_draft:
        return False
    if not _is_drafted_to_self(email=email, config=config, client=client):
        return False

    min_priority = str(automation.get("min_priority_for_draft", "high")).strip().lower()
    threshold = PRIORITY_ORDER.get(min_priority, PRIORITY_ORDER["high"])
    current = PRIORITY_ORDER.get(priority, PRIORITY_ORDER["low"])
    if current < threshold:
        return False

    if automation.get("draft_actionable_only", True) and not actionable:
        return False

    return True


def should_archive_priority(
    *,
    apply_mode: bool,
    automation: dict[str, Any],
    priority: str,
) -> bool:
    if not apply_mode:
        return False
    archive_priorities = {p.lower() for p in automation.get("auto_archive_priorities", [])}
    return str(priority).lower() in archive_priorities


def build_codex_client(config: dict[str, Any], automation: dict[str, Any]) -> Any | None:
    if not automation.get("use_codex", True):
        return None
    try:
        ai_settings = normalize_ai_settings(config)
        timeout = int(automation.get("codex_timeout_seconds", 60))
        if ai_settings.get("auth_mode") == "api_key":
            return CodexClient(ai_settings, timeout_seconds=timeout)
        return CodexSubscriptionClient(ai_settings, timeout_seconds=timeout)
    except Exception as exc:  # noqa: BLE001
        if automation.get("codex_fallback_to_rules", True):
            return None
        raise TriageRuntimeError(f"Codex initialization failed: {exc}") from exc


def process_one_cycle(
    *,
    client: JMAPClient,
    config: dict[str, Any],
    automation: dict[str, Any],
    state_conn: sqlite3.Connection,
    apply_mode: bool,
    limit_override: int | None,
    reprocess: bool,
) -> dict[str, Any]:
    codex_client = build_codex_client(config, automation)

    mailboxes = list_mailboxes(client)
    inbox_name = config["mail"].get("mailbox")
    inbox = find_mailbox(
        mailboxes,
        mailbox_name=inbox_name,
        role=mailbox_role_hint(inbox_name) or "inbox",
    )

    limit = max(1, int(limit_override or automation.get("max_emails_per_cycle", 20)))

    emails = query_emails(
        client,
        mailbox_id=inbox["id"],
        limit=limit,
        unread_only=True,
    )

    run_at = utc_now_iso()
    summary: dict[str, Any] = {
        "run_at": run_at,
        "apply_mode": apply_mode,
        "emails_seen": len(emails),
        "triaged_count": 0,
        "archived_count": 0,
        "drafted_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "emails": [],
    }
    vip_senders = get_vip_senders(state_conn)
    blocked_sender_emails = get_draft_blocked_senders(state_conn)

    for email in emails:
        email_id = str(email.get("id") or "")
        if not email_id:
            summary["error_count"] += 1
            continue

        now = utc_now_iso()
        existing = get_state_row(state_conn, email_id)
        existing_draft_id = existing["draft_id"] if (existing and existing["draft_id"]) else None
        has_existing_draft = bool(existing_draft_id)

        if has_existing_draft and not reprocess:
            state_conn.execute(
                "UPDATE triage_state SET last_seen_at = ?, updated_at = ? WHERE email_id = ?",
                (now, now, email_id),
            )
            summary["skipped_count"] += 1
            summary["emails"].append(
                {
                    "email_id": email_id,
                    "status": "skipped",
                    "reason": "already has draft",
                    "draft_id": existing["draft_id"],
                    "priority": existing["priority"] if existing else "unknown",
                }
            )
            continue

        rule_priority, rule_actionable, rule_reason, rule_summary = classify_priority(
            email,
            config,
            vip_senders=vip_senders,
        )
        rule_reply = compose_auto_reply(email, rule_priority, config)

        priority, actionable, reason, summary_line, reply_text, source = apply_codex_intelligence(
            email=email,
            config=config,
            automation=automation,
            codex_client=codex_client,
            rule_priority=rule_priority,
            rule_actionable=rule_actionable,
            rule_reason=rule_reason,
            rule_summary=rule_summary,
            rule_reply=rule_reply,
        )
        sender_email = str(((email.get("from") or [{}])[0] or {}).get("email") or "").strip().lower()
        auto_promoted_vip = maybe_auto_promote_vip_from_high_frequency(
            state_conn=state_conn,
            config=config,
            sender_email=sender_email,
            previous_priority=(existing["priority"] if existing else None),
            current_priority=priority,
        )

        status = "triaged"
        draft_id = existing_draft_id if (existing_draft_id and not reprocess) else None
        error_text = ""

        if should_archive_priority(
            apply_mode=apply_mode,
            automation=automation,
            priority=priority,
        ):
            try:
                move_email_to_archive(client, email_id)
                status = "archived"
                summary["archived_count"] += 1
            except Exception as exc:  # noqa: BLE001
                status = "error"
                error_text = str(exc)
                summary["error_count"] += 1
        elif should_create_draft(
            apply_mode=apply_mode,
            automation=automation,
            blocked_sender_emails=blocked_sender_emails,
            priority=priority,
            actionable=actionable,
            has_existing_draft=bool(existing_draft_id and not reprocess),
            email=email,
            sender_email=sender_email,
            client=client,
            config=config,
        ):
            try:
                draft_id = create_reply_draft_from_email(
                    client,
                    original_email=email,
                    reply_content=reply_text,
                    reply_all=bool(automation.get("reply_all", True)),
                )
                status = "drafted"
                summary["drafted_count"] += 1
            except Exception as exc:  # noqa: BLE001
                status = "error"
                error_text = str(exc)
                if existing_draft_id and not draft_id:
                    draft_id = existing_draft_id
                summary["error_count"] += 1

        if status != "error":
            summary["triaged_count"] += 1

        state_payload = {
            "email_id": email_id,
            "subject": str(email.get("subject") or ""),
            "sender": format_address((email.get("from") or [{}])[0]),
            "sender_email": sender_email,
            "received_at": str(email.get("receivedAt") or ""),
            "priority": priority,
            "actionable": 1 if actionable else 0,
            "reason": reason,
            "summary": summary_line,
            "reply_text": reply_text,
            "drafted": 1 if draft_id else 0,
            "draft_id": draft_id,
            "status": status,
            "error": error_text,
            "raw_email": json.dumps(email, ensure_ascii=False),
            "first_seen_at": existing["first_seen_at"] if existing else now,
            "last_seen_at": now,
            "updated_at": now,
        }
        upsert_state_row(state_conn, state_payload)

        summary["emails"].append(
            {
                "email_id": email_id,
                "priority": priority,
                "actionable": actionable,
                "status": status,
                "draft_id": draft_id,
                "reason": reason,
                "source": source,
                "sender_email": sender_email,
                "auto_promoted_vip": auto_promoted_vip,
            }
        )

    record_run(state_conn, summary, apply_mode)
    state_conn.commit()
    return summary


def print_summary(summary: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    mode = "APPLY" if summary["apply_mode"] else "DRY-RUN"
    print(
        f"[{mode}] {summary['run_at']} | seen={summary['emails_seen']} triaged={summary['triaged_count']} "
        f"archived={summary['archived_count']} drafted={summary['drafted_count']} "
        f"skipped={summary['skipped_count']} errors={summary['error_count']}"
    )

    archived_items = [e for e in summary["emails"] if e.get("status") == "archived"]
    if archived_items:
        print("Archived:")
        for item in archived_items:
            print(f"- {item['email_id']}")

    drafted_items = [e for e in summary["emails"] if e.get("draft_id")]
    if drafted_items:
        print("Drafts created/linked:")
        for item in drafted_items:
            priority = item.get("priority") or "unknown"
            source = item.get("source") or "unknown"
            print(f"- {item['email_id']} -> {item['draft_id']} ({priority}, {source})")

    auto_promoted_items = [e for e in summary["emails"] if e.get("auto_promoted_vip")]
    if auto_promoted_items:
        print("Auto-promoted VIP senders:")
        for item in auto_promoted_items:
            print(f"- {item['sender_email']}")


def handle_vip_commands(
    *,
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    as_json: bool,
) -> int:
    def dedupe(values: list[str]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for value in values:
            value = normalize_vip_address(value)
            if not value:
                continue
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    to_add = dedupe(split_address_values(args.vip_add))
    to_remove = dedupe(split_address_values(args.vip_remove))

    added: list[str] = []
    already_present: list[str] = []
    removed: list[str] = []
    not_present: list[str] = []
    invalid: list[str] = []

    for email in to_add:
        if add_vip_sender(conn, email):
            added.append(email)
        elif "@" in email:
            already_present.append(email)
        else:
            invalid.append(email)

    for email in to_remove:
        if remove_vip_sender(conn, email):
            removed.append(email)
        elif "@" in email:
            not_present.append(email)
        else:
            invalid.append(email)

    current = list_vip_senders(conn)
    conn.commit()

    if as_json:
        print(
            json.dumps(
                {
                    "added": added,
                    "already_present": already_present,
                    "removed": removed,
                    "not_present": not_present,
                    "invalid": invalid,
                    "vip_senders": current,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if added:
        print("added:", ", ".join(added))
    if already_present:
        print("already present:", ", ".join(already_present))
    if removed:
        print("removed:", ", ".join(removed))
    if not_present:
        print("not present:", ", ".join(not_present))
    if invalid:
        print("invalid:", ", ".join(invalid))

    if args.vip_list or to_add or to_remove:
        print("vip_senders:")
        if current:
            for email in current:
                print(f"- {email}")
        else:
            print("- none")

    return 0


def handle_draft_block_commands(
    *,
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    as_json: bool,
) -> int:
    def dedupe(values: list[str]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for value in values:
            value = normalize_vip_address(value)
            if not value:
                continue
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    to_add = dedupe(split_address_values(args.draft_block_add))
    to_remove = dedupe(split_address_values(args.draft_block_remove))

    added: list[str] = []
    already_present: list[str] = []
    removed: list[str] = []
    not_present: list[str] = []
    invalid: list[str] = []

    for email in to_add:
        if add_draft_blocked_sender(conn, email):
            added.append(email)
        elif "@" in email:
            already_present.append(email)
        else:
            invalid.append(email)

    for email in to_remove:
        if remove_draft_blocked_sender(conn, email):
            removed.append(email)
        elif "@" in email:
            not_present.append(email)
        else:
            invalid.append(email)

    current = list_draft_blocked_senders(conn)
    conn.commit()

    if as_json:
        print(
            json.dumps(
                {
                    "added": added,
                    "already_present": already_present,
                    "removed": removed,
                    "not_present": not_present,
                    "invalid": invalid,
                    "draft_blocked_senders": current,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if added:
        print("added:", ", ".join(added))
    if already_present:
        print("already present:", ", ".join(already_present))
    if removed:
        print("removed:", ", ".join(removed))
    if not_present:
        print("not present:", ", ".join(not_present))
    if invalid:
        print("invalid:", ", ".join(invalid))

    if args.draft_block_list or to_add or to_remove:
        print("draft_blocked_senders:")
        if current:
            for email in current:
                print(f"- {email}")
        else:
            print("- none")

    return 0


def main() -> int:
    args = parse_args()

    if args.vip_list or args.vip_add or args.vip_remove:
        state_db = Path(str(args.state_db or DEFAULT_STATE_DB)).expanduser()
        conn = open_state_db(state_db)
        try:
            return handle_vip_commands(conn=conn, args=args, as_json=args.json)
        finally:
            conn.close()

    if args.draft_block_list or args.draft_block_add or args.draft_block_remove:
        state_db = Path(str(args.state_db or DEFAULT_STATE_DB)).expanduser()
        conn = open_state_db(state_db)
        try:
            return handle_draft_block_commands(conn=conn, args=args, as_json=args.json)
        finally:
            conn.close()

    config, _ = load_config(args.config)
    automation = normalize_automation_settings(config)

    if args.state_db:
        automation["state_db"] = args.state_db

    if args.no_codex:
        automation["use_codex"] = False

    state_db = Path(str(automation.get("state_db", DEFAULT_STATE_DB))).expanduser()
    conn = open_state_db(state_db)
    if seed_vip_senders_from_config(conn, config):
        conn.commit()

    loop_seconds = args.loop_seconds
    if loop_seconds is None and args.cycles:
        loop_seconds = int(automation.get("loop_interval_seconds", 900))

    cycle_counter = 0
    while True:
        cycle_counter += 1
        try:
            client = JMAPClient(config)
            summary = process_one_cycle(
                client=client,
                config=config,
                automation=automation,
                state_conn=conn,
                apply_mode=args.apply,
                limit_override=args.limit,
                reprocess=args.reprocess,
            )
            print_summary(summary, args.json)
        except Exception as exc:  # noqa: BLE001
            state_conn_rollback_ok = True
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                state_conn_rollback_ok = False

            if args.json:
                print(
                    json.dumps(
                        {
                            "error": str(exc),
                            "cycle": cycle_counter,
                            "rolled_back": state_conn_rollback_ok,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                print(f"ERROR:{exc}")

            if not loop_seconds:
                if args.json:
                    conn.close()
                    return 1
                raise

        if not loop_seconds:
            break

        if args.cycles and cycle_counter >= args.cycles:
            break

        time.sleep(max(1, loop_seconds))

    conn.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JMAPError, FileNotFoundError, RuntimeError, TriageRuntimeError, CodexClientError) as exc:
        print(f"ERROR:{exc}")
        raise SystemExit(1)
