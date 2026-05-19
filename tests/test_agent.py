from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path

from tests.fakes import test_config, valid_description
from taskforge.agent import CodexWorktreeRunner, slugify
from taskforge.events import CardTaskEvent


class AgentTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Add login & SSO!"), "add-login-sso")
        self.assertEqual(slugify(""), "task")

    def test_read_result_defaults_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = CodexWorktreeRunner(test_config())

            result = runner._read_result(Path(tmp))

            self.assertEqual(result["status"], "review")

    def test_read_result_accepts_done_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            result_dir = worktree / ".codex"
            result_dir.mkdir()
            (result_dir / "trello-result.json").write_text(
                '{"status":"done","summary":"Finished","question":""}',
                encoding="utf-8",
            )
            runner = CodexWorktreeRunner(test_config())

            result = runner._read_result(worktree)

            self.assertEqual(result["status"], "done")
            self.assertEqual(result["summary"], "Finished")

    def test_read_result_invalid_status_defaults_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            result_dir = worktree / ".codex"
            result_dir.mkdir()
            (result_dir / "trello-result.json").write_text('{"status":"unknown"}', encoding="utf-8")
            runner = CodexWorktreeRunner(test_config())

            result = runner._read_result(worktree)

            self.assertEqual(result["status"], "review")

    def test_prepare_worktree_reuses_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = CodexWorktreeRunner(test_config())

            branch, worktree = runner._prepare_worktree(
                event=object(),
                existing_branch="codex/existing",
                existing_worktree=tmp,
            )

            self.assertEqual(branch, "codex/existing")
            self.assertEqual(worktree, Path(tmp))

    def test_build_command_expands_placeholders_without_shell(self) -> None:
        runner = CodexWorktreeRunner(
            test_config(codex_command_template="codex exec --cd {workdir} -")
        )

        command = runner._build_command(
            event=type("Event", (), {"card_id": "card", "card_url": "url", "card_name": "Name"})(),
            branch="branch",
            worktree=Path("/tmp/work tree"),
            prompt_file=Path("/tmp/prompt file.md"),
        )

        self.assertEqual(command, ["codex", "exec", "--cd", "/tmp/work tree", "-"])

    def test_prompt_stdin_reads_prompt_when_command_ends_with_dash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "prompt.md"
            prompt_file.write_text("do the task", encoding="utf-8")
            runner = CodexWorktreeRunner(test_config())

            prompt_stdin = runner._prompt_stdin(["codex", "exec", "-"], prompt_file)

            self.assertEqual(prompt_stdin, "do the task")

    def test_run_creates_worktree_runs_command_and_commits_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._git_repo(root / "repo")
            worktrees = root / "worktrees"
            script = root / "codex_stub.py"
            script.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "import json, sys",
                        "workdir = Path(sys.argv[1])",
                        "print('running task')",
                        "(workdir / 'feature.txt').write_text('done', encoding='utf-8')",
                        "result_dir = workdir / '.codex'",
                        "result_dir.mkdir(exist_ok=True)",
                        "(result_dir / 'trello-result.json').write_text(json.dumps({'status':'review','summary':'ok','question':''}), encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )
            runner = CodexWorktreeRunner(
                test_config(
                    target_repo=repo,
                    worktree_root=worktrees,
                    codex_command_template=f"python3 {script} {{workdir}}",
                    state_file=root / "state.json",
                )
            )
            started = []

            run = runner.run(
                self._event(),
                on_started=lambda branch, worktree, log_file: started.append((branch, worktree, log_file)),
            )

            self.assertEqual(run.exit_code, 0)
            self.assertEqual(run.status, "review")
            self.assertIn("feature.txt", run.changed_files)
            self.assertTrue(run.head_sha)
            self.assertTrue((run.worktree / "feature.txt").exists())
            logs = list((run.worktree / ".codex" / "logs").glob("run-*.log"))
            self.assertTrue(logs)
            self.assertIn("running task", logs[0].read_text(encoding="utf-8"))
            self.assertEqual(started[0][1], run.worktree)
            self.assertEqual(started[0][2], logs[0])

    def test_run_refreshes_main_before_creating_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._git_repo(root / "source")
            origin = root / "origin.git"
            target = root / "target"
            subprocess.run(["git", "clone", "--bare", str(source), str(origin)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "clone", str(origin), str(target)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "-C", str(target), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(target), "config", "user.name", "Test User"], check=True)

            subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(origin)], check=True)
            (source / "README.md").write_text("updated", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "Update main"], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "-C", str(source), "push", "origin", "main"], check=True, stdout=subprocess.PIPE)

            script = root / "codex_stub.py"
            script.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "import json, sys",
                        "workdir = Path(sys.argv[1])",
                        "(workdir / 'feature.txt').write_text('done', encoding='utf-8')",
                        "result_dir = workdir / '.codex'",
                        "result_dir.mkdir(exist_ok=True)",
                        "(result_dir / 'trello-result.json').write_text(json.dumps({'status':'review','summary':'ok','question':''}), encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )
            runner = CodexWorktreeRunner(
                test_config(
                    target_repo=target,
                    worktree_root=root / "worktrees",
                    codex_command_template=f"python3 {script} {{workdir}}",
                    state_file=root / "state.json",
                )
            )

            run = runner.run(self._event())

            self.assertEqual(run.exit_code, 0)
            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "updated")
            self.assertEqual((run.worktree / "README.md").read_text(encoding="utf-8"), "updated")
            self.assertIn("feature.txt", run.changed_files)
            self.assertNotIn("README.md", run.changed_files)

    def test_run_timeout_returns_question_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._git_repo(root / "repo")
            script = root / "slow.py"
            script.write_text("import time; time.sleep(2)", encoding="utf-8")
            runner = CodexWorktreeRunner(
                test_config(
                    target_repo=repo,
                    worktree_root=root / "worktrees",
                    codex_command_template=f"python3 {script}",
                    codex_timeout_seconds=1,
                    state_file=root / "state.json",
                )
            )

            run = runner.run(self._event())

            self.assertEqual(run.exit_code, 124)
            self.assertEqual(run.status, "question")
            self.assertIn("timed out", run.summary)

    def _event(self) -> CardTaskEvent:
        return CardTaskEvent(
            action_id="action-1",
            card_id="card-1",
            card_short_id="7",
            card_name="Build invite flow",
            card_url="https://trello.test/c/abc",
            description=valid_description(),
            source="test",
        )

    def _git_repo(self, path: Path) -> Path:
        path.mkdir()
        subprocess.run(["git", "-C", str(path), "init", "-b", "main"], check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
        (path / "README.md").write_text("initial", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-m", "Initial"], check=True, stdout=subprocess.PIPE)
        return path


if __name__ == "__main__":
    unittest.main()
