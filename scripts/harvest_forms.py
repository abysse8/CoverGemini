#!/usr/bin/env python3
"""Grow the form-field catalog: ingest saved scans + harvest reachable live forms.

Reachable without a human right now = LinkedIn Easy Apply (in-app, logged in) and any ATS
whose CAPTCHA you have already blessed. Fortress ATSes are recorded as captcha_blocked when
we hit them, which is itself data (tells us coverage). Headless -- opens no windows.

Usage (repo root):  python3 scripts/harvest_forms.py [easy_apply_limit]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coverai.form_catalog import FormCatalog, ingest_scan_file  # noqa: E402
from coverai.browser_apply import scan_current  # noqa: E402

LINKEDIN_PROFILE = ".coverai-browser/users/julien/linkedin"
SEARCHES = [
    ("développeur", "France"),
    ("ingénieur systèmes embarqués", "Île-de-France"),
    ("data engineer", "France"),
]


def harvest_easy_apply(catalog: FormCatalog, limit: int) -> int:
    from playwright.sync_api import sync_playwright
    added = 0
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(LINKEDIN_PROFILE, headless=True)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        seen: set[str] = set()
        for kw, loc in SEARCHES:
            if added >= limit:
                break
            url = (f"https://www.linkedin.com/jobs/search/?f_AL=true"
                   f"&keywords={kw.replace(' ', '%20')}&location={loc.replace(' ', '%20')}")
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(4000)
            ids = pg.eval_on_selector_all(
                "a[href*='/jobs/view/']",
                "els=>[...new Set(els.map(e=>e.href.match(/jobs\\/view\\/(\\d+)/)?.[1]).filter(Boolean))]")
            for jid in ids:
                if added >= limit or jid in seen:
                    continue
                seen.add(jid)
                try:
                    pg.goto(f"https://www.linkedin.com/jobs/view/{jid}/",
                            wait_until="domcontentloaded", timeout=45000)
                    pg.wait_for_timeout(3500)
                    btn = pg.locator("button:has-text('Candidature simplifiée'), "
                                     "a:has-text('Candidature simplifiée'), "
                                     "button:has-text('Easy Apply')").filter(visible=True)
                    if not btn.count():
                        continue
                    btn.first.click(timeout=6000)
                    pg.wait_for_timeout(4000)
                    scan = scan_current(pg)
                    if len(scan.get("controls", [])) >= 3:
                        catalog.record(scan, offer_ref=f"linkedin:{jid}", source="linkedin_easyapply")
                        added += 1
                        print(f"   + easy-apply {jid}: {len(scan['controls'])} fields")
                    # close the modal before the next job
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(800)
                except Exception as e:  # noqa: BLE001
                    print(f"   ! {jid}: {str(e)[:60]}")
        ctx.close()
    return added


def main(argv: list[str]) -> int:
    limit = int(argv[0]) if argv else 4
    catalog = FormCatalog()

    # 1) ingest the scans we already saved
    for f, ref in [("scripts/smartrecruiters_netatmo_form.json", "offer:off_a6b310e9"),
                   ("scripts/linkedin_easyapply_form.json", "linkedin:4431061971")]:
        if Path(f).exists():
            ingest_scan_file(f, catalog, offer_ref=ref)
            print(f"ingested {f}")

    # 2) harvest a few live Easy Apply forms
    print(f"\nharvesting up to {limit} Easy Apply forms (headless)...")
    added = harvest_easy_apply(catalog, limit)
    print(f"harvested {added} new forms")

    # 3) report the evidence
    s = catalog.stats()
    print(f"\n=== CATALOG: {s['scans']} forms | by ATS: {s['by_ats']} | captcha-blocked: {s['captcha_blocked']} ===")
    print(f"{'field label':38s} {'type':9s} {'forms':6s} {'%':4s} {'req':4s}")
    for r in catalog.field_frequency():
        print(f"{r['label'][:38]:38s} {r['type']:9s} {r['forms']:<6d} {r['pct_of_forms']:<4d} {r['required_in']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
