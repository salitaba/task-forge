from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fakes import test_config
from taskforge.config import Config


class ConfigTests(unittest.TestCase):
    def test_require_safe_repo_allows_when_no_allowlist(self) -> None:
        test_config(repo_allowlist=()).require_safe_repo()

    def test_require_safe_repo_rejects_repo_outside_allowlist(self) -> None:
        cfg = test_config(target_repo=Path("/tmp/repo-a"), repo_allowlist=(Path("/tmp/repo-b"),))

        with self.assertRaises(ValueError):
            cfg.require_safe_repo()

    def test_require_github_requires_token_and_repo(self) -> None:
        with self.assertRaises(ValueError):
            test_config(enable_pr_creation=True).require_github()

    def test_from_env_parses_runtime_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "TRELLO_KEY": "key",
                "TRELLO_TOKEN": "token",
                "TRELLO_CALLBACK_URL": "https://example.com/webhooks/trello",
                "TRELLO_BOARD_ID": "board",
                "TRELLO_TODO_LIST_ID": "todo",
                "TARGET_REPO": tmp,
                "REPO_ALLOWLIST": tmp,
                "ENABLE_GIT_PUSH": "true",
                "ENABLE_PR_CREATION": "yes",
                "MAX_CONCURRENT_JOBS": "3",
                "CODEX_TIMEOUT_SECONDS": "60",
                "REQUIRED_CARD_SECTIONS": "Problem,Scope",
                "DRY_RUN": "true",
                "JOB_POLL_INTERVAL_SECONDS": "0.5",
                "CLEANUP_STATUSES": "done,review",
                "TRELLO_START_LABEL_IDS": "start-label,approved-label",
            }
            with patch.dict("os.environ", env, clear=True):
                cfg = Config.from_env()

        self.assertTrue(cfg.enable_git_push)
        self.assertTrue(cfg.enable_pr_creation)
        self.assertEqual(cfg.max_concurrent_jobs, 3)
        self.assertEqual(cfg.codex_timeout_seconds, 60)
        self.assertEqual(cfg.required_card_sections, ("Problem", "Scope"))
        self.assertEqual(len(cfg.repo_allowlist), 1)
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.job_poll_interval_seconds, 0.5)
        self.assertEqual(cfg.cleanup_statuses, ("done", "review"))
        self.assertEqual(cfg.trello_start_label_ids, ("start-label", "approved-label"))


if __name__ == "__main__":
    unittest.main()
