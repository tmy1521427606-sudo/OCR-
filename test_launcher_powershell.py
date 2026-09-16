from __future__ import annotations

import unittest
from pathlib import Path


class PowerShellLauncherTests(unittest.TestCase):
    def test_dependency_install_does_not_become_the_returned_python_path(self) -> None:
        script = (Path(__file__).parent / "run_demo.ps1").read_text(encoding="utf-8")

        self.assertIn("$null = & $venvPython -m pip install", script)

    def test_paddle_route_helper_uses_ascii_startup_and_writes_a_diagnostic_log(self) -> None:
        helper = (Path(__file__).parent / "修复Paddle路由.cmd").read_text(encoding="utf-8")
        script = (Path(__file__).parent / "fix_paddle_route.ps1").read_text(encoding="utf-8")

        self.assertIn("fix_paddle_route.ps1", helper)
        self.assertIn("-NoExit", helper)
        self.assertIn("-Verb RunAs", script)
        self.assertIn("-NoExit", script)
        self.assertIn("Read-Host", script)
        self.assertIn("paddle_route_helper.log", script)


if __name__ == "__main__":
    unittest.main()
