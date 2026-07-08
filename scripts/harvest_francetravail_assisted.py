#!/usr/bin/env python3
"""Roam & harvest: apply to France Travail jobs normally; Helene banks every form's questions.

Why supervised: France Travail is DataDome-walled and its applies redirect to partner ATSs in
popups -- headless automation does not reach them reliably, but a real-Chrome window you drive
does (you clear any challenge, you log in). So this rides YOUR applying session: you search and
click "Postuler" as usual; whenever a NEW application form appears in ANY window (incl. a partner
popup), Helene records it to form_catalog.db and prints + saves its required questions -- content
for Marie (answers) and Camille (coaching). Read-only: it never types and never submits, so you
fill and submit each application yourself.

Usage (repo root):
    HARVEST_SECONDS=900 python3 scripts/harvest_francetravail_assisted.py [start_url]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402

from coverai.browser_apply import _looks_like_application, scan_current, unmapped_questions  # noqa: E402
from coverai.form_catalog import FormCatalog  # noqa: E402
from assisted_scan import CHROME, _CONSENT, _click_first  # noqa: E402

PROFILE = ".coverai-browser/users/julien/francetravail"
START_URL = "https://candidat.francetravail.fr/offres/emploi"
OUT = "scripts/francetravail_questions.json"
DURATION = int(os.environ.get("HARVEST_SECONDS", "900"))  # how long to keep harvesting


def main(argv: list[str]) -> int:
    start_url = argv[0] if argv else START_URL
    catalog = FormCatalog()
    captured: dict[str, dict] = {}   # final_url -> scan  (dedupe: one form per URL)
    questions: list[dict] = []

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
            page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            _click_first(page, _CONSENT, timeout=1500)
            print(f"\n>>> Roam & harvest is on for up to {DURATION//60} min.")
            print(">>> Search and apply to jobs as usual. Each apply form you open, I bank its")
            print(">>> questions for Marie + Camille. I never type or submit -- that's yours.\n", flush=True)

            end = time.time() + DURATION
            last_print = 0.0
            while time.time() < end:
                page.wait_for_timeout(3000)
                try:
                    pages = list(ctx.pages)
                except Exception:  # noqa: BLE001 -- browser closed by the user
                    break
                if not pages:
                    break
                for pg in pages:
                    try:
                        s = scan_current(pg)
                    except Exception:  # noqa: BLE001 -- page mid-nav; skip this tick
                        continue
                    ctrls = s.get("controls", [])
                    if s.get("captcha_detected") or not (ctrls and _looks_like_application(ctrls)):
                        continue
                    key = s.get("final_url") or ""
                    if not key or key in captured:
                        continue
                    captured[key] = s
                    catalog.record(s, offer_ref=f"francetravail_roam:{len(captured)}", source="francetravail")
                    qs = unmapped_questions(s)
                    for q in qs:
                        q["source_url"] = key
                    questions.extend(qs)
                    print(f"[captured #{len(captured)}] {s.get('ats')} | {key[:70]}")
                    print(f"    {len(ctrls)} fields, {len(qs)} question(s) for Marie/Camille:")
                    for q in qs:
                        opts = f"  options={q['options']}" if q.get("options") else ""
                        print(f"      ? [{q['field_type']}] {(q['label'] or '(unlabeled)')[:60]}{opts}")
                    print(flush=True)

                now = time.time()
                if now - last_print > 30:
                    print(f"   ...roaming ({len(captured)} forms banked, {int(end - now)}s left) — "
                          f"close the window when done.", flush=True)
                    last_print = now
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    # Deduplicate questions across forms by (label, field_type) and save for Marie + Camille.
    uniq: dict[tuple, dict] = {}
    for q in questions:
        uniq.setdefault((q["label"].lower().strip(), q["field_type"]), q)
    out = list(uniq.values())
    Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n=== harvest done: {len(captured)} forms, {len(out)} unique questions -> {OUT} ===")
    for r in catalog.field_frequency():
        print(f"  {r['label'][:40]:40s} {r['type']:9s} in {r['forms']} ({r['pct_of_forms']}%) req×{r['required_in']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
