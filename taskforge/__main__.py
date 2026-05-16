from __future__ import annotations

import argparse
import json
import sys
import time

from .cleanup import cleanup_by_status
from .config import Config
from .events import CardTaskEvent
from .server import make_processor, run_server
from .state import StateStore
from .trello import TrelloClient


def _print_card_template(config: Config) -> None:
    for section in config.required_card_sections:
        print(f"## {section}")
        print()
        print("TODO")
        print()
    print("## Out of Scope")
    print()
    print("TODO")
    print()
    print("## Dependencies")
    print()
    print("TODO")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taskforge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve", help="Start the Trello webhook server")
    subparsers.add_parser("register-webhook", help="Create or replace the Trello webhook")
    subparsers.add_parser("validate-config", help="Validate local configuration")
    subparsers.add_parser("card-template", help="Print the required product card template")
    subparsers.add_parser("status", help="Print local automation state")
    cleanup = subparsers.add_parser("cleanup", help="Remove recorded worktrees for completed cards")
    cleanup.add_argument("--dry-run", action="store_true", help="Show cleanup targets without deleting")
    cleanup.add_argument("--status", action="append", dest="statuses", help="Card status to clean")
    run_card = subparsers.add_parser("run-card", help="Manually run or resume a Trello card")
    run_card.add_argument("card_id", help="Trello card ID")

    args = parser.parse_args(argv)
    config = Config.from_env_file()

    if args.command == "serve":
        run_server(config)
        return 0

    if args.command == "register-webhook":
        client = TrelloClient(config)
        webhook = client.create_webhook()
        print(f"registered webhook {webhook.get('id', '<unknown>')} for {config.trello_callback_url}")
        return 0

    if args.command == "validate-config":
        config.require_trello_api()
        config.require_safe_repo()
        if config.enable_pr_creation:
            config.require_github()
        print("configuration ok")
        return 0

    if args.command == "card-template":
        _print_card_template(config)
        return 0

    if args.command == "status":
        data = StateStore(config.state_file).read_all()
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0

    if args.command == "run-card":
        client = TrelloClient(config)
        card = client.get_card(args.card_id)
        event = CardTaskEvent(
            action_id=f"manual-{args.card_id}-{int(time.time())}",
            card_id=args.card_id,
            card_short_id=str(card.get("idShort") or args.card_id[-6:]),
            card_name=card.get("name") or "Untitled Trello task",
            card_url=card.get("shortUrl") or card.get("url") or "",
            description=card.get("desc") or "",
            source="manual run",
        )
        state = StateStore(config.state_file)
        make_processor(config, state).process(event)
        print(f"processed {args.card_id}")
        return 0

    if args.command == "cleanup":
        state = StateStore(config.state_file)
        statuses = tuple(args.statuses) if args.statuses else None
        results = cleanup_by_status(config=config, state=state, statuses=statuses, dry_run=args.dry_run)
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
