from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.fakes import test_config, valid_description
from taskforge import __main__ as cli
from taskforge.state import StateStore


class FakeCliTrello:
    def __init__(self, config):
        self.config = config

    def create_webhook(self):
        return {"id": "webhook-1"}

    def get_card(self, card_id):
        return {
            "id": card_id,
            "idShort": 7,
            "name": "Build invite flow",
            "desc": valid_description(),
            "shortUrl": "https://trello.test/c/abc",
        }


class FakeCliProcessor:
    def __init__(self):
        self.events = []

    def process(self, event):
        self.events.append(event)


class CliTests(unittest.TestCase):
    def test_card_template_prints_required_sections(self) -> None:
        cfg = test_config(required_card_sections=("Problem", "Scope"))
        output = io.StringIO()

        with patch.object(cli.Config, "from_env_file", return_value=cfg), redirect_stdout(output):
            code = cli.main(["card-template"])

        self.assertEqual(code, 0)
        self.assertIn("## Problem", output.getvalue())
        self.assertIn("## Scope", output.getvalue())

    def test_status_prints_state_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = test_config(state_file=Path(tmp) / "state.json")
            StateStore(cfg.state_file).set_card("card-1", status="review")
            output = io.StringIO()

            with patch.object(cli.Config, "from_env_file", return_value=cfg), redirect_stdout(output):
                code = cli.main(["status"])

        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["cards"]["card-1"]["status"], "review")

    def test_validate_config_returns_zero_for_valid_config(self) -> None:
        cfg = test_config(trello_key="key", trello_token="token", trello_callback_url="url", trello_board_id="board")
        output = io.StringIO()

        with patch.object(cli.Config, "from_env_file", return_value=cfg), redirect_stdout(output):
            code = cli.main(["validate-config"])

        self.assertEqual(code, 0)
        self.assertIn("configuration ok", output.getvalue())

    def test_register_webhook_uses_trello_client(self) -> None:
        cfg = test_config()
        output = io.StringIO()

        with (
            patch.object(cli.Config, "from_env_file", return_value=cfg),
            patch.object(cli, "TrelloClient", FakeCliTrello),
            redirect_stdout(output),
        ):
            code = cli.main(["register-webhook"])

        self.assertEqual(code, 0)
        self.assertIn("registered webhook webhook-1", output.getvalue())

    def test_run_card_fetches_card_and_processes_manual_event(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = test_config(state_file=Path(tmp.name) / "state.sqlite3")
        processor = FakeCliProcessor()
        output = io.StringIO()

        with (
            patch.object(cli.Config, "from_env_file", return_value=cfg),
            patch.object(cli, "TrelloClient", FakeCliTrello),
            patch.object(cli, "make_processor", return_value=processor),
            redirect_stdout(output),
        ):
            code = cli.main(["run-card", "card-1"])

        self.assertEqual(code, 0)
        self.assertEqual(processor.events[0].card_id, "card-1")
        self.assertEqual(processor.events[0].source, "manual run")
        self.assertIn("processed card-1", output.getvalue())

    def test_cleanup_prints_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = test_config(state_file=Path(tmp) / "state.sqlite3", worktree_root=Path(tmp) / "worktrees")
            state = StateStore(cfg.state_file)
            state.set_card("card-1", status="done", worktree=str(cfg.worktree_root / "one"))
            output = io.StringIO()

            with patch.object(cli.Config, "from_env_file", return_value=cfg), redirect_stdout(output):
                code = cli.main(["cleanup", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("card-1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
