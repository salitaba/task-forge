from __future__ import annotations

import re
from dataclasses import dataclass


SECTION_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$|^\s*\*\*(.+?)\*\*\s*$", re.MULTILINE)


@dataclass(frozen=True)
class CardContract:
    sections: dict[str, str]
    missing_sections: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.missing_sections


def normalize_heading(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def parse_sections(description: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(description))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1) or match.group(2) or ""
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(description)
        sections[normalize_heading(heading)] = description[start:end].strip()
    return sections


def validate_card_contract(description: str, required_sections: tuple[str, ...]) -> CardContract:
    sections = parse_sections(description)
    missing = []
    for section in required_sections:
        normalized = normalize_heading(section)
        if not sections.get(normalized):
            missing.append(section)
    return CardContract(sections=sections, missing_sections=tuple(missing))

