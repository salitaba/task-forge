from __future__ import annotations

import unittest
from pathlib import Path


class DockerComposeTests(unittest.TestCase):
    def test_state_file_uses_mounted_data_volume_by_default(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("STATE_FILE: ${TASKFORGE_STATE_FILE:-/data/taskforge-state.sqlite3}", compose)
        self.assertIn("./data:/data", compose)


if __name__ == "__main__":
    unittest.main()
