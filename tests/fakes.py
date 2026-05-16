from __future__ import annotations

from pathlib import Path

from taskforge.agent import AgentRunResult
from taskforge.config import Config


def valid_description() -> str:
    return "\n".join(
        [
            "## Problem",
            "Users need this capability.",
            "## Scope",
            "Implement the requested behavior.",
            "## Acceptance Criteria",
            "- Behavior works",
            "## Test Plan",
            "- Run tests",
        ]
    )


def test_config(**overrides: object) -> Config:
    values = {
        "server_host": "127.0.0.1",
        "server_port": 8080,
        "trello_key": "key",
        "trello_token": "token",
        "trello_callback_url": "https://example.com/webhooks/trello",
        "trello_webhook_secret": "",
        "trello_board_id": "board",
        "trello_todo_list_id": "todo",
        "trello_question_list_id": "question",
        "trello_review_list_id": "review",
        "trello_done_list_id": "done",
        "trello_running_label_id": "running-label",
        "trello_question_label_id": "question-label",
        "trello_review_label_id": "review-label",
        "trello_done_label_id": "done-label",
        "trello_failed_label_id": "failed-label",
        "trello_start_label_ids": (),
        "target_repo": Path("."),
        "repo_allowlist": (),
        "base_branch": "main",
        "remote_name": "origin",
        "worktree_root": Path("worktrees"),
        "codex_command_template": "true",
        "codex_timeout_seconds": 7200,
        "max_concurrent_jobs": 1,
        "required_card_sections": ("Problem", "Scope", "Acceptance Criteria", "Test Plan"),
        "github_token": "",
        "github_repo": "",
        "github_api_url": "https://api.github.com",
        "enable_git_push": False,
        "enable_pr_creation": False,
        "pr_base_branch": "main",
        "dry_run": False,
        "state_file": Path("state.sqlite3"),
        "job_poll_interval_seconds": 0.01,
        "cleanup_statuses": ("done",),
    }
    values.update(overrides)
    return Config(**values)


def result(
    *,
    status: str = "review",
    exit_code: int = 0,
    branch: str = "codex/trello-7-task",
    worktree: Path = Path("/tmp/worktree"),
    summary: str = "Implemented and tested.",
    question: str = "",
    output: str = "ok",
    changed_files: tuple[str, ...] = ("app.py",),
    head_sha: str = "abc123",
) -> AgentRunResult:
    return AgentRunResult(
        branch=branch,
        worktree=worktree,
        exit_code=exit_code,
        output=output,
        status=status,
        summary=summary,
        question=question,
        changed_files=changed_files,
        head_sha=head_sha,
    )


class FakeTrello:
    def __init__(self, config: Config, desc: str | None = None, label_ids: tuple[str, ...] = ()):
        self.config = config
        self.desc = valid_description() if desc is None else desc
        self.label_ids = label_ids
        self.comments: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.labels_added: list[tuple[str, str]] = []
        self.labels_removed: list[tuple[str, str]] = []

    def get_card(self, card_id: str) -> dict[str, object]:
        return {
            "id": card_id,
            "idShort": 7,
            "name": "Build invite flow",
            "desc": self.desc,
            "shortUrl": "https://trello.test/c/abc",
            "idLabels": list(self.label_ids),
            "labels": [{"id": label_id} for label_id in self.label_ids],
        }

    def add_comment(self, card_id: str, text: str) -> dict[str, object]:
        self.comments.append((card_id, text))
        return {}

    def move_card(self, card_id: str, list_id: str) -> dict[str, object]:
        self.moves.append((card_id, list_id))
        return {}

    def set_status_label(self, card_id: str, status: str) -> None:
        labels = {
            "running": self.config.trello_running_label_id,
            "question": self.config.trello_question_label_id,
            "review": self.config.trello_review_label_id,
            "done": self.config.trello_done_label_id,
            "failed": self.config.trello_failed_label_id,
        }
        for label_status, label_id in labels.items():
            if not label_id:
                continue
            if label_status == status:
                self.labels_added.append((card_id, label_id))
            else:
                self.labels_removed.append((card_id, label_id))


class FakeRunner:
    def __init__(self, config: Config, run_result: AgentRunResult | None = None, error: Exception | None = None):
        self.config = config
        self.run_result = run_result or result()
        self.error = error
        self.calls: list[dict[str, str]] = []
        self.pushes: list[tuple[str, Path]] = []

    def run(self, event: object, *, existing_branch: str = "", existing_worktree: str = "") -> AgentRunResult:
        self.calls.append({"existing_branch": existing_branch, "existing_worktree": existing_worktree})
        if self.error:
            raise self.error
        return self.run_result

    def push_branch(self, branch: str, worktree: Path) -> None:
        self.pushes.append((branch, worktree))


class FakeGitHub:
    def __init__(self, pr_url: str = "https://github.test/owner/repo/pull/1", ci_state: str = "success"):
        self.pr_url = pr_url
        self.ci_state = ci_state
        self.created_prs: list[dict[str, str]] = []

    def create_pull_request(self, *, branch: str, title: str, body: str) -> dict[str, str]:
        self.created_prs.append({"branch": branch, "title": title, "body": body})
        return {"html_url": self.pr_url}

    def combined_status(self, sha: str) -> dict[str, str]:
        return {"state": self.ci_state}
