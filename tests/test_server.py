from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import server
from coverai.storage import CoverAiStore


class FakeSms:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_sms(self, number: str, text: str) -> dict:
        self.sent.append((number, text))
        return {"ok": True, "dryRun": True}


class FakeRunner:
    def __init__(self) -> None:
        self.started = False

    def status(self) -> dict:
        return {"enabled": True, "running": False, "interval_seconds": 900, "last_run": None}

    def run_once(self, trigger: str = "manual") -> dict:
        return {"ok": True, "trigger": trigger, "result": {"run": {"status": "completed"}}}

    def run_async(self, trigger: str = "manual") -> dict:
        self.started = True
        return {"started": True, "trigger": trigger, "automation": self.status()}


class ServerRouteTests(unittest.TestCase):
    def test_offer_routes_and_sms_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_store = server.coverai_store
            original_sms_builder = server.build_sms_client
            original_config = server.DEFAULT_JOB_SEARCH_CONFIG
            fake_sms = FakeSms()
            try:
                server.coverai_store = CoverAiStore(Path(tmp) / "coverai.db")
                server.DEFAULT_JOB_SEARCH_CONFIG = Path(tmp) / "missing.json"
                server.build_sms_client = lambda: fake_sms
                offer, _ = server.coverai_store.upsert_offer({
                    "url": "https://example.com/jobs/1",
                    "title": "Embedded Apprentice",
                    "company": "Example",
                    "score": 77,
                    "summary": "Good match",
                })

                client = server.app.test_client()
                list_response = client.get("/offers?limit=5")
                get_response = client.get(f"/offers/{offer['id']}")
                sms_response = client.post(f"/offers/{offer['id']}/sms-report", json={"number": "+33123456789"})
                status_response = client.post(f"/offers/{offer['id']}/status", json={"status": "selected"})

                self.assertEqual(list_response.status_code, 200)
                self.assertEqual(get_response.status_code, 200)
                self.assertEqual(sms_response.status_code, 200)
                self.assertEqual(status_response.status_code, 200)
                self.assertEqual(fake_sms.sent[0][0], "+33123456789")
                self.assertEqual(status_response.get_json()["offer"]["status"], "selected")
            finally:
                server.coverai_store = original_store
                server.build_sms_client = original_sms_builder
                server.DEFAULT_JOB_SEARCH_CONFIG = original_config

    def test_automation_and_sms_inbound_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_store = server.coverai_store
            original_runner = server.automation_runner
            original_config = server.DEFAULT_JOB_SEARCH_CONFIG
            original_sms_builder = server.build_sms_client
            original_openai = server.openai_client
            fake_sms = FakeSms()
            fake_runner = FakeRunner()
            try:
                server.coverai_store = CoverAiStore(Path(tmp) / "coverai.db")
                server.automation_runner = fake_runner
                server.DEFAULT_JOB_SEARCH_CONFIG = Path(tmp) / "job_search.json"
                server.DEFAULT_JOB_SEARCH_CONFIG.write_text('{"minimum_score": 50}', encoding="utf-8")
                server.build_sms_client = lambda: fake_sms
                server.openai_client = None

                client = server.app.test_client()
                status_response = client.get("/automation/status")
                run_response = client.post("/automation/run-now", json={"trigger": "test"})
                sms_response = client.post("/sms/inbound", json={"sender": "+33600000000", "message": "STATUS"})

                self.assertEqual(status_response.status_code, 200)
                self.assertEqual(run_response.status_code, 200)
                self.assertEqual(sms_response.status_code, 200)
                self.assertIn("Text CAPABILITIES", sms_response.get_json()["reply"])
            finally:
                server.coverai_store = original_store
                server.automation_runner = original_runner
                server.DEFAULT_JOB_SEARCH_CONFIG = original_config
                server.build_sms_client = original_sms_builder
                server.openai_client = original_openai

    def test_application_routes_track_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_store = server.coverai_store
            try:
                server.coverai_store = CoverAiStore(Path(tmp) / "coverai.db")
                offer, _ = server.coverai_store.upsert_offer({
                    "url": "https://example.com/jobs/app",
                    "title": "Embedded Apprentice",
                    "company": "Example",
                    "score": 88,
                })

                client = server.app.test_client()
                create_response = client.post("/applications", json={"offer_id": offer["id"]})
                app = create_response.get_json()["application"]
                get_response = client.get(f"/applications/{app['id']}")
                answer_response = client.post(f"/applications/{app['id']}/questions/next-answer", json={"answer": "September 2026"})
                list_response = client.get("/applications")
                app_packet_response = client.get(f"/applications/{app['id']}/submission-packet")
                offer_packet_response = client.get(f"/offers/{offer['id']}/submission-packet")
                ref_packet_response = client.get("/submission-packets?reference=Example")

                self.assertEqual(create_response.status_code, 201)
                self.assertEqual(get_response.status_code, 200)
                self.assertEqual(answer_response.status_code, 200)
                self.assertEqual(list_response.status_code, 200)
                self.assertEqual(app_packet_response.status_code, 200)
                self.assertEqual(offer_packet_response.status_code, 200)
                self.assertEqual(ref_packet_response.status_code, 200)
                self.assertGreater(answer_response.get_json()["application"]["readiness_percent"], app["readiness_percent"])
                self.assertTrue(app_packet_response.get_json()["packet"]["ready_answers"])
                self.assertEqual(offer_packet_response.get_json()["packet"]["offer"]["id"], offer["id"])
                self.assertEqual(ref_packet_response.get_json()["packet"]["application"]["id"], app["id"])
            finally:
                server.coverai_store = original_store

    def test_user_and_platform_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_store = server.coverai_store
            original_base_dir = server.BASE_DIR
            try:
                server.coverai_store = CoverAiStore(Path(tmp) / "coverai.db")
                server.BASE_DIR = Path(tmp)
                offer, _ = server.coverai_store.upsert_offer({
                    "url": "https://example.com/jobs/platforms",
                    "title": "Platform Engineer",
                    "company": "Example",
                    "score": 82,
                })

                client = server.app.test_client()
                me_response = client.get("/users/me")
                platforms_response = client.get("/platforms")
                accounts_response = client.get("/users/julien/platforms")
                login_response = client.post("/users/julien/platforms/linkedin/login-session", json={"launch": False})
                offers_response = client.get("/users/julien/offers?limit=5")

                self.assertEqual(me_response.status_code, 200)
                self.assertEqual(platforms_response.status_code, 200)
                self.assertEqual(accounts_response.status_code, 200)
                self.assertEqual(login_response.status_code, 200)
                self.assertEqual(offers_response.status_code, 200)
                self.assertEqual(me_response.get_json()["user"]["id"], "julien")
                self.assertIn("linkedin", {platform["id"] for platform in platforms_response.get_json()["platforms"]})
                self.assertIn("accounts", accounts_response.get_json())
                self.assertTrue(Path(login_response.get_json()["profile_dir"]).exists())
                self.assertEqual(offers_response.get_json()["offers"][0]["id"], offer["id"])
            finally:
                server.coverai_store = original_store
                server.BASE_DIR = original_base_dir

    def test_generate_job_still_requires_offer_text(self) -> None:
        client = server.app.test_client()
        response = client.post("/generate-job", json={})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
