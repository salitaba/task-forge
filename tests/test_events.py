from __future__ import annotations

import unittest

from tests.fakes import test_config
from taskforge.events import command_event_from_trello, task_event_from_trello


def config():
    return test_config(trello_webhook_secret="secret")


class EventTests(unittest.TestCase):
    def test_create_card_in_todo_list_becomes_task(self) -> None:
        payload = {
            "action": {
                "id": "action-1",
                "type": "createCard",
                "data": {
                    "list": {"id": "todo"},
                    "card": {
                        "id": "card-1",
                        "idShort": 7,
                        "name": "Add login",
                        "desc": "Build it",
                        "labels": [{"id": "start-label"}],
                    },
                },
            }
        }

        event = task_event_from_trello(payload, config())

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.card_id, "card-1")
        self.assertEqual(event.card_short_id, "7")
        self.assertEqual(event.source, "created in To Do")
        self.assertEqual(event.label_ids, ("start-label",))

    def test_create_card_elsewhere_is_ignored(self) -> None:
        payload = {
            "action": {
                "id": "action-1",
                "type": "createCard",
                "data": {"list": {"id": "backlog"}, "card": {"id": "card-1"}},
            }
        }

        self.assertIsNone(task_event_from_trello(payload, config()))

    def test_move_into_todo_becomes_task(self) -> None:
        payload = {
            "action": {
                "id": "action-2",
                "type": "updateCard",
                "data": {
                    "listBefore": {"id": "backlog"},
                    "listAfter": {"id": "todo"},
                    "card": {"id": "card-2", "name": "Fix bug"},
                },
            }
        }

        event = task_event_from_trello(payload, config())

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.source, "moved into To Do")

    def test_required_start_label_accepts_matching_card(self) -> None:
        payload = {
            "action": {
                "id": "action-1",
                "type": "createCard",
                "data": {
                    "list": {"id": "todo"},
                    "card": {"id": "card-1", "idLabels": ["start-label"]},
                },
            }
        }

        event = task_event_from_trello(payload, test_config(trello_start_label_ids=("start-label",)))

        self.assertIsNotNone(event)

    def test_required_start_label_ignores_unlabeled_card(self) -> None:
        payload = {
            "action": {
                "id": "action-1",
                "type": "createCard",
                "data": {"list": {"id": "todo"}, "card": {"id": "card-1", "idLabels": ["other"]}},
            }
        }

        event = task_event_from_trello(payload, test_config(trello_start_label_ids=("start-label",)))

        self.assertIsNone(event)

    def test_required_start_label_allows_partial_move_payload_for_refresh(self) -> None:
        payload = {
            "action": {
                "id": "action-1",
                "type": "updateCard",
                "data": {
                    "listBefore": {"id": "backlog"},
                    "listAfter": {"id": "todo"},
                    "card": {"id": "card-1", "name": "Fix bug"},
                },
            }
        }

        event = task_event_from_trello(payload, test_config(trello_start_label_ids=("start-label",)))

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.label_ids, ())

    def test_comment_command_is_parsed(self) -> None:
        payload = {
            "action": {
                "id": "cmd-1",
                "type": "commentCard",
                "data": {"card": {"id": "card-1"}, "text": "/codex cleanup"},
            }
        }

        command = command_event_from_trello(payload)

        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.command, "cleanup")

    def test_unknown_comment_command_becomes_help(self) -> None:
        payload = {
            "action": {
                "id": "cmd-1",
                "type": "commentCard",
                "data": {"card": {"id": "card-1"}, "text": "/codex wat"},
            }
        }

        command = command_event_from_trello(payload)

        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.command, "help")


if __name__ == "__main__":
    unittest.main()
