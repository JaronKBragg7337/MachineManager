"""Evidence-backed worker profiles and non-destructive constraint audits.

Machine Manager should learn from what a worker actually did, rather than
permanently encoding an assumption about what a provider or model might do.
This module keeps two distinct local records:

* worker capability profiles, which state what a particular model/runtime was
  tested to do or was observed doing; and
* constraint-audit candidates, which point out wording worth reviewing without
  changing a project or declaring the wording redundant.

Only compact summaries are intended for public telemetry.  File paths,
excerpts, and other audit evidence remain in the local SQLite store.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .state_store import StateStore


OBSERVATION_STATES = {
    "TESTED_PASS",
    "TESTED_FAIL",
    "OBSERVED",
    "UNKNOWN",
    "UNAVAILABLE",
}

TEXT_FILE_SUFFIXES = {
    ".bat",
    ".c",
    ".cc",
    ".cfg",
    ".cmake",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonc",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "logs",
    "node_modules",
    "secrets",
    "var",
    "vendor",
    "venv",
}
EXCLUDED_FILENAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}
SENSITIVE_CONTENT = re.compile(
    r"(?i)(?:github_pat_|ghp_[A-Za-z0-9_]+|(?:token|secret|password|credential|api[ _-]?key)\s*[:=])"
)
AUDIT_ALGORITHM_VERSION = "2"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _identifier(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    if len(normalized) > 100:
        raise ValueError(f"{field} is too long")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", normalized):
        raise ValueError(f"{field} must use letters, numbers, dots, dashes, underscores, or colons")
    return normalized


def _safe_text(value: Any, *, max_len: int = 180) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())[:max_len]
    return "[redacted]" if SENSITIVE_CONTENT.search(text) else text


def _parse_timestamp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class CapabilityObservation:
    """A compact statement about behavior actually tested or observed."""

    capability_id: str
    status: str
    summary: str = ""
    evidence_id: str = ""
    observed_at: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CapabilityObservation":
        capability_id = _identifier(value.get("id", value.get("capability_id")), "capability id")
        status = str(value.get("status", "UNKNOWN")).strip().upper() or "UNKNOWN"
        if status not in OBSERVATION_STATES:
            raise ValueError(f"unsupported capability observation status: {status}")
        evidence_id = str(value.get("evidence_id", "")).strip()
        if evidence_id:
            evidence_id = _identifier(evidence_id, "evidence id")
        return cls(
            capability_id=capability_id,
            status=status,
            summary=_safe_text(value.get("summary"), max_len=180),
            evidence_id=evidence_id,
            observed_at=_safe_text(value.get("observed_at"), max_len=40),
        )

    def public_record(self) -> dict[str, str]:
        return {
            "id": self.capability_id,
            "status": self.status,
            "summary": self.summary,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class WorkerProfile:
    """A model/runtime profile whose current validity follows its version."""

    worker_id: str
    provider: str
    model: str
    model_version: str
    verified_model_version: str
    last_verified: str
    capabilities: tuple[CapabilityObservation, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkerProfile":
        worker_id = _identifier(value.get("id", value.get("worker_id")), "worker profile id")
        provider = _safe_text(value.get("provider", "unknown"), max_len=60) or "unknown"
        model = _safe_text(value.get("model", "unknown"), max_len=100) or "unknown"
        model_version = _safe_text(value.get("model_version", value.get("version", "unknown")), max_len=100)
        verified_model_version = _safe_text(
            value.get("verified_model_version", value.get("verified_version", model_version)),
            max_len=100,
        )
        raw_capabilities = value.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            raise ValueError("worker profile capabilities must be an array")
        capabilities = tuple(
            CapabilityObservation.from_mapping(item)
            for item in raw_capabilities
            if isinstance(item, Mapping)
        )
        seen = [item.capability_id for item in capabilities]
        if len(seen) != len(set(seen)):
            raise ValueError(f"worker profile {worker_id} contains duplicate capability ids")
        return cls(
            worker_id=worker_id,
            provider=provider,
            model=model,
            model_version=model_version or "unknown",
            verified_model_version=verified_model_version or "unknown",
            last_verified=_safe_text(value.get("last_verified"), max_len=40),
            capabilities=capabilities,
        )

    @property
    def fingerprint(self) -> str:
        material = "\0".join((self.provider, self.model, self.model_version)).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @property
    def retest_required(self) -> bool:
        return self.model_version != self.verified_model_version

    def local_record(self) -> dict[str, Any]:
        return {
            "id": self.worker_id,
            "provider": self.provider,
            "model": self.model,
            "model_version": self.model_version,
            "verified_model_version": self.verified_model_version,
            "fingerprint": self.fingerprint,
            "state": "RETEST_REQUIRED" if self.retest_required else "READY",
            "retest_required": self.retest_required,
            "last_verified": self.last_verified,
            "updated": utc_now(),
            "capabilities": [item.public_record() for item in self.capabilities],
        }


@dataclass(frozen=True)
class AuditTarget:
    """A local, explicitly named source tree that may be reviewed."""

    target_id: str
    label: str
    root: Path
    enabled: bool = True
    max_files: int = 1200
    max_file_bytes: int = 512_000
    max_findings: int = 250
    interval_s: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, base: Path) -> "AuditTarget":
        target_id = _identifier(value.get("id", value.get("target_id")), "audit target id")
        raw_path = str(value.get("path", "")).strip()
        if not raw_path:
            raise ValueError(f"audit target {target_id} requires a path")
        root = Path(os.path.expandvars(raw_path))
        if not root.is_absolute():
            root = base / root
        raw_interval = value.get("interval_s")
        interval_s = (
            None
            if raw_interval is None
            else max(60.0, min(float(raw_interval), 604_800.0))
        )
        return cls(
            target_id=target_id,
            label=_safe_text(value.get("label", target_id), max_len=100) or target_id,
            root=root.resolve(),
            enabled=bool(value.get("enabled", True)),
            max_files=max(1, min(int(value.get("max_files", 1200)), 10_000)),
            max_file_bytes=max(1_000, min(int(value.get("max_file_bytes", 512_000)), 5_000_000)),
            max_findings=max(1, min(int(value.get("max_findings", 250)), 2_000)),
            interval_s=interval_s,
        )

    @property
    def scan_signature(self) -> str:
        """Identify a scan strategy without storing a local path in telemetry."""
        material = json.dumps(
            {
                "algorithm": AUDIT_ALGORITHM_VERSION,
                "max_file_bytes": self.max_file_bytes,
                "max_files": self.max_files,
                "max_findings": self.max_findings,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class ConstraintRule:
    rule_id: str
    category: str
    description: str
    pattern: re.Pattern[str]


CONSTRAINT_RULES = (
    ConstraintRule(
        "approval-gate",
        "approval_gate",
        "Language that may require a human approval or confirmation step.",
        re.compile(
            r"\b(?:must|need to|should|requires?)\s+(?:ask|request|obtain|wait for)\s+(?:the\s+)?(?:user|operator|human|approval|confirmation|permission)\b",
            re.IGNORECASE,
        ),
    ),
    ConstraintRule(
        "manual-workflow",
        "approval_gate",
        "Language that may make a workflow manual by default.",
        re.compile(
            r"\b(?:manual(?:ly)?|human[- ]in[- ]the[- ]loop)\b.*\b(?:only|required|approval|confirm)\b",
            re.IGNORECASE,
        ),
    ),
    ConstraintRule(
        "autonomy-prohibition",
        "autonomy_limit",
        "Language that may prohibit an otherwise delegated automated action.",
        re.compile(
            r"\b(?:never|do not|must not|should not)\b.*\b(?:automatically|autonomously|without approval|publish|commit|deploy|install)\b",
            re.IGNORECASE,
        ),
    ),
    ConstraintRule(
        "scope-boundary",
        "scope_boundary",
        "Language that may define an authorized service, target, or technique boundary.",
        re.compile(
            r"\b(?:only\s+(?:allowed|permitted)|not authorized|out of scope|authorized scope|within scope)\b",
            re.IGNORECASE,
        ),
    ),
    ConstraintRule(
        "sensitive-data-boundary",
        "data_boundary",
        "Language that may protect credentials or sensitive material.",
        re.compile(
            r"\b(?:never|do not|must not)\b.*\b(?:token|secret|password|credential|private key|seed)\b",
            re.IGNORECASE,
        ),
    ),
)


# These are evidence prompts, not permissions or automatic decisions.  Keep
# them deterministic so an audit can resume after a restart and the public
# dashboard can explain what a review lead means without exposing its source.
AUDIT_REVIEW_PLAN = {
    "approval_gate": {
        "test_id": "approval-gate-check",
        "recommended_test": (
            "Test a harmless delegated action and record whether the platform "
            "already owns the approval step."
        ),
    },
    "autonomy_limit": {
        "test_id": "delegated-capability-check",
        "recommended_test": (
            "Run one bounded capability test and record what the configured "
            "worker actually permits."
        ),
    },
    "scope_boundary": {
        "test_id": "scope-boundary-check",
        "recommended_test": (
            "Verify the intended service, target, and technique boundary "
            "before the representative test."
        ),
    },
    "data_boundary": {
        "test_id": "sanitized-egress-check",
        "recommended_test": (
            "Run the sanitized-publication regression test and confirm "
            "sensitive details stay local."
        ),
    },
}


def review_plan_for_categories(categories: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return deterministic, public-safe evidence tests for known categories."""
    plan: list[dict[str, Any]] = []
    for category, definition in AUDIT_REVIEW_PLAN.items():
        value = (categories or {}).get(category)
        if isinstance(value, bool):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        plan.append(
            {
                "category": category,
                "candidate_count": count,
                "test_id": definition["test_id"],
                "recommended_test": definition["recommended_test"],
            }
        )
    return plan


