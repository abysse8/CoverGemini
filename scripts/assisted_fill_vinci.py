#!/usr/bin/env python3
"""Supervised end-to-end fill on the REAL France Travail -> Vinci Energies apply flow.

Proves the whole browser-apply pipeline against a live partner form, with a human watching:

  France Travail offer -> click "Postuler" -> Vinci popup (jobs.vinci.com) ->
  wait_for_application follows the popup -> fill_form types the packet + uploads the CV
  (resolved from an artifact ref) -> STOPS. Never submits, never checks a consent control.

Isolated from Marie's scripts/assisted_fill.py: it imports only Helene's browser_apply and
the shared wait_for_application helper, with a hardcoded packet, so it does not depend on the
submission-packet producer or the coverai.db while those are in flux.

Usage (from repo root):
    ASSISTED_WAIT=300 python3 scripts/assisted_fill_vinci.py [france_travail_offer_url]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402

from coverai.browser_apply import fill_form, map_fields  # noqa: E402
# Reuse Helene's popup-following waiter and launch bits (no duplication, no Marie files).
from assisted_scan import CHROME, _CONSENT, _click_first, wait_for_application  # noqa: E402

DEFAULT_OFFER = "https://candidat.francetravail.fr/offres/recherche/detail/210KBYK"
PROFILE = ".coverai-browser/users/julien/francetravail"
WAIT_SECONDS = int(os.environ.get("ASSISTED_WAIT", "240"))

# A real CV on disk, resolved through the artifact seam so this exercises _resolve_artifact_path.
_CV_CANDIDATES = ["library/CV_2025-12-07_JULIEN_GONZALES.pdf", "astek_cv.pdf", "test_cv.pdf"]
_cv_path = next((REPO_ROOT / c for c in _CV_CANDIDATES if (REPO_ROOT / c).exists()), None)

# Values Julien already uses publicly; email marked sensitive so it is masked in the record.
# approved_for_autofill=True is the gate -- set here because a human is supervising this run.
PACKET = {
    "offer_ref": "offer:ft_210KBYK",
    "company": "Vinci Energies (via France Travail)",
    "approved_for_autofill": True,
    "fields": [
        {"name": "first_name", "value": "Julien", "status": "ready", "source": "user"},
        {"name": "last_name", "value": "Gonzales", "status": "ready", "source": "user"},
        {"name": "email", "value": "julienabdougonzales@gmail.com", "status": "ready", "sensitive": True},
        {"name": "cv_upload", "value": "artifact:art_cv_julien", "status": "ready", "source": "library"},
    ],
    "artifacts": [
        {"artifact_id": "art_cv_julien", "kind": "pdf", "title": "Julien Gonzales CV",
         "storage_ref": _cv_path.as_uri() if _cv_path else ""},
    ],
}


def main(argv: list[str]) -> int:
    url = argv[0] if argv else DEFAULT_OFFER
    if _cv_path is None:
        print("[warn] no CV file found; the upload will be skipped (file_needs_real_path).")
    else:
        print(f"[cv] will upload: {_cv_path}")

    launch_kw = dict(headless=False, ignore_default_args=["--enable-automation"],
                     args=["--disable-blink-features=AutomationControlled", "--start-maximized"])
    if CHROME.exists():
        launch_kw["executable_path"] = str(CHROME)

    with sync_playwright() as p:
        prof = (REPO_ROOT / PROFILE)
        prof.mkdir(parents=True, exist_ok=True)
        ctx = p.chromium.launch_persistent_context(str(prof), **launch_kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            _click_first(page, _CONSENT, timeout=1500)
            print("\n>>> Chrome is open. You are logged in already.")
            print(">>> Click 'Postuler à l'offre' and let the Vinci popup open with the questions.")
            print(">>> The moment I see the Vinci form, I fill it in front of you and STOP (no submit).\n",
                  flush=True)

            app_page, scan = wait_for_application(ctx, page, manual=True, wait_seconds=WAIT_SECONDS)
            if scan is None:
                print("\n[TIMEOUT] Vinci form not reached; nothing filled.")
                return 1

            mapped = map_fields(scan)["mapped"]
            print(f"\n[form ready] {scan.get('ats')} @ {scan.get('final_url')}")
            print(f"[form ready] {len(scan['controls'])} fields, {len(mapped)} mapped. Filling now...\n")
            record = fill_form(app_page, PACKET, scan)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            print("\n>>> FILLED. Nothing was submitted; consent was NOT checked. Verify in the window;")
            print(">>> it closes in 30s.\n", flush=True)
            app_page.wait_for_timeout(30000)
            return 0
        finally:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
