#!/usr/bin/env python3
"""Assisted form scan -- the human clears the CAPTCHA, Helene reads the form.

Anti-bot services (DataDome, etc.) CAPTCHA any automation-driven browser, headless or
not, real Chrome or not. That is by design: the CAPTCHA is a HUMAN checkpoint. So this
tool does the only thing that works -- it opens a VISIBLE browser, lets a human clear the
challenge, then reads the now-unlocked application form. This is the assistive model
(docs/auth-and-session-strategy.md) applied to evidence gathering.

Usage (from the CoverGemini repo root):
    python3 scripts/assisted_scan.py <job_or_form_url> [profile_dir] [out.json]

A Chrome window opens. Solve the CAPTCHA if one appears. When the form renders, the
tool captures the field list, writes it to JSON, and closes. Read-only: it never fills
or submits.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from coverai.browser_apply import _looks_like_application, scan_current  # noqa: E402

# Real Chrome, extracted from the .deb into the home dir (no admin install).
CHROME = Path.home() / ".local/opt/chrome/opt/google/chrome/chrome"

# Text on the buttons that advance a posting toward its application form.
_ADVANCE = ["Je suis intéressé", "intéressé", "Postuler", "I'm interested", "Apply", "Candidater"]
_CONSENT = ["Accept", "Accepter", "Tout accepter", "J'accepte"]

# Human time to log in / clear the CAPTCHA / reach the form. Override for login-heavy
# first runs (France Travail Connect) with ASSISTED_WAIT=300.
WAIT_SECONDS = int(os.environ.get("ASSISTED_WAIT", "240"))


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


def wait_for_application(ctx, main_page, manual: bool, wait_seconds: int):
    """Watch EVERY window in the context until a real application form appears.

    The apply flow often opens the real form in a NEW window (France Travail -> a partner
    ATS like Vinci Energies), so we scan every page in the context, not just the first.
    Returns (page, scan) for the first page showing an application form with no CAPTCHA, or
    (None, None) on timeout. In non-manual mode it re-clicks the apply affordance to drive
    toward the form; in manual mode it only watches (so it never disrupts a human login).
    """
    start = time.time()
    form_url = None
    last_readvance = last_print = 0.0
    while time.time() - start < wait_seconds:
        main_page.wait_for_timeout(2000)
        any_captcha = False
        main_fields = 0
        for pg in list(ctx.pages):
            try:
                s = scan_current(pg)
            except Exception:  # noqa: BLE001 -- page mid-load / navigating; skip this tick
                continue
            ctrls = s.get("controls", [])
            if pg is main_page:
                main_fields = len(ctrls)
            if s.get("captcha_detected"):
                any_captcha = True
                if "oneclick" in (s.get("final_url") or ""):
                    form_url = s.get("final_url")
                continue
            if ctrls and _looks_like_application(ctrls):
                return pg, s
        now = time.time()
        if not manual and not any_captcha and now - last_readvance > 6:
            try:
                if form_url:
                    main_page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
                else:
                    _click_first(main_page, _ADVANCE, timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            last_readvance = now
        if now - last_print > 8:
            left = int(wait_seconds - (now - start))
            print(f"   ...waiting (captcha={any_captcha}, windows={len(ctx.pages)}, "
                  f"ft_tab_fields={main_fields}, {left}s left)", flush=True)
            last_print = now
    return None, None


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: python3 scripts/assisted_scan.py <url> [profile_dir] [out.json]")
        return 2
    url = argv[0]
    profile = argv[1] if len(argv) > 1 else ".coverai-browser/users/julien/smartrecruiters"
    out = argv[2] if len(argv) > 2 else "scripts/last_assisted_scan.json"

    # MANUAL mode (ASSISTED_MANUAL=1): the tool never clicks apply/re-advance; the human
    # drives login + navigation and the loop only watches and captures. Needed for
    # login-gated sites where an auto-click would disrupt the human mid-login.
    manual = os.environ.get("ASSISTED_MANUAL") == "1"

    chrome = str(CHROME) if CHROME.exists() else None
    launch_kw = dict(
        headless=False,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
    )
    if chrome:
        launch_kw["executable_path"] = chrome
        print(f"[browser] real Chrome: {chrome}")
    else:
        print("[browser] real Chrome not found; using Playwright Chromium")

    with sync_playwright() as p:
        prof = Path(profile).resolve()
        prof.mkdir(parents=True, exist_ok=True)
        ctx = p.chromium.launch_persistent_context(str(prof), **launch_kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            _click_first(page, _CONSENT, timeout=1500)
            if manual:
                print("\n>>> A Chrome window is open (MANUAL mode -- I will NOT click for you).")
                print(">>> 1) Log into France Travail Connect if prompted.")
                print(">>> 2) Click 'Postuler' and reach the candidature form yourself.")
                print(">>> 3) Solve any CAPTCHA. The moment the form appears, I capture it (read-only).")
            else:
                advanced = _click_first(page, _ADVANCE, timeout=4000)
                print(f"[nav] advanced to form: {advanced}")
                print("\n>>> A Chrome window is open.")
                print(">>> If you see a CAPTCHA ('prove you are human' / a puzzle), SOLVE it.")
            print(f">>> Waiting up to {WAIT_SECONDS}s for the application form to appear...\n", flush=True)

            _, captured = wait_for_application(ctx, page, manual, WAIT_SECONDS)

            if captured:
                Path(out).write_text(json.dumps(captured, ensure_ascii=False, indent=2))
                # Bless-and-catalog: record the form so this ATS is now in the corpus, and
                # the clearance cookie persists in the profile for future headless harvest.
                try:
                    from coverai.form_catalog import FormCatalog
                    FormCatalog().record(captured, source=f"blessed:{captured.get('ats', '?')}")
                    cataloged = "and added to the field catalog"
                except Exception as e:  # noqa: BLE001
                    cataloged = f"(catalog write skipped: {str(e)[:40]})"
                print(f"\n[OK] FORM CAPTURED -> {out} {cataloged}")
                print(f"     url: {captured.get('final_url')}")
                print(f"     ats: {captured.get('ats')} | fields: {len(captured['controls'])}")
                print(f"     this ATS is now blessed -- future scans of it run headless.\n")
                for c in captured["controls"]:
                    lbl = (c["label"] or c["name"] or c["placeholder"] or "").replace("\n", " ")[:50]
                    print(f"   {c['type']:10s} req={int(c['required'])} | {lbl}")
                return 0
            print("\n[TIMEOUT] Form not reached (CAPTCHA unsolved, or a different flow). "
                  "Re-run and solve the challenge, or navigate to the form manually in the window.")
            return 1
        finally:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