@dataclass(frozen=True)
class ConstraintFinding:
    rule_id: str
    category: str
    description: str
    relative_path: str
    line: int
    excerpt: str

    def local_record(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "description": self.description,
            "relative_path": self.relative_path,
            "line": self.line,
            "excerpt": self.excerpt,
            "disposition": "NEEDS_EVIDENCE_REVIEW",
        }


@dataclass(frozen=True)
class ConstraintAuditReport:
    audit_id: str
    target_id: str
    label: str
    state: str
    scanned_at: str
    files_scanned: int
    files_skipped: int
    truncated: bool
    more_pending: bool
    next_cursor: str
    scan_signature: str
    findings: tuple[ConstraintFinding, ...]

    def local_record(self) -> dict[str, Any]:
        categories = self.category_counts()
        return {
            "id": self.audit_id,
            "target_id": self.target_id,
            "label": self.label,
            "state": self.state,
            "scanned_at": self.scanned_at,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "truncated": self.truncated,
            "more_pending": self.more_pending,
            "next_cursor": self.next_cursor,
            "scan_signature": self.scan_signature,
            "candidate_count": len(self.findings),
            "categories": categories,
            "review_plan": review_plan_for_categories(categories),
            "findings": [item.local_record() for item in self.findings],
        }

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.findings:
            counts[item.category] = counts.get(item.category, 0) + 1
        return dict(sorted(counts.items()))


