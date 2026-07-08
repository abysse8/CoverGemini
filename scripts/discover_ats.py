#!/usr/bin/env python3
"""Map where our real offers actually apply -> which ATSes to bless first.

For a sample of offers, follow the apply affordance one hop (handling the new-tab handoff
aggregators use) and record the destination HOST. Aggregating gives the ATS distribution:
the hosts with the most offers behind them are the ones worth a one-time CAPTCHA blessing.

Headless. Does not fill anything. Usage: python3 scripts/discover_ats.py [limit]
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coverai.browser_apply import resolve_apply_target  # noqa: E402

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


def host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)/", (url or "") + "/")
    return m.group(1) if m else "?"


def normalize_url(url: str, source: str) -> str:
    """LinkedIn's guest (fr.) view hides the real apply flow; the logged-in www view with
    the numeric job id shows Easy Apply or the true external-ATS redirect."""
    if source.startswith("linkedin"):
        m = re.search(r"(\d{7,})", url)
        if m:
            return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return url


def main(argv: list[str]) -> int:
    limit = int(argv[0]) if argv else 15
    conn = sqlite3.connect("coverai.db")
    conn.row_factory = sqlite3.Row
    offers = [dict(r) for r in conn.execute(
        "SELECT id, url, source, company FROM offers WHERE url LIKE 'http%' "
        "ORDER BY score DESC LIMIT ?", (limit,)).fetchall()]

    print(f"probing apply destinations for {len(offers)} offers...\n")
    dest_hosts: Counter = Counter()
    ats_kinds: Counter = Counter()
    for i, off in enumerate(offers, 1):
        prof = profile_for(off["source"])
        url = normalize_url(off["url"], off["source"])
        try:
            res = resolve_apply_target(url, profile_dir=prof, headless=True, timeout_ms=30000)
        except Exception as e:  # noqa: BLE001
            print(f"[{i:2d}] {off['source'][:16]:16s} ERROR {str(e)[:40]}")
            continue
        chain = res.get("chain", [])
        dest = chain[-1] if chain else ""
        start_host = host_of(chain[0]) if chain else "?"
        dest_host = host_of(dest)
        moved = dest_host != start_host
        ats = res.get("ats", "?")
        if moved:                      # only count a real redirect to somewhere new
            dest_hosts[dest_host] += 1
            ats_kinds[ats] += 1
        print(f"[{i:2d}] {off['source'][:16]:16s} {start_host:22s} -> {dest_host:28s} {'(redirect)' if moved else '(same site)'}")
        time.sleep(2)

    print("\n=== APPLY-DESTINATION HOSTS (redirects only) ===")
    for host, n in dest_hosts.most_common():
        print(f"   {n:2d}  {host}")
    print("\n=== ATS kinds ===")
    for k, n in ats_kinds.most_common():
        print(f"   {n:2d}  {k}")
    print("\nBless the top hosts: one CAPTCHA solve each unlocks that ATS for headless harvest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
