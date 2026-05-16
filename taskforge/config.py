from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    value = _env(name, default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Config:
    server_host: str
    server_port: int
    trello_key: str
    trello_token: str
    trello_callback_url: str
    trello_webhook_secret: str
    trello_board_id: str
    trello_todo_list_id: str
    trello_question_list_id: str
    trello_review_list_id: str
    trello_done_list_id: str
    trello_running_label_id: str
    trello_question_label_id: str
    trello_review_label_id: str
    trello_done_label_id: str
    trello_failed_label_id: str
    trello_start_label_ids: tuple[str, ...]
    target_repo: Path
    repo_allowlist: tuple[Path, ...]
    base_branch: str
    remote_name: str
    worktree_root: Path
    codex_command_template: str
    codex_timeout_seconds: int
    max_concurrent_jobs: int
    required_card_sections: tuple[str, ...]
    github_token: str
    github_repo: str
    github_api_url: str
    enable_git_push: bool
    enable_pr_creation: bool
    pr_base_branch: str
    dry_run: bool
    state_file: Path
    job_poll_interval_seconds: float
    cleanup_statuses: tuple[str, ...]

    @classmethod
    def from_env_file(cls, path: str | Path = ".env") -> "Config":
        _load_dotenv(Path(path))
        return cls.from_env()

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            server_host=_env("SERVER_HOST", "0.0.0.0"),
            server_port=int(_env("SERVER_PORT", "8080")),
            trello_key=_env("TRELLO_KEY"),
            trello_token=_env("TRELLO_TOKEN"),
            trello_callback_url=_env("TRELLO_CALLBACK_URL"),
            trello_webhook_secret=_env("TRELLO_WEBHOOK_SECRET"),
            trello_board_id=_env("TRELLO_BOARD_ID"),
            trello_todo_list_id=_env("TRELLO_TODO_LIST_ID"),
            trello_question_list_id=_env("TRELLO_QUESTION_LIST_ID"),
            trello_review_list_id=_env("TRELLO_REVIEW_LIST_ID"),
            trello_done_list_id=_env("TRELLO_DONE_LIST_ID"),
            trello_running_label_id=_env("TRELLO_RUNNING_LABEL_ID"),
            trello_question_label_id=_env("TRELLO_QUESTION_LABEL_ID"),
            trello_review_label_id=_env("TRELLO_REVIEW_LABEL_ID"),
            trello_done_label_id=_env("TRELLO_DONE_LABEL_ID"),
            trello_failed_label_id=_env("TRELLO_FAILED_LABEL_ID"),
            trello_start_label_ids=_env_csv("TRELLO_START_LABEL_IDS"),
            target_repo=Path(_env("TARGET_REPO", ".")).expanduser().resolve(),
            repo_allowlist=tuple(
                Path(path).expanduser().resolve() for path in _env_csv("REPO_ALLOWLIST")
            ),
            base_branch=_env("BASE_BRANCH", "main"),
            remote_name=_env("REMOTE_NAME", "origin"),
            worktree_root=Path(_env("WORKTREE_ROOT", "./worktrees")).expanduser().resolve(),
            codex_command_template=_env(
                "CODEX_COMMAND_TEMPLATE",
                "codex exec --cd {workdir} --full-auto --input-file {prompt_file}",
            ),
            codex_timeout_seconds=_env_int("CODEX_TIMEOUT_SECONDS", 7200),
            max_concurrent_jobs=_env_int("MAX_CONCURRENT_JOBS", 1),
            required_card_sections=_env_csv(
                "REQUIRED_CARD_SECTIONS",
                "Problem,Scope,Acceptance Criteria,Test Plan",
            ),
            github_token=_env("GITHUB_TOKEN"),
            github_repo=_env("GITHUB_REPO"),
            github_api_url=_env("GITHUB_API_URL", "https://api.github.com"),
            enable_git_push=_env_bool("ENABLE_GIT_PUSH", False),
            enable_pr_creation=_env_bool("ENABLE_PR_CREATION", False),
            pr_base_branch=_env("PR_BASE_BRANCH", _env("BASE_BRANCH", "main")),
            dry_run=_env_bool("DRY_RUN", False),
            state_file=Path(_env("STATE_FILE", ".taskforge-state.sqlite3")).expanduser().resolve(),
            job_poll_interval_seconds=float(_env("JOB_POLL_INTERVAL_SECONDS", "1.0")),
            cleanup_statuses=_env_csv("CLEANUP_STATUSES", "done"),
        )

    def require_trello_api(self) -> None:
        missing = [
            name
            for name, value in {
                "TRELLO_KEY": self.trello_key,
                "TRELLO_TOKEN": self.trello_token,
                "TRELLO_CALLBACK_URL": self.trello_callback_url,
                "TRELLO_BOARD_ID": self.trello_board_id,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"missing required Trello configuration: {', '.join(missing)}")

    def require_safe_repo(self) -> None:
        if not self.repo_allowlist:
            return
        if self.target_repo not in self.repo_allowlist:
            allowed = ", ".join(str(path) for path in self.repo_allowlist)
            raise ValueError(f"TARGET_REPO is not in REPO_ALLOWLIST: {self.target_repo}; allowed: {allowed}")

    def require_github(self) -> None:
        missing = [
            name
            for name, value in {
                "GITHUB_TOKEN": self.github_token,
                "GITHUB_REPO": self.github_repo,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"missing required GitHub configuration: {', '.join(missing)}")
