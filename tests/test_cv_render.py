"""Offline tests for the CV renderer (Marie / coverai.forms).

These run without a LaTeX toolchain: they exercise stage 2 (semantic JSON -> CV.tex)
and the artifact-ref contract shape, and prove the packet wires a rendered CV into
its cv_upload field. Stage 3 (pdflatex -> PDF) is validated on a host that has
LaTeX; here we force run_pdflatex=False so the tests are deterministic and offline.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from coverai.cv_render import render_cv
from coverai.storage import CoverAiStore
from coverai.submission_packet import build_submission_packet

_FIXTURE = Path(__file__).parent / "fixtures" / "cv_sections_agixis.json"
# The contract lives in the sibling coordination repo; validate against it when present.
_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "personal-agentic-workflow" / "contracts" / "artifact-ref.schema.json"
)
_ARTIFACT_REF_REQUIRED = ("artifact_id", "kind", "title", "owner_user_id", "storage_ref", "created_at")
_ARTIFACT_KINDS = {"pdf", "image", "text", "csv", "json", "html", "database_snapshot", "link"}


def _sections() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


class RenderCvTests(unittest.TestCase):
    def test_stage2_renders_tailored_tex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            render_cv(_sections(), out_dir=tmp, offer_ref="offer:off_agixis", run_pdflatex=False)
            tex = (Path(tmp) / "CV.tex").read_text(encoding="utf-8")
            self.assertIn("\\documentclass", tex)          # a real LaTeX doc
            self.assertIn("Objectif", tex)                  # objective block rendered
            self.assertIn("Kalman", tex)                    # apl_items rendered
            self.assertIn("STM32", tex)                     # skills rendered

    def test_returns_valid_artifact_ref_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ref = render_cv(
                _sections(), out_dir=tmp, user_id="julien",
                offer_ref="offer:off_agixis",
                created_at="2026-07-08T15:00:00+00:00", run_pdflatex=False,
            )
            for key in _ARTIFACT_REF_REQUIRED:
                self.assertIn(key, ref)
            self.assertIn(ref["kind"], _ARTIFACT_KINDS)
            self.assertEqual(ref["owner_user_id"], "julien")
            self.assertTrue(ref["storage_ref"].startswith("file://"))

    def test_degrades_honestly_without_pdflatex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ref = render_cv(_sections(), out_dir=tmp, run_pdflatex=False)
            # No PDF produced -> the .tex is the artifact, and the gap is flagged,
            # not hidden behind a fake PDF.
            self.assertEqual(ref["kind"], "text")
            self.assertEqual(ref["metadata"]["pdf_status"], "pending_compile")

    def test_deterministic_artifact_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = render_cv(_sections(), out_dir=Path(tmp) / "a", offer_ref="offer:off_agixis", run_pdflatex=False)
            b = render_cv(_sections(), out_dir=Path(tmp) / "b", offer_ref="offer:off_agixis", run_pdflatex=False)
            self.assertEqual(a["artifact_id"], b["artifact_id"])

    @unittest.skipUnless(_SCHEMA.exists(), "artifact-ref contract schema not available")
    def test_validates_against_contract_schema(self) -> None:
        import jsonschema

        with tempfile.TemporaryDirectory() as tmp:
            ref = render_cv(_sections(), out_dir=tmp, offer_ref="offer:off_agixis",
                            created_at="2026-07-08T15:00:00+00:00", run_pdflatex=False)
            jsonschema.validate(ref, json.loads(_SCHEMA.read_text(encoding="utf-8")))


class PacketWiringTests(unittest.TestCase):
    def _store_with_application(self, tmp: str) -> tuple[CoverAiStore, str]:
        store = CoverAiStore(Path(tmp) / "coverai.db")
        store.upsert_profile(first_name="Julien", last_name="Gonzales", email="j@example.com")
        offer, _ = store.upsert_offer({
            "url": "https://example.com/jobs/1", "title": "Embedded Intern",
            "company": "Agixis", "score": 90, "summary": "C firmware role",
        })
        app, _ = store.upsert_application_task(offer["id"])
        return store, app["id"]

    def test_injected_cv_becomes_cv_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            ref = render_cv(_sections(), out_dir=Path(tmp) / "cv",
                            offer_ref="offer:off_agixis", run_pdflatex=False)
            packet = build_submission_packet(store, app_id, cv_artifact=ref)
            fields = {f["name"]: f for f in packet["fields"]}
            self.assertEqual(fields["cv_upload"]["value"], f"artifact:{ref['artifact_id']}")
            self.assertEqual(fields["cv_upload"]["status"], "needs_review")
            self.assertIn(ref, packet["artifacts"])

    def test_no_cv_still_reports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, app_id = self._store_with_application(tmp)
            packet = build_submission_packet(store, app_id)  # no cv_artifact
            fields = {f["name"]: f for f in packet["fields"]}
            self.assertEqual(fields["cv_upload"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
