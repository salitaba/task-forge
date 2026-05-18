from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from .config import Config


class TrelloClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = "https://api.trello.com/1"

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.config.require_trello_api()
        params = {
            "key": self.config.trello_key,
            "token": self.config.trello_token,
            **(query or {}),
        }
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        body = None
        headers = {"Accept": "application/json"}
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}

    def create_webhook(self) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/tokens/{self.config.trello_token}/webhooks/",
            data={
                "description": "TaskForge",
                "callbackURL": self.config.trello_callback_url,
                "idModel": self.config.trello_board_id,
            },
        )

    def add_comment(self, card_id: str, text: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/cards/{card_id}/actions/comments",
            data={"text": text},
        )

    def move_card(self, card_id: str, list_id: str) -> dict[str, Any]:
        return self._request("PUT", f"/cards/{card_id}", data={"idList": list_id})

    def get_card(self, card_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/cards/{card_id}",
            query={"fields": "name,desc,url,shortUrl,idShort,idList,idLabels,labels"},
        )

    def add_label(self, card_id: str, label_id: str) -> dict[str, Any]:
        if not label_id:
            return {}
        return self._request("POST", f"/cards/{card_id}/idLabels", data={"value": label_id})

    def remove_label(self, card_id: str, label_id: str) -> dict[str, Any]:
        if not label_id:
            return {}
        return self._request("DELETE", f"/cards/{card_id}/idLabels/{label_id}")

    def set_status_label(self, card_id: str, status: str) -> None:
        labels = {
            "running": self.config.trello_running_label_id,
            "question": self.config.trello_question_label_id,
            "review": self.config.trello_review_label_id,
            "done": self.config.trello_done_label_id,
            "failed": self.config.trello_failed_label_id,
        }
        for label_status, label_id in labels.items():
            if not label_id:
                continue
            if label_status == status:
                self.add_label(card_id, label_id)
            else:
                self.remove_label(card_id, label_id)