class ConstraintAuditor:
    """Find candidate policy language in an explicit source tree.

    The auditor does not rewrite, delete, classify as redundant, or publish
    source excerpts.  It creates a local review queue backed by evidence.
    """

    def __init__(self, target: AuditTarget) -> None:
        self.target = target

    def _candidate_files(self) -> list[tuple[str, Path]]:
        root = self.target.root
        files: list[tuple[str, Path]] = []
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [
                item
                for item in directories
                if item.casefold() not in EXCLUDED_DIRECTORIES
            ]
            directories.sort(key=str.casefold)
            for name in sorted(filenames, key=str.casefold):
                path = Path(current) / name
                if path.is_symlink() or name.casefold() in EXCLUDED_FILENAMES:
                    continue
                if path.suffix.casefold() not in TEXT_FILE_SUFFIXES:
                    continue
                if path.name.startswith(".") or path.name.casefold().endswith(".env"):
                    continue
                files.append((path.relative_to(root).as_posix(), path))
        return sorted(files, key=lambda item: item[0].casefold())

    @staticmethod
    def _excerpt(line: str) -> str:
        compact = " ".join(line.replace("\x00", " ").split())[:240]
        return "[redacted]" if SENSITIVE_CONTENT.search(compact) else compact

    def run(self, *, start_after: str = "") -> ConstraintAuditReport:
        if not self.target.root.is_dir():
            raise FileNotFoundError(f"audit target is unavailable: {self.target.target_id}")
        candidate_files = self._candidate_files()
        cursor = str(start_after or "").replace("\\", "/")
        start_index = 0
        if cursor:
            while start_index < len(candidate_files) and candidate_files[start_index][0].casefold() <= cursor.casefold():
                start_index += 1
            if start_index >= len(candidate_files):
                start_index = 0
        window = candidate_files[start_index : start_index + self.target.max_files]
        findings: list[ConstraintFinding] = []
        files_scanned = 0
        files_skipped = 0
        truncated = False
        last_processed = ""
        for relative_path, path in window:
            last_processed = relative_path
            try:
                if path.stat().st_size > self.target.max_file_bytes:
                    files_skipped += 1
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                files_skipped += 1
                continue
            files_scanned += 1
            for line_number, line in enumerate(content.splitlines(), start=1):
                for rule in CONSTRAINT_RULES:
                    if not rule.pattern.search(line):
                        continue
                    findings.append(
                        ConstraintFinding(
                            rule_id=rule.rule_id,
                            category=rule.category,
                            description=rule.description,
                            relative_path=relative_path,
                            line=line_number,
                            excerpt=self._excerpt(line),
                        )
                    )
                    break
                if len(findings) >= self.target.max_findings:
                    truncated = True
                    break
            if truncated:
                break
        processed_all_window = not truncated
        more_pending = (
            bool(last_processed)
            and (
                (start_index + len(window) < len(candidate_files))
                or not processed_all_window
            )
        )
        next_cursor = last_processed if more_pending else ""
        state = "NEEDS_EVIDENCE_REVIEW" if findings else "NO_CANDIDATES"
        return ConstraintAuditReport(
            audit_id=f"audit-{self.target.target_id}-{int(time.time() * 1000)}",
            target_id=self.target.target_id,
            label=self.target.label,
            state=state,
            scanned_at=utc_now(),
            files_scanned=files_scanned,
            files_skipped=files_skipped,
            truncated=truncated,
            more_pending=more_pending,
            next_cursor=next_cursor,
            scan_signature=self.target.scan_signature,
            findings=tuple(findings),
        )


