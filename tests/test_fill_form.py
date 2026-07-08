"""WP-H4 injection contract, proven against a local Shadow-DOM fixture (no live site).

Verifies, end to end in a real browser:
  * the approval gate: fill_form refuses unless approved_for_autofill is True;
  * Shadow-DOM filling: values land in inputs hidden inside web components;
  * index disambiguation: two file inputs sharing an id are told apart;
  * human-only consent: the GDPR checkbox is never auto-checked;
  * no submit: the submit button is never clicked.

Skipped automatically if Playwright's browser is unavailable.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from coverai.browser_apply import (
    fill_form,
    map_fields,
    playwright_available,
    scan_current,
    submit_form,
)

FIXTURE = Path(__file__).parent / "fixtures" / "shadow_apply_form.html"


def _packet(approved: bool) -> dict:
    return {
        "offer_ref": "offer:off_fixture",
        "approved_for_autofill": approved,
        "fields": [
            {"name": "first_name", "value": "Julien", "status": "ready", "source": "memory"},
            {"name": "last_name", "value": "Gonzales", "status": "ready", "source": "memory"},
            {"name": "email", "value": "julien@example.com", "status": "ready", "sensitive": True},
            {"name": "motivation", "value": "Tailored paragraph.", "status": "needs_review"},
            {"name": "cv_upload", "value": "artifact:cv_pdf", "status": "ready"},
        ],
    }


@unittest.skipUnless(playwright_available(), "playwright browser not available")
class FillFormContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def _fresh_page(self):
        page = self.browser.new_page()
        page.goto(FIXTURE.as_uri(), wait_until="domcontentloaded")
        page.wait_for_timeout(200)
        return page

    def test_scan_pierces_shadow_and_indexes_duplicate_ids(self):
        page = self._fresh_page()
        scan = scan_current(page)
        ids = {c["id"] for c in scan["controls"]}
        self.assertIn("first-name-input", ids)      # inside a shadow root
        self.assertIn("hiring-manager-message-input", ids)
        files = [c for c in scan["controls"] if c["type"] == "file"]
        self.assertEqual(len(files), 2)             # both shadow roots' file inputs seen
        self.assertEqual({f["selector_index"] for f in files}, {0, 1})  # disambiguated
        page.close()

    def test_gate_refuses_without_approval(self):
        page = self._fresh_page()
        record = fill_form(page, _packet(approved=False))
        self.assertIn("refused", record)
        self.assertEqual(record["filled"], [])
        self.assertEqual(page.locator("#first-name-input").input_value(), "")  # nothing typed
        page.close()

    def test_approved_fill_lands_values_but_respects_limits(self):
        page = self._fresh_page()
        record = fill_form(page, _packet(approved=True))
        filled = {e["logical"] for e in record["filled"]}
        skipped = {e["logical"]: e["status"] for e in record["skipped"]}

        # text/email/textarea landed in the Shadow DOM
        self.assertIn("first_name", filled)
        self.assertIn("email", filled)
        self.assertIn("motivation", filled)
        self.assertEqual(page.locator("#first-name-input").input_value(), "Julien")
        self.assertEqual(page.locator("#email-input").input_value(), "julien@example.com")
        # confirm_email mirrors the email value even with no packet field of its own
        self.assertIn("confirm_email", filled)
        self.assertEqual(page.locator("#confirm-email-input").input_value(), "julien@example.com")

        # consent is human-only -> never auto-checked
        self.assertEqual(skipped.get("consent_gdpr"), "skipped_human_only")
        self.assertFalse(page.locator("#noPolicy").is_checked())

        # a bare artifact ref with no resolution table -> upload skipped, not errored
        self.assertEqual(skipped.get("cv_upload"), "file_needs_real_path")

        # and we NEVER submit
        self.assertFalse(record["submitted"])
        self.assertFalse(page.evaluate("window.__submitted"))
        page.close()

    def test_cv_upload_resolves_artifact_ref_to_real_file(self):
        # WP-H4 follow-up: a cv_upload artifact ref that the packet's `artifacts` table
        # resolves to a real local file is actually uploaded into the (Shadow-DOM) input.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cv_path = Path(td) / "julien_cv.pdf"
            cv_path.write_bytes(b"%PDF-1.4 fake cv for test\n")
            packet = _packet(approved=True)
            for f in packet["fields"]:
                if f["name"] == "cv_upload":
                    f["value"] = "artifact:art_cv_test"
            packet["artifacts"] = [
                {"artifact_id": "art_cv_test", "kind": "pdf",
                 "storage_ref": cv_path.as_uri()},  # file:///.../julien_cv.pdf
            ]

            page = self._fresh_page()
            record = fill_form(page, packet)
            filled = {e["logical"]: e for e in record["filled"]}

            # the resolver turned the ref into a path and the record names the file
            self.assertIn("cv_upload", filled)
            self.assertEqual(filled["cv_upload"]["file"], "julien_cv.pdf")

            # readback: the file object actually landed on the input in the Shadow DOM
            cv = map_fields(scan_current(page))["mapped"]["cv_upload"]
            loc = page.locator(cv["selector"]).nth(cv["selector_index"])
            landed = loc.evaluate("el => el.files.length ? el.files[0].name : null")
            self.assertEqual(landed, "julien_cv.pdf")

            # still never submits
            self.assertFalse(record["submitted"])
            page.close()

    # --- WP-H5: the final-submit gate -------------------------------------------------
    @staticmethod
    def _submit_approval(offer_ref, *, status="approved", risk="final_submit", bind=True):
        bound = offer_ref if bind else "offer:someone_else"
        return {"approval_id": "apr_1", "task_id": "app_1", "status": status,
                "risk_level": risk, "actions": [{"type": "final_submit", "offer_ref": bound}]}

    def test_submit_refuses_without_approval(self):
        page = self._fresh_page()
        rec = submit_form(page, {"offer_ref": "offer:x"},
                          self._submit_approval("offer:x", status="pending"))
        self.assertFalse(rec["submitted"])
        self.assertFalse(page.evaluate("window.__submitted"))  # button never clicked
        page.close()

    def test_submit_refuses_wrong_risk_level(self):
        page = self._fresh_page()
        rec = submit_form(page, {"offer_ref": "offer:x"},
                          self._submit_approval("offer:x", risk="browser_autofill"))
        self.assertFalse(rec["submitted"])
        self.assertFalse(page.evaluate("window.__submitted"))
        page.close()

    def test_submit_refuses_approval_bound_to_a_different_offer(self):
        page = self._fresh_page()
        rec = submit_form(page, {"offer_ref": "offer:x"},
                          self._submit_approval("offer:x", bind=False))
        self.assertEqual(rec["refused"], "approval_not_bound_to_offer")
        self.assertFalse(page.evaluate("window.__submitted"))
        page.close()

    def test_submit_clicks_only_under_valid_bound_approval(self):
        page = self._fresh_page()
        rec = submit_form(page, {"offer_ref": "offer:x"}, self._submit_approval("offer:x"))
        self.assertTrue(rec["submitted"])
        self.assertEqual(rec["clicked"], "envoyer ma candidature")
        self.assertTrue(page.evaluate("window.__submitted"))  # the real button fired
        page.close()


if __name__ == "__main__":
    unittest.main()
