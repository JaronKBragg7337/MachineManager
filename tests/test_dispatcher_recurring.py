from __future__ import annotations

from pathlib import Path
import threading
import tempfile
import time
import unittest

from manager.dispatcher import BackgroundWorkDispatcher, DispatchOutcome
from manager.recurring import RecurringTaskSeeder
from manager.scheduler import WorkScheduler
from manager.state_store import StateStore


class BackgroundDispatcherTests(unittest.TestCase):
    def test_background_dispatch_does_not_wait_for_handler(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                scheduler.enqueue(
                    kind="research",
                    objective_id="research-objective",
                    task_id="task-background-1",
                    scheduled_at=100,
                )
                started = threading.Event()
                release = threading.Event()

                def handler(_item):
                    started.set()
                    release.wait(2)
                    return DispatchOutcome(metrics={"source_count": 1})

                dispatcher = BackgroundWorkDispatcher(
                    scheduler,
                    {"research": handler},
                    max_workers=1,
                    max_in_flight=1,
                )
                try:
                    begin = time.monotonic()
                    launched = dispatcher.dispatch(limit=1, now=100)
                    elapsed = time.monotonic() - begin
                    self.assertLess(elapsed, 0.5)
                    self.assertEqual(launched[0].status, "RUNNING")
                    self.assertTrue(started.wait(1))
                    self.assertEqual(store.task_status("task-background-1"), "RUNNING")
                    self.assertEqual(dispatcher.dispatch(limit=1, now=101), [])

                    release.set()
                    completed = []
                    for _ in range(30):
                        completed = dispatcher.dispatch(limit=1, now=101)
                        if completed and completed[0].status == "COMPLETE":
                            break
                        time.sleep(0.02)
                    self.assertEqual(completed[0].status, "COMPLETE")
                    self.assertEqual(store.task_status("task-background-1"), "COMPLETE")
                    self.assertIn(
                        "queue_task_completed",
                        [event["event_type"] for event in store.recent_events(limit=10)],
                    )
                finally:
                    release.set()
                    dispatcher.close()

    def test_background_dispatch_requeues_running_work_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                scheduler.enqueue(
                    kind="build",
                    objective_id="build-objective",
                    task_id="task-background-close-1",
                    scheduled_at=100,
                )
                started = threading.Event()
                release = threading.Event()

                def handler(_item):
                    started.set()
                    release.wait(2)
                    return DispatchOutcome()

                dispatcher = BackgroundWorkDispatcher(scheduler, {"build": handler})
                dispatcher.dispatch(limit=1, now=100)
                self.assertTrue(started.wait(1))
                dispatcher.close()
                self.assertEqual(store.task_status("task-background-close-1"), "QUEUED")
                release.set()


class RecurringTaskSeederTests(unittest.TestCase):
    def test_seeder_is_idempotent_and_avoids_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                seeder = RecurringTaskSeeder.from_config(
                    scheduler,
                    [
                        {
                            "id": "research-lane",
                            "kind": "research",
                            "objective_id": "research-objective",
                            "interval_s": 10,
                            "payload": {"question": "test", "sources": ["https://example.com"]},
                        }
                    ],
                )

                first = seeder.tick(now=1000)
                self.assertEqual(len(first), 1)
                self.assertEqual(store.task_status("recurring-research-lane-000001"), "QUEUED")
                self.assertEqual(seeder.tick(now=1000.1), [])

                self.assertTrue(scheduler.start("recurring-research-lane-000001"))
                self.assertEqual(seeder.tick(now=1011), [])
                self.assertEqual(store.task_status("recurring-research-lane-000001"), "RUNNING")

                scheduler.complete("recurring-research-lane-000001")
                second = seeder.tick(now=1072)
                self.assertEqual(len(second), 1)
                self.assertEqual(store.task_status("recurring-research-lane-000002"), "QUEUED")
                self.assertEqual(store.task_status("recurring-research-lane-000001"), "COMPLETE")

    def test_seeder_rejects_duplicate_and_private_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with StateStore(Path(raw) / "state.sqlite3") as store:
                scheduler = WorkScheduler(store)
                with self.assertRaises(ValueError):
                    RecurringTaskSeeder.from_config(
                        scheduler,
                        [
                            {"id": "same", "kind": "research", "objective_id": "one", "interval_s": 1},
                            {"id": "same", "kind": "research", "objective_id": "two", "interval_s": 1},
                        ],
                    )
                with self.assertRaises(ValueError):
                    RecurringTaskSeeder.from_config(
                        scheduler,
                        [
                            {
                                "id": "private-check",
                                "kind": "research",
                                "objective_id": "token=must-not-be-an-id",
                                "interval_s": 1,
                            }
                        ],
                    )


if __name__ == "__main__":
    unittest.main()
