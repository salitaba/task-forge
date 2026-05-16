from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .state import StateStore


def cleanup_card_worktree(
    *,
    config: Config,
    state: StateStore,
    card_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    card = state.get_card(card_id)
    worktree_value = str(card.get("worktree", ""))
    branch = str(card.get("branch", ""))
    if not worktree_value:
        return {"removed": False, "reason": "card has no recorded worktree"}

    worktree = Path(worktree_value)
    if not _is_within(worktree, config.worktree_root):
        return {"removed": False, "reason": f"worktree is outside WORKTREE_ROOT: {worktree}"}

    if dry_run:
        return {"removed": worktree.exists(), "worktree": str(worktree), "branch": branch, "dry_run": True}

    if worktree.exists():
        subprocess.run(
            ["git", "-C", str(config.target_repo), "worktree", "remove", "--force", str(worktree)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if worktree.exists():
            shutil.rmtree(worktree)

    state.set_card(card_id, worktree="", cleaned_up=True)
    return {"removed": True, "worktree": str(worktree), "branch": branch}


def cleanup_by_status(
    *,
    config: Config,
    state: StateStore,
    statuses: tuple[str, ...] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    data = state.read_all()
    allowed = set(statuses or config.cleanup_statuses)
    results = []
    for card_id, card in data.get("cards", {}).items():
        if str(card.get("status", "")) in allowed and card.get("worktree"):
            result = cleanup_card_worktree(config=config, state=state, card_id=card_id, dry_run=dry_run)
            result["card_id"] = card_id
            results.append(result)
    return results


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False

