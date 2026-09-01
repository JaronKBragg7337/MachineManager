from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from manager.research_worker import (
    BoundedSourceFetcher,
    LocalOllamaResearchSummarizer,
    PublicResearchHandler,
    ResearchTaskError,
)
from manager.scheduler import WorkItem


class _FakeFetcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def fetch(self, source):
        self.calls.append(dict(source))
        return {
            "url": source["url"],
            "title": source.get("title") or "Example source",
            "content_type": "text/html",
            "status": "FETCHED",
            "word_count": 12,
            "sha256": "a" * 64,
            "excerpt": "Public source evidence.",
        }


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps(
            {
                "response": json.dumps(
                    {
                        "summary": "A concise source-backed summary.",
                        "findings": ["Example source supports the observation."],
                        "next_action": "Review the evidence.",
                    }
                )
            }
        ).encode("utf-8")


class _LargeResponse:
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b"<html><title>Large source</title><body>" + (b"evidence " * 5000) + b"</body></html>"


class _LargeOpener:
    def open(self, _request, timeout):
        return _LargeResponse()


class ResearchWorkerTests(unittest.TestCase):
    def test_public_research_handler_writes_evidence_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fetcher = _FakeFetcher()

            def summarize(question, sources):
                self.assertEqual(question, "What changed?")
                self.assertEqual(len(sources), 2)
                return {
                    "summary": "Observed change.",
                    "findings": ["A source-backed finding."],
                    "next_action": "Verify the finding.",
                }

            handler = PublicResearchHandler(
                Path(raw),
                fetcher=fetcher,
                summarizer=summarize,
            )
            outcome = handler(
                WorkItem(
                    task_id="task-research-1",
                    kind="research",
                    objective_id="research-objective",
                    attempts=1,
                    payload={
                        "question": "What changed?",
                        "sources": [
                            {"url": "https://example.org/one", "title": "One"},
                            {"url": "https://example.org/two", "title": "Two"},
                        ],
                        "private_note": "not included",
                    },
                )
            )
            artifact = json.loads((Path(raw) / "task-research-1.json").read_text(encoding="utf-8"))
            self.assertEqual(outcome.status, "COMPLETE")
            self.assertEqual(outcome.metrics["source_count"], 2)
            self.assertTrue(outcome.metrics["summary_available"])
            self.assertEqual(artifact["question"], "What changed?")
            self.assertEqual(len(artifact["sources"]), 2)
            self.assertNotIn("private_note", json.dumps(artifact))
            self.assertEqual(len(fetcher.calls), 2)

    def test_research_handler_rejects_local_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            handler = PublicResearchHandler(Path(raw), fetcher=_FakeFetcher())
            with self.assertRaises(ResearchTaskError):
                handler(
                    WorkItem(
                        task_id="task-research-local",
                        kind="research",
                        objective_id="research-objective",
                        attempts=1,
                        payload={
                            "question": "No local access",
                            "sources": ["http://127.0.0.1:11434/api/tags"],
                        },
                    )
                )

    def test_ollama_research_summary_requests_cpu_only_json(self) -> None:
        summarizer = LocalOllamaResearchSummarizer(model="qwen3.5:4b")
        with patch("manager.research_worker.urllib.request.urlopen", return_value=_FakeResponse()) as opener:
            result = summarizer(
                "What changed?",
                [
                    {
                        "title": "Example",
                        "url": "https://example.org",
                        "excerpt": "Public source evidence.",
                    }
                ],
            )
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result["summary"], "A concise source-backed summary.")
        self.assertFalse(body["think"])
        self.assertEqual(body["options"]["num_gpu"], 0)
        self.assertEqual(body["options"]["num_predict"], 320)

    def test_source_fetcher_rejects_private_url(self) -> None:
        fetcher = BoundedSourceFetcher()
        with self.assertRaises(ResearchTaskError):
            fetcher.fetch({"url": "http://10.0.0.1/private"})

    def test_source_fetcher_marks_oversized_sources_as_truncated(self) -> None:
        fetcher = BoundedSourceFetcher(max_bytes=1024)
        with patch("manager.research_worker.urllib.request.build_opener", return_value=_LargeOpener()):
            source = fetcher.fetch({"url": "https://example.org/large"})
        self.assertTrue(source["truncated"])
        self.assertEqual(source["status"], "FETCHED")
        self.assertLessEqual(len(source["excerpt"]), 4000)


if __name__ == "__main__":
    unittest.main()
