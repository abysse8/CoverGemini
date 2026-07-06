"""QA safety net for the Browser Apply agent (Helene).

These tests protect the three invariants that keep browser autofill from doing
harm BEFORE any injection code exists:

  1. Kind-gating  -- a logical field never maps onto the wrong control type
                     (e.g. a file upload must not be driven as a free-text box).
  2. Redaction    -- prepare_autofill() never echoes a `sensitive` value into
                     the fill plan (the plan is a loggable/SMS-able artifact).
  3. Approval gate-- the `approved_for_autofill` flag is surfaced faithfully so
                     the future injection step (WP-H4) can refuse to type
                     without it.

All tests are offline and read-only: they build fake scan/packet dicts and call
the pure planning functions. No browser is launched and nothing is submitted.

Run with:  .venv/bin/python -m unittest tests.test_browser_apply -v
"""

from __future__ import annotations

import json
import unittest

from coverai import browser_apply
from coverai.browser_apply import _kind_ok, map_fields, prepare_autofill

# _looks_like_application is a classifier Helene hasn't built yet (WP-H3b). We look
# it up softly instead of importing it hard: a missing name at import time would take
# the WHOLE test file down with it. If it's absent, the spec tests below skip.
_looks_like_application = getattr(browser_apply, "_looks_like_application", None)


# --- fixtures ---------------------------------------------------------------
#
# A "control" is one entry as scan_form()'s in-page JS emits it. A "scan" is the
# dict scan_form() returns. We hand-build them so the tests never need a browser.

def control(**over):
    """One scanned form control with sane defaults; override per test."""
    base = {
        "tag": "input", "type": "text", "name": "", "id": "",
        "selector": "#x", "label": "", "placeholder": "",
        "required": False, "visible": True, "options": [],
    }
    base.update(over)
    return base


def scan(controls):
    return {
        "final_url": "https://jobs.lever.co/acme/apply",
        "requested_url": "https://jobs.lever.co/acme/apply",
        "ats": "lever",
        "controls": controls,
    }


def packet(fields, approved=None):
    p = {"offer_ref": "offer:off_test", "fields": fields}
    if approved is not None:
        p["approved_for_autofill"] = approved
    return p


# --- 1. kind-gating ---------------------------------------------------------

class KindGatingTests(unittest.TestCase):
    """A file field must never be treated as a textarea, and vice versa."""

    def test_kind_ok_matrix(self):
        # Direct truth table for _kind_ok: the single choke point for gating.
        self.assertTrue(_kind_ok("file", control(type="file")))
        self.assertFalse(_kind_ok("file", control(type="text")))

        self.assertTrue(_kind_ok("textarea", control(tag="textarea", type="textarea")))
        self.assertFalse(_kind_ok("textarea", control(tag="input", type="file")))

        self.assertTrue(_kind_ok("checkbox", control(type="checkbox")))
        self.assertTrue(_kind_ok("checkbox", control(type="radio")))
        self.assertFalse(_kind_ok("checkbox", control(type="text")))

        # "text" accepts single-line inputs but rejects file / textarea / boxes.
        self.assertTrue(_kind_ok("text", control(type="email")))
        self.assertFalse(_kind_ok("text", control(type="file")))
        self.assertFalse(_kind_ok("text", control(tag="textarea", type="textarea")))
        self.assertFalse(_kind_ok("text", control(type="checkbox")))

    def test_file_and_textarea_do_not_cross_map(self):
        # A form with a file upload AND a free-text box, both about "motivation".
        # cover_letter_upload (kind=file) must land on the file input; motivation
        # (kind=textarea) must land on the textarea -- never the reverse.
        s = scan([
            control(tag="input", type="file", id="cl",
                    selector="#cl", label="Lettre de motivation", required=True),
            control(tag="textarea", type="textarea", name="msg",
                    selector='textarea[name="msg"]', label="Votre message de motivation"),
        ])
        mapped = map_fields(s)["mapped"]

        self.assertEqual(mapped["cover_letter_upload"]["selector"], "#cl")
        self.assertEqual(mapped["motivation"]["selector"], 'textarea[name="msg"]')
        # The textarea logical field must not have grabbed the file control.
        self.assertNotEqual(mapped["motivation"]["selector"], "#cl")


# --- 2. sensitive-value masking --------------------------------------------

