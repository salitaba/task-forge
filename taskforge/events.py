from __future__ import annotations

from dataclasses import dataclass
from dataclasses import asdict
from typing import Any

from .config import Config


@dataclass(frozen=True)
class CardTaskEvent:
    action_id: str
    card_id: str
    card_short_id: str
    card_name: str
    card_url: str
    description: str
    source: str
    label_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CardCommandEvent:
    action_id: str
    card_id: str
    command: str
    text: str
    source: str
    list_id: str = ""


def task_event_to_payload(event: CardTaskEvent) -> dict[str, Any]:
    return asdict(event)


def task_event_from_payload(payload: dict[str, Any]) -> CardTaskEvent:
    return CardTaskEvent(**payload)


def command_event_to_payload(event: CardCommandEvent) -> dict[str, Any]:
    return asdict(event)


def command_event_from_payload(payload: dict[str, Any]) -> CardCommandEvent:
    return CardCommandEvent(**payload)


def task_event_from_trello(payload: dict[str, Any], config: Config) -> CardTaskEvent | None:
    action = payload.get("action") or {}
    action_type = action.get("type")
    data = action.get("data") or {}
    card = data.get("card") or {}

    if not action.get("id") or not card.get("id"):
        return None

    source = ""
    if action_type == "createCard":
        list_data = data.get("list") or {}
        if list_data.get("id") != config.trello_todo_list_id:
            return None
        source = "created in To Do"
    elif action_type == "updateCard":
        list_after = data.get("listAfter") or {}
        list_before = data.get("listBefore") or {}
        if list_after.get("id") != config.trello_todo_list_id:
            return None
        if list_before.get("id") == list_after.get("id"):
            return None
        source = "moved into To Do"
    else:
        return None

    label_ids = _label_ids(card)
    if (
        config.trello_start_label_ids
        and _has_label_fields(card)
        and not set(label_ids).intersection(config.trello_start_label_ids)
    ):
        return None

    return CardTaskEvent(
        action_id=action["id"],
        card_id=card["id"],
        card_short_id=str(card.get("idShort") or card["id"][-6:]),
        card_name=card.get("name") or "Untitled Trello task",
        card_url=card.get("shortLink") or card.get("url") or "",
        description=card.get("desc") or "",
        source=source,
        label_ids=label_ids,
    )


def _label_ids(card: dict[str, Any]) -> tuple[str, ...]:
    raw_labels = card.get("labels") or []
    label_ids = []
    for label in raw_labels:
        if isinstance(label, dict) and label.get("id"):
            label_ids.append(str(label["id"]))
    for label_id in card.get("idLabels") or []:
        if label_id:
            label_ids.append(str(label_id))
    return tuple(dict.fromkeys(label_ids))


def _has_label_fields(card: dict[str, Any]) -> bool:
    return "labels" in card or "idLabels" in card


def command_event_from_trello(payload: dict[str, Any]) -> CardCommandEvent | None:
    action = payload.get("action") or {}
    if action.get("type") != "commentCard" or not action.get("id"):
        return None

    data = action.get("data") or {}
    card = data.get("card") or {}
    list_data = data.get("list") or {}
    text = str(data.get("text") or "").strip()
    if not card.get("id") or not text.lower().startswith("/codex"):
        return None

    parts = text.split()
    command = parts[1].lower() if len(parts) > 1 else "help"
    if command not in {"retry", "stop", "done", "cleanup", "help"}:
        command = "feedback"

    return CardCommandEvent(
        action_id=action["id"],
        card_id=card["id"],
        command=command,
        text=text,
        source="trello comment command",
        list_id=str(list_data.get("id") or ""),
    )
