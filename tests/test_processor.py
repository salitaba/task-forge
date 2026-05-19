from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.fakes import FakeGitHub, FakeRunner, FakeTrello, result, test_config, valid_description
from taskforge.events import CardCommandEvent, CardTaskEvent
from taskforge.processor import TaskProcessor
from taskforge.state import StateStore


def event(action_id: str = "action-1", desc: str | None = None) -> CardTaskEvent:
    return CardTaskEvent(
        action_id=action_id,
        card_id="card-1",
        card_short_id="7",
        card_name="Build invite flow",
        card_url="https://trello.test/c/abc",
        description=valid_description() if desc is None else desc,
        source="created in To Do",
    )


class ProcessorTests(unittest.TestCase):
    def cfg(self, **overrides: object):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        overrides.setdefault("state_file", Path(tmp.name) / "state.json")
        return test_config(**overrides)

    def make_processor(
        self,
        *,
        config=None,
        trello: FakeTrello | None = None,
        runner: FakeRunner | None = None,
        github: FakeGitHub | None = None,
    ) -> tuple[TaskProcessor, StateStore, FakeTrello, FakeRunner]:
        cfg = config or self.cfg()
        state = StateStore(cfg.state_file)
        fake_trello = trello or FakeTrello(cfg)
        fake_runner = runner or FakeRunner(cfg)
        return TaskProcessor(state, fake_trello, fake_runner, github), state, fake_trello, fake_runner

    def test_incomplete_card_moves_to_question_without_running_codex(self) -> None:
        cfg = self.cfg()
        trello = FakeTrello(cfg, desc="## Problem\nOnly a problem")
        runner = FakeRunner(cfg)
        processor, state, trello, runner = self.make_processor(config=cfg, trello=trello, runner=runner)

        processor.process(event(desc="ignored because card is refreshed"))

        self.assertEqual(state.card_status("card-1"), "question")
        self.assertEqual(runner.calls, [])
        self.assertIn(("card-1", "question"), trello.moves)
        self.assertIn(("card-1", "question-label"), trello.labels_added)
        self.assertIn("Missing required sections", trello.comments[-1][1])

    def test_valid_card_review_result_moves_to_review(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(
            config=cfg,
            runner=FakeRunner(cfg, result(status="review", changed_files=("app.py", "tests/test_app.py"))),
        )

        processor.process(event())

        self.assertEqual(state.card_status("card-1"), "review")
        self.assertEqual(state.get_card("card-1")["branch"], "codex/trello-7-task")
        self.assertIn(("card-1", "review"), trello.moves)
        self.assertIn(("card-1", "review-label"), trello.labels_added)
        self.assertIn("Changed files:", trello.comments[-1][1])

    def test_done_result_moves_to_done(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(
            config=cfg,
            runner=FakeRunner(cfg, result(status="done")),
        )

        processor.process(event())

        self.assertEqual(state.card_status("card-1"), "done")
        self.assertIn(("card-1", "done"), trello.moves)
        self.assertIn(("card-1", "done-label"), trello.labels_added)

    def test_question_result_moves_to_question(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(
            config=cfg,
            runner=FakeRunner(cfg, result(status="question", question="Which billing plan should apply?")),
        )

        processor.process(event())

        self.assertEqual(state.card_status("card-1"), "question")
        self.assertIn(("card-1", "question"), trello.moves)
        self.assertIn("Which billing plan", trello.comments[-1][1])

    def test_nonzero_result_moves_to_question_with_output(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(
            config=cfg,
            runner=FakeRunner(cfg, result(status="review", exit_code=2, output="tests failed")),
        )

        processor.process(event())

        self.assertEqual(state.card_status("card-1"), "question")
        self.assertIn("tests failed", trello.comments[-1][1])

    def test_runner_git_error_moves_to_failed(self) -> None:
        cfg = self.cfg()
        error = subprocess.CalledProcessError(1, ["git"], output="bad git")
        processor, state, trello, runner = self.make_processor(config=cfg, runner=FakeRunner(cfg, error=error))

        processor.process(event())

        self.assertEqual(state.card_status("card-1"), "failed")
        self.assertIn(("card-1", "failed-label"), trello.labels_added)
        self.assertIn("Could not prepare", trello.comments[-1][1])

    def test_file_not_found_moves_to_failed(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(
            config=cfg,
            runner=FakeRunner(cfg, error=FileNotFoundError("codex")),
        )

        processor.process(event())

        self.assertEqual(state.card_status("card-1"), "failed")
        self.assertIn("was not found", trello.comments[-1][1])

    def test_duplicate_action_is_ignored(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)
        state.mark_action_processed("action-1")

        processor.process(event())

        self.assertEqual(runner.calls, [])
        self.assertEqual(trello.comments, [])

    def test_running_review_and_done_cards_are_ignored(self) -> None:
        for status in ("running", "review", "done"):
            cfg = self.cfg()
            processor, state, trello, runner = self.make_processor(config=cfg)
            state.set_card("card-1", status=status)

            processor.process(event(action_id=f"action-{status}"))

            self.assertEqual(runner.calls, [])
            self.assertTrue(state.has_processed_action(f"action-{status}"))

    def test_resume_passes_existing_branch_and_worktree_to_runner(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)
        state.set_card("card-1", status="question", branch="codex/existing", worktree="/tmp/existing")

        processor.process(event())

        self.assertEqual(runner.calls[0]["existing_branch"], "codex/existing")
        self.assertEqual(runner.calls[0]["existing_worktree"], "/tmp/existing")

    def test_requeued_running_same_action_can_resume_after_restart(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)
        state.mark_action_processed("action-1")
        state.set_card(
            "card-1",
            status="running",
            action_id="action-1",
            branch="codex/existing",
            worktree="/tmp/existing",
        )

        processor.process(event())

        self.assertEqual(runner.calls[0]["existing_branch"], "codex/existing")
        self.assertEqual(state.card_status("card-1"), "review")

    def test_requeued_running_same_action_skips_start_label_gate(self) -> None:
        cfg = self.cfg(trello_start_label_ids=("start-label",))
        trello = FakeTrello(cfg, label_ids=())
        processor, state, trello, runner = self.make_processor(config=cfg, trello=trello)
        state.mark_action_processed("action-1")
        state.set_card(
            "card-1",
            status="running",
            action_id="action-1",
            branch="codex/existing",
            worktree="/tmp/existing",
        )

        processor.process(event())

        self.assertEqual(runner.calls[0]["existing_branch"], "codex/existing")
        self.assertEqual(state.card_status("card-1"), "review")

    def test_requeued_running_same_action_reuses_existing_pr(self) -> None:
        cfg = self.cfg(
            enable_git_push=True,
            enable_pr_creation=True,
            github_token="token",
            github_repo="owner/repo",
        )
        github = FakeGitHub(ci_state="pending")
        runner = FakeRunner(
            cfg,
            result(
                status="review",
                branch="codex/existing",
                worktree=Path("/tmp/existing"),
            ),
        )
        processor, state, trello, runner = self.make_processor(config=cfg, runner=runner, github=github)
        state.mark_action_processed("action-1")
        state.set_card(
            "card-1",
            status="running",
            action_id="action-1",
            branch="codex/existing",
            worktree="/tmp/existing",
            pr_url="https://github.test/owner/repo/pull/1",
        )

        processor.process(event())

        self.assertEqual(github.created_prs, [])
        self.assertEqual(runner.pushes, [("codex/existing", Path("/tmp/existing"))])
        self.assertEqual(state.card_status("card-1"), "review")
        self.assertEqual(state.get_card("card-1")["pr_url"], "https://github.test/owner/repo/pull/1")

    def test_dry_run_validates_but_skips_runner(self) -> None:
        cfg = self.cfg(dry_run=True)
        processor, state, trello, runner = self.make_processor(config=cfg)

        processor.process(event())

        self.assertEqual(runner.calls, [])
        self.assertEqual(state.card_status("card-1"), "dry_run")
        self.assertIn("Dry run", trello.comments[-1][1])

    def test_required_start_label_skips_card_after_refresh_if_missing(self) -> None:
        cfg = self.cfg(trello_start_label_ids=("start-label",))
        trello = FakeTrello(cfg, label_ids=("other-label",))
        runner = FakeRunner(cfg)
        processor, state, trello, runner = self.make_processor(config=cfg, trello=trello, runner=runner)

        processor.process(event())

        self.assertEqual(runner.calls, [])
        self.assertTrue(state.has_processed_action("action-1"))
        self.assertEqual(state.card_status("card-1"), "")

    def test_required_start_label_runs_when_refreshed_card_matches(self) -> None:
        cfg = self.cfg(trello_start_label_ids=("start-label",))
        trello = FakeTrello(cfg, label_ids=("start-label",))
        runner = FakeRunner(cfg)
        processor, state, trello, runner = self.make_processor(config=cfg, trello=trello, runner=runner)

        processor.process(event())

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(state.card_status("card-1"), "review")

    def test_publish_creates_pr_and_comments_url_and_ci(self) -> None:
        cfg = self.cfg(
            enable_git_push=True,
            enable_pr_creation=True,
            github_token="token",
            github_repo="owner/repo",
        )
        github = FakeGitHub(ci_state="pending")
        runner = FakeRunner(cfg, result(status="review", head_sha="abc123"))
        processor, state, trello, runner = self.make_processor(config=cfg, runner=runner, github=github)

        processor.process(event())

        self.assertEqual(runner.pushes, [("codex/trello-7-task", Path("/tmp/worktree"))])
        self.assertEqual(github.created_prs[0]["branch"], "codex/trello-7-task")
        self.assertEqual(state.get_card("card-1")["pr_url"], "https://github.test/owner/repo/pull/1")
        self.assertIn("Pull request: https://github.test/owner/repo/pull/1", trello.comments[-1][1])
        self.assertIn("GitHub commit status: `pending`", trello.comments[-1][1])

    def test_review_feedback_command_reuses_existing_branch_and_pr(self) -> None:
        cfg = self.cfg(
            enable_git_push=True,
            enable_pr_creation=True,
            github_token="token",
            github_repo="owner/repo",
        )
        github = FakeGitHub(ci_state="pending")
        runner = FakeRunner(
            cfg,
            result(
                status="review",
                branch="codex/existing",
                worktree=Path("/tmp/existing"),
                changed_files=("app.py", "tests/test_app.py"),
            ),
        )
        processor, state, trello, runner = self.make_processor(config=cfg, runner=runner, github=github)
        state.set_card(
            "card-1",
            status="review",
            branch="codex/existing",
            worktree="/tmp/existing",
            pr_url="https://github.test/owner/repo/pull/1",
        )

        processor.process_command(
            CardCommandEvent(
                "cmd-1",
                "card-1",
                "feedback",
                "/codex add regression coverage",
                "comment",
                list_id="review",
            )
        )

        self.assertEqual(runner.calls[0]["existing_branch"], "codex/existing")
        self.assertEqual(runner.calls[0]["existing_worktree"], "/tmp/existing")
        self.assertIn("Tech Lead Review Feedback", runner.calls[0]["event"].description)
        self.assertIn("add regression coverage", runner.calls[0]["event"].description)
        self.assertEqual(runner.pushes, [("codex/existing", Path("/tmp/existing"))])
        self.assertEqual(github.created_prs, [])
        self.assertEqual(state.card_status("card-1"), "review")
        self.assertEqual(state.get_card("card-1")["pr_url"], "https://github.test/owner/repo/pull/1")
        self.assertIn("updated the PR", trello.comments[-1][1])
        self.assertIn("GitHub commit status: `pending`", trello.comments[-1][1])

    def test_requeued_review_feedback_same_action_can_resume_after_restart(self) -> None:
        cfg = self.cfg()
        runner = FakeRunner(
            cfg,
            result(
                status="review",
                branch="codex/existing",
                worktree=Path("/tmp/existing"),
            ),
        )
        processor, state, trello, runner = self.make_processor(config=cfg, runner=runner)
        state.mark_action_processed("cmd-1")
        state.set_card(
            "card-1",
            status="running",
            action_id="cmd-1",
            branch="codex/existing",
            worktree="/tmp/existing",
            pr_url="https://github.test/owner/repo/pull/1",
        )

        processor.process_command(
            CardCommandEvent(
                "cmd-1",
                "card-1",
                "feedback",
                "/codex add regression coverage",
                "comment",
                list_id="review",
            )
        )

        self.assertEqual(runner.calls[0]["existing_branch"], "codex/existing")
        self.assertEqual(runner.calls[0]["existing_worktree"], "/tmp/existing")
        self.assertEqual(state.card_status("card-1"), "review")
        self.assertIn("resumed", trello.comments[0][1])

    def test_duplicate_review_feedback_different_running_action_is_ignored(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)
        state.mark_action_processed("cmd-1")
        state.set_card(
            "card-1",
            status="running",
            action_id="cmd-other",
            branch="codex/existing",
            worktree="/tmp/existing",
        )

        processor.process_command(
            CardCommandEvent("cmd-1", "card-1", "feedback", "/codex add tests", "comment", list_id="review")
        )

        self.assertEqual(runner.calls, [])
        self.assertEqual(trello.comments, [])

    def test_review_feedback_command_outside_review_lists_supported_commands(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)

        processor.process_command(
            CardCommandEvent("cmd-1", "card-1", "feedback", "/codex add tests", "comment")
        )

        self.assertEqual(runner.calls, [])
        self.assertIn("only run for cards in Review", trello.comments[-1][1])

    def test_push_failure_moves_back_to_question(self) -> None:
        class PushFailRunner(FakeRunner):
            def push_branch(self, branch: str, worktree: Path) -> None:
                raise subprocess.CalledProcessError(1, ["git", "push"], output="push denied")

        cfg = self.cfg(enable_git_push=True)
        processor, state, trello, runner = self.make_processor(config=cfg, runner=PushFailRunner(cfg))

        processor.process(event())

        self.assertEqual(state.card_status("card-1"), "question")
        self.assertIn("pushing the branch failed", trello.comments[-1][1])

    def test_comment_command_done_moves_card_to_done(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)

        processor.process_command(CardCommandEvent("cmd-1", "card-1", "done", "/codex done", "comment"))

        self.assertEqual(state.card_status("card-1"), "done")
        self.assertIn(("card-1", "done"), trello.moves)
        self.assertIn("Marked done", trello.comments[-1][1])

    def test_comment_command_stop_sets_stopped(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)

        processor.process_command(CardCommandEvent("cmd-1", "card-1", "stop", "/codex stop", "comment"))

        self.assertEqual(state.card_status("card-1"), "stopped")
        self.assertIn("stopped", trello.comments[-1][1])

    def test_comment_command_retry_comments_next_step(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)

        processor.process_command(CardCommandEvent("cmd-1", "card-1", "retry", "/codex retry", "comment"))

        self.assertEqual(state.card_status("card-1"), "question")
        self.assertIn("Retry requested", trello.comments[-1][1])

    def test_comment_command_help_lists_commands(self) -> None:
        cfg = self.cfg()
        processor, state, trello, runner = self.make_processor(config=cfg)

        processor.process_command(CardCommandEvent("cmd-1", "card-1", "help", "/codex wat", "comment"))

        self.assertIn("Supported commands", trello.comments[-1][1])


if __name__ == "__main__":
    unittest.main()