class SensitiveMaskingTests(unittest.TestCase):
    """prepare_autofill() must not leak a sensitive value into its report."""

    def _plan_for(self, sensitive_value):
        s = scan([
            control(type="text", id="email", selector="#email", label="Email"),
            control(type="text", id="name", selector="#name", label="Full name"),
        ])
        p = packet([
            {"name": "email", "value": sensitive_value, "status": "ready", "sensitive": True},
            {"name": "full_name", "value": "Julien G", "status": "ready", "sensitive": False},
        ])
        return prepare_autofill(p, s)

    def test_sensitive_value_is_masked_in_plan(self):
        plan = self._plan_for("private@example.com")
        by_logical = {e["logical"]: e for e in plan["fill_plan"]}
        self.assertEqual(by_logical["email"]["value_preview"], "***")
        self.assertTrue(by_logical["email"]["sensitive"])

    def test_nonsensitive_value_is_shown(self):
        plan = self._plan_for("private@example.com")
        by_logical = {e["logical"]: e for e in plan["fill_plan"]}
        self.assertEqual(by_logical["full_name"]["value_preview"], "Julien G")

    def test_sensitive_value_never_appears_anywhere_in_serialized_report(self):
        # The whole point: the fill plan is a thing we might log or SMS. The raw
        # secret must not survive JSON serialization of the entire result.
        secret = "TOPSECRET-token-9f3a"
        plan = self._plan_for(secret)
        self.assertNotIn(secret, json.dumps(plan, ensure_ascii=False))


# --- 3. approved_for_autofill gate -----------------------------------------

class ApprovalGateTests(unittest.TestCase):
    """The gate must be reported exactly as the packet sets it -- fail-closed."""

    def _simple(self, approved):
        s = scan([control(type="text", id="email", selector="#email", label="Email")])
        p = packet([{"name": "email", "value": "a@b.c", "status": "ready"}], approved=approved)
        return prepare_autofill(p, s)

    def test_gate_true_when_packet_approved(self):
        self.assertTrue(self._simple(True)["approved_for_autofill"])

    def test_gate_false_when_packet_denies(self):
        self.assertFalse(self._simple(False)["approved_for_autofill"])

    def test_gate_defaults_false_when_absent(self):
        # Fail-closed: a packet with no flag must NOT be treated as approved.
        self.assertFalse(self._simple(None)["approved_for_autofill"])

    def test_prepare_autofill_performs_no_fills(self):
        # Guard the contract note: this function only plans. If someone later
        # wires real typing into prepare_autofill, this marker should be revisited.
        plan = self._simple(True)
        self.assertIn("read-only", plan["note"])


# --- 4. auth-wall vs real form (resolve_apply_target classifier) -----------

@unittest.skipIf(
    _looks_like_application is None,
    "_looks_like_application not implemented yet (WP-H3b, Helene) — spec is red on purpose",
)
class AuthWallExclusionTests(unittest.TestCase):
    """_looks_like_application must not mistake a login/signup wall for a form.

    Evidence 2026-07-05: APEC, HelloWork's external ATS, and LinkedIn all present
    an auth wall (name + email, and often a password) before the real application
    form. Those carry the same name+email signals as a form, so the classifier has
    to fail-closed on password / session markers.
    """

    def test_login_wall_with_password_is_rejected(self):
        controls = [
            control(type="text", name="session_key", label="Email or phone"),
            control(type="password", name="session_password", label="Password"),
        ]
        self.assertFalse(_looks_like_application(controls))

    def test_signup_wall_without_password_is_rejected_by_markers(self):
        # LinkedIn public join wall: name + email but a session_key marker.
        controls = [
            control(type="search", name="firstName", label="Prénom"),
            control(type="search", name="lastName", label="Nom"),
            control(type="text", id="csm-v2_session_key", label="E-mail ou téléphone"),
        ]
        self.assertFalse(_looks_like_application(controls))

    def test_apec_credentials_gate_is_rejected(self):
        controls = [
            control(type="text", id="emailid", label="Adresse email*"),
            control(type="password", id="password", label="Mot de passe*"),
        ]
        self.assertFalse(_looks_like_application(controls))

    def test_real_application_form_is_accepted(self):
        controls = [
            control(type="email", name="email", label="Adresse e-mail"),
            control(tag="input", type="file", name="cv", label="Déposez votre CV"),
            control(tag="textarea", type="textarea", name="msg", label="Lettre de motivation"),
        ]
        self.assertTrue(_looks_like_application(controls))


if __name__ == "__main__":
    unittest.main()
