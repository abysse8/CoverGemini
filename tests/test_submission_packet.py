from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coverai.storage import CoverAiStore
from coverai.submission_packet import build_submission_packet


class SubmissionPacketTests(unittest.TestCase):
    def _store_with_application(self, tmp: str) -> tuple[CoverAiStore, str]:
        """A store with a profile and one application whose start_date is confirmed."""
        store = CoverAiStore(Path(tmp) / "coverai.db")
        store.upsert_profile(
            first_name="Julien", last_name="Gonzales",
            email="julien@example.com", phone="+33000000000",
            location_city="Paris", location_country="France",
            linkedin_url="https://linkedin.com/in/julien", portfolio_url="https://github.com/x",
        )
        offer, _ = store.upsert_offer({
            "url": "https://example.com/jobs/1", "title": "Embedded Intern",
            "company": "Agixis", "score": 90, "summary": "C firmware role",
        })
        app, _ = store.upsert_application_task(offer["id"])
        # Confirm the availability question so it becomes a 'ready' logical field.
        questions = store.list_application_questions(app["id"])
        start = next(q for q in questions if q["label"].startswith("Start date"))
        store.update_application_question(
            start["id"], answer="September 2026", answer_source="user_sms",
            confidence=100, status="confirmed",
        )
        return store, app["id"]

    def _by_name(self, packet: dict) -> dict:
        return {f["name"]: f for f in packet["fields"]}

    def test_identity_is_copied_from_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            fields = self._by_name(build_submission_packet(store, app_id))
            self.assertEqual(fields["first_name"]["value"], "Julien")
            self.assertEqual(fields["first_name"]["status"], "ready")
            self.assertEqual(fields["first_name"]["source"], "memory")
            self.assertEqual(fields["linkedin_url"]["value"], "https://linkedin.com/in/julien")

    def test_field_key_is_translated_to_logical_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            fields = self._by_name(build_submission_packet(store, app_id))
            # start_date_availability -> start_date; motivation angle -> motivation
            self.assertIn("start_date", fields)
            self.assertIn("motivation", fields)
            # app-specific keys never leak through untranslated
            self.assertNotIn("start_date_availability", fields)
            self.assertNotIn("cover_application_motivation_angle", fields)

    def test_status_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            fields = self._by_name(build_submission_packet(store, app_id))
            # confirmed answer -> ready
            self.assertEqual(fields["start_date"]["status"], "ready")
            # AI-drafted motivation -> needs_review (a human must read it first)
            self.assertEqual(fields["motivation"]["status"], "needs_review")
            # no CV artifact attached -> cv_upload missing (not faked)
            self.assertEqual(fields["cv_upload"]["status"], "missing")

    def test_email_and_phone_are_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            fields = self._by_name(build_submission_packet(store, app_id))
            self.assertTrue(fields["email"]["sensitive"])
            self.assertTrue(fields["phone"]["sensitive"])
            self.assertFalse(fields["first_name"]["sensitive"])

    def test_marie_never_pre_approves_or_emits_derived_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            packet = build_submission_packet(store, app_id)
            names = {f["name"] for f in packet["fields"]}
            self.assertFalse(packet["approved_for_autofill"])
            self.assertNotIn("full_name", names)      # Helene derives this
            self.assertNotIn("confirm_email", names)   # Helene derives this

    def test_consent_is_emitted_but_human_only(self) -> None:
        # Freeze redline #5: Marie emits the consent_gdpr slot (so Helene has a
        # stable field to map) but NEVER marks it ready or sources a value -- only
        # the human can consent, at fill time.
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            fields = self._by_name(build_submission_packet(store, app_id))
            self.assertIn("consent_gdpr", fields)
            self.assertEqual(fields["consent_gdpr"]["status"], "needs_review")
            self.assertNotEqual(fields["consent_gdpr"]["status"], "ready")
            self.assertEqual(fields["consent_gdpr"]["value"], "")
            self.assertNotEqual(fields["consent_gdpr"]["source"], "generated")

    def test_cover_letter_upload_is_present_and_artifact_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            # No rendered letter -> missing, not faked.
            fields = self._by_name(build_submission_packet(store, app_id))
            self.assertEqual(fields["cover_letter_upload"]["status"], "missing")
            # Injected letter artifact -> referenced as artifact:<id>.
            letter = {"artifact_id": "art_letter_1", "kind": "pdf", "title": "Letter",
                      "owner_user_id": "julien", "storage_ref": "file:///tmp/letter.pdf",
                      "created_at": "2026-07-08T15:00:00+00:00"}
            fields = self._by_name(
                build_submission_packet(store, app_id, cover_letter_artifact=letter)
            )
            self.assertEqual(fields["cover_letter_upload"]["value"], "artifact:art_letter_1")
            self.assertEqual(fields["cover_letter_upload"]["status"], "needs_review")

    def test_store_build_contract_packet_returns_full_14(self) -> None:
        # The gateway seam: MarieFormsAgent calls store.build_contract_packet(),
        # which must return the complete frozen 14-field contract.
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            packet = store.build_contract_packet(app_id)
            names = {f["name"] for f in packet["fields"]}
            self.assertEqual(len(names), 14)
            self.assertIn("cover_letter_upload", names)
            self.assertIn("consent_gdpr", names)

    def test_contract_required_keys_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            packet = build_submission_packet(store, app_id)
            for key in ("packet_id", "offer_ref", "fields", "readiness", "created_at"):
                self.assertIn(key, packet)
            self.assertIn("ready_count", packet["readiness"])
            self.assertIn("total_count", packet["readiness"])


if __name__ == "__main__":
    unittest.main()
