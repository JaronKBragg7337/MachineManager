import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from manager.scheduler import WorkItem
from manager.verification_worker import RepositoryVerificationHandler, VerificationRetryableError


class VerificationWorkerTests(unittest.TestCase):
    def test_success_writes_local_evidence_and_returns_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact_dir = root / "artifacts"

            def runner(command, **kwargs):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="Ran 69 tests in 1.2s\nOK\n",
                    stderr="",
                )

            handler = RepositoryVerificationHandler(root, artifact_dir=artifact_dir, runner=runner)
            outcome = handler(
                WorkItem("verification-task-1", "verification", "objective-1", 1, {})
            )

            self.assertEqual(outcome.status, "COMPLETE")
            self.assertEqual(outcome.metrics["tests_run"], 69)
            self.assertTrue(outcome.metrics["passed"])
            self.assertIn("Repository verification passed 69 test(s)", outcome.public_message)
            evidence = json.loads((artifact_dir / "verification-task-1.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["return_code"], 0)
            self.assertEqual(evidence["tests_run"], 69)
            self.assertNotIn("stdout", evidence)

    def test_test_failure_is_visible_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def runner(command, **kwargs):
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="Ran 69 tests in 1.2s\nFAILED (failures=1)\n",
                    stderr="",
                )

            outcome = RepositoryVerificationHandler(root, artifact_dir=root / "artifacts", runner=runner)(
                WorkItem("verification-task-2", "verification", "objective-1", 1, {})
            )
            self.assertEqual(outcome.status, "FAILED")
            self.assertFalse(outcome.metrics["passed"])
            self.assertIn("Repository verification failed 69 test(s)", outcome.public_message)

    def test_timeout_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def runner(command, **kwargs):
                raise subprocess.TimeoutExpired(command, 1)

            handler = RepositoryVerificationHandler(root, artifact_dir=root / "artifacts", runner=runner)
            with self.assertRaises(VerificationRetryableError):
                handler(WorkItem("verification-task-3", "verification", "objective-1", 1, {}))


if __name__ == "__main__":
    unittest.main()