class EvidenceCoordinator:
    """Persist profiles, schedule low-priority audits, and emit safe events."""

    def __init__(
        self,
        store: StateStore,
        config: Mapping[str, Any] | None,
        *,
        base: Path,
        actor: str,
        objective_id: str,
        job_id: str,
    ) -> None:
        raw = {} if config is None else config
        if not isinstance(raw, Mapping):
            raise ValueError("config.evidence must be an object")
        raw_profiles = raw.get("worker_profiles", [])
        raw_targets = raw.get("constraint_audits", [])
        if not isinstance(raw_profiles, list):
            raise ValueError("config.evidence.worker_profiles must be an array")
        if not isinstance(raw_targets, list):
            raise ValueError("config.evidence.constraint_audits must be an array")
        self.store = store
        self.profiles = tuple(
            WorkerProfile.from_mapping(item) for item in raw_profiles if isinstance(item, Mapping)
        )
        self.targets = tuple(
            AuditTarget.from_mapping(item, base=base)
            for item in raw_targets
            if isinstance(item, Mapping) and bool(item.get("enabled", True))
        )
        profile_ids = [profile.worker_id for profile in self.profiles]
        target_ids = [target.target_id for target in self.targets]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("config.evidence contains duplicate worker profile ids")
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("config.evidence contains duplicate audit target ids")
        self.audit_interval_s = max(60.0, float(raw.get("audit_interval_s", 86_400)))
        self.actor = _safe_text(actor, max_len=60) or "local-manager"
        self.objective_id = _safe_text(objective_id, max_len=80)
        self.job_id = _safe_text(job_id, max_len=80)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="constraint-audit")
        self._pending: dict[str, Future[ConstraintAuditReport]] = {}
        self._profiles_synced = False

    @property
    def enabled(self) -> bool:
        return bool(self.profiles or self.targets)

    def _event(
        self,
        *,
        event_type: str,
        state: str,
        action: str,
        outcome: str,
        metrics: Mapping[str, int | float | bool] | None = None,
        artifact_ref: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "timestamp": utc_now(),
            "event_id": f"evt-evidence-{event_type}-{time.time_ns()}",
            "objective_id": self.objective_id,
            "job_id": self.job_id,
            "worker_id": "",
            "actor": self.actor,
            "event_type": event_type,
            "previous_state": "UNKNOWN",
            "new_state": state,
            "metrics": dict(metrics or {}),
            "action": action,
            "outcome": outcome,
            "artifact_refs": [artifact_ref] if artifact_ref else [],
            "error": error,
            "duration": None,
        }

    def _sync_profiles(self) -> list[dict[str, Any]]:
        if self._profiles_synced:
            return []
        events: list[dict[str, Any]] = []
        for profile in self.profiles:
            record = profile.local_record()
            prior = self.store.get_worker_profile(profile.worker_id)
            prior_retest = bool((prior or {}).get("retest_required", False))
            prior_fingerprint = str((prior or {}).get("fingerprint", ""))
            self.store.upsert_worker_profile(record)
            if record["retest_required"] and (
                not prior or not prior_retest or prior_fingerprint != record["fingerprint"]
            ):
                events.append(
                    self._event(
                        event_type="worker_profile_retest_due",
                        state="RETEST_REQUIRED",
                        action="record_worker_profile",
                        outcome="retest_due",
                        metrics={"capability_count": len(profile.capabilities)},
                        artifact_ref=f"worker-profile:{profile.worker_id}",
                    )
                )
            elif not prior:
                events.append(
                    self._event(
                        event_type="worker_profile_recorded",
                        state=str(record["state"]),
                        action="record_worker_profile",
                        outcome="evidence_recorded",
                        metrics={"capability_count": len(profile.capabilities)},
                        artifact_ref=f"worker-profile:{profile.worker_id}",
                    )
                )
        self._profiles_synced = True
        return events

    def _audit_due(self, target: AuditTarget, *, now: float) -> bool:
        if target.target_id in self._pending:
            return False
        previous = self.store.get_constraint_audit(target.target_id)
        if previous and str(previous.get("scan_signature", "")) != target.scan_signature:
            return True
        previous_time = _parse_timestamp((previous or {}).get("scanned_at"))
        interval = target.interval_s if target.interval_s is not None else self.audit_interval_s
        return previous_time is None or now - previous_time >= interval

    def tick(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Schedule due audits without ever blocking worker supervision."""
        now = time.time() if now is None else float(now)
        events = self._sync_profiles()
        for target_id, future in list(self._pending.items()):
            if not future.done():
                continue
            self._pending.pop(target_id, None)
            target = next(item for item in self.targets if item.target_id == target_id)
            try:
                report = future.result()
            except Exception as exc:  # Keep an optional audit from affecting the manager.
                events.append(
                    self._event(
                        event_type="constraint_audit_failed",
                        state="FAILED",
                        action="scan_constraints",
                        outcome="audit_deferred",
                        artifact_ref=f"constraint-audit:{target.target_id}",
                        error=type(exc).__name__,
                    )
                )
                continue
            record = report.local_record()
            self.store.upsert_constraint_audit(record)
            events.append(
                self._event(
                    event_type="constraint_audit_completed",
                    state=report.state,
                    action="scan_constraints",
                    outcome="review_candidates_recorded",
                    metrics={
                        "files_scanned": report.files_scanned,
                        "files_skipped": report.files_skipped,
                        "candidate_count": len(report.findings),
                        "truncated": report.truncated,
                        "more_pending": report.more_pending,
                    },
                    artifact_ref=f"constraint-audit:{target.target_id}",
                )
            )
        for target in self.targets:
            if not self._audit_due(target, now=now):
                continue
            previous = self.store.get_constraint_audit(target.target_id) or {}
            start_after = str(previous.get("next_cursor", ""))
            self._pending[target.target_id] = self._executor.submit(
                ConstraintAuditor(target).run,
                start_after=start_after,
            )
            events.append(
                self._event(
                    event_type="constraint_audit_started",
                    state="RUNNING",
                    action="scan_constraints",
                    outcome="audit_scheduled",
                    artifact_ref=f"constraint-audit:{target.target_id}",
                )
            )
        return events

    def public_profiles(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for record in self.store.list_worker_profiles():
            capabilities = record.get("capabilities", [])
            profiles.append(
                {
                    "id": _safe_text(record.get("id"), max_len=100),
                    "provider": _safe_text(record.get("provider"), max_len=60),
                    "model": _safe_text(record.get("model"), max_len=100),
                    "model_version": _safe_text(record.get("model_version"), max_len=100),
                    "state": _safe_text(record.get("state"), max_len=40).upper() or "UNKNOWN",
                    "retest_required": bool(record.get("retest_required", False)),
                    "last_verified": _safe_text(record.get("last_verified"), max_len=40),
                    "capabilities": [
                        {
                            "id": _safe_text(item.get("id"), max_len=100),
                            "status": _safe_text(item.get("status"), max_len=30).upper(),
                            "summary": _safe_text(item.get("summary"), max_len=180),
                            "observed_at": _safe_text(item.get("observed_at"), max_len=40),
                        }
                        for item in capabilities
                        if isinstance(item, Mapping)
                    ],
                }
            )
        return profiles

    def public_audits(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for record in self.store.list_constraint_audits():
            raw_categories = record.get("categories", {})
            categories: dict[str, int] = {}
            if isinstance(raw_categories, Mapping):
                for key, value in raw_categories.items():
                    clean_key = _safe_text(key, max_len=40)
                    if clean_key in AUDIT_REVIEW_PLAN and isinstance(value, (int, float)) and not isinstance(value, bool):
                        categories[clean_key] = max(0, int(value))
            summaries.append(
                {
                    "id": _safe_text(record.get("target_id", record.get("id")), max_len=100),
                    "label": _safe_text(record.get("label"), max_len=100),
                    "state": _safe_text(record.get("state"), max_len=40).upper() or "UNKNOWN",
                    "scanned_at": _safe_text(record.get("scanned_at"), max_len=40),
                    "files_scanned": max(0, int(record.get("files_scanned", 0) or 0)),
                    "files_skipped": max(0, int(record.get("files_skipped", 0) or 0)),
                    "candidate_count": max(0, int(record.get("candidate_count", 0) or 0)),
                    "truncated": bool(record.get("truncated", False)),
                    "more_pending": bool(record.get("more_pending", False)),
                    "categories": categories,
                    "review_plan": review_plan_for_categories(categories),
                }
            )
        return summaries

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
