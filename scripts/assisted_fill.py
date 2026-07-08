#!/usr/bin/env python3
"""Supervised live fill -- prove WP-H4 injection on a REAL form, with a human watching.

Opens the real apply form in a VISIBLE Chrome window using the blessed profile. You clear
the CAPTCHA if one appears (the clearance cookie may already be valid). Once the form is
reachable, fill_form() types the packet values in front of you and STOPS. It never clicks
submit, never checks the GDPR box, and never uploads a file it cannot verify.

Usage (from repo root):
    python3 scripts/assisted_fill.py <job_url> [profile_dir]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from coverai.browser_apply import (  # noqa: E402
    _looks_like_application,
    fill_form,
    scan_current,
)

CHROME = Path.home() / ".local/opt/chrome/opt/google/chrome/chrome"
_ADVANCE = ["Je suis intéressé", "intéressé", "Postuler", "I'm interested", "Apply", "Candidater"]
_CONSENT = ["Accept", "Accepter", "Tout accepter", "J'accepte"]
WAIT_SECONDS = 200

# A minimal real packet. Values you already use publicly; email marked sensitive.
# approved_for_autofill=True is what unlocks typing -- set here because YOU are supervising.
PACKET = {
    "offer_ref": "offer:off_a6b310e9",
    "approved_for_autofill": True,
    "fields": [
        {"name": "first_name", "value": "Julien", "status": "ready", "source": "user"},
        {"name": "last_name", "value": "Gonzales", "status": "ready", "source": "user"},
        {"name": "email", "value": "julienabdougonzales@gmail.com", "status": "ready", "sensitive": True},
        {"name": "location_city", "value": "Paris", "status": "ready", "source": "user"},
        {"name": "motivation", "value": "Draft: strong fit for embedded systems; happy to detail.",
         "status": "needs_review", "source": "generated"},
    ],
}


def _click_first(page, texts, timeout=2500) -> bool:
    for t in texts:
        loc = page.locator(f'a:has-text("{t}"), button:has-text("{t}")').filter(visible=True)
        if loc.count():
            try:
                loc.first.click(timeout=timeout)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: python3 scripts/assisted_fill.py <job_url> [profile_dir]")
        return 2
    url = argv[0]
    profile = argv[1] if len(argv) > 1 else ".coverai-browser/users/julien/smartrecruiters"

    launch_kw = dict(headless=False, ignore_default_args=["--enable-automation"],
                     args=["--disable-blink-features=AutomationControlled", "--start-maximized"])
    if CHROME.exists():
        launch_kw["executable_path"] = str(CHROME)

    with sync_playwright() as p:
        prof = Path(profile).resolve()
        prof.mkdir(parents=True, exist_ok=True)
        ctx = p.chromium.launch_persistent_context(str(prof), **launch_kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            _click_first(page, _CONSENT, timeout=1500)
            _click_first(page, _ADVANCE, timeout=4000)
            print("\n>>> Chrome window open. Solve the CAPTCHA if one appears.")
            print(">>> When the form shows, I will fill it in front of you and STOP (no submit).\n", flush=True)

            form_url, last_readvance, start = None, 0.0, time.time()
            scan = None
            while time.time() - start < WAIT_SECONDS:
                page.wait_for_timeout(2000)
                s = scan_current(page)
                ctrls = s.get("controls", [])
                if not s.get("captcha_detected") and ctrls and _looks_like_application(ctrls):
                    scan = s
                    break
                if s.get("captcha_detected") and "oneclick" in (s.get("final_url") or ""):
                    form_url = s.get("final_url")
                now = time.time()
                if not s.get("captcha_detected") and now - last_readvance > 6:
                    try:
                        page.goto(form_url, timeout=30000) if form_url else _click_first(page, _ADVANCE, 3000)
                    except Exception:  # noqa: BLE001
                        pass
                    last_readvance = now
                print(f"   ...waiting (captcha={s.get('captcha_detected')}, fields={len(ctrls)})", flush=True)

            if not scan:
                print("\n[TIMEOUT] form not reached; nothing filled.")
                return 1

            print(f"\n[form ready] {len(scan['controls'])} fields. Filling now...\n")
            record = fill_form(page, PACKET, scan)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            print("\n>>> Filled. NOTHING was submitted. Look at the window to verify, "
                  "then it will close in 25s.\n", flush=True)
            page.wait_for_timeout(25000)
            return 0
        finally:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
