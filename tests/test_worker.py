from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from tests.test_processor import event
from taskforge.events import CardCommandEvent, command_event_to_payload, task_event_to_payload
from taskforge.server import JobWorker
from taskforge.state import StateStore


class RecordingProcessor:
    def __init__(self):
        self.tasks = []
        self.commands = []

    def process(self, task):
        self.tasks.append(task)

    def process_command(self, command):
        self.commands.append(command)


class WorkerTests(unittest.TestCase):
    def test_worker_processes_task_and_command_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite3")
            processor = RecordingProcessor()
            state.enqueue_job(kind="task", action_id="action-1", payload=task_event_to_payload(event()))
            state.enqueue_job(
                kind="command",
                action_id="cmd-1",
                payload=command_event_to_payload(
                    CardCommandEvent("cmd-1", "card-1", "retry", "/codex retry", "comment")
                ),
            )
            worker = JobWorker(state=state, processor=processor, poll_interval=0.01)

            worker.start()
            deadline = time.time() + 2
            while len(processor.tasks) < 1 or len(processor.commands) < 1:
                if time.time() > deadline:
                    self.fail("worker did not process queued jobs")
                time.sleep(0.01)
            worker.stop()

            statuses = [job["status"] for job in state.read_all()["jobs"]]
            self.assertEqual(statuses, ["done", "done"])
            self.assertEqual(processor.tasks[0].card_id, "card-1")
            self.assertEqual(processor.commands[0].command, "retry")


if __name__ == "__main__":
    unittest.main()

