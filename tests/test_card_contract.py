from __future__ import annotations

import unittest

from taskforge.card_contract import parse_sections, validate_card_contract


class CardContractTests(unittest.TestCase):
    def test_parse_markdown_sections(self) -> None:
        sections = parse_sections(
            "\n".join(
                [
                    "## Problem",
                    "Users cannot invite teammates.",
                    "## Acceptance Criteria",
                    "- Invite sends email",
                ]
            )
        )

        self.assertEqual(sections["problem"], "Users cannot invite teammates.")
        self.assertEqual(sections["acceptance criteria"], "- Invite sends email")

    def test_validate_required_sections(self) -> None:
        contract = validate_card_contract(
            "\n".join(
                [
                    "## Problem",
                    "P",
                    "## Scope",
                    "S",
                    "## Acceptance Criteria",
                    "A",
                ]
            ),
            ("Problem", "Scope", "Acceptance Criteria", "Test Plan"),
        )

        self.assertFalse(contract.is_valid)
        self.assertEqual(contract.missing_sections, ("Test Plan",))


if __name__ == "__main__":
    unittest.main()

