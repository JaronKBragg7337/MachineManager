"""Regression checks for the rerun-safe GitHub Pages workflow."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
UNIQUE_ARTIFACT = "github-pages-${{ github.run_id }}-${{ github.run_attempt }}"


class PagesWorkflowTests(unittest.TestCase):
    def test_upload_and_deploy_share_a_unique_attempt_artifact(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("uses: actions/upload-pages-artifact@v3", workflow)
        self.assertIn("uses: actions/deploy-pages@v4", workflow)
        self.assertEqual(workflow.count(f"\n          name: {UNIQUE_ARTIFACT}"), 1)
        self.assertEqual(workflow.count(f"\n          artifact_name: {UNIQUE_ARTIFACT}"), 1)
        self.assertIn("github.run_id", workflow)
        self.assertIn("github.run_attempt", workflow)


if __name__ == "__main__":
    unittest.main()
