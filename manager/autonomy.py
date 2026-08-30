"""Operator mandate and respectful outreach primitives for Machine Manager.

The charter describes what Jaron has already authorized the system to pursue.
It deliberately keeps external-service handoffs concrete: a worker keeps its
job moving until a provider actually asks for a human-owned credential, 2FA
challenge, identity check, or payment connection.  That is a service boundary,
not a vague request to stop working.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from .state_store import StateStore


FIRST_CONTACT_DISCLOSURE = "Hello — I’m an AI assistant acting on Jaron’s behalf."


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _enabled(value: Mapping[str, Any], key: str, default: bool) -> bool:
    raw = value.get(key, default)
    return raw if isinstance(raw, bool) else default


@dataclass(frozen=True)
class OperatingCharter:
    """The active mandate for capable workers and integrations."""

    mode: str = "EXECUTE_AND_REPORT"
    allow_account_enrollment: bool = True
    allow_public_submissions: bool = True
    allow_outreach: bool = True
    honor_outreach_opt_out: bool = True
    allow_paid_work: bool = True
    allow_procurement_when_funded: bool = True
    allow_tool_installation: bool = True
    allow_gpu_when_protected_worker_idle: bool = True
    respect_program_terms: bool = True
    first_contact_disclosure: str = FIRST_CONTACT_DISCLOSURE

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None = None) -> "OperatingCharter":
        value = {} if value is None else value
        if not isinstance(value, Mapping):
            raise ValueError("config.autonomy must be an object")
        disclosure = str(value.get("first_contact_disclosure", FIRST_CONTACT_DISCLOSURE)).strip()
        return cls(
            mode=str(value.get("mode", "EXECUTE_AND_REPORT")).strip().upper() or "EXECUTE_AND_REPORT",
            allow_account_enrollment=_enabled(value, "allow_account_enrollment", True),
            allow_public_submissions=_enabled(value, "allow_public_submissions", True),
            allow_outreach=_enabled(value, "allow_outreach", True),
            honor_outreach_opt_out=_enabled(value, "honor_outreach_opt_out", True),
            allow_paid_work=_enabled(value, "allow_paid_work", True),
            allow_procurement_when_funded=_enabled(value, "allow_procurement_when_funded", True),
            allow_tool_installation=_enabled(value, "allow_tool_installation", True),
            allow_gpu_when_protected_worker_idle=_enabled(
                value,
                "allow_gpu_when_protected_worker_idle",
                True,
            ),
            respect_program_terms=_enabled(value, "respect_program_terms", True),
            first_contact_disclosure=disclosure or FIRST_CONTACT_DISCLOSURE,
        )

    def public_summary(self) -> dict[str, str | bool]:
        """Return only the public-safe execution mandate, never credentials."""
        return {
            "mode": self.mode,
            "account_enrollment": self.allow_account_enrollment,
            "public_submissions": self.allow_public_submissions,
            "transparent_outreach": self.allow_outreach,
            "outreach_opt_out": self.honor_outreach_opt_out,
            "paid_work": self.allow_paid_work,
            "procurement_when_funded": self.allow_procurement_when_funded,
            "developer_tools": self.allow_tool_installation,
            "gpu_idle_use": self.allow_gpu_when_protected_worker_idle,
            "terms_aware_execution": self.respect_program_terms,
            "handoff_style": "service-required",
        }


class OutreachBlockedError(RuntimeError):
    """Raised when a recipient has opted out of future contact."""


@dataclass(frozen=True)
class OutreachDraft:
    """A local-only prepared message; it contains no public telemetry fields."""

    channel: str
    recipient_hash: str
    content: str
    first_contact: bool


class OutreachRegistry:
    """Keep transparent first contact and opt-out handling durable and local."""

    def __init__(self, store: StateStore, charter: OperatingCharter) -> None:
        self.store = store
        self.charter = charter

    @staticmethod
    def recipient_hash(channel: str, recipient: str) -> str:
        normalized_channel = str(channel).strip().lower()
        normalized_recipient = str(recipient).strip().lower()
        if not normalized_channel or not normalized_recipient:
            raise ValueError("outreach channel and recipient are required")
        material = f"{normalized_channel}\0{normalized_recipient}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def prepare(self, *, channel: str, recipient: str, message: str) -> OutreachDraft:
        if not self.charter.allow_outreach:
            raise OutreachBlockedError("outreach is disabled by the operating charter")
        recipient_hash = self.recipient_hash(channel, recipient)
        record = self.store.get_outreach_contact(channel=str(channel), recipient_hash=recipient_hash)
        if self.charter.honor_outreach_opt_out and record and record.get("state") == "DO_NOT_CONTACT":
            raise OutreachBlockedError("recipient opted out of future outreach")
        first_contact = not record or not record.get("first_contacted_at")
        content = str(message).strip()
        if not content:
            raise ValueError("outreach message is required")
        if first_contact:
            content = f"{self.charter.first_contact_disclosure}\n\n{content}"
        return OutreachDraft(
            channel=str(channel).strip().lower(),
            recipient_hash=recipient_hash,
            content=content,
            first_contact=first_contact,
        )

    def record_sent(self, draft: OutreachDraft, *, timestamp: str | None = None) -> None:
        timestamp = timestamp or utc_now()
        existing = self.store.get_outreach_contact(
            channel=draft.channel,
            recipient_hash=draft.recipient_hash,
        ) or {}
        self.store.upsert_outreach_contact(
            channel=draft.channel,
            recipient_hash=draft.recipient_hash,
            state="ACTIVE",
            first_contacted_at=str(existing.get("first_contacted_at") or timestamp),
            last_contacted_at=timestamp,
            updated=timestamp,
        )

    def record_opt_out(self, *, channel: str, recipient: str, timestamp: str | None = None) -> None:
        timestamp = timestamp or utc_now()
        recipient_hash = self.recipient_hash(channel, recipient)
        existing = self.store.get_outreach_contact(
            channel=str(channel).strip().lower(),
            recipient_hash=recipient_hash,
        ) or {}
        self.store.upsert_outreach_contact(
            channel=str(channel).strip().lower(),
            recipient_hash=recipient_hash,
            state="DO_NOT_CONTACT",
            first_contacted_at=existing.get("first_contacted_at"),
            last_contacted_at=existing.get("last_contacted_at"),
            updated=timestamp,
        )
