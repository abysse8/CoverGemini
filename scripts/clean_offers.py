#!/usr/bin/env python3
"""Reversible offer-table cleanup (Scout / Sophie).

Flags -- never deletes -- the three kinds of junk the evidence phase surfaced so
downstream agents (Coach/Forms) can read a clean surface via the `offers_clean`
view:

  * noise      -- nav/label rows the scraper mistook for jobs ("Deconnexion", ...)
  * duplicate  -- the same posting stored twice (tracking params differed); the
                  canonical copy is kept, the rest point at it via canonical_ref
  * thin_body  -- real offer whose scraped body is a login/consent wall or an
                  aggregator search grid (kept visible, flagged as untrustworthy)

Reversible: every change is a value in cleanup_status/canonical_ref. `--reset`
sets them all back to 'ok'/''. A timestamped backup of the DB is written first.

Usage:
  python3 -m scripts.clean_offers [--db coverai.db] [--dry-run] [--reset] [--no-backup]
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from coverai.explorer import is_noise_title, looks_like_login_wall
from coverai.storage import CoverAiStore, normalize_offer_url

# Offers other agents already reference by id: never demote these to a duplicate.
PROTECTED_IDS = {"off_984130e4", "off_2c39ff94", "off_578ccd6b", "off_01a8c06b"}

# URL shapes that are search/listing pages, not a single posting -> body is a grid.
LISTING_URL_FRAGMENTS = ("/fr-fr/emploi/metier_", "/candidat/recherche-emploi")


def is_listing_url(url: str) -> bool:
    low = (url or "").lower()
    return any(fragment in low for fragment in LISTING_URL_FRAGMENTS)


def classify(rows: list[sqlite3.Row]) -> dict[str, tuple[str, str]]:
    """Return {offer_id: (cleanup_status, canonical_ref)} for every row."""
    verdict: dict[str, tuple[str, str]] = {}

    # 1. Noise first -- it never competes for canonical. A nav/label title, or a
    #    company that names several employers (an aggregator listing, not one offer).
    non_noise: list[sqlite3.Row] = []
    for r in rows:
        company = (r["company"] or "").lower()
        if is_noise_title(r["title"]) or "multiple opportunities" in company:
            verdict[r["id"]] = ("noise", "")
        else:
            non_noise.append(r)

    # 2. Duplicates: group by canonical URL; keep one, flag the rest.
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in non_noise:
        groups[normalize_offer_url(r["url"])].append(r)

    for members in groups.values():
        if len(members) == 1:
            continue
        # canonical = best by (protected, score, body length); tie-break lowest id.
        best = max(
            members,
            key=lambda r: (r["id"] in PROTECTED_IDS, int(r["score"] or 0), len(r["raw_text"] or ""), _neg_id(r["id"])),
        )
        for r in members:
            if r["id"] != best["id"]:
                verdict[r["id"]] = ("duplicate", best["id"])

    # 3. Remaining unflagged rows: thin_body if login-wall / listing grid, else ok.
    for r in non_noise:
        if r["id"] in verdict:
            continue
        if looks_like_login_wall(r["raw_text"]) or is_listing_url(r["url"]):
            verdict[r["id"]] = ("thin_body", "")
        else:
            verdict[r["id"]] = ("ok", "")
    return verdict


def _neg_id(offer_id: str) -> tuple:
    # Sort key that makes the LOWEST id win a tie (stable, deterministic).
    return tuple(-ord(c) for c in offer_id)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="coverai.db")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--reset", action="store_true", help="set every row back to ok/''")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    # Instantiating the store runs the migration that adds the cleanup columns + view.
    CoverAiStore(db_path)

    if not args.dry_run and not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_suffix(f".backup-{stamp}.db")
        shutil.copy2(db_path, backup)
        print(f"backup written: {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if args.reset:
        n = conn.execute("SELECT COUNT(*) FROM offers WHERE cleanup_status != 'ok' OR canonical_ref != ''").fetchone()[0]
        if not args.dry_run:
            conn.execute("UPDATE offers SET cleanup_status = 'ok', canonical_ref = ''")
            conn.commit()
        print(f"reset {n} rows back to ok/'' ({'dry-run' if args.dry_run else 'applied'})")
        conn.close()
        return

    rows = conn.execute("SELECT id, url, title, company, score, raw_text FROM offers").fetchall()
    verdict = classify(rows)

    counts: dict[str, int] = defaultdict(int)
    for status, _ in verdict.values():
        counts[status] += 1

    print(f"\n{len(rows)} offers classified:")
    for status in ("ok", "thin_body", "duplicate", "noise"):
        print(f"  {status:<10} {counts[status]}")

    print("\nnoise (hidden from offers_clean):")
    for r in rows:
        if verdict[r["id"]][0] == "noise":
            print(f"  {r['id']}  {(r['title'] or '')[:55]!r}")
    print("\nduplicate -> canonical kept:")
    for r in rows:
        st, ref = verdict[r["id"]]
        if st == "duplicate":
            print(f"  {r['id']}  ->  {ref}   {(r['title'] or '')[:40]!r}")

    if args.dry_run:
        print("\n(dry-run: no changes written)")
        conn.close()
        return

    for offer_id, (status, ref) in verdict.items():
        conn.execute(
            "UPDATE offers SET cleanup_status = ?, canonical_ref = ? WHERE id = ?",
            (status, ref, offer_id),
        )
    conn.commit()
    clean = conn.execute("SELECT COUNT(*) FROM offers_clean").fetchone()[0]
    print(f"\napplied. offers_clean now exposes {clean} offers (of {len(rows)}).")
    conn.close()


if __name__ == "__main__":
    main()
