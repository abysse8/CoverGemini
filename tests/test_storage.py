from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coverai.storage import CoverAiStore


class StorageTests(unittest.TestCase):
    def test_seeds_default_user_and_platform_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")

            user = store.get_user("julien")
            platforms = store.list_platforms()
            accounts = store.user_platform_accounts("julien")

            self.assertIsNotNone(user)
            self.assertEqual(user["role"], "admin")
            self.assertIn("linkedin", {platform["id"] for platform in platforms})
            self.assertEqual(len(accounts), len(platforms))
            linkedin = store.get_user_platform_account("julien", "linkedin")
            self.assertIn(".coverai-browser", linkedin["profile_dir"])

    def test_offer_upsert_dedupes_by_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            first, created_first = store.upsert_offer({
                "url": "https://example.com/jobs/1",
                "title": "Embedded Apprentice",
                "company": "Example",
                "score": 80,
                "summary": "first",
            })
            second, created_second = store.upsert_offer({
                "url": "https://example.com/jobs/1",
                "title": "Embedded Apprentice Updated",
                "company": "Example",
                "score": 85,
                "summary": "second",
            })

            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["score"], 85)
            self.assertEqual(len(store.list_offers()), 1)

    def test_offers_are_scoped_by_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            with store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (id, email, display_name, role, phone, created_at, updated_at)
                    VALUES ('alice', '', 'Alice', 'user', '', 'now', 'now')
                    """
                )
            julien_offer, _ = store.upsert_offer({"url": "https://example.com/jobs/shared", "title": "Julien"}, user_id="julien")
            alice_offer, _ = store.upsert_offer({"url": "https://example.com/jobs/shared", "title": "Alice"}, user_id="alice")

            self.assertNotEqual(julien_offer["id"], alice_offer["id"])
            self.assertEqual(store.list_offers(user_id="julien")[0]["title"], "Julien")
            self.assertEqual(store.list_offers(user_id="alice")[0]["title"], "Alice")
            with self.assertRaises(KeyError):
                store.mark_offer_status(julien_offer["id"], "selected", user_id="alice")

    def test_mark_status_and_sms_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer, _ = store.upsert_offer({"url": "https://example.com/jobs/2", "title": "Firmware Intern"})

            updated = store.mark_offer_status(offer["id"], "reported")
            report = store.record_sms_report(offer["id"], "+33123456789", "hello", "sent", {"ok": True})

            self.assertEqual(updated["status"], "reported")
            self.assertEqual(report["offer_id"], offer["id"])
            self.assertEqual(report["status"], "sent")

    def test_records_sms_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            message = store.record_sms_message("inbound", "+33123456789", "STATUS", "idle", "status")

            self.assertEqual(message["command"], "status")
            self.assertEqual(message["response_text"], "idle")

    def test_application_task_tracks_readiness_questions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer, _ = store.upsert_offer({
                "url": "https://example.com/jobs/netatmo",
                "title": "Embedded Intern",
                "company": "Netatmo",
                "score": 92,
                "summary": "Zephyr and C firmware role",
            })

            app, created = store.upsert_application_task(offer["id"])
            questions = store.list_application_questions(app["id"])
            next_question = next(question for question in questions if question["status"] == "needs_user")
            store.update_application_question(next_question["id"], answer="September 2026", answer_source="user_sms", confidence=100, status="confirmed")
            updated = store.recalculate_application_readiness(app["id"])
            packet = store.application_submission_packet(app["id"])

            self.assertTrue(created)
            self.assertEqual(app["offer_id"], offer["id"])
            self.assertGreater(len(questions), 0)
            self.assertGreater(updated["readiness_percent"], app["readiness_percent"])
            self.assertEqual(packet["application"]["id"], app["id"])
            self.assertIn("playwright_payload", packet)
            self.assertGreaterEqual(len(packet["ready_answers"]), 3)
            self.assertTrue(any(answer["field_key"] == "start_date_availability" for answer in packet["ready_answers"]))
            self.assertTrue(any(field["field_key"] == "start_date_availability" for field in packet["playwright_payload"]["fields"]))
            self.assertTrue(packet["missing_required"])

    def test_offer_reference_resolves_latest_reported_and_company_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer, _ = store.upsert_offer({"url": "https://example.com/jobs/1", "title": "Embedded Intern", "company": "Netatmo", "score": 90})
            kelenn, _ = store.upsert_offer({"url": "https://example.com/jobs/kelenn", "title": "Embedded Engineer", "company": "KELENN Technology", "score": 80})
            store.record_sms_report(offer["id"], "+33123456789", "hello", "sent", {"ok": True})
            store.record_sms_report(kelenn["id"], "+33123456789", "hello", "sent", {"ok": True})

            self.assertEqual(store.find_offer_by_reference("the last one", phone="+33123456789")["id"], kelenn["id"])
            self.assertEqual(store.find_offer_by_reference("tell me about Netatmo", phone="+33123456789")["id"], offer["id"])
            self.assertEqual(store.find_offer_by_reference("what is ready for submission at Kellen", phone="+33123456789")["id"], kelenn["id"])
            self.assertEqual(store.find_offer_by_reference(f"VIEW {offer['id']}", phone="+33123456789")["id"], offer["id"])

    def test_marks_stale_running_explorer_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            run = store.create_explorer_run("config.json")

            count = store.mark_stale_explorer_runs("restart")
            updated = store.get_explorer_run(run["id"])

            self.assertEqual(count, 1)
            self.assertEqual(updated["status"], "failed")
            self.assertEqual(updated["error"], "restart")


if __name__ == "__main__":
    unittest.main()
