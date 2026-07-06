from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coverai.explorer import discover_offer_candidates, format_offer_sms, is_direct_offer_url, report_offer_by_sms, run_offer_explorer
from coverai.storage import CoverAiStore


class FakeSms:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_sms(self, number: str, text: str) -> dict:
        self.sent.append((number, text))
        return {"ok": True, "dryRun": True}


class ExplorerTests(unittest.TestCase):
    def test_discovers_job_links_from_seed_page(self) -> None:
        html = """
        <html><body>
          <a href="/jobs/embedded-apprentice">Embedded firmware apprentice</a>
          <a href="/about">About us</a>
        </body></html>
        """

        def fetcher(url: str) -> str:
            if url.endswith("/jobs/embedded-apprentice"):
                return "<html><body>Alternance systemes embarques Linux FPGA Paris</body></html>"
            return html

        candidates = discover_offer_candidates(
            {"source_urls": ["https://example.com/careers"], "keywords": ["embedded"], "max_offers_per_run": 5},
            fetcher=fetcher,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].url, "https://example.com/jobs/embedded-apprentice")
        self.assertIn("Alternance", candidates[0].raw_text)

    def test_prioritizes_direct_offer_urls_and_skips_navigation(self) -> None:
        source_url = "https://www.hellowork.com/fr-fr/emploi/metier_ingenieur-systemes-embarques-region_ile-de-france.html"
        html = """
        <html><body>
          <a href="#main-content">Skip to main content</a>
          <a href="/fr-fr/stage.html">Offres de stage</a>
          <a href="/fr-fr/emplois/80143629.html">Stage - Ingénieur en Système Embarqué H/F Netatmo</a>
          <a href="/fr-fr/emplois/80102550.html">Alternance - Assistant Développement Embarqué H/F AURLOM</a>
        </body></html>
        """

        def fetcher(url: str) -> str:
            if url.endswith("80143629.html"):
                return "<html><body>Netatmo stage ingénieur système embarqué Boulogne-Billancourt</body></html>"
            if url.endswith("80102550.html"):
                return "<html><body>AURLOM alternance développement embarqué Paris</body></html>"
            return html

        candidates = discover_offer_candidates(
            {"source_urls": [source_url], "keywords": ["embarqué"], "max_offers_per_run": 5},
            fetcher=fetcher,
        )

        self.assertTrue(is_direct_offer_url("https://www.hellowork.com/fr-fr/emplois/80143629.html"))
        self.assertEqual([candidate.title for candidate in candidates], [
            "Stage - Ingénieur en Système Embarqué H/F Netatmo",
            "Alternance - Assistant Développement Embarqué H/F AURLOM",
        ])

    def test_run_explorer_stores_and_reports_top_offer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "COVERAI_SMS_ENABLED": "",
            "COVERAI_SMS_NUMBER": "",
            "COVERAI_SMS_MIN_SCORE": "",
            "COVERAI_SMS_MAX_REPORTS_PER_RUN": "",
        }, clear=False):
            config_path = Path(tmp) / "job_search.json"
            config_path.write_text(json.dumps({
                "keywords": ["embedded"],
                "locations": ["Paris"],
                "source_urls": ["https://example.com/careers"],
                "max_offers_per_run": 5,
                "minimum_score": 40,
                "use_playwright": False,
                "sms": {"enabled": True, "number": "+33123456789", "min_score": 40, "max_reports_per_run": 1},
            }), encoding="utf-8")
            store = CoverAiStore(Path(tmp) / "coverai.db")
            sms = FakeSms()

            def fetcher(url: str) -> str:
                if url.endswith("/jobs/1"):
                    return "<html><body>Embedded firmware apprentice Linux FPGA Paris</body></html>"
                return "<html><body><a href='/jobs/1'>Embedded firmware apprentice</a></body></html>"

            result = run_offer_explorer(store, config_path, sms_client=sms, fetcher=fetcher)

            self.assertEqual(result["run"]["status"], "completed")
            self.assertEqual(result["run"]["offers_new"], 1)
            self.assertEqual(result["run"]["offers_reported"], 1)
            self.assertEqual(len(sms.sent), 1)
            offers = store.list_offers()
            self.assertEqual(len(offers), 1)
            self.assertEqual(offers[0]["status"], "reported")

            second = run_offer_explorer(store, config_path, sms_client=sms, fetcher=fetcher)
            self.assertEqual(second["run"]["offers_new"], 0)
            self.assertEqual(second["run"]["offers_reported"], 0)
            self.assertEqual(len(sms.sent), 1)

    def test_format_offer_sms_is_command_friendly(self) -> None:
        text = format_offer_sms({
            "id": "off_1234",
            "score": 88,
            "company": "Acme",
            "title": "Embedded Apprentice",
            "location": "Paris",
        })
        self.assertIn("tell me about this one", text)
        self.assertIn("start applying", text)
        self.assertNotIn("off_1234", text)

    def test_report_offer_by_sms_records_failure(self) -> None:
        class FailingSms:
            def send_sms(self, number: str, text: str) -> dict:
                raise RuntimeError("blocked")

        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer, _ = store.upsert_offer({"url": "https://example.com/jobs/3", "title": "IoT Apprentice"})
            report = report_offer_by_sms(store, offer["id"], "+33123456789", FailingSms())
            self.assertEqual(report["status"], "failed")
            self.assertIn("blocked", report["response"]["error"])


if __name__ == "__main__":
    unittest.main()
