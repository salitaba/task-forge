from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taskforge.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_set_card_merges_existing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.json")

            state.set_card("card-1", status="question", branch="branch")
            state.set_card("card-1", status="review")

            card = state.get_card("card-1")
            self.assertEqual(card["status"], "review")
            self.assertEqual(card["branch"], "branch")

    def test_processed_actions_are_deduplicated_and_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.json")

            for index in range(1005):
                state.mark_action_processed(f"action-{index}")
            state.mark_action_processed("action-1004")

            data = state.read_all()
            self.assertEqual(len(data["processed_actions"]), 1000)
            self.assertTrue(state.has_processed_action("action-1004"))
            self.assertFalse(state.has_processed_action("action-0"))

    def test_job_queue_claim_complete_and_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite3")

            self.assertTrue(state.enqueue_job(kind="task", action_id="action-1", payload={"card_id": "card-1"}))
            self.assertFalse(state.enqueue_job(kind="task", action_id="action-1", payload={"card_id": "card-1"}))
            job = state.claim_next_job()
            assert job is not None
            self.assertEqual(job["kind"], "task")
            self.assertEqual(job["payload"]["card_id"], "card-1")

            state.complete_job(job["id"])

            self.assertEqual(state.read_all()["jobs"][0]["status"], "done")

    def test_requeue_running_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite3")
            state.enqueue_job(kind="task", action_id="action-1", payload={})
            job = state.claim_next_job()
            assert job is not None

            count = state.requeue_running_jobs()

            self.assertEqual(count, 1)
            self.assertEqual(state.read_all()["jobs"][0]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
