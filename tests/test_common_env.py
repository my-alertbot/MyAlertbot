from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alertbot.common import load_env_file


class LoadEnvFileTests(unittest.TestCase):
    def test_loads_env_then_env_private_without_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / ".env").write_text(
                "PUBLIC_KEY=from_env\nSHARED_KEY=base\nQUOTED='hello world'\n",
                encoding="utf-8",
            )
            (tmp_path / ".env.private").write_text(
                "PRIVATE_KEY=from_private\nSHARED_KEY=private_attempt\n",
                encoding="utf-8",
            )

            old_cwd = os.getcwd()
            try:
                os.chdir(tmp_path)
                with patch.dict(os.environ, {"PUBLIC_KEY": "shell_wins"}, clear=True):
                    load_env_file()
                    self.assertEqual(os.environ["PUBLIC_KEY"], "shell_wins")
                    self.assertEqual(os.environ["PRIVATE_KEY"], "from_private")
                    self.assertEqual(os.environ["SHARED_KEY"], "base")
                    self.assertEqual(os.environ["QUOTED"], "hello world")
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
