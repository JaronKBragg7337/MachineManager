"""Safe GitHub Pages uploader for already-sanitized dashboard data."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping


PUBLIC_FILES = (
    "latest.json",
    "events.json",
    "scenarios.json",
)
MAX_FILE_BYTES = {
    "latest.json": 32_000,
    "events.json": 512_000,
    "scenarios.json": 256_000,
}


class PublicUploadError(RuntimeError):
    """Raised when a public upload cannot be completed safely."""


def _safe_commit_stamp(value: Any) -> str:
    value = str(value or "now")
    return re.sub(r"[^0-9A-Za-z_.:-]", "-", value)[:40]


def _assert_safe_payload(name: str, raw: bytes) -> None:
    if len(raw) > MAX_FILE_BYTES[name]:
        raise PublicUploadError(f"{name} exceeds the public size limit")
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicUploadError(f"{name} is not valid UTF-8 JSON") from exc
    text = raw.decode("utf-8")
    if re.search(
        r'(?i)"(?:pid|private_key|privatekey|secret|token|password|seed|api_key)"\s*:',
        text,
    ):
        raise PublicUploadError(f"{name} contains a prohibited field")
    if re.search(r"(?i)(?:[A-Za-z]:[\\/]|/Users/|/home/|\\\\)", text):
        raise PublicUploadError(f"{name} contains a private path")


class GitHubPagesPublisher:
    """Commit the three compact dashboard files through GitHub's Git API.

    The token is read only from an environment variable populated by a local
    secret mechanism. It is never put in a command line, config file, commit,
    log, or public payload.
    """

    def __init__(
        self,
        dashboard_dir: Path,
        *,
        owner: str,
        repository: str,
        branch: str = "main",
        token_env: str = "MACHINE_MANAGER_GITHUB_TOKEN",
        interval_s: float = 300.0,
        path_prefix: str = "dashboard/data",
        local_repo_dir: Path | None = None,
    ) -> None:
        if not owner or not repository or not branch:
            raise ValueError("GitHub owner, repository, and branch are required")
        if interval_s <= 0:
            raise ValueError("public upload interval must be positive")
        self.dashboard_dir = Path(dashboard_dir)
        self.owner = owner
        self.repository = repository
        self.branch = branch
        self.token_env = token_env
        self.interval_s = float(interval_s)
        self.path_prefix = path_prefix.strip("/")
        self.local_repo_dir = Path(local_repo_dir) if local_repo_dir else self.dashboard_dir.parent
        self.last_publish_monotonic: float | None = None
        self.last_digest: str | None = None
        self.last_published_at: str | None = None
        self.last_error: str | None = None
        self.last_mirror_status: str = "not_attempted"

    @classmethod
    def from_mapping(cls, dashboard_dir: Path, value: Mapping[str, Any]) -> "GitHubPagesPublisher":
        return cls(
            dashboard_dir,
            owner=str(value.get("owner", "JaronKBragg7337")),
            repository=str(value.get("repository", "MachineManager")),
            branch=str(value.get("branch", "main")),
            token_env=str(value.get("token_env", "MACHINE_MANAGER_GITHUB_TOKEN")),
            interval_s=float(value.get("interval_s", 300)),
            path_prefix=str(value.get("path_prefix", "dashboard/data")),
            local_repo_dir=(
                Path(str(value["local_repo_dir"]))
                if value.get("local_repo_dir")
                else dashboard_dir.parent
            ),
        )

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self.token_env))

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "last_published_at": self.last_published_at,
            "last_error": self.last_error,
            "local_mirror": self.last_mirror_status,
        }

    def _api_url(self, suffix: str) -> str:
        owner = urllib.parse.quote(self.owner, safe="")
        repository = urllib.parse.quote(self.repository, safe="")
        return f"https://api.github.com/repos/{owner}/{repository}/{suffix.lstrip('/')}"

    def _request(self, method: str, suffix: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        token = os.environ.get(self.token_env)
        if not token:
            raise PublicUploadError(f"missing token environment variable: {self.token_env}")
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._api_url(suffix),
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "MachineManager-public-telemetry",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(2_000_000)
        except (OSError, urllib.error.URLError) as exc:
            raise PublicUploadError("GitHub API request failed") from exc
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicUploadError("GitHub API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise PublicUploadError("GitHub API returned an unexpected response")
        return decoded

    def _read_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        data_dir = self.dashboard_dir / "data"
        for name in PUBLIC_FILES:
            path = data_dir / name
            if not path.is_file():
                raise PublicUploadError(f"missing public telemetry file: {name}")
            raw = path.read_bytes()
            _assert_safe_payload(name, raw)
            files[name] = raw
        return files

    def _digest(self, files: Mapping[str, bytes]) -> str:
        digest = hashlib.sha256()
        for name in PUBLIC_FILES:
            digest.update(name.encode("utf-8"))
            digest.update(files[name])
        return digest.hexdigest()

    def _mirror_local_ref(self, remote_sha: str) -> str:
        """Fast-forward the local branch without touching the working files.

        The manager writes the three public files in its working tree while the
        API publisher commits the same sanitized bytes remotely. Once that
        commit exists, advancing the local ref keeps the checkout's history in
        step without replacing live files. Any staged, unrelated, untracked, or
        divergent local work defers the mirror rather than overwriting it.
        """

        if not re.fullmatch(r"[0-9a-fA-F]{40}", remote_sha):
            return "deferred"
        repository = self.local_repo_dir
        if not repository or not (repository / ".git").exists():
            return "unavailable"
        allowed_paths = {f"{self.path_prefix}/{name}" for name in PUBLIC_FILES}

        def git(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(repository), *arguments],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        try:
            branch = git("symbolic-ref", "--short", "HEAD")
            if branch.returncode != 0 or branch.stdout.strip() != self.branch:
                return "deferred"
            head = git("rev-parse", "HEAD")
            local_sha = head.stdout.strip()
            if head.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", local_sha):
                return "deferred"
            status = git("status", "--porcelain")
            if status.returncode != 0:
                return "deferred"
            for line in status.stdout.splitlines():
                path = line[3:] if len(line) >= 3 else ""
                if len(line) < 3 or line[0] != " " or line[1] != "M" or path not in allowed_paths:
                    return "deferred"
            fetched = git("fetch", "--quiet", "origin", self.branch)
            if fetched.returncode != 0:
                return "deferred"
            ancestor = git("merge-base", "--is-ancestor", local_sha, remote_sha)
            if ancestor.returncode != 0:
                return "deferred"
            if local_sha != remote_sha:
                updated = git("update-ref", f"refs/heads/{self.branch}", remote_sha, local_sha)
                if updated.returncode != 0:
                    return "deferred"
            tracking = git("show-ref", "--verify", "--quiet", f"refs/remotes/origin/{self.branch}")
            if tracking.returncode == 0:
                git("update-ref", f"refs/remotes/origin/{self.branch}", remote_sha)
            return "synced"
        except (OSError, subprocess.TimeoutExpired):
            return "deferred"

    def publish(self, *, force: bool = False) -> bool:
        files = self._read_files()
        digest = self._digest(files)
        if not force and digest == self.last_digest:
            return False

        latest: dict[str, Any] = {}
        try:
            loaded = json.loads(files["latest.json"].decode("utf-8"))
            if isinstance(loaded, dict):
                latest = loaded
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        stamp = _safe_commit_stamp(latest.get("updated"))
        commit_message = (
            f"chore(telemetry): publish sanitized snapshot {stamp}\n\n"
            "Co-Authored-By: Codex <noreply@openai.com>"
        )

        try:
            ref = self._request("GET", f"git/ref/heads/{urllib.parse.quote(self.branch, safe='')}")
            current_commit = str(ref.get("object", {}).get("sha", ""))
            if not current_commit:
                raise PublicUploadError("GitHub branch ref did not contain a commit")
            commit = self._request("GET", f"git/commits/{current_commit}")
            base_tree = str(commit.get("tree", {}).get("sha", ""))
            if not base_tree:
                raise PublicUploadError("GitHub commit did not contain a tree")

            tree_entries = []
            for name, raw in files.items():
                blob = self._request(
                    "POST",
                    "git/blobs",
                    {
                        "content": base64.b64encode(raw).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                blob_sha = str(blob.get("sha", ""))
                if not blob_sha:
                    raise PublicUploadError(f"GitHub did not return a blob for {name}")
                tree_entries.append(
                    {
                        "path": f"{self.path_prefix}/{name}",
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
                    }
                )

            tree = self._request(
                "POST",
                "git/trees",
                {"base_tree": base_tree, "tree": tree_entries},
            )
            tree_sha = str(tree.get("sha", ""))
            if not tree_sha:
                raise PublicUploadError("GitHub did not return the new tree")
            new_commit = self._request(
                "POST",
                "git/commits",
                {
                    "message": commit_message,
                    "tree": tree_sha,
                    "parents": [current_commit],
                },
            )
            new_sha = str(new_commit.get("sha", ""))
            if not new_sha:
                raise PublicUploadError("GitHub did not return the new commit")
            self._request(
                "PATCH",
                f"git/refs/heads/{urllib.parse.quote(self.branch, safe='')}",
                {"sha": new_sha, "force": False},
            )
            self.last_mirror_status = self._mirror_local_ref(new_sha)
        except PublicUploadError as exc:
            self.last_error = type(exc).__name__
            raise

        self.last_digest = digest
        self.last_publish_monotonic = time.monotonic()
        self.last_published_at = str(latest.get("updated", ""))
        self.last_error = None
        return True

    def maybe_publish(self, *, now: float | None = None, immediate: bool = False) -> bool:
        """Publish on the regular cadence, or immediately for a public state change."""
        if not self.configured:
            return False
        now = time.monotonic() if now is None else float(now)
        if (
            not immediate and self.last_publish_monotonic is not None
            and now - self.last_publish_monotonic < self.interval_s
        ):
            return False
        return self.publish()
