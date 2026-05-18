from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_actions (
                    action_id TEXT PRIMARY KEY,
                    processed_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS cards (
                    card_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    action_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def has_processed_action(self, action_id: str) -> bool:
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT 1 FROM processed_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            return row is not None

    def read_all(self) -> dict[str, Any]:
        with self._lock, self._db() as db:
            actions = [
                row["action_id"]
                for row in db.execute(
                    "SELECT action_id FROM processed_actions ORDER BY processed_at"
                ).fetchall()
            ]
            cards = {
                row["card_id"]: json.loads(row["data"])
                for row in db.execute("SELECT card_id, data FROM cards ORDER BY updated_at").fetchall()
            }
            jobs = [
                dict(row)
                for row in db.execute(
                    "SELECT id, kind, action_id, payload, status, attempts, last_error, created_at, updated_at "
                    "FROM jobs ORDER BY id"
                ).fetchall()
            ]
            for job in jobs:
                job["payload"] = json.loads(job["payload"])
            return {"processed_actions": actions, "cards": cards, "jobs": jobs}

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT id, kind, action_id, payload, status, attempts, last_error, created_at, updated_at "
                "FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            job["payload"] = json.loads(job["payload"])
            return job

    def mark_action_processed(self, action_id: str) -> None:
        now = time.time()
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO processed_actions(action_id, processed_at) VALUES (?, ?)",
                (action_id, now),
            )
            rows = db.execute(
                "SELECT action_id FROM processed_actions ORDER BY processed_at DESC LIMIT -1 OFFSET 1000"
            ).fetchall()
            if rows:
                db.executemany(
                    "DELETE FROM processed_actions WHERE action_id = ?",
                    [(row["action_id"],) for row in rows],
                )

    def card_status(self, card_id: str) -> str:
        return str(self.get_card(card_id).get("status", ""))

    def get_card(self, card_id: str) -> dict[str, Any]:
        with self._lock, self._db() as db:
            row = db.execute("SELECT data FROM cards WHERE card_id = ?", (card_id,)).fetchone()
            return json.loads(row["data"]) if row else {}

    def set_card(self, card_id: str, **fields: Any) -> None:
        now = time.time()
        with self._lock, self._db() as db:
            row = db.execute("SELECT data FROM cards WHERE card_id = ?", (card_id,)).fetchone()
            current = json.loads(row["data"]) if row else {}
            current.update(fields)
            db.execute(
                """
                INSERT INTO cards(card_id, data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
                """,
                (card_id, json.dumps(current, sort_keys=True), now),
            )

    def enqueue_job(self, *, kind: str, action_id: str, payload: dict[str, Any]) -> bool:
        now = time.time()
        with self._lock, self._db() as db:
            try:
                db.execute(
                    """
                    INSERT INTO jobs(kind, action_id, payload, status, attempts, created_at, updated_at)
                    VALUES (?, ?, ?, 'queued', 0, ?, ?)
                    """,
                    (kind, action_id, json.dumps(payload, sort_keys=True), now, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def claim_next_job(self) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            db.commit()
            job = dict(row)
            job["payload"] = json.loads(job["payload"])
            return job

    def complete_job(self, job_id: int) -> None:
        now = time.time()
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE jobs SET status = 'done', updated_at = ? WHERE id = ?",
                (now, job_id),
            )

    def fail_job(self, job_id: int, error: str) -> None:
        now = time.time()
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                (error[-4000:], now, job_id),
            )

    def requeue_running_jobs(self) -> int:
        now = time.time()
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (now,),
            )
            return cursor.rowcount
