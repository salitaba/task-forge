from __future__ import annotations

import html
import json
import logging
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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


logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, *, state: StateStore, processor: TaskProcessor, poll_interval: float):
        self.state = state
        self.processor = processor
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        requeued = self.state.requeue_running_jobs()
        logger.info("worker_start requeued_jobs=%s poll_interval=%s", requeued, self.poll_interval)
        self._thread.start()

    def stop(self) -> None:
        logger.info("worker_stop requested=true")
        self._stop.set()
        self._thread.join(timeout=5)
        logger.info("worker_stop complete=true alive=%s", self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self.state.claim_next_job()
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            try:
                logger.info(
                    "job_claimed id=%s kind=%s action_id=%s attempt=%s",
                    job["id"],
                    job["kind"],
                    job["action_id"],
                    job["attempts"] + 1,
                )
                if job["kind"] == "task":
                    self.processor.process(task_event_from_payload(job["payload"]))
                elif job["kind"] == "command":
                    self.processor.process_command(command_event_from_payload(job["payload"]))
                else:
                    raise ValueError(f"unknown job kind: {job['kind']}")
                self.state.complete_job(job["id"])
                logger.info("job_completed id=%s kind=%s action_id=%s", job["id"], job["kind"], job["action_id"])
            except Exception:
                error = traceback.format_exc()
                self.state.fail_job(job["id"], error)
                logger.exception("job_failed id=%s kind=%s action_id=%s", job["id"], job["kind"], job["action_id"])


class TrelloWebhookHandler(BaseHTTPRequestHandler):
    processor: TaskProcessor
    config: Config
    state: StateStore

    def log_message(self, format: str, *args: object) -> None:
        logger.info("http remote=%s message=%s", self.address_string(), format % args)

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
        job_log_id = _job_log_id(path)
        if job_log_id is not None:
            payload = _job_log_payload(self.state, self.config, job_log_id)
            if payload is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(payload)
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
        logger.info(
            "webhook_received remote=%s content_length=%s has_signature=%s",
            self.client_address[0],
            content_length,
            bool(self.headers.get("X-Trello-Webhook")),
        )
        if not verify_trello_signature(
            secret=self.config.trello_webhook_secret,
            callback_url=self.config.trello_callback_url,
            raw_body=raw_body,
            header_value=self.headers.get("X-Trello-Webhook"),
        ):
            logger.warning("webhook_rejected reason=invalid_signature remote=%s", self.client_address[0])
            self.send_error(HTTPStatus.UNAUTHORIZED, "invalid Trello webhook signature")
            return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            logger.warning("webhook_rejected reason=invalid_json remote=%s bytes=%s", self.client_address[0], len(raw_body))
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return
        if not isinstance(payload, dict):
            logger.warning("webhook_rejected reason=payload_not_object remote=%s", self.client_address[0])
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid JSON object")
            return

        logger.info("webhook_action %s", _trello_action_summary(payload))
        task = task_event_from_trello(payload, self.config)
        command = command_event_from_trello(payload)
        enqueued = False
        if task is not None:
            enqueued = self.state.enqueue_job(
                kind="task",
                action_id=task.action_id,
                payload=task_event_to_payload(task),
            )
            logger.info(
                "webhook_decision decision=enqueue_task enqueued=%s action_id=%s card_id=%s card_short_id=%s source=%r labels=%s",
                enqueued,
                task.action_id,
                task.card_id,
                task.card_short_id,
                task.source,
                ",".join(task.label_ids),
            )
        elif command is not None:
            enqueued = self.state.enqueue_job(
                kind="command",
                action_id=command.action_id,
                payload=command_event_to_payload(command),
            )
            logger.info(
                "webhook_decision decision=enqueue_command enqueued=%s action_id=%s card_id=%s command=%s source=%r",
                enqueued,
                command.action_id,
                command.card_id,
                command.command,
                command.source,
            )
        else:
            action = payload.get("action") or {}
            logger.info(
                "webhook_decision decision=ignore reason=%s action_id=%s action_type=%s",
                _webhook_ignore_reason(payload, self.config),
                action.get("id", ""),
                action.get("type", ""),
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = make_server(config)
    server.worker.start()
    logger.info("server_listening url=http://%s:%s/webhooks/trello", config.server_host, config.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("server_stopping reason=keyboard_interrupt")
    finally:
        server.worker.stop()
        server.server_close()


def _trello_action_summary(payload: dict[str, object]) -> str:
    action = payload.get("action") if isinstance(payload, dict) else {}
    if not isinstance(action, dict):
        return "action_id= action_type= card_id= card_name= list_before= list_after= comment_length=0"
    data = action.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    card = data.get("card") or {}
    if not isinstance(card, dict):
        card = {}
    list_data = data.get("list") or {}
    list_before = data.get("listBefore") or {}
    list_after = data.get("listAfter") or {}
    if not isinstance(list_data, dict):
        list_data = {}
    if not isinstance(list_before, dict):
        list_before = {}
    if not isinstance(list_after, dict):
        list_after = {}
    text = str(data.get("text") or "")
    desc = str(card.get("desc") or "")
    labels = _ids_from_sequence(card.get("idLabels")) or _ids_from_sequence(card.get("labels"))
    return (
        f"action_id={action.get('id', '')} "
        f"action_type={action.get('type', '')} "
        f"card_id={card.get('id', '')} "
        f"card_short_id={card.get('idShort', '')} "
        f"card_name={str(card.get('name') or '')!r} "
        f"list_id={list_data.get('id', '')} "
        f"list_before={list_before.get('id', '')} "
        f"list_after={list_after.get('id', '')} "
        f"labels={','.join(labels)} "
        f"description_chars={len(desc)} "
        f"comment_length={len(text)}"
    )


def _webhook_ignore_reason(payload: dict[str, object], config: Config) -> str:
    action = payload.get("action") or {}
    if not isinstance(action, dict):
        return "missing_action"
    if not action.get("id"):
        return "missing_action_id"

    action_type = action.get("type")
    data = action.get("data") or {}
    if not isinstance(data, dict):
        return "missing_action_data"
    card = data.get("card") or {}
    if not isinstance(card, dict) or not card.get("id"):
        return "missing_card_id"

    if action_type == "createCard":
        list_data = data.get("list") or {}
        if not isinstance(list_data, dict) or list_data.get("id") != config.trello_todo_list_id:
            return "create_card_not_todo_list"
        if _missing_required_start_label(card, config):
            return "missing_start_label"
        return "create_card_not_task"

    if action_type == "updateCard":
        list_before = data.get("listBefore") or {}
        list_after = data.get("listAfter") or {}
        if not isinstance(list_after, dict) or list_after.get("id") != config.trello_todo_list_id:
            return "update_card_not_todo_list"
        if isinstance(list_before, dict) and list_before.get("id") == list_after.get("id"):
            return "card_not_moved_between_lists"
        if _missing_required_start_label(card, config):
            return "missing_start_label"
        return "update_card_not_task"

    if action_type == "commentCard":
        text = str(data.get("text") or "").strip()
        if not text.lower().startswith("/codex"):
            return "comment_not_codex_command"
        return "comment_not_command"

    return "unsupported_action_type"


def _missing_required_start_label(card: dict[str, object], config: Config) -> bool:
    required = set(config.trello_start_label_ids)
    if not required:
        return False
    if "labels" not in card and "idLabels" not in card:
        return False
    labels = set(_ids_from_sequence(card.get("idLabels")) or _ids_from_sequence(card.get("labels")))
    return not bool(required.intersection(labels))


def _ids_from_sequence(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    ids = []
    for item in value:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
        elif item:
            ids.append(str(item))
    return ids


def _job_log_id(path: str) -> int | None:
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[:2] != ["api", "jobs"] or parts[3] != "logs":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def _job_log_payload(state: StateStore, config: Config, job_id: int) -> dict[str, object] | None:
    job = state.get_job(job_id)
    if job is None:
        return None

    payload = job.get("payload", {})
    card_id = payload.get("card_id", "") if isinstance(payload, dict) else ""
    card = state.get_card(str(card_id)) if card_id else {}
    log_path = Path(str(card.get("log_path", ""))) if card.get("log_path") else None
    log = ""
    if log_path is not None and _path_is_relative_to(log_path, config.worktree_root):
        try:
            log = log_path.read_text(encoding="utf-8", errors="replace")[-120000:]
        except OSError:
            log = ""
    if not log and job.get("last_error"):
        log = str(job.get("last_error", ""))

    return {
        "job_id": job["id"],
        "card_id": str(card_id),
        "status": job["status"],
        "action_id": job["action_id"],
        "log_path": str(log_path or ""),
        "log": log,
    }


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


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
        job_id = html.escape(str(job.get("id", "")))
        job_rows.append(
            "<tr>"
            f"<td>{job_id}</td>"
            f"<td>{html.escape(str(job.get('kind', '')))}</td>"
            f"<td>{html.escape(str(job.get('status', '')))}</td>"
            f"<td>{html.escape(str(job.get('action_id', '')))}</td>"
            f"<td><button type=\"button\" data-job-log=\"{job_id}\">Logs</button></td>"
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
    button {{ border: 1px solid #b8c0cc; background: #fff; border-radius: 6px; padding: 5px 10px; cursor: pointer; }}
    pre {{ background: #0f1720; color: #d6e2f0; padding: 16px; min-height: 280px; max-height: 60vh; overflow: auto; white-space: pre-wrap; }}
    #logPanel[hidden] {{ display: none; }}
  </style>
</head>
<body>
  <h1>TaskForge</h1>
  <p><a href="/healthz">healthz</a> <a href="/api/state">state json</a></p>
  <h2>Cards</h2>
  <table><thead><tr><th>Card</th><th>Status</th><th>Branch</th><th>PR</th></tr></thead><tbody>{''.join(card_rows)}</tbody></table>
  <h2>Jobs</h2>
  <table><thead><tr><th>ID</th><th>Kind</th><th>Status</th><th>Action</th><th>Logs</th></tr></thead><tbody>{''.join(job_rows)}</tbody></table>
  <section id="logPanel" hidden>
    <h2 id="logTitle">Job Log</h2>
    <p><code id="logPath"></code></p>
    <pre id="logOutput"></pre>
  </section>
  <script>
    let activeJobId = "";
    let logTimer = 0;

    async function loadJobLog() {{
      if (!activeJobId) return;
      const response = await fetch(`/api/jobs/${{activeJobId}}/logs`, {{ cache: "no-store" }});
      if (!response.ok) return;
      const data = await response.json();
      document.getElementById("logTitle").textContent = `Job ${{data.job_id}} Log (${{
        data.status
      }})`;
      document.getElementById("logPath").textContent = data.log_path || "";
      const output = document.getElementById("logOutput");
      output.textContent = data.log || "(No output yet.)";
      output.scrollTop = output.scrollHeight;
      clearTimeout(logTimer);
      if (data.status === "queued" || data.status === "running") {{
        logTimer = setTimeout(loadJobLog, 1500);
      }}
    }}

    document.addEventListener("click", (event) => {{
      const button = event.target.closest("[data-job-log]");
      if (!button) return;
      activeJobId = button.dataset.jobLog;
      document.getElementById("logPanel").hidden = false;
      loadJobLog();
    }});
  </script>
</body>
</html>"""
