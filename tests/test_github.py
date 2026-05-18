from __future__ import annotations

import unittest

from tests.fakes import test_config
from taskforge.github import GitHubClient


class GitHubClientTests(unittest.TestCase):
    def test_repo_path_accepts_owner_repo(self) -> None:
        client = GitHubClient(test_config(github_repo="owner/repo"))

        self.assertEqual(client._repo_path(), "owner/repo")

    def test_repo_path_strips_git_suffix(self) -> None:
        client = GitHubClient(test_config(github_repo="owner/repo.git"))

        self.assertEqual(client._repo_path(), "owner/repo")

    def test_repo_path_accepts_ssh_remote_url(self) -> None:
        client = GitHubClient(test_config(github_repo="git@github.com:owner/repo.git"))

        self.assertEqual(client._repo_path(), "owner/repo")


if __name__ == "__main__":
    unittest.main()
