from __future__ import annotations

import unittest

from tests.fakes import test_config
from taskforge.trello import TrelloClient


class RecordingTrello(TrelloClient):
    def __init__(self):
        super().__init__(test_config())
        self.requests = []

    def _request(self, method, path, *, query=None, data=None):
        self.requests.append({"method": method, "path": path, "query": query, "data": data})
        return {}


class TrelloClientTests(unittest.TestCase):
    def test_set_status_label_adds_current_and_removes_others(self) -> None:
        client = RecordingTrello()

        client.set_status_label("card-1", "review")

        add_requests = [request for request in client.requests if request["method"] == "POST"]
        delete_requests = [request for request in client.requests if request["method"] == "DELETE"]
        self.assertEqual(add_requests[0]["path"], "/cards/card-1/idLabels")
        self.assertEqual(add_requests[0]["data"], {"value": "review-label"})
        self.assertEqual(len(delete_requests), 4)
        self.assertIn("/cards/card-1/idLabels/running-label", [request["path"] for request in delete_requests])

    def test_move_card_targets_list(self) -> None:
        client = RecordingTrello()

        client.move_card("card-1", "review")

        self.assertEqual(client.requests[-1]["method"], "PUT")
        self.assertEqual(client.requests[-1]["path"], "/cards/card-1")
        self.assertEqual(client.requests[-1]["data"], {"idList": "review"})


if __name__ == "__main__":
    unittest.main()

