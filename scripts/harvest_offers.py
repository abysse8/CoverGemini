#!/usr/bin/env python3
"""Walk real offers -> follow each to its apply form -> record ATS + fields (or 'blocked').

This is the breadth harvester. It characterizes the whole landscape from our real 73
offers: which ATSes they actually land on, which forms are reachable now, and which are
CAPTCHA-walled (recorded as blocked -- coverage data that says what to bless next).

Headless -- opens no windows. Gentle pacing to avoid tripping rate limits.

Usage (repo root):  python3 scripts/harvest_offers.py [limit] [source_filter]
    python3 scripts/harvest_offers.py 12
    python3 scripts/harvest_offers.py 8 linkedin
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coverai.browser_apply import _looks_like_application, resolve_apply_target  # noqa: E402
from coverai.form_catalog import FormCatalog  # noqa: E402

# Map an offer's source to the logged-in browser profile to use (None = fresh context).
PROFILE_BY_SOURCE = {
    "linkedin": ".coverai-browser/users/julien/linkedin",
    "apec": ".coverai-browser/users/julien/apec",
    "hellowork": ".coverai-browser/users/julien/hellowork",
}


def profile_for(source: str) -> str | None:
    for key, prof in PROFILE_BY_SOURCE.items():
        if source.startswith(key):
            return prof
    return None


def load_offers(limit: int, source_filter: str | None) -> list[dict]:
    conn = sqlite3.connect("coverai.db")
    conn.row_factory = sqlite3.Row
    q = "SELECT id, url, source, company FROM offers WHERE url LIKE 'http%'"
    args: list = []
    if source_filter:
        q += " AND source LIKE ?"
        args.append(source_filter + "%")
    q += " ORDER BY score DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def main(argv: list[str]) -> int:
    limit = int(argv[0]) if argv else 12
    source_filter = argv[1] if len(argv) > 1 else None
    catalog = FormCatalog()
    offers = load_offers(limit, source_filter)
    print(f"walking {len(offers)} offers (headless, gentle)...\n")

    reached, blocked, errored = 0, 0, 0
    for i, off in enumerate(offers, 1):
        prof = profile_for(off["source"])
        try:
            res = resolve_apply_target(off["url"], profile_dir=prof, headless=True, timeout_ms=30000)
        except Exception as e:  # noqa: BLE001
            errored += 1
            print(f"[{i:2d}] {off['id']} {off['source'][:18]:18s} ERROR {str(e)[:40]}")
            continue

        scan = res.get("scan") or {}
        ats = res.get("ats") or scan.get("ats") or "?"
        controls = scan.get("controls", [])
        n = len(controls)
        capt = scan.get("captcha_detected")
        is_form = _looks_like_application(controls)
        # QUALITY GATE: only catalog a genuine application form, or a real CAPTCHA block
        # (coverage data). A search/listing page is neither -- do NOT pollute the catalog.
        if scan and (is_form or capt):
            scan.setdefault("ats", ats)
            catalog.record(scan, offer_ref=f"offer:{off['id']}", source=off["source"])
        if capt:
            blocked += 1
            tag = "BLOCKED(captcha)"
        elif is_form:
            reached += 1
            tag = f"FORM {n} fields (recorded)"
        else:
            tag = f"not-a-form ({n} ctrls, skipped)"
        print(f"[{i:2d}] {off['id']} {off['source'][:18]:18s} -> {ats:16s} {tag}")
        time.sleep(2)  # gentle

    s = catalog.stats()
    print(f"\n=== harvest done: reached={reached} blocked={blocked} errored={errored} ===")
    print(f"=== CATALOG now: {s['scans']} forms | by ATS: {s['by_ats']} | captcha-blocked rows: {s['captcha_blocked']} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
