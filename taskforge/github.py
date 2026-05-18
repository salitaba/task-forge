from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from .config import Config


class GitHubClient:
    def __init__(self, config: Config):
        self.config = config

    def _repo_path(self) -> str:
        repo = self.config.github_repo.strip()
        if repo.startswith("git@github.com:"):
            repo = repo.removeprefix("git@github.com:")
        elif repo.startswith("https://github.com/"):
            repo = repo.removeprefix("https://github.com/")
        elif repo.startswith("http://github.com/"):
            repo = repo.removeprefix("http://github.com/")
        repo = repo.removesuffix(".git").strip("/")
        return urllib.parse.quote(repo, safe="/")

    def _request(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.config.require_github()
        url = f"{self.config.github_api_url.rstrip('/')}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.config.github_token}",
                "Content-Type": "application/json",
                "User-Agent": "taskforge",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}

    def create_pull_request(self, *, branch: str, title: str, body: str) -> dict[str, Any]:
        repo = self._repo_path()
        return self._request(
            "POST",
            f"/repos/{repo}/pulls",
            {
                "title": title,
                "head": branch,
                "base": self.config.pr_base_branch,
                "body": body,
                "maintainer_can_modify": True,
            },
        )

    def combined_status(self, sha: str) -> dict[str, Any]:
        repo = self._repo_path()
        return self._request("GET", f"/repos/{repo}/commits/{sha}/status")
