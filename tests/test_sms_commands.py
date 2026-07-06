from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from coverai.sms_commands import handle_coverai_sms
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

    def run_async(self, trigger: str) -> dict:
        self.started = True
        return {"started": True, "trigger": trigger}

    def status(self) -> dict:
        return {"running": False, "interval_seconds": 900}


class ExplodingOpenAi:
    class Chat:
        class Completions:
            @staticmethod
            def create(**_kwargs):
                raise AssertionError("OpenAI should not be called for hardcoded commands")

        completions = Completions()

    chat = Chat()


class SmsCommandTests(unittest.TestCase):
    def config_path(self, tmp: str) -> Path:
        path = Path(tmp) / "job_search.json"
        path.write_text(json.dumps({"minimum_score": 60, "sms": {"min_score": 60}}), encoding="utf-8")
        return path

    def seed_offer(self, store: CoverAiStore) -> dict:
        offer, _ = store.upsert_offer({
            "url": "https://example.com/jobs/embedded",
            "title": "Embedded Apprentice",
            "company": "Example",
            "location": "Paris",
            "score": 88,
            "summary": "Embedded Linux apprenticeship",
        })
        return offer

    def test_agent_scouts_and_reports_status_naturally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            self.seed_offer(store)
            runner = FakeRunner()
            sms = FakeSms()
            config = self.config_path(tmp)

            run = handle_coverai_sms(store, "+336", "scout for new roles", config, sms, automation_runner=runner)
            status = handle_coverai_sms(store, "+336", "what is my status?", config, sms, automation_runner=runner)

            self.assertTrue(runner.started)
            self.assertEqual(run["command"], "agent.scout")
            self.assertIn("tracking", status["reply"])

    def test_hardcoded_capabilities_status_offers_and_queue_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer = self.seed_offer(store)
            store.record_sms_report(offer["id"], "+336", "hello", "sent", {"ok": True})
            store.upsert_application_task(offer["id"])
            config = self.config_path(tmp)
            sms = FakeSms()
            runner = FakeRunner()

            capabilities = handle_coverai_sms(store, "+336", "CAPABILTIES", config, sms, openai_client=ExplodingOpenAi(), automation_runner=runner)
            status = handle_coverai_sms(store, "+336", "STATUS", config, sms, openai_client=ExplodingOpenAi(), automation_runner=runner)
            offers = handle_coverai_sms(store, "+336", "OFFERS", config, sms, openai_client=ExplodingOpenAi(), automation_runner=runner)
            queue = handle_coverai_sms(store, "+336", "QUEUE", config, sms, openai_client=ExplodingOpenAi(), automation_runner=runner)

            self.assertEqual(capabilities["command"], "agent.capabilities")
            self.assertIn("SCOUT", capabilities["reply"])
            self.assertEqual(status["command"], "agent.status")
            self.assertIn("Offers:", status["reply"])
            self.assertEqual(offers["command"], "agent.offers")
            self.assertIn("reported", offers["reply"])
            self.assertEqual(queue["command"], "agent.queue")
            self.assertIn("Example", queue["reply"])

    def test_agent_sends_more_waiting_offers_and_marks_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer = self.seed_offer(store)
            sms = FakeSms()

            result = handle_coverai_sms(store, "+336", "send me more opportunities", self.config_path(tmp), sms)

            self.assertEqual(result["command"], "agent.more")
            self.assertEqual(len(sms.sent), 1)
            self.assertEqual(store.get_offer(offer["id"])["status"], "reported")
            self.assertNotIn("VIEW", sms.sent[0][1])

    def test_agent_coaches_about_latest_reported_offer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer = self.seed_offer(store)
            store.record_sms_report(offer["id"], "+336", "hello", "sent", {"ok": True})

            result = handle_coverai_sms(store, "+336", "tell me about this one", self.config_path(tmp), FakeSms())

            self.assertEqual(result["command"], "agent.coach")
            self.assertIn("embedded", result["reply"].lower())

    def test_agent_creates_application_and_answers_missing_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer = self.seed_offer(store)
            store.record_sms_report(offer["id"], "+336", "hello", "sent", {"ok": True})

            created = handle_coverai_sms(store, "+336", "start applying to this one", self.config_path(tmp), FakeSms())
            answered = handle_coverai_sms(store, "+336", "September 2026", self.config_path(tmp), FakeSms())
            packet = handle_coverai_sms(store, "+336", "what is ready for submission for this one?", self.config_path(tmp), FakeSms())

            app = store.get_application_for_offer(offer["id"])
            self.assertEqual(created["command"], "agent.apply")
            self.assertIn("required answers handled", created["reply"])
            self.assertEqual(answered["command"], "agent.answer_question")
            self.assertEqual(packet["command"], "agent.submission_packet")
            self.assertIn("Ready to inject", packet["reply"])
            self.assertGreater(app["readiness_percent"], 0)

    def test_agent_saves_mixed_answer_and_review_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer = self.seed_offer(store)
            store.record_sms_report(offer["id"], "+336", "hello", "sent", {"ok": True})
            handle_coverai_sms(store, "+336", "start applying to this one", self.config_path(tmp), FakeSms())
            handle_coverai_sms(store, "+336", "September 2026", self.config_path(tmp), FakeSms())

            result = handle_coverai_sms(
                store,
                "+336",
                "I have French nationality so I'm allowed to work. Can I see what you have written up for the application so far?",
                self.config_path(tmp),
                FakeSms(),
                openai_client=ExplodingOpenAi(),
            )
            app = store.get_application_for_offer(offer["id"])
            questions = store.list_application_questions(app["id"])
            work_auth = next(question for question in questions if question["label"] == "Work authorization / location constraints")

            self.assertEqual(result["command"], "agent.answer_and_review")
            self.assertIn("Saved Work authorization", result["reply"])
            self.assertEqual(work_auth["status"], "confirmed")
            self.assertIn("French nationality", work_auth["answer"])

    def test_agent_reviews_current_application_without_openai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer = self.seed_offer(store)
            store.upsert_offer({"url": "https://example.com/jobs/bad", "title": "N/A", "company": "N/A", "score": 0})
            store.record_sms_report(offer["id"], "+336", "hello", "sent", {"ok": True})
            handle_coverai_sms(store, "+336", "start applying to this one", self.config_path(tmp), FakeSms())

            result = handle_coverai_sms(
                store,
                "+336",
                "Can I see what you have written up for the application so far?",
                self.config_path(tmp),
                FakeSms(),
                openai_client=ExplodingOpenAi(),
            )

            self.assertEqual(result["command"], "agent.submission_packet")
            self.assertIn("Ready to inject", result["reply"])
            self.assertIn("Missing", result["reply"])
            self.assertNotIn("N/A", result["reply"])
            self.assertNotIn("Use the stored CoverAI CV context", result["reply"])

    def test_agent_reviews_coverai_pipeline_without_application_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer = self.seed_offer(store)
            store.upsert_application_task(offer["id"])

            result = handle_coverai_sms(
                store,
                "+336",
                "Can you review the CoverAI pipeline and suggest fixes or modifications?",
                self.config_path(tmp),
                FakeSms(),
                openai_client=ExplodingOpenAi(),
            )

            self.assertEqual(result["command"], "agent.system_review")
            self.assertIn("pipeline review", result["reply"].lower())
            self.assertIn("deterministic tools", result["reply"])
            self.assertNotIn("80%", result["reply"])
            self.assertNotIn("Agixis", result["reply"])

    def test_agent_does_not_show_latest_application_for_named_offer_without_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            agixis = self.seed_offer(store)
            kelenn, _ = store.upsert_offer({
                "url": "https://example.com/jobs/kelenn",
                "title": "Ingénieur Systèmes Embarqués H/F",
                "company": "KELENN Technology",
                "location": "Igny",
                "score": 95,
            })
            store.record_sms_report(kelenn["id"], "+336", "hello", "sent", {"ok": True})
            store.upsert_application_task(agixis["id"])

            result = handle_coverai_sms(
                store,
                "+336",
                "What is ready for submission at Kellen?",
                self.config_path(tmp),
                FakeSms(),
                openai_client=ExplodingOpenAi(),
            )

            self.assertEqual(result["command"], "agent.submission_packet")
            self.assertIn("KELENN Technology", result["reply"])
            self.assertIn("no application task", result["reply"])
            self.assertNotIn("Example:", result["reply"])

    def test_agent_still_handles_old_view_and_skip_phrasing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer = self.seed_offer(store)

            view = handle_coverai_sms(store, "+336", f"VIEW {offer['id']}", self.config_path(tmp), FakeSms())
            skip = handle_coverai_sms(store, "+336", f"SKIP {offer['id']}", self.config_path(tmp), FakeSms())

            self.assertEqual(view["command"], "agent.coach")
            self.assertEqual(skip["command"], "agent.skip")
            self.assertEqual(store.get_offer(offer["id"])["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
