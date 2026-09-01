"""Credential-aware, read-only discovery for Superteam Earn opportunities."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .dispatcher import DispatchOutcome
from .research_worker import _local_text
from .scheduler import WorkItem
from .supervisor import utc_now
from .telemetry import atomic_json_write


_SAFE_TASK_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_ALLOWED_ACCESS = {"AGENT_ALLOWED", "AGENT_ONLY"}


class RevenueTaskError(RuntimeError):
    """A revenue-discovery task cannot be completed with its current input."""


class RevenueRetryableError(RevenueTaskError):
    """A temporary revenue-discovery failure should be retried."""


def _base_url(value: Any) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "superteam.fun"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Superteam base_url must be https://superteam.fun")
    return "https://superteam.fun"


def _listing_rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        raise RevenueTaskError("listing response shape is invalid")
    for key in ("listings", "results", "data", "items"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, Mapping)]
        if isinstance(candidate, Mapping) and isinstance(candidate.get("items"), list):
            return [item for item in candidate["items"] if isinstance(item, Mapping)]
    raise RevenueTaskError("listing response contains no listing array")


def _scalar_label(value: Any, *, max_len: int = 120) -> str:
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key in ("amount", "min", "max", "currency", "symbol", "label"):
            if value.get(key) is not None:
                parts.append(str(value[key]))
        value = " ".join(parts)
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value[:8])
    return _local_text(value, max_len=max_len)


class SuperteamOpportunityHandler:
    """Discover agent-eligible listings without submitting or claiming work."""

    def __init__(
        self,
        artifact_dir: Path,
        *,
        api_key_env: str = "SUPERTEAM_AGENT_API_KEY",
        base_url: str = "https://superteam.fun",
        timeout_s: float = 20.0,
        max_bytes: int = 512_000,
        max_listings: int = 50,
    ) -> None:
        if not _ENV_NAME.fullmatch(api_key_env):
            raise ValueError("api_key_env must be an uppercase environment variable name")
        if timeout_s <= 0:
            raise ValueError("revenue timeout_s must be positive")
        if max_bytes < 1024:
            raise ValueError("revenue max_bytes must be at least 1024")
        if max_listings < 1 or max_listings > 100:
            raise ValueError("revenue max_listings must be between 1 and 100")
        self.artifact_dir = Path(artifact_dir)
        self.api_key_env = api_key_env
        self.base_url = _base_url(base_url)
        self.timeout_s = float(timeout_s)
        self.max_bytes = int(max_bytes)
        self.max_listings = int(max_listings)

    def _endpoint(self, payload: Mapping[str, Any]) -> str:
        params: dict[str, str] = {}
        take = payload.get("take", 20)
        try:
            take_value = max(1, min(int(take), self.max_listings))
        except (TypeError, ValueError) as error:
            raise RevenueTaskError("take must be an integer") from error
        params["take"] = str(take_value)
        deadline = str(payload.get("deadline", "")).strip()
        if deadline:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
                raise RevenueTaskError("deadline must use YYYY-MM-DD")
            params["deadline"] = deadline
        listing_type = str(payload.get("type", "")).strip().lower()
        if listing_type:
            if listing_type not in {"bounty", "project", "hackathon"}:
                raise RevenueTaskError("type must be bounty, project, or hackathon")
            params["type"] = listing_type
        return self.base_url + "/api/agents/listings/live?" + urllib.parse.urlencode(params)

    def _artifact_path(self, task_id: str) -> Path:
        safe_name = _SAFE_TASK_ID.sub("_", task_id).strip("._")[:80] or "revenue-task"
        return self.artifact_dir / f"{safe_name}.json"

    def _write_artifact(
        self,
        item: WorkItem,
        *,
        status: str,
        listings: list[dict[str, Any]],
        error_category: str | None = None,
    ) -> None:
        artifact: dict[str, Any] = {
            "version": 1,
            "provider": "superteam",
            "task_id": _local_text(item.task_id, max_len=100),
            "objective_id": _local_text(item.objective_id, max_len=120),
            "completed_at": utc_now(),
            "status": status,
            "listings": listings,
        }
        if error_category:
            artifact["error_category"] = error_category
        atomic_json_write(self._artifact_path(item.task_id), artifact)

    def _safe_listing(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        listing_id = _local_text(
            row.get("id") or row.get("listingId") or row.get("slug"),
            max_len=120,
        )
        if not listing_id:
            return None
        access = _local_text(
            row.get("agentAccess") or row.get("agent_access") or row.get("access"),
            max_len=30,
        ).upper()
        questions = row.get("eligibilityQuestions") or row.get("eligibility_questions")
        question_count = len(questions) if isinstance(questions, list) else 0
        return {
            "listing_id": listing_id,
            "slug": _local_text(row.get("slug"), max_len=120),
            "title": _local_text(row.get("title") or row.get("name"), max_len=240),
            "type": _local_text(row.get("type") or row.get("listingType"), max_len=30),
            "agent_access": access,
            "agent_eligible": access in _ALLOWED_ACCESS,
            "reward": _scalar_label(
                row.get("reward") or row.get("compensation") or row.get("prize")
            ),
            "deadline": _local_text(row.get("deadline") or row.get("dueDate"), max_len=40),
            "eligibility_question_count": question_count,
        }

    def __call__(self, item: WorkItem) -> DispatchOutcome:
        payload = dict(item.payload)
        api_key = os.environ.get(self.api_key_env, "").strip()
        if not api_key:
            self._write_artifact(item, status="AUTHORITY_REQUIRED", listings=[], error_category="missing_api_key")
            return DispatchOutcome(
                status="ESCALATED",
                metrics={"authority_required": True, "listings_found": 0, "eligible_listings": 0},
                public_message="Authenticated discovery is waiting for the agent credential handoff.",
            )
        request = urllib.request.Request(
            self._endpoint(payload),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "MachineManager-superteam/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read(self.max_bytes + 1)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                self._write_artifact(
                    item,
                    status="AUTHORITY_REQUIRED",
                    listings=[],
                    error_category="service_rejected_credential",
                )
                return DispatchOutcome(
                    status="ESCALATED",
                    metrics={"authority_required": True, "listings_found": 0, "eligible_listings": 0},
                    public_message="The service rejected the agent credential; refresh the credential handoff.",
                )
            if error.code == 429 or error.code >= 500:
                raise RevenueRetryableError("Superteam discovery temporarily unavailable") from error
            raise RevenueTaskError("Superteam discovery returned an HTTP error") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RevenueRetryableError("Superteam discovery request failed") from error
        if len(raw) > self.max_bytes:
            raise RevenueTaskError("Superteam discovery response is too large")
        try:
            response_data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as error:
            raise RevenueTaskError("Superteam discovery response is not JSON") from error
        rows = _listing_rows(response_data)[: self.max_listings]
        listings = [safe for row in rows if (safe := self._safe_listing(row)) is not None]
        eligible = [item for item in listings if item["agent_eligible"]]
        self._write_artifact(item, status="COMPLETE", listings=listings)
        return DispatchOutcome(
            status="COMPLETE",
            metrics={
                "listings_found": len(listings),
                "eligible_listings": len(eligible),
                "agent_only_listings": sum(item["agent_access"] == "AGENT_ONLY" for item in eligible),
                "agent_allowed_listings": sum(item["agent_access"] == "AGENT_ALLOWED" for item in eligible),
                "authority_required": False,
            },
            public_message=(
                f"Authenticated discovery found {len(listings)} listing(s); "
                f"{len(eligible)} agent-eligible."
            ),
        )
