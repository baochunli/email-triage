#!/usr/bin/env python3
"""Shared helpers for Fastmail JMAP scripts."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"


class JMAPError(RuntimeError):
    """Raised when a JMAP call fails."""


def load_config(config_path: str | None = None) -> tuple[dict[str, Any], Path]:
    """Load config from explicit path/env/default locations."""
    candidates: list[Path] = []

    if config_path:
        candidates.append(Path(config_path).expanduser())

    env_path = os.environ.get("EMAIL_TRIAGE_CONFIG")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    candidates.extend(
        [
            Path("~/.config/email-triage/config.yaml").expanduser(),
            Path("~/.config/email-triage/config.yml").expanduser(),
            Path("~/.config/email-triage/config.json").expanduser(),
            Path("~/.config/email-manager/config.yaml").expanduser(),
            Path("~/.config/email-manager/config.yml").expanduser(),
            Path("~/.config/email-manager/config.json").expanduser(),
        ]
    )

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.exists():
            continue
        loaded = _parse_config_file(candidate)
        normalized = _normalize_config(loaded)
        return normalized, candidate

    expected = "\n- ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        "Config file not found. Searched:\n- "
        + expected
        + "\nSet EMAIL_TRIAGE_CONFIG or pass --config."
    )


def _parse_config_file(path: Path) -> dict[str, Any]:
    lower_name = path.name.lower()
    with path.open("r", encoding="utf-8") as fh:
        raw = fh.read()

    is_json = lower_name.endswith(".json") or lower_name.endswith(".json.example")
    is_yaml = (
        lower_name.endswith(".yml")
        or lower_name.endswith(".yaml")
        or lower_name.endswith(".yml.example")
        or lower_name.endswith(".yaml.example")
    )

    if is_json:
        loaded = json.loads(raw)
    elif is_yaml:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PyYAML is required for YAML config. Install with: pip install pyyaml"
            ) from exc
        loaded = yaml.safe_load(raw)
    else:
        raise RuntimeError(f"Unsupported config format: {path}")

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Config root must be an object/map: {path}")
    return loaded


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)

    fastmail = dict(normalized.get("fastmail") or {})
    token = fastmail.get("api_token") or os.environ.get("FASTMAIL_API_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing Fastmail API token. Set fastmail.api_token in config "
            "or FASTMAIL_API_TOKEN in environment."
        )

    fastmail.setdefault("session_url", "https://api.fastmail.com/jmap/session")
    fastmail["api_token"] = token
    normalized["fastmail"] = fastmail

    mail = dict(normalized.get("mail") or {})
    mail.setdefault("account", "Fastmail")
    mail.setdefault("mailbox", "INBOX")
    mail.setdefault("sent_mailbox", "Sent")
    mail.setdefault("drafts_mailbox", "Drafts")
    mail.setdefault("trash_mailbox", "Trash")
    mail.setdefault("archive_mailbox", "Archive")
    normalized["mail"] = mail

    return normalized


class JMAPClient:
    """Minimal JMAP client for Fastmail."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.fastmail = config["fastmail"]
        self.mail = config["mail"]
        self.token = self.fastmail["api_token"]
        self._session: dict[str, Any] | None = None
        self.api_url: str | None = self.fastmail.get("api_url")
        self.account_id: str | None = self.fastmail.get("account_id")

    def session(self) -> dict[str, Any]:
        if self._session is not None:
            return self._session

        response = self._http_json(
            method="GET",
            url=self.fastmail["session_url"],
            payload=None,
        )

        if not isinstance(response, dict):
            raise JMAPError("Session response was not a JSON object")

        self._session = response
        if not self.api_url:
            self.api_url = response.get("apiUrl")
        if not self.api_url:
            raise JMAPError("No apiUrl found in session response")

        if not self.account_id:
            primary = (response.get("primaryAccounts") or {}).get(MAIL_CAPABILITY)
            if primary:
                self.account_id = primary
            else:
                accounts = response.get("accounts") or {}
                if accounts:
                    self.account_id = next(iter(accounts.keys()))

        if not self.account_id:
            raise JMAPError("No usable accountId found in session response")

        return response

    def call(
        self,
        method_calls: list[list[Any]],
        using: list[str] | None = None,
    ) -> dict[str, Any]:
        self.session()
        payload = {
            "using": using or [CORE_CAPABILITY, MAIL_CAPABILITY],
            "methodCalls": method_calls,
        }
        response = self._http_json(
            method="POST",
            url=self.api_url,
            payload=payload,
        )

        if not isinstance(response, dict):
            raise JMAPError("JMAP response was not a JSON object")

        for method_name, method_result, call_id in response.get("methodResponses", []):
            if method_name == "error":
                err_type = (method_result or {}).get("type", "unknown")
                description = (method_result or {}).get("description", "")
                raise JMAPError(f"JMAP error ({call_id}): {err_type} {description}".strip())

        return response

    def _http_json(self, method: str, url: str | None, payload: dict[str, Any] | None) -> Any:
        if not url:
            raise JMAPError("URL is missing")

        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        req = request.Request(
            url=url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with request.urlopen(req) as res:
                raw = res.read().decode("utf-8")
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise JMAPError(f"HTTP {exc.code}: {body_text}") from exc
        except error.URLError as exc:
            raise JMAPError(f"Network error: {exc.reason}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JMAPError(f"Invalid JSON response: {raw[:500]}") from exc


def get_call(response: dict[str, Any], call_id: str) -> tuple[str, dict[str, Any]]:
    for method_name, method_result, cid in response.get("methodResponses", []):
        if cid == call_id:
            if not isinstance(method_result, dict):
                raise JMAPError(f"Unexpected response payload type for {call_id}")
            if method_name == "error":
                err_type = method_result.get("type", "unknown")
                description = method_result.get("description", "")
                raise JMAPError(f"{err_type}: {description}".strip())
            return method_name, method_result
    raise JMAPError(f"Missing call response for {call_id}")


def list_mailboxes(client: JMAPClient) -> list[dict[str, Any]]:
    account_id = client.account_id or client.session().get("primaryAccounts", {}).get(MAIL_CAPABILITY)
    if not account_id:
        raise JMAPError("No accountId available")

    response = client.call(
        method_calls=[
            [
                "Mailbox/query",
                {
                    "accountId": account_id,
                    "sort": [{"property": "name", "isAscending": True}],
                },
                "mbq",
            ],
            [
                "Mailbox/get",
                {
                    "accountId": account_id,
                    "#ids": {"resultOf": "mbq", "name": "Mailbox/query", "path": "/ids"},
                    "properties": [
                        "id",
                        "name",
                        "role",
                        "parentId",
                        "totalEmails",
                        "unreadEmails",
                    ],
                },
                "mbg",
            ],
        ]
    )

    _, mbg = get_call(response, "mbg")
    return list(mbg.get("list") or [])


def find_mailbox(
    mailboxes: list[dict[str, Any]],
    *,
    mailbox_name: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    if role:
        for mailbox in mailboxes:
            if (mailbox.get("role") or "").lower() == role.lower():
                return mailbox

    if mailbox_name:
        wanted = mailbox_name.strip().lower()
        for mailbox in mailboxes:
            if (mailbox.get("name") or "").strip().lower() == wanted:
                return mailbox

    raise JMAPError(
        f"Mailbox not found (name={mailbox_name!r}, role={role!r})"
    )


def mailbox_role_hint(mailbox_name: str | None) -> str | None:
    if not mailbox_name:
        return None
    normalized = mailbox_name.strip().lower()
    hints = {
        "inbox": "inbox",
        "sent": "sent",
        "sent messages": "sent",
        "drafts": "drafts",
        "trash": "trash",
        "deleted": "trash",
        "junk": "junk",
        "spam": "junk",
        "archive": "archive",
    }
    return hints.get(normalized)


def escape_field(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|")
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    return text.replace("\n", "\\n")


def format_address(person: dict[str, Any]) -> str:
    name = (person.get("name") or "").strip()
    email = (person.get("email") or "").strip()
    if name and email:
        return f"{name} <{email}>"
    return email or name


def format_address_list(people: list[dict[str, Any]] | None) -> str:
    if not people:
        return ""
    return "; ".join(format_address(p) for p in people if isinstance(p, dict))


def extract_text_content(email_obj: dict[str, Any]) -> str:
    body_values = email_obj.get("bodyValues") or {}
    text_parts = email_obj.get("textBody") or []

    chunks: list[str] = []
    for part in text_parts:
        part_id = (part or {}).get("partId")
        if not part_id:
            continue
        value_obj = body_values.get(part_id) or {}
        value = value_obj.get("value")
        if value:
            chunks.append(str(value))

    if chunks:
        return "\n\n".join(chunks).strip()

    if body_values:
        first = next(iter(body_values.values()))
        if isinstance(first, dict):
            value = first.get("value")
            if value:
                return str(value).strip()

    preview = email_obj.get("preview")
    return str(preview or "").strip()


def ensure_reply_subject(subject: str | None) -> str:
    cleaned = (subject or "").strip()
    if cleaned.lower().startswith("re:"):
        return cleaned
    if not cleaned:
        return "Re:"
    return f"Re: {cleaned}"


def quote_lines(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines() or [text]
    return "\n".join(f"> {line}" for line in lines)


def parse_csv_addresses(raw: str) -> list[dict[str, str]]:
    addresses: list[dict[str, str]] = []
    for chunk in raw.split(","):
        email = chunk.strip()
        if email:
            addresses.append({"email": email})
    return addresses


def create_reply_draft_from_email(
    client: JMAPClient,
    *,
    original_email: dict[str, Any],
    reply_content: str,
    reply_all: bool = True,
) -> str:
    """Create a reply draft from an existing email object."""
    orig_from = list(original_email.get("from") or [])
    if not orig_from:
        raise JMAPError("Original message has no sender")

    to_recipients = [orig_from[0]]

    cc_recipients: list[dict[str, str]] = []
    if reply_all:
        seen = {((orig_from[0].get("email") or "").strip().lower())}
        own = _resolve_primary_sender_email(client)
        if not own:
            own = str((client.session().get("accounts") or {}).get(client.account_id or "", {}).get("name") or "").strip().lower()
        if own:
            seen.add(own)

        for person in list(original_email.get("to") or []) + list(original_email.get("cc") or []):
            email = (person.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            cc_recipients.append({"email": person.get("email"), "name": person.get("name")})

    subject = ensure_reply_subject(original_email.get("subject"))
    sender_display = format_address(orig_from[0])
    original_date = original_email.get("receivedAt") or original_email.get("sentAt") or ""
    original_body = extract_text_content(original_email)
    quote_header = f"On {original_date}, {sender_display} wrote:"
    full_body = f"{reply_content}\n\n{quote_header}\n\n{quote_lines(original_body)}"

    message_id = original_email.get("messageId")
    refs = list(original_email.get("references") or [])

    in_reply_to: list[str] = []
    if isinstance(message_id, str) and message_id:
        in_reply_to = [message_id]
    elif isinstance(message_id, list):
        in_reply_to = [m for m in message_id if isinstance(m, str) and m]

    if in_reply_to:
        for msgid in in_reply_to:
            if msgid not in refs:
                refs.append(msgid)

    return create_draft(
        client,
        to=to_recipients,
        cc=cc_recipients,
        subject=subject,
        body=full_body,
        in_reply_to=in_reply_to or None,
        references=refs or None,
    )


def compute_after_iso(days_back: int) -> str:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)
    return cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def query_emails(
    client: JMAPClient,
    *,
    mailbox_id: str,
    limit: int,
    unread_only: bool = False,
    days_back: int = 0,
) -> list[dict[str, Any]]:
    filter_obj: dict[str, Any] = {"inMailbox": mailbox_id}
    if unread_only:
        filter_obj["notKeyword"] = "$seen"
    if days_back > 0:
        filter_obj["after"] = compute_after_iso(days_back)

    response = client.call(
        method_calls=[
            [
                "Email/query",
                {
                    "accountId": client.account_id,
                    "filter": filter_obj,
                    "sort": [{"property": "receivedAt", "isAscending": False}],
                    "position": 0,
                    "limit": max(1, limit),
                },
                "eq",
            ],
            [
                "Email/get",
                {
                    "accountId": client.account_id,
                    "#ids": {"resultOf": "eq", "name": "Email/query", "path": "/ids"},
                    "properties": [
                        "id",
                        "subject",
                        "from",
                        "to",
                        "cc",
                        "receivedAt",
                        "sentAt",
                        "preview",
                        "textBody",
                        "bodyValues",
                        "keywords",
                        "messageId",
                    ],
                    "fetchTextBodyValues": True,
                    "maxBodyValueBytes": 120000,
                },
                "eg",
            ],
        ]
    )
    _, eg = get_call(response, "eg")
    return list(eg.get("list") or [])


def get_email_by_id(client: JMAPClient, email_id: str) -> dict[str, Any]:
    response = client.call(
        method_calls=[
            [
                "Email/get",
                {
                    "accountId": client.account_id,
                    "ids": [email_id],
                    "properties": [
                        "id",
                        "subject",
                        "from",
                        "to",
                        "cc",
                        "receivedAt",
                        "sentAt",
                        "preview",
                        "textBody",
                        "bodyValues",
                        "keywords",
                        "messageId",
                        "references",
                        "mailboxIds",
                    ],
                    "fetchTextBodyValues": True,
                    "maxBodyValueBytes": 120000,
                },
                "eg",
            ]
        ]
    )
    _, eg = get_call(response, "eg")
    listed = list(eg.get("list") or [])
    if not listed:
        raise JMAPError(f"Message not found with ID {email_id}")
    return listed[0]


def create_draft(
    client: JMAPClient,
    *,
    to: list[dict[str, str]],
    subject: str,
    body: str,
    cc: list[dict[str, str]] | None = None,
    in_reply_to: list[str] | None = None,
    references: list[str] | None = None,
) -> str:
    mailboxes = list_mailboxes(client)
    drafts_name = client.mail.get("drafts_mailbox")
    drafts_role = mailbox_role_hint(drafts_name) or "drafts"
    drafts_box = find_mailbox(
        mailboxes,
        mailbox_name=drafts_name,
        role=drafts_role,
    )

    email_obj: dict[str, Any] = {
        "mailboxIds": {drafts_box["id"]: True},
        "keywords": {"$draft": True},
        "to": to,
        "subject": subject,
        "textBody": [{"partId": "1", "type": "text/plain"}],
        "bodyValues": {"1": {"value": body}},
    }

    if cc:
        email_obj["cc"] = cc

    sender_email = _resolve_primary_sender_email(client)
    sender_name = (client.mail.get("sender_name") or "").strip()
    if sender_email:
        from_entry: dict[str, str] = {"email": sender_email}
        if sender_name:
            from_entry["name"] = sender_name
        email_obj["from"] = [from_entry]

    if in_reply_to:
        email_obj["inReplyTo"] = in_reply_to
    if references:
        email_obj["references"] = references

    response = client.call(
        method_calls=[
            [
                "Email/set",
                {
                    "accountId": client.account_id,
                    "create": {"draft-1": email_obj},
                },
                "es",
            ]
        ]
    )
    _, es = get_call(response, "es")

    not_created = es.get("notCreated") or {}
    if "draft-1" in not_created:
        reason = not_created["draft-1"].get("description") or not_created["draft-1"].get("type")
        raise JMAPError(f"Draft create failed: {reason}")

    created = (es.get("created") or {}).get("draft-1") or {}
    draft_id = created.get("id")
    if not draft_id:
        raise JMAPError("Draft created but no id returned")

    return str(draft_id)


def _resolve_primary_sender_email(client: JMAPClient) -> str:
    configured_sender_email = str((client.mail or {}).get("sender_email") or "").strip().lower()
    if configured_sender_email:
        return configured_sender_email

    try:
        session = client.session()
        account = (session.get("accounts") or {}).get(client.account_id or "", {})
        if not account:
            return ""

        candidates: list[Any] = []
        for key in ("email", "emailAddress", "email_address", "address"):
            value = account.get(key)
            if value:
                candidates.append(value)

        addresses = account.get("emailAddresses") or account.get("addresses") or []
        if addresses:
            candidates.append(addresses)

        for value in candidates:
            for email in _extract_email_values(value):
                if "@" in email:
                    return email.lower()
    except Exception:
        pass
    return ""


def _extract_email_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [v.strip().lower() for v in value.replace(";", ",").split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_extract_email_values(item))
        return out
    if isinstance(value, dict):
        values = []
        for key in ("email", "address", "mail"):
            if key in value:
                values.extend(_extract_email_values(value[key]))
        return values
    return []


def move_email_to_trash(client: JMAPClient, email_id: str) -> dict[str, Any]:
    return move_email_to_mailbox(
        client=client,
        email_id=email_id,
        mailbox_name=(client.mail.get("trash_mailbox") or "Trash"),
        mailbox_role_hint_key="trash",
    )


def move_email_to_archive(client: JMAPClient, email_id: str) -> dict[str, Any]:
    return move_email_to_mailbox(
        client=client,
        email_id=email_id,
        mailbox_name=(client.mail.get("archive_mailbox") or "Archive"),
        mailbox_role_hint_key="archive",
    )


def move_email_to_mailbox(
    client: JMAPClient,
    email_id: str,
    mailbox_name: str,
    mailbox_role_hint_key: str,
) -> dict[str, Any]:
    email_obj = get_email_by_id(client, email_id)

    mailboxes = list_mailboxes(client)
    mailbox_box = find_mailbox(
        mailboxes,
        mailbox_name=mailbox_name,
        role=mailbox_role_hint(mailbox_name) or mailbox_role_hint_key,
    )

    response = client.call(
        method_calls=[
            [
                "Email/set",
                {
                    "accountId": client.account_id,
                    "update": {
                        email_id: {
                            "mailboxIds": {mailbox_box["id"]: True},
                        }
                    },
                },
                "es",
            ]
        ]
    )
    _, es = get_call(response, "es")

    not_updated = es.get("notUpdated") or {}
    if email_id in not_updated:
        reason = not_updated[email_id].get("description") or not_updated[email_id].get("type")
        raise JMAPError(f"Move failed: {reason}")

    return email_obj
