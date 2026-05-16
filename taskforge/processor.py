from __future__ import annotations

import threading
import subprocess
import traceback
from dataclasses import replace

from .agent import CodexWorktreeRunner
from .card_contract import validate_card_contract
from .cleanup import cleanup_card_worktree
from .events import CardCommandEvent, CardTaskEvent
from .github import GitHubClient
from .state import StateStore
from .trello import TrelloClient


class TaskProcessor:
    def __init__(
        self,
        state: StateStore,
        trello: TrelloClient,
        runner: CodexWorktreeRunner,
        github: GitHubClient | None = None,
    ):
        self.state = state
        self.trello = trello
        self.runner = runner
        self.github = github
        self._semaphore = threading.Semaphore(max(1, runner.config.max_concurrent_jobs))

    def process(self, event: CardTaskEvent) -> None:
        with self._semaphore:
            self._process(event)

    def process_command(self, event: CardCommandEvent) -> None:
        if self.state.has_processed_action(event.action_id):
            return
        self.state.mark_action_processed(event.action_id)

        if event.command == "retry":
            self.state.set_card(event.card_id, status="question")
            self._set_status(event.card_id, "question")
            self.trello.add_comment(
                event.card_id,
                "Retry requested. Move this card back to To Do or run `python3 -m taskforge run-card "
                f"{event.card_id}` to resume.",
            )
            return

        if event.command == "stop":
            self.state.set_card(event.card_id, status="stopped")
            self.trello.add_comment(event.card_id, "Codex automation marked this card as stopped.")
            return

        if event.command == "done":
            self.state.set_card(event.card_id, status="done")
            self._set_status(event.card_id, "done")
            if self.trello.config.trello_done_list_id:
                self.trello.move_card(event.card_id, self.trello.config.trello_done_list_id)
            self.trello.add_comment(event.card_id, "Marked done from Trello command.")
            return

        if event.command == "cleanup":
            result = cleanup_card_worktree(
                config=self.runner.config,
                state=self.state,
                card_id=event.card_id,
                dry_run=self.runner.config.dry_run,
            )
            self.trello.add_comment(event.card_id, f"Cleanup result: `{result}`")
            return

        self.trello.add_comment(
            event.card_id,
            "Supported commands: `/codex retry`, `/codex stop`, `/codex done`, `/codex cleanup`.",
        )

    def _process(self, event: CardTaskEvent) -> None:
        existing_before = self.state.get_card(event.card_id)
        is_same_running_action = (
            existing_before.get("status") == "running"
            and existing_before.get("action_id") == event.action_id
        )
        if self.state.has_processed_action(event.action_id) and not is_same_running_action:
            return
        if self.state.card_status(event.card_id) in {"running", "review", "done"} and not is_same_running_action:
            self.state.mark_action_processed(event.action_id)
            return

        event = self._refresh_event(event)
        if not self._has_start_label(event):
            self.state.mark_action_processed(event.action_id)
            return

        contract = validate_card_contract(event.description, self.runner.config.required_card_sections)
        if not contract.is_valid:
            self.state.mark_action_processed(event.action_id)
            self._question(
                event,
                "\n".join(
                    [
                        "This card needs product detail before Codex can implement it.",
                        "",
                        "Missing required sections:",
                        *[f"- {section}" for section in contract.missing_sections],
                        "",
                        "Expected card template:",
                        "",
                        "```md",
                        "## Problem",
                        "## Scope",
                        "## Acceptance Criteria",
                        "## Test Plan",
                        "```",
                    ]
                ),
            )
            return

        self.state.mark_action_processed(event.action_id)

        if self.runner.config.dry_run:
            self.state.set_card(event.card_id, status="dry_run", action_id=event.action_id)
            self.trello.add_comment(
                event.card_id,
                "Dry run: card passed validation. Codex, git changes, push, and PR creation were skipped.",
            )
            return

        self.state.set_card(event.card_id, status="running", action_id=event.action_id)
        self._set_status(event.card_id, "running")

        self.trello.add_comment(
            event.card_id,
            f"Codex picked this up ({event.source}). Creating a branch and isolated worktree now.",
        )

        existing = self.state.get_card(event.card_id)
        try:
            result = self.runner.run(
                event,
                existing_branch=str(existing.get("branch", "")),
                existing_worktree=str(existing.get("worktree", "")),
            )
        except subprocess.CalledProcessError as exc:
            output = (exc.stderr or exc.stdout or str(exc))[-3000:]
            self._failed(event, f"Could not prepare the git worktree.\n\n```text\n{output}\n```")
            return
        except FileNotFoundError as exc:
            self._failed(event, f"Codex or git command was not found: `{exc.filename}`.")
            return
        except Exception:
            self._failed(event, f"Unexpected runner failure.\n\n```text\n{traceback.format_exc()[-3000:]}\n```")
            return

        if result.exit_code == 0 and result.status == "done":
            pr = self._publish(event, result)
            if pr.get("error"):
                self._question(event, str(pr["error"]))
                return
            self.state.set_card(
                event.card_id,
                status="done",
                branch=result.branch,
                worktree=str(result.worktree),
                pr_url=pr.get("url", ""),
                head_sha=result.head_sha,
            )
            lines = [
                "Codex marked this task done.",
                "",
                f"Branch: `{result.branch}`",
                f"Worktree: `{result.worktree}`",
            ]
            if pr.get("url"):
                lines.append(f"Pull request: {pr['url']}")
            if result.changed_files:
                lines.extend(["", "Changed files:", *[f"- `{path}`" for path in result.changed_files]])
            if result.summary:
                lines.extend(["", result.summary])
            self.trello.add_comment(event.card_id, "\n".join(lines))
            self._set_status(event.card_id, "done")
            if self.trello.config.trello_done_list_id:
                self.trello.move_card(event.card_id, self.trello.config.trello_done_list_id)
            return

        if result.exit_code == 0 and result.status == "review":
            pr = self._publish(event, result)
            if pr.get("error"):
                self._question(event, str(pr["error"]))
                return
            self.state.set_card(
                event.card_id,
                status="review",
                branch=result.branch,
                worktree=str(result.worktree),
                pr_url=pr.get("url", ""),
                head_sha=result.head_sha,
            )
            lines = [
                "Codex finished implementation and this is ready for tech lead review.",
                "",
                f"Branch: `{result.branch}`",
                f"Worktree: `{result.worktree}`",
            ]
            if pr.get("url"):
                lines.append(f"Pull request: {pr['url']}")
            if pr.get("ci_state"):
                lines.append(f"GitHub commit status: `{pr['ci_state']}`")
            if result.changed_files:
                lines.extend(["", "Changed files:", *[f"- `{path}`" for path in result.changed_files]])
            if result.summary:
                lines.extend(["", result.summary])
            self.trello.add_comment(event.card_id, "\n".join(lines))
            self._set_status(event.card_id, "review")
            if self.trello.config.trello_review_list_id:
                self.trello.move_card(event.card_id, self.trello.config.trello_review_list_id)
            return

        if result.exit_code == 0 and result.status == "question":
            self._question(
                event,
                "\n".join(
                    [
                        "Codex needs input before it can continue.",
                        "",
                        result.question or result.summary or "No question was provided.",
                        "",
                        f"Branch: `{result.branch}`",
                        f"Worktree: `{result.worktree}`",
                    ]
                ),
            )
            return

        self._question(
            event,
            "\n".join(
                [
                    "Codex stopped before completing the task and needs attention.",
                    "",
                    "Last output:",
                    "",
                    "```text",
                    result.output[-3000:],
                    "```",
                    "",
                    f"Branch: `{result.branch}`",
                    f"Worktree: `{result.worktree}`",
                ]
            ),
        )

    def _question(self, event: CardTaskEvent, message: str) -> None:
        self.state.set_card(event.card_id, status="question")
        self._set_status(event.card_id, "question")
        self.trello.add_comment(event.card_id, message)
        if self.trello.config.trello_question_list_id:
            self.trello.move_card(event.card_id, self.trello.config.trello_question_list_id)

    def _failed(self, event: CardTaskEvent, message: str) -> None:
        self.state.set_card(event.card_id, status="failed")
        self._set_status(event.card_id, "failed")
        self.trello.add_comment(event.card_id, message)
        if self.trello.config.trello_question_list_id:
            self.trello.move_card(event.card_id, self.trello.config.trello_question_list_id)

    def _set_status(self, card_id: str, status: str) -> None:
        try:
            self.trello.set_status_label(card_id, status)
        except Exception as exc:
            print(f"could not update Trello labels for {card_id}: {exc}")

    def _refresh_event(self, event: CardTaskEvent) -> CardTaskEvent:
        try:
            card = self.trello.get_card(event.card_id)
        except Exception as exc:
            print(f"could not refresh Trello card {event.card_id}: {exc}")
            return event

        return replace(
            event,
            card_name=card.get("name") or event.card_name,
            card_url=card.get("shortUrl") or card.get("url") or event.card_url,
            description=card.get("desc") or event.description,
            card_short_id=str(card.get("idShort") or event.card_short_id),
            label_ids=self._label_ids(card) or event.label_ids,
        )

    def _has_start_label(self, event: CardTaskEvent) -> bool:
        required = set(self.runner.config.trello_start_label_ids)
        if not required:
            return True
        return bool(required.intersection(event.label_ids))

    def _label_ids(self, card: dict[str, object]) -> tuple[str, ...]:
        label_ids = []
        for label in card.get("labels") or []:
            if isinstance(label, dict) and label.get("id"):
                label_ids.append(str(label["id"]))
        for label_id in card.get("idLabels") or []:
            if label_id:
                label_ids.append(str(label_id))
        return tuple(dict.fromkeys(label_ids))

    def _publish(self, event: CardTaskEvent, result: object) -> dict[str, str]:
        if not (self.runner.config.enable_git_push or self.runner.config.enable_pr_creation):
            return {}
        try:
            self.runner.push_branch(result.branch, result.worktree)
        except subprocess.CalledProcessError as exc:
            output = (exc.stderr or exc.stdout or str(exc))[-3000:]
            return {"error": f"Implementation is ready, but pushing the branch failed.\n\n```text\n{output}\n```"}

        if not self.runner.config.enable_pr_creation:
            return {}
        if self.github is None:
            return {"error": "ENABLE_PR_CREATION is true, but GitHub client is not configured."}

        try:
            body = self._pr_body(event, result)
            pr = self.github.create_pull_request(
                branch=result.branch,
                title=f"{event.card_name}",
                body=body,
            )
            response = {"url": str(pr.get("html_url", ""))}
            if result.head_sha:
                try:
                    status = self.github.combined_status(result.head_sha)
                    response["ci_state"] = str(status.get("state", ""))
                except Exception as exc:
                    response["ci_state"] = f"unknown ({exc})"
            return response
        except Exception as exc:
            return {"error": f"Implementation is ready, but creating the pull request failed: {exc}"}

    def _pr_body(self, event: CardTaskEvent, result: object) -> str:
        changed = "\n".join(f"- `{path}`" for path in result.changed_files) or "- No files reported"
        return "\n".join(
            [
                f"Trello card: {event.card_url or event.card_id}",
                "",
                "## Summary",
                "",
                result.summary or "Implemented by Codex.",
                "",
                "## Changed Files",
                "",
                changed,
                "",
                "## Review",
                "",
                "Please verify acceptance criteria, product behavior, and test coverage before merging.",
            ]
        )
