#!/usr/bin/env python3
"""Supervised live fill -- inject a REAL DB-backed submission packet, human watching.

Opens the real apply form in a VISIBLE Chrome window using the blessed profile. You clear
the CAPTCHA if one appears (the clearance cookie may already be valid). Once the form is
reachable:

  * WITHOUT --approve: it prints the fill plan (logical field -> selector -> value) and
    types NOTHING. This is the default, so a bare run can never touch the form.
  * WITH --approve: fill_form() types the packet values in front of you and STOPS. It
    never clicks submit, never checks the GDPR box, and never uploads a file it can't
    verify. --approve is the explicit human approval the contract requires before any fill.

The packet is built by Marie's producer (coverai.submission_packet.build_submission_packet)
from your profile + the application's answers -- not a hardcoded literal.

Usage (from repo root):
    python3 scripts/assisted_fill.py <job_url> [--app <application_id>] [--profile <dir>] [--approve]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from coverai.browser_apply import (  # noqa: E402
    _looks_like_application,
    fill_form,
    prepare_autofill,
    scan_current,
)
from coverai.storage import CoverAiStore  # noqa: E402
from coverai.submission_packet import build_submission_packet  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CHROME = Path.home() / ".local/opt/chrome/opt/google/chrome/chrome"
_ADVANCE = ["Je suis intéressé", "intéressé", "Postuler", "I'm interested", "Apply", "Candidater"]
_CONSENT = ["Accept", "Accepter", "Tout accepter", "J'accepte"]
WAIT_SECONDS = 200


def _load_packet(app_id: str | None, approve: bool) -> dict:
    """Build the real submission packet, and flip the approval flag iff --approve.

    The producer always emits approved_for_autofill=False; approval is a separate,
    explicit human act -- here, passing --approve on the command line.
    """
    store = CoverAiStore(str(REPO_ROOT / "coverai.db"))
    if not app_id:
        apps = store.list_application_tasks()
        if not apps:
            raise SystemExit("No application tasks in coverai.db; pass --app <id> or create one.")
        app_id = apps[0]["id"]  # most recent
        print(f">>> No --app given; using most recent application: {app_id} ({apps[0].get('company')})")
    packet = build_submission_packet(store, app_id)
    packet["approved_for_autofill"] = bool(approve)
    print(f">>> Packet for {packet['company']}: {packet['readiness']['summary']}")
    return packet


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
    parser = argparse.ArgumentParser(description="Supervised live fill of a real application form.")
    parser.add_argument("url", help="the job/apply URL to open")
    parser.add_argument("--app", default=None, help="application_id in coverai.db (default: most recent)")
    parser.add_argument("--profile", default=".coverai-browser/users/julien/smartrecruiters",
                        help="persistent Chromium profile dir (a logged-in/blessed session)")
    parser.add_argument("--approve", action="store_true",
                        help="explicit approval to TYPE into the form; without it, only the plan is printed")
    ns = parser.parse_args(argv)
    url, profile = ns.url, ns.profile

    packet = _load_packet(ns.app, ns.approve)
    if not ns.approve:
        print(">>> DRY RUN (no --approve): I will show the fill plan and type NOTHING.\n", flush=True)

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

            print(f"\n[form ready] {len(scan['controls'])} fields.\n")
            if not ns.approve:
                plan = prepare_autofill(packet, scan)
                print(">>> FILL PLAN (dry run -- nothing typed):")
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                print("\n>>> Re-run with --approve to type these values. Window closes in 20s.\n", flush=True)
                page.wait_for_timeout(20000)
                return 0
            print("Filling now (approved)...\n")
            record = fill_form(page, packet, scan)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            print("\n>>> Filled. NOTHING was submitted. Look at the window to verify, "
                  "then it will close in 25s.\n", flush=True)
            page.wait_for_timeout(25000)
            return 0
        finally:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
