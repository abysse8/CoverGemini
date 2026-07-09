from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coverai.browser_apply import collect_interview_questions
from coverai.coach import draft_interview_answers
from coverai.storage import CoverAiStore


class _FakeOpenAI:
    """Minimal stand-in for the OpenAI client: echoes a job-tailored answer."""

    class _Comp:
        def create(self, model, messages):  # noqa: ANN001
            import json
            payload = json.loads(messages[1]["content"])
            text = f"Tailored for {payload['offer']['company']}: {payload['question'][:20]}"
            return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": text})})]})

    chat = type("Chat", (), {"completions": _Comp()})


class HeleneCollectTests(unittest.TestCase):
    def test_classifies_by_dominant_phrasing(self) -> None:
        items = collect_interview_questions([
            "Tell me about a challenging debugging problem you solved.",  # behavioral beats 'debug'
            "Explain the difference between a mutex and a semaphore.",     # technical
            "Why do you want to work at Agixis?",                          # company
            "What is the weather today",                                    # general
        ])
        by_q = {i["question"][:10]: i["category"] for i in items}
        self.assertEqual(by_q["Tell me ab"], "behavioral")
        self.assertEqual(by_q["Explain th"], "technical")
        self.assertEqual(by_q["Why do you"], "company")
        self.assertEqual(by_q["What is th"], "general")

    def test_drops_blanks_and_duplicates(self) -> None:
        items = collect_interview_questions(["Same?", "same?", "  ", "Other?"])
        self.assertEqual(len(items), 2)

    def test_source_is_carried(self) -> None:
        items = collect_interview_questions(["A technical question?"], source="glassdoor")
        self.assertEqual(items[0]["source"], "glassdoor")


class CamilleCoachTests(unittest.TestCase):
    def test_fallback_without_client(self) -> None:
        offer = {"company": "Agixis", "title": "Embedded Dev"}
        drafts = draft_interview_answers(offer, [{"id": "iq1", "question": "Why us?", "category": "company"}])
        self.assertEqual(len(drafts), 1)
        self.assertIn("no AI client", drafts[0]["suggested_answer"])

    def test_uses_injected_client(self) -> None:
        offer = {"company": "Agixis", "title": "Embedded Dev"}
        drafts = draft_interview_answers(
            offer, [{"id": "iq1", "question": "Explain a mutex", "category": "technical"}],
            openai_client=_FakeOpenAI(),
        )
        self.assertIn("Tailored for Agixis", drafts[0]["suggested_answer"])


class InterviewStoreTests(unittest.TestCase):
    def _offer(self, store: CoverAiStore) -> str:
        offer, _ = store.upsert_offer({
            "url": "https://x/agixis", "title": "Embedded Dev", "company": "Agixis",
            "score": 90, "summary": "C firmware",
        })
        return offer["id"]

    def test_add_dedupes_and_list_and_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer_id = self._offer(store)
            items = collect_interview_questions([
                "Explain a mutex.", "Why us?", "Explain a mutex.",  # dup dropped on collect
            ])
            store.add_interview_questions(offer_id, items)
            # Re-adding is idempotent on question text.
            store.add_interview_questions(offer_id, items)
            rows = store.list_interview_questions(offer_id)
            self.assertEqual(len(rows), 2)

            r0 = store.interview_readiness(offer_id)
            self.assertEqual(r0, {"total": 2, "answered": 0, "coached": 0, "percent": 0})

            store.update_interview_question(rows[0]["id"], suggested_answer="draft", status="coached")
            store.update_interview_question(rows[0]["id"], answer="my answer", status="answered", confidence=100)
            r1 = store.interview_readiness(offer_id)
            self.assertEqual(r1["answered"], 1)
            self.assertEqual(r1["percent"], 50)

    def test_update_rejects_unknown_field_and_bad_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "coverai.db")
            offer_id = self._offer(store)
            store.add_interview_questions(offer_id, [{"question": "Q?"}])
            qid = store.list_interview_questions(offer_id)[0]["id"]
            row = store.update_interview_question(qid, status="coached", bogus="x")
            self.assertNotIn("bogus", row)
            with self.assertRaises(KeyError):
                store.update_interview_question("nope")


if __name__ == "__main__":
    unittest.main()
