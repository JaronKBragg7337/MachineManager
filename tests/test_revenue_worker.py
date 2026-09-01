from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from manager.revenue_worker import RevenueRetryableError, SuperteamOpportunityHandler
from manager.scheduler import WorkItem


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._payload


def _item(task_id: str = "revenue-task-1", payload: dict[str, object] | None = None) -> WorkItem:
    return WorkItem(task_id, "revenue", "revenue-objective", 1, payload or {})


class RevenueWorkerTests(unittest.TestCase):
    def test_authenticated_discovery_writes_only_safe_listing_fields(self) -> None:
        response = _Response(
            {
                "listings": [
                    {
                        "id": "listing-1",
                        "slug": "docs-bounty",
                        "title": "Improve the public docs",
                        "type": "bounty",
                        "agentAccess": "AGENT_ONLY",
                        "reward": {"amount": 500, "currency": "USDC"},
                        "deadline": "2026-12-31",
                        "eligibilityQuestions": ["What will you change?"],
                        "privateField": "should not be copied",
                    }
                ]
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            handler = SuperteamOpportunityHandler(Path(raw))
            with patch.dict(os.environ, {"SUPERTEAM_AGENT_API_KEY": "test-key"}, clear=False):
                with patch("manager.revenue_worker.urllib.request.urlopen", return_value=response) as opener:
                    outcome = handler(_item(payload={"take": 20, "deadline": "2026-12-31"}))

            self.assertEqual(outcome.status, "COMPLETE")
            self.assertEqual(outcome.metrics["listings_found"], 1)
            self.assertEqual(outcome.metrics["eligible_listings"], 1)
            self.assertIn("Authenticated discovery found 1 listing(s); 1 agent-eligible.", outcome.public_message)
            request = opener.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            artifact = json.loads((Path(raw) / "revenue-task-1.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["listings"][0]["agent_access"], "AGENT_ONLY")
            self.assertNotIn("privateField", json.dumps(artifact))
            self.assertNotIn("test-key", json.dumps(artifact))

    def test_missing_api_key_escalates_without_a_request(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            handler = SuperteamOpportunityHandler(Path(raw))
            with patch.dict(os.environ, {}, clear=True):
                with patch("manager.revenue_worker.urllib.request.urlopen") as opener:
                    outcome = handler(_item("revenue-missing-key"))

            self.assertEqual(outcome.status, "ESCALATED")
            self.assertTrue(outcome.metrics["authority_required"])
            self.assertIn("waiting for the agent credential handoff", outcome.public_message)
            opener.assert_not_called()
            artifact = json.loads((Path(raw) / "revenue-missing-key.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["status"], "AUTHORITY_REQUIRED")
            self.assertEqual(artifact["error_category"], "missing_api_key")

    def test_service_rejection_escalates_without_storing_response(self) -> None:
        def reject(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"token":"do-not-store"}'),
            )

        with tempfile.TemporaryDirectory() as raw:
            handler = SuperteamOpportunityHandler(Path(raw))
            with patch.dict(os.environ, {"SUPERTEAM_AGENT_API_KEY": "test-key"}, clear=False):
                with patch("manager.revenue_worker.urllib.request.urlopen", side_effect=reject):
                    outcome = handler(_item("revenue-rejected"))

            self.assertEqual(outcome.status, "ESCALATED")
            self.assertTrue(outcome.metrics["authority_required"])
            self.assertIn("rejected the agent credential", outcome.public_message)
            artifact = json.loads((Path(raw) / "revenue-rejected.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["error_category"], "service_rejected_credential")
            self.assertNotIn("do-not-store", json.dumps(artifact))

    def test_temporary_service_failure_is_retryable(self) -> None:
        def unavailable(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, io.BytesIO(b"{}"))

        with tempfile.TemporaryDirectory() as raw:
            handler = SuperteamOpportunityHandler(Path(raw))
            with patch.dict(os.environ, {"SUPERTEAM_AGENT_API_KEY": "test-key"}, clear=False):
                with patch("manager.revenue_worker.urllib.request.urlopen", side_effect=unavailable):
                    with self.assertRaises(RevenueRetryableError):
                        handler(_item("revenue-temporary"))


if __name__ == "__main__":
    unittest.main()
