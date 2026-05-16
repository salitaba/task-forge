from __future__ import annotations

import html
import json
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .agent import CodexWorktreeRunner
from .config import Config
from .events import (
    command_event_from_payload,
    command_event_from_trello,
    command_event_to_payload,
    task_event_from_payload,
    task_event_from_trello,
    task_event_to_payload,
)
from .github import GitHubClient
from .processor import TaskProcessor
from .security import verify_trello_signature
from .state import StateStore
from .trello import TrelloClient


class JobWorker:
    def __init__(self, *, state: StateStore, processor: TaskProcessor, poll_interval: float):
        self.state = state
        self.processor = processor
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.state.requeue_running_jobs()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self.state.claim_next_job()
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            try:
                if job["kind"] == "task":
                    self.processor.process(task_event_from_payload(job["payload"]))
                elif job["kind"] == "command":
                    self.processor.process_command(command_event_from_payload(job["payload"]))
                else:
                    raise ValueError(f"unknown job kind: {job['kind']}")
                self.state.complete_job(job["id"])
            except Exception:
                self.state.fail_job(job["id"], traceback.format_exc())


class TrelloWebhookHandler(BaseHTTPRequestHandler):
    processor: TaskProcessor
    config: Config
    state: StateStore

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_HEAD(self) -> None:
        if urlparse(self.path).path != "/webhooks/trello":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json({"ok": True, "time": time.time()})
            return
        if path == "/api/state":
            self._json(self.state.read_all())
            return
        if path in {"/", "/dashboard"}:
            self._html(_dashboard_html(self.state.read_all()))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/webhooks/trello":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        if not verify_trello_signature(
            secret=self.config.trello_webhook_secret,
            callback_url=self.config.trello_callback_url,
            raw_body=raw_body,
            header_value=self.headers.get("X-Trello-Webhook"),
        ):
            self.send_error(HTTPStatus.UNAUTHORIZED, "invalid Trello webhook signature")
            return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return

        task = task_event_from_trello(payload, self.config)
        command = command_event_from_trello(payload)
        enqueued = False
        if task is not None:
            enqueued = self.state.enqueue_job(
                kind="task",
                action_id=task.action_id,
                payload=task_event_to_payload(task),
            )
        elif command is not None:
            enqueued = self.state.enqueue_job(
                kind="command",
                action_id=command.action_id,
                payload=command_event_to_payload(command),
            )

        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "enqueued": enqueued}).encode("utf-8"))

    def _json(self, data: dict[str, object]) -> None:
        body = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def make_processor(config: Config, state: StateStore) -> TaskProcessor:
    trello = TrelloClient(config)
    runner = CodexWorktreeRunner(config)
    github = GitHubClient(config) if config.enable_pr_creation else None
    return TaskProcessor(state, trello, runner, github)


def make_server(config: Config) -> ThreadingHTTPServer:
    state = StateStore(config.state_file)
    processor = make_processor(config, state)

    class Handler(TrelloWebhookHandler):
        pass

    Handler.processor = processor
    Handler.config = config
    Handler.state = state
    server = ThreadingHTTPServer((config.server_host, config.server_port), Handler)
    server.state = state
    server.worker = JobWorker(
        state=state,
        processor=processor,
        poll_interval=config.job_poll_interval_seconds,
    )
    return server


def run_server(config: Config) -> None:
    server = make_server(config)
    server.worker.start()
    print(f"listening on http://{config.server_host}:{config.server_port}/webhooks/trello")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        server.worker.stop()
        server.server_close()


def _dashboard_html(data: dict[str, object]) -> str:
    cards = data.get("cards", {})
    jobs = data.get("jobs", [])
    card_rows = []
    for card_id, card in cards.items():
        card_rows.append(
            "<tr>"
            f"<td>{html.escape(str(card_id))}</td>"
            f"<td>{html.escape(str(card.get('status', '')))}</td>"
            f"<td>{html.escape(str(card.get('branch', '')))}</td>"
            f"<td>{html.escape(str(card.get('pr_url', '')))}</td>"
            "</tr>"
        )
    job_rows = []
    for job in jobs:
        job_rows.append(
            "<tr>"
            f"<td>{job.get('id')}</td>"
            f"<td>{html.escape(str(job.get('kind', '')))}</td>"
            f"<td>{html.escape(str(job.get('status', '')))}</td>"
            f"<td>{html.escape(str(job.get('action_id', '')))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>TaskForge</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 32px; color: #17202a; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 32px; }}
    th, td {{ border-bottom: 1px solid #d8dee4; padding: 8px; text-align: left; }}
    th {{ background: #f6f8fa; }}
    code {{ background: #f6f8fa; padding: 2px 4px; }}
  </style>
</head>
<body>
  <h1>TaskForge</h1>
  <p><a href="/healthz">healthz</a> <a href="/api/state">state json</a></p>
  <h2>Cards</h2>
  <table><thead><tr><th>Card</th><th>Status</th><th>Branch</th><th>PR</th></tr></thead><tbody>{''.join(card_rows)}</tbody></table>
  <h2>Jobs</h2>
  <table><thead><tr><th>ID</th><th>Kind</th><th>Status</th><th>Action</th></tr></thead><tbody>{''.join(job_rows)}</tbody></table>
</body>
</html>"""

