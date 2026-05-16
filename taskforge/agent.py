from __future__ import annotations

import re
import shlex
import subprocess
import time
import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .events import CardTaskEvent


@dataclass(frozen=True)
class AgentRunResult:
    branch: str
    worktree: Path
    exit_code: int
    output: str
    status: str
    summary: str
    question: str
    changed_files: tuple[str, ...]
    head_sha: str


def slugify(value: str, *, max_length: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (slug or "task")[:max_length].strip("-") or "task"


class CodexWorktreeRunner:
    def __init__(self, config: Config):
        self.config = config

    def run(
        self,
        event: CardTaskEvent,
        *,
        existing_branch: str = "",
        existing_worktree: str = "",
    ) -> AgentRunResult:
        self.config.require_safe_repo()
        branch, worktree = self._prepare_worktree(
            event,
            existing_branch=existing_branch,
            existing_worktree=existing_worktree,
        )
        prompt_file = self._write_prompt(event, branch, worktree, resumed=bool(existing_worktree))
        command = self._build_command(event, branch, worktree, prompt_file)

        timed_out = False
        try:
            completed = subprocess.run(
                command,
                cwd=worktree,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.config.codex_timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            output = completed.stdout
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            output = (exc.stdout or "") + "\nCodex command timed out."

        output = output[-8000:]
        self._write_log(worktree, output)
        result_data = self._read_result(worktree)
        if timed_out:
            result_data = {
                "status": "question",
                "summary": "Codex command timed out.",
                "question": "Should this task be split, clarified, or retried with a longer timeout?",
            }
        if exit_code == 0 and result_data["status"] in {"review", "done"}:
            commit = self.commit_changes(worktree, f"Implement Trello card {event.card_short_id}: {event.card_name}")
            if commit.returncode != 0:
                exit_code = commit.returncode
                output = (output + "\n" + commit.stdout)[-8000:]
                self._write_log(worktree, output)
                result_data = {
                    "status": "question",
                    "summary": "Implementation completed but could not be committed.",
                    "question": commit.stdout[-3000:],
                }
        return AgentRunResult(
            branch=branch,
            worktree=worktree,
            exit_code=exit_code,
            output=output,
            status=result_data["status"],
            summary=result_data["summary"],
            question=result_data["question"],
            changed_files=self.changed_files(worktree),
            head_sha=self.head_sha(worktree),
        )

    def _prepare_worktree(
        self,
        event: CardTaskEvent,
        *,
        existing_branch: str,
        existing_worktree: str,
    ) -> tuple[str, Path]:
        if existing_branch and existing_worktree:
            worktree = Path(existing_worktree)
            if worktree.exists():
                return existing_branch, worktree
        return self._create_worktree(event)

    def _create_worktree(self, event: CardTaskEvent) -> tuple[str, Path]:
        self.config.worktree_root.mkdir(parents=True, exist_ok=True)
        slug = slugify(event.card_name)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        branch = f"codex/trello-{event.card_short_id}-{slug}-{stamp}"
        worktree = self.config.worktree_root / f"{stamp}-{event.card_short_id}-{slug}"

        subprocess.run(
            ["git", "-C", str(self.config.target_repo), "rev-parse", "--is-inside-work-tree"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.config.target_repo),
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                self.config.base_branch,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return branch, worktree

    def _write_prompt(self, event: CardTaskEvent, branch: str, worktree: Path, *, resumed: bool) -> Path:
        prompt_dir = worktree / ".codex"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompt_dir / f"trello-task-{time.strftime('%Y%m%d-%H%M%S')}.md"
        prompt_file.write_text(
            "\n".join(
                [
                    "# Trello Task",
                    "",
                    f"Card: {event.card_name}",
                    f"Card ID: {event.card_id}",
                    f"Card URL: {event.card_url}",
                    f"Branch: {branch}",
                    f"Mode: {'resume existing worktree' if resumed else 'new worktree'}",
                    "",
                    "## Instructions",
                    "",
                    "Implement the requested task in this worktree.",
                    "Keep changes focused on the card requirements.",
                    "Run relevant tests or checks before finishing.",
                    "If required information is missing, stop and clearly describe the question.",
                    "",
                    "Before exiting, write `.codex/trello-result.json` with this schema:",
                    "",
                    "```json",
                    '{"status":"review","summary":"What changed and how it was checked.","question":""}',
                    "```",
                    "",
                    "Use `question` when blocked by missing requirements.",
                    "Use `review` when implementation is ready for tech lead review.",
                    "Use `done` only when the requested work is complete and no review is needed.",
                    "",
                    "For product work, treat these sections as authoritative when present:",
                    "Problem, Scope, Acceptance Criteria, Out of Scope, Dependencies, Test Plan.",
                    "",
                    "## Card Description",
                    "",
                    event.description or "(No description provided.)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return prompt_file

    def _build_command(
        self,
        event: CardTaskEvent,
        branch: str,
        worktree: Path,
        prompt_file: Path,
    ) -> list[str]:
        values = {
            "prompt_file": shlex.quote(str(prompt_file)),
            "workdir": shlex.quote(str(worktree)),
            "branch": shlex.quote(branch),
            "card_id": shlex.quote(event.card_id),
            "card_url": shlex.quote(event.card_url),
            "card_name": shlex.quote(event.card_name),
        }
        rendered = self.config.codex_command_template.format(**values)
        return shlex.split(rendered)

    def push_branch(self, branch: str, worktree: Path) -> None:
        subprocess.run(
            ["git", "-C", str(worktree), "push", "-u", self.config.remote_name, branch],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def head_sha(self, worktree: Path) -> str:
        completed = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def changed_files(self, worktree: Path) -> tuple[str, ...]:
        completed = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", self.config.base_branch, "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            return ()
        return tuple(line for line in completed.stdout.splitlines() if line)

    def commit_changes(self, worktree: Path, message: str) -> subprocess.CompletedProcess[str]:
        status = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if status.returncode != 0:
            return status
        if not status.stdout.strip():
            return subprocess.CompletedProcess(status.args, 0, "No changes to commit.")

        add = subprocess.run(
            ["git", "-C", str(worktree), "add", "-A"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if add.returncode != 0:
            return add
        return subprocess.run(
            ["git", "-C", str(worktree), "commit", "-m", message],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def _write_log(self, worktree: Path, output: str) -> None:
        log_dir = worktree / ".codex" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"
        log_file.write_text(output, encoding="utf-8")

    def _read_result(self, worktree: Path) -> dict[str, str]:
        result_file = worktree / ".codex" / "trello-result.json"
        default = {"status": "review", "summary": "", "question": ""}
        if not result_file.exists():
            return default

        try:
            raw = json.loads(result_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default

        status = str(raw.get("status", "review")).lower()
        if status not in {"question", "review", "done"}:
            status = "review"
        return {
            "status": status,
            "summary": str(raw.get("summary", "")),
            "question": str(raw.get("question", "")),
        }
