from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.fakes import test_config, valid_description
from taskforge.server import TrelloWebhookHandler
from taskforge.state import StateStore


class FakeSocket:
    def __init__(self, request: bytes):
        self.rfile = io.BytesIO(request)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: object):
        if "r" in mode:
            return self.rfile
        return self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)

    def close(self) -> None:
        pass


def trello_payload(list_id: str = "todo") -> dict[str, object]:
    return {
        "action": {
            "id": "action-1",
            "type": "createCard",
            "data": {
                "list": {"id": list_id},
                "card": {
                    "id": "card-1",
                    "idShort": 7,
                    "name": "Build invite flow",
                    "desc": valid_description(),
                },
            },
        }
    }


def signature(secret: str, body: bytes, callback_url: str) -> str:
    digest = hmac.new(secret.encode(), body + callback_url.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class ServerTests(unittest.TestCase):
    def handle(self, raw_request: bytes, *, config=None, state=None) -> tuple[str, StateStore]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = config or test_config()
        if state is None:
            cfg = replace(cfg, state_file=Path(tmp.name) / "state.sqlite3")
        store = state or StateStore(cfg.state_file)

        class Handler(TrelloWebhookHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

        Handler.config = cfg
        Handler.state = store
        Handler.processor = object()

        sock = FakeSocket(raw_request)
        Handler(sock, ("127.0.0.1", 12345), object())
        return sock.wfile.getvalue().decode("iso-8859-1"), store

    def test_head_webhook_returns_ok(self) -> None:
        response, state = self.handle(b"HEAD /webhooks/trello HTTP/1.1\r\nHost: test\r\n\r\n")

        self.assertIn("200 OK", response)
        self.assertEqual(state.read_all()["jobs"], [])

    def test_bad_path_returns_not_found(self) -> None:
        response, state = self.handle(b"POST /bad HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\n\r\n")

        self.assertIn("404", response)
        self.assertEqual(state.read_all()["jobs"], [])

    def test_bad_json_returns_bad_request(self) -> None:
        response, state = self.handle(
            b"POST /webhooks/trello HTTP/1.1\r\nHost: test\r\nContent-Length: 1\r\n\r\n{"
        )

        self.assertIn("400", response)
        self.assertEqual(state.read_all()["jobs"], [])

    def test_bad_signature_returns_unauthorized(self) -> None:
        cfg = test_config(trello_webhook_secret="secret")
        body = json.dumps(trello_payload()).encode()
        request = (
            b"POST /webhooks/trello HTTP/1.1\r\n"
            b"Host: test\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"\r\n"
            + body
        )

        response, state = self.handle(request, config=cfg)

        self.assertIn("401", response)
        self.assertEqual(state.read_all()["jobs"], [])

    def test_valid_event_is_accepted_and_enqueued(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = test_config(trello_webhook_secret="secret", state_file=Path(tmp.name) / "state.sqlite3")
        body = json.dumps(trello_payload()).encode()
        sig = signature("secret", body, cfg.trello_callback_url)
        request = (
            b"POST /webhooks/trello HTTP/1.1\r\n"
            b"Host: test\r\n"
            + f"X-Trello-Webhook: {sig}\r\n".encode()
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"\r\n"
            + body
        )

        response, state = self.handle(request, config=cfg)

        self.assertIn("202", response)
        jobs = state.read_all()["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["kind"], "task")
        self.assertEqual(jobs[0]["payload"]["card_id"], "card-1")

    def test_partial_move_payload_with_required_label_is_enqueued_for_refresh(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = test_config(
            state_file=Path(tmp.name) / "state.sqlite3",
            trello_start_label_ids=("start-label",),
        )
        payload = {
            "action": {
                "id": "action-1",
                "type": "updateCard",
                "data": {
                    "listBefore": {"id": "backlog"},
                    "listAfter": {"id": "todo"},
                    "card": {"id": "card-1", "name": "Fix bug"},
                },
            }
        }
        body = json.dumps(payload).encode()
        request = (
            b"POST /webhooks/trello HTTP/1.1\r\n"
            b"Host: test\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"\r\n"
            + body
        )

        response, state = self.handle(request, config=cfg)

        self.assertIn("202", response)
        jobs = state.read_all()["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["kind"], "task")
        self.assertEqual(jobs[0]["payload"]["label_ids"], [])

    def test_ignored_event_is_accepted_without_dispatch(self) -> None:
        body = json.dumps(trello_payload(list_id="backlog")).encode()
        request = (
            b"POST /webhooks/trello HTTP/1.1\r\n"
            b"Host: test\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"\r\n"
            + body
        )

        response, state = self.handle(request)

        self.assertIn("202", response)
        self.assertEqual(state.read_all()["jobs"], [])

    def test_comment_command_is_enqueued(self) -> None:
        payload = {
            "action": {
                "id": "command-1",
                "type": "commentCard",
                "data": {"card": {"id": "card-1"}, "text": "/codex retry"},
            }
        }
        body = json.dumps(payload).encode()
        request = (
            b"POST /webhooks/trello HTTP/1.1\r\n"
            b"Host: test\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"\r\n"
            + body
        )

        response, state = self.handle(request)

        self.assertIn("202", response)
        jobs = state.read_all()["jobs"]
        self.assertEqual(jobs[0]["kind"], "command")
        self.assertEqual(jobs[0]["payload"]["command"], "retry")

    def test_health_dashboard_and_state_endpoints(self) -> None:
        for path, expected in [
            ("/healthz", '"ok": true'),
            ("/api/state", '"cards"'),
            ("/dashboard", "data-job-log"),
        ]:
            response, state = self.handle(f"GET {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
            self.assertIn("200 OK", response)
            self.assertIn(expected, response)

    def test_job_log_endpoint_returns_card_log(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = test_config(
            state_file=Path(tmp.name) / "state.sqlite3",
            worktree_root=Path(tmp.name) / "worktrees",
        )
        state = StateStore(cfg.state_file)
        worktree = cfg.worktree_root / "run-1"
        log_path = worktree / ".codex" / "logs" / "run.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("first\nsecond\n", encoding="utf-8")
        state.enqueue_job(kind="task", action_id="action-1", payload={"card_id": "card-1"})
        state.set_card("card-1", status="running", worktree=str(worktree), log_path=str(log_path))

        response, _ = self.handle(b"GET /api/jobs/1/logs HTTP/1.1\r\nHost: test\r\n\r\n", config=cfg, state=state)

        self.assertIn("200 OK", response)
        self.assertIn('"log": "first\\nsecond\\n"', response)
        self.assertIn('"status": "queued"', response)


if __name__ == "__main__":
    unittest.main()
