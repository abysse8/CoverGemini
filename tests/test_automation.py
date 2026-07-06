from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from coverai.automation import OfferAutomationRunner
from coverai.storage import CoverAiStore


class AutomationTests(unittest.TestCase):
    def test_run_once_updates_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = OfferAutomationRunner(
                CoverAiStore(Path(tmp) / "coverai.db"),
                Path(tmp) / "config.json",
                openai_client_getter=lambda: None,
                model_getter=lambda: "test-model",
                sms_client_factory=lambda: object(),
                enabled=False,
                interval_seconds=900,
            )

            with patch("coverai.automation.run_offer_explorer") as fake_run:
                fake_run.return_value = {"run": {"status": "completed", "offers_new": 1, "offers_reported": 1}, "offers": []}
                result = runner.run_once("test")

            self.assertTrue(result["ok"])
            self.assertFalse(runner.status()["running"])
            self.assertEqual(runner.status()["last_run"]["status"], "completed")

    def test_overlapping_runs_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = threading.Event()
            runner = OfferAutomationRunner(
                CoverAiStore(Path(tmp) / "coverai.db"),
                Path(tmp) / "config.json",
                openai_client_getter=lambda: None,
                model_getter=lambda: "test-model",
                sms_client_factory=lambda: object(),
                enabled=False,
                interval_seconds=900,
            )

            def slow_run(*_args, **_kwargs):
                release.wait(timeout=2)
                return {"run": {"status": "completed", "offers_new": 0, "offers_reported": 0}, "offers": []}

            with patch("coverai.automation.run_offer_explorer", side_effect=slow_run):
                thread = threading.Thread(target=runner.run_once, args=("first",))
                thread.start()
                for _ in range(50):
                    if runner.status()["running"]:
                        break
                    time.sleep(0.01)
                skipped = runner.run_once("second")
                release.set()
                thread.join(timeout=5)

            self.assertEqual(skipped["skipped"], "already_running")
            self.assertFalse(runner.status()["running"])


if __name__ == "__main__":
    unittest.main()
