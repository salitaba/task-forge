from __future__ import annotations

import threading
import logging
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


logger = logging.getLogger(__name__)


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
        logger.info(
            "task_process_waiting action_id=%s card_id=%s card_short_id=%s source=%r",
            event.action_id,
            event.card_id,
            event.card_short_id,
            event.source,
        )
        with self._semaphore:
            logger.info("task_process_started action_id=%s card_id=%s", event.action_id, event.card_id)
            self._process(event)

    def process_command(self, event: CardCommandEvent) -> None:
        logger.info(
            "command_process_started action_id=%s card_id=%s command=%s source=%r text_length=%s",
            event.action_id,
            event.card_id,
            event.command,
            event.source,
            len(event.text),
        )
        existing_before = self.state.get_card(event.card_id)
        is_same_running_feedback = (
            event.command == "feedback"
            and existing_before.get("status") == "running"
            and existing_before.get("action_id") == event.action_id
        )
        action_processed = self.state.has_processed_action(event.action_id)
        if action_processed and not is_same_running_feedback:
            logger.info("command_decision decision=ignore reason=duplicate_action action_id=%s card_id=%s", event.action_id, event.card_id)
            return
        if not action_processed:
            self.state.mark_action_processed(event.action_id)

        if event.command == "retry":
            logger.info("command_decision decision=retry card_id=%s action_id=%s", event.card_id, event.action_id)
            self.state.set_card(event.card_id, status="question")
            self._set_status(event.card_id, "question")
            self.trello.add_comment(
                event.card_id,
                "Retry requested. Move this card back to To Do or run `python3 -m taskforge run-card "
                f"{event.card_id}` to resume.",
            )
            return

        if event.command == "stop":
            logger.info("command_decision decision=stop card_id=%s action_id=%s", event.card_id, event.action_id)
            self.state.set_card(event.card_id, status="stopped")
            self.trello.add_comment(event.card_id, "Codex automation marked this card as stopped.")
            return

        if event.command == "done":
            logger.info("command_decision decision=done card_id=%s action_id=%s", event.card_id, event.action_id)
            self.state.set_card(event.card_id, status="done")
            self._set_status(event.card_id, "done")
            if self.trello.config.trello_done_list_id:
                logger.info("command_side_effect action=move_card card_id=%s list_id=%s", event.card_id, self.trello.config.trello_done_list_id)
                self.trello.move_card(event.card_id, self.trello.config.trello_done_list_id)
            self.trello.add_comment(event.card_id, "Marked done from Trello command.")
            return

        if event.command == "cleanup":
            logger.info("command_decision decision=cleanup card_id=%s action_id=%s", event.card_id, event.action_id)
            result = cleanup_card_worktree(
                config=self.runner.config,
                state=self.state,
                card_id=event.card_id,
                dry_run=self.runner.config.dry_run,
            )
            self.trello.add_comment(event.card_id, f"Cleanup result: `{result}`")
            return

        if event.command == "feedback":
            logger.info("command_decision decision=review_feedback card_id=%s action_id=%s", event.card_id, event.action_id)
            self._process_review_feedback(event, resume_running_action=is_same_running_feedback)
            return

        logger.info("command_decision decision=help card_id=%s action_id=%s command=%s", event.card_id, event.action_id, event.command)
        self.trello.add_comment(
            event.card_id,
            "Supported commands: `/codex retry`, `/codex stop`, `/codex done`, `/codex cleanup`, "
            "or `/codex <review feedback>` on a card in Review.",
        )

    def _process_review_feedback(self, event: CardCommandEvent, *, resume_running_action: bool = False) -> None:
        existing = self.state.get_card(event.card_id)
        card = self._safe_get_card(event.card_id)
        if not resume_running_action and not self._is_review_feedback_card(event, existing, card):
            logger.info(
                "review_feedback_decision decision=help reason=not_review_card card_id=%s action_id=%s status=%s list_id=%s",
                event.card_id,
                event.action_id,
                existing.get("status", ""),
                event.list_id or card.get("idList", ""),
            )
            self.trello.add_comment(
                event.card_id,
                "Review feedback commands only run for cards in Review. Supported commands: "
                "`/codex retry`, `/codex stop`, `/codex done`, `/codex cleanup`.",
            )
            return

        if existing.get("status") == "running" and not resume_running_action:
            logger.info("review_feedback_decision decision=ignore reason=already_running card_id=%s action_id=%s", event.card_id, event.action_id)
            self.trello.add_comment(event.card_id, "Codex is already running for this card.")
            return

        existing_branch = str(existing.get("branch", ""))
        existing_worktree = str(existing.get("worktree", ""))
        if not existing_branch or not existing_worktree:
            logger.info("review_feedback_decision decision=question reason=missing_branch_or_worktree card_id=%s action_id=%s", event.card_id, event.action_id)
            self.state.set_card(event.card_id, status="question")
            self._set_status(event.card_id, "question")
            self.trello.add_comment(
                event.card_id,
                "I can only apply review feedback when this card has a recorded branch and worktree from the initial run. "
                "Move it back to To Do or run it manually to recreate the implementation context.",
            )
            return

        feedback = self._review_feedback_text(event.text)
        review_event = self._review_feedback_task_event(event, card, feedback)
        self.state.set_card(event.card_id, status="running", action_id=event.action_id)
        self._set_status(event.card_id, "running")
        if resume_running_action:
            self.trello.add_comment(
                event.card_id,
                "Codex resumed this tech lead review comment after a restart. Reusing the existing branch and worktree to update the PR.",
            )
        else:
            self.trello.add_comment(
                event.card_id,
                "Codex picked up this tech lead review comment. Reusing the existing branch and worktree to update the PR.",
            )

        try:
            result = self.runner.run(
                review_event,
                existing_branch=existing_branch,
                existing_worktree=existing_worktree,
                on_started=lambda branch, worktree, log_file: self.state.set_card(
                    event.card_id,
                    status="running",
                    action_id=event.action_id,
                    branch=branch,
                    worktree=str(worktree),
                    log_path=str(log_file),
                ),
            )
        except subprocess.CalledProcessError as exc:
            output = (exc.stderr or exc.stdout or str(exc))[-3000:]
            logger.warning("review_feedback_runner_failed card_id=%s reason=git_command_error output_chars=%s", event.card_id, len(output))
            self._failed(review_event, f"Could not prepare the existing git worktree.\n\n```text\n{output}\n```")
            return
        except FileNotFoundError as exc:
            logger.warning("review_feedback_runner_failed card_id=%s reason=file_not_found filename=%s", event.card_id, exc.filename)
            self._failed(review_event, f"Codex or git command was not found: `{exc.filename}`.")
            return
        except Exception:
            logger.exception("review_feedback_runner_failed card_id=%s reason=unexpected_exception", event.card_id)
            self._failed(review_event, f"Unexpected runner failure.\n\n```text\n{traceback.format_exc()[-3000:]}\n```")
            return

        logger.info(
            "review_feedback_runner_finished action_id=%s card_id=%s exit_code=%s status=%s branch=%r worktree=%r changed_files=%s head_sha=%r",
            event.action_id,
            event.card_id,
            result.exit_code,
            result.status,
            result.branch,
            str(result.worktree),
            ",".join(result.changed_files),
            result.head_sha,
        )
        if result.exit_code == 0 and result.status in {"review", "done"}:
            pr = self._publish(review_event, result, existing_pr_url=str(existing.get("pr_url", "")))
            if pr.get("error"):
                logger.info("review_feedback_decision decision=question reason=publish_error action_id=%s card_id=%s", event.action_id, event.card_id)
                self._question(review_event, str(pr["error"]))
                return

            final_status = "done" if result.status == "done" else "review"
            self.state.set_card(
                event.card_id,
                status=final_status,
                branch=result.branch,
                worktree=str(result.worktree),
                pr_url=pr.get("url", "") or str(existing.get("pr_url", "")),
                head_sha=result.head_sha,
            )
            lines = [
                "Codex addressed the tech lead review feedback and updated the PR.",
                "",
                f"Branch: `{result.branch}`",
                f"Worktree: `{result.worktree}`",
            ]
            if pr.get("url") or existing.get("pr_url"):
                lines.append(f"Pull request: {pr.get('url') or existing.get('pr_url')}")
            if pr.get("ci_state"):
                lines.append(f"GitHub commit status: `{pr['ci_state']}`")
            if result.changed_files:
                lines.extend(["", "Changed files:", *[f"- `{path}`" for path in result.changed_files]])
            if result.summary:
                lines.extend(["", result.summary])
            self.trello.add_comment(event.card_id, "\n".join(lines))
            self._set_status(event.card_id, final_status)
            if final_status == "done" and self.trello.config.trello_done_list_id:
                self.trello.move_card(event.card_id, self.trello.config.trello_done_list_id)
            elif final_status == "review" and self.trello.config.trello_review_list_id:
                self.trello.move_card(event.card_id, self.trello.config.trello_review_list_id)
            return

        if result.exit_code == 0 and result.status == "question":
            self._question(
                review_event,
                "\n".join(
                    [
                        "Codex needs input before it can address the tech lead review feedback.",
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
            review_event,
            "\n".join(
                [
                    "Codex stopped before completing the review feedback and needs attention.",
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

    def _process(self, event: CardTaskEvent) -> None:
        existing_before = self.state.get_card(event.card_id)
        logger.info(
            "task_state_loaded action_id=%s card_id=%s existing_status=%s existing_action_id=%s",
            event.action_id,
            event.card_id,
            existing_before.get("status", ""),
            existing_before.get("action_id", ""),
        )
        is_same_running_action = (
            existing_before.get("status") == "running"
            and existing_before.get("action_id") == event.action_id
        )
        if self.state.has_processed_action(event.action_id) and not is_same_running_action:
            logger.info("task_decision decision=ignore reason=duplicate_action action_id=%s card_id=%s", event.action_id, event.card_id)
            return
        if self.state.card_status(event.card_id) in {"running", "review", "done"} and not is_same_running_action:
            logger.info(
                "task_decision decision=ignore reason=card_already_terminal_or_running action_id=%s card_id=%s status=%s",
                event.action_id,
                event.card_id,
                self.state.card_status(event.card_id),
            )
            self.state.mark_action_processed(event.action_id)
            return

        event = self._refresh_event(event)
        logger.info(
            "task_refreshed action_id=%s card_id=%s card_short_id=%s card_name=%r labels=%s description_chars=%s",
            event.action_id,
            event.card_id,
            event.card_short_id,
            event.card_name,
            ",".join(event.label_ids),
            len(event.description),
        )
        if not self._has_start_label(event):
            logger.info(
                "task_decision decision=ignore reason=missing_start_label action_id=%s card_id=%s required_labels=%s actual_labels=%s",
                event.action_id,
                event.card_id,
                ",".join(self.runner.config.trello_start_label_ids),
                ",".join(event.label_ids),
            )
            self.state.mark_action_processed(event.action_id)
            return

        contract = validate_card_contract(event.description, self.runner.config.required_card_sections)
        if not contract.is_valid:
            logger.info(
                "task_decision decision=question reason=invalid_card_contract action_id=%s card_id=%s missing_sections=%s",
                event.action_id,
                event.card_id,
                ",".join(contract.missing_sections),
            )
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
            logger.info("task_decision decision=dry_run action_id=%s card_id=%s", event.action_id, event.card_id)
            self.state.set_card(event.card_id, status="dry_run", action_id=event.action_id)
            self.trello.add_comment(
                event.card_id,
                "Dry run: card passed validation. Codex, git changes, push, and PR creation were skipped.",
            )
            return

        self.state.set_card(event.card_id, status="running", action_id=event.action_id)
        logger.info("task_decision decision=run_codex action_id=%s card_id=%s", event.action_id, event.card_id)
        self._set_status(event.card_id, "running")

        self.trello.add_comment(
            event.card_id,
            f"Codex picked this up ({event.source}). Creating a branch and isolated worktree now.",
        )

        existing = self.state.get_card(event.card_id)
        logger.info(
            "runner_start action_id=%s card_id=%s existing_branch=%r existing_worktree=%r",
            event.action_id,
            event.card_id,
            existing.get("branch", ""),
            existing.get("worktree", ""),
        )
        try:
            result = self.runner.run(
                event,
                existing_branch=str(existing.get("branch", "")),
                existing_worktree=str(existing.get("worktree", "")),
                on_started=lambda branch, worktree, log_file: self.state.set_card(
                    event.card_id,
                    status="running",
                    action_id=event.action_id,
                    branch=branch,
                    worktree=str(worktree),
                    log_path=str(log_file),
                ),
            )
        except subprocess.CalledProcessError as exc:
            output = (exc.stderr or exc.stdout or str(exc))[-3000:]
            logger.warning("runner_failed action_id=%s card_id=%s reason=git_command_error output_chars=%s", event.action_id, event.card_id, len(output))
            self._failed(event, f"Could not prepare the git worktree.\n\n```text\n{output}\n```")
            return
        except FileNotFoundError as exc:
            logger.warning("runner_failed action_id=%s card_id=%s reason=file_not_found filename=%s", event.action_id, event.card_id, exc.filename)
            self._failed(event, f"Codex or git command was not found: `{exc.filename}`.")
            return
        except Exception:
            logger.exception("runner_failed action_id=%s card_id=%s reason=unexpected_exception", event.action_id, event.card_id)
            self._failed(event, f"Unexpected runner failure.\n\n```text\n{traceback.format_exc()[-3000:]}\n```")
            return

        logger.info(
            "runner_finished action_id=%s card_id=%s exit_code=%s status=%s branch=%r worktree=%r changed_files=%s head_sha=%r",
            event.action_id,
            event.card_id,
            result.exit_code,
            result.status,
            result.branch,
            str(result.worktree),
            ",".join(result.changed_files),
            result.head_sha,
        )
        if result.exit_code == 0 and result.status == "done":
            pr = self._publish(event, result)
            if pr.get("error"):
                logger.info("task_decision decision=question reason=publish_error action_id=%s card_id=%s", event.action_id, event.card_id)
                self._question(event, str(pr["error"]))
                return
            logger.info("task_decision decision=done action_id=%s card_id=%s pr_url=%r", event.action_id, event.card_id, pr.get("url", ""))
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
                logger.info("task_side_effect action=move_card card_id=%s list_id=%s", event.card_id, self.trello.config.trello_done_list_id)
                self.trello.move_card(event.card_id, self.trello.config.trello_done_list_id)
            return

        if result.exit_code == 0 and result.status == "review":
            pr = self._publish(event, result)
            if pr.get("error"):
                logger.info("task_decision decision=question reason=publish_error action_id=%s card_id=%s", event.action_id, event.card_id)
                self._question(event, str(pr["error"]))
                return
            logger.info(
                "task_decision decision=review action_id=%s card_id=%s pr_url=%r ci_state=%r",
                event.action_id,
                event.card_id,
                pr.get("url", ""),
                pr.get("ci_state", ""),
            )
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
                logger.info("task_side_effect action=move_card card_id=%s list_id=%s", event.card_id, self.trello.config.trello_review_list_id)
                self.trello.move_card(event.card_id, self.trello.config.trello_review_list_id)
            return

        if result.exit_code == 0 and result.status == "question":
            logger.info("task_decision decision=question reason=runner_requested_input action_id=%s card_id=%s", event.action_id, event.card_id)
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

        logger.info(
            "task_decision decision=question reason=runner_incomplete action_id=%s card_id=%s exit_code=%s status=%s output_chars=%s",
            event.action_id,
            event.card_id,
            result.exit_code,
            result.status,
            len(result.output),
        )
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
        logger.info("card_status_update card_id=%s status=question message_chars=%s", event.card_id, len(message))
        self.state.set_card(event.card_id, status="question")
        self._set_status(event.card_id, "question")
        self.trello.add_comment(event.card_id, message)
        if self.trello.config.trello_question_list_id:
            logger.info("task_side_effect action=move_card card_id=%s list_id=%s", event.card_id, self.trello.config.trello_question_list_id)
            self.trello.move_card(event.card_id, self.trello.config.trello_question_list_id)

    def _failed(self, event: CardTaskEvent, message: str) -> None:
        logger.info("card_status_update card_id=%s status=failed message_chars=%s", event.card_id, len(message))
        self.state.set_card(event.card_id, status="failed")
        self._set_status(event.card_id, "failed")
        self.trello.add_comment(event.card_id, message)
        if self.trello.config.trello_question_list_id:
            logger.info("task_side_effect action=move_card card_id=%s list_id=%s", event.card_id, self.trello.config.trello_question_list_id)
            self.trello.move_card(event.card_id, self.trello.config.trello_question_list_id)

    def _set_status(self, card_id: str, status: str) -> None:
        try:
            logger.info("trello_status_label_update card_id=%s status=%s", card_id, status)
            self.trello.set_status_label(card_id, status)
        except Exception as exc:
            logger.warning("trello_status_label_update_failed card_id=%s status=%s error=%s", card_id, status, exc)

    def _refresh_event(self, event: CardTaskEvent) -> CardTaskEvent:
        try:
            logger.info("trello_card_refresh_start card_id=%s action_id=%s", event.card_id, event.action_id)
            card = self.trello.get_card(event.card_id)
        except Exception as exc:
            logger.warning("trello_card_refresh_failed card_id=%s action_id=%s error=%s", event.card_id, event.action_id, exc)
            return event

        return replace(
            event,
            card_name=card.get("name") or event.card_name,
            card_url=card.get("shortUrl") or card.get("url") or event.card_url,
            description=card.get("desc") or event.description,
            card_short_id=str(card.get("idShort") or event.card_short_id),
            label_ids=self._label_ids(card) or event.label_ids,
        )

    def _safe_get_card(self, card_id: str) -> dict[str, object]:
        try:
            logger.info("trello_card_refresh_start card_id=%s source=command", card_id)
            return self.trello.get_card(card_id)
        except Exception as exc:
            logger.warning("trello_card_refresh_failed card_id=%s source=command error=%s", card_id, exc)
            return {}

    def _is_review_feedback_card(
        self,
        event: CardCommandEvent,
        existing: dict[str, object],
        card: dict[str, object],
    ) -> bool:
        review_list_id = self.trello.config.trello_review_list_id
        return (
            existing.get("status") == "review"
            or bool(review_list_id and event.list_id == review_list_id)
            or bool(review_list_id and card.get("idList") == review_list_id)
        )

    def _review_feedback_text(self, text: str) -> str:
        parts = text.strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else "Please address the latest tech lead review feedback."

    def _review_feedback_task_event(
        self,
        event: CardCommandEvent,
        card: dict[str, object],
        feedback: str,
    ) -> CardTaskEvent:
        description = str(card.get("desc") or "")
        feedback_section = "\n".join(
            [
                "",
                "## Tech Lead Review Feedback",
                "",
                feedback,
                "",
                "Update the existing implementation for this review comment, keep the changes focused, "
                "and preserve the pull request branch.",
            ]
        )
        return CardTaskEvent(
            action_id=event.action_id,
            card_id=event.card_id,
            card_short_id=str(card.get("idShort") or event.card_id[-6:]),
            card_name=str(card.get("name") or "Trello review feedback"),
            card_url=str(card.get("shortUrl") or card.get("url") or ""),
            description=f"{description}\n{feedback_section}" if description else feedback_section.strip(),
            source="tech lead review comment",
            label_ids=self._label_ids(card),
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

    def _publish(self, event: CardTaskEvent, result: object, *, existing_pr_url: str = "") -> dict[str, str]:
        if not (self.runner.config.enable_git_push or self.runner.config.enable_pr_creation):
            logger.info("publish_skipped card_id=%s branch=%s reason=disabled", event.card_id, result.branch)
            return {"url": existing_pr_url} if existing_pr_url else {}
        try:
            logger.info("publish_push_start card_id=%s branch=%s worktree=%s", event.card_id, result.branch, result.worktree)
            self.runner.push_branch(result.branch, result.worktree)
        except subprocess.CalledProcessError as exc:
            output = (exc.stderr or exc.stdout or str(exc))[-3000:]
            logger.warning("publish_push_failed card_id=%s branch=%s output_chars=%s", event.card_id, result.branch, len(output))
            return {"error": f"Implementation is ready, but pushing the branch failed.\n\n```text\n{output}\n```"}

        if existing_pr_url:
            response = {"url": existing_pr_url}
            if result.head_sha and self.github is not None:
                try:
                    logger.info("publish_ci_status_start card_id=%s head_sha=%s", event.card_id, result.head_sha)
                    status = self.github.combined_status(result.head_sha)
                    response["ci_state"] = str(status.get("state", ""))
                except Exception as exc:
                    response["ci_state"] = f"unknown ({exc})"
                    logger.warning("publish_ci_status_failed card_id=%s head_sha=%s error=%s", event.card_id, result.head_sha, exc)
            logger.info("publish_pr_reused card_id=%s branch=%s url=%r ci_state=%r", event.card_id, result.branch, response.get("url", ""), response.get("ci_state", ""))
            return response

        if not self.runner.config.enable_pr_creation:
            logger.info("publish_pr_skipped card_id=%s branch=%s reason=pr_creation_disabled", event.card_id, result.branch)
            return {}
        if self.github is None:
            logger.warning("publish_pr_failed card_id=%s branch=%s reason=github_client_missing", event.card_id, result.branch)
            return {"error": "ENABLE_PR_CREATION is true, but GitHub client is not configured."}

        try:
            body = self._pr_body(event, result)
            logger.info("publish_pr_start card_id=%s branch=%s title=%r", event.card_id, result.branch, event.card_name)
            pr = self.github.create_pull_request(
                branch=result.branch,
                title=f"{event.card_name}",
                body=body,
            )
            response = {"url": str(pr.get("html_url", ""))}
            if result.head_sha:
                try:
                    logger.info("publish_ci_status_start card_id=%s head_sha=%s", event.card_id, result.head_sha)
                    status = self.github.combined_status(result.head_sha)
                    response["ci_state"] = str(status.get("state", ""))
                except Exception as exc:
                    response["ci_state"] = f"unknown ({exc})"
                    logger.warning("publish_ci_status_failed card_id=%s head_sha=%s error=%s", event.card_id, result.head_sha, exc)
            logger.info("publish_pr_complete card_id=%s branch=%s url=%r ci_state=%r", event.card_id, result.branch, response.get("url", ""), response.get("ci_state", ""))
            return response
        except Exception as exc:
            logger.warning("publish_pr_failed card_id=%s branch=%s error=%s", event.card_id, result.branch, exc)
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
