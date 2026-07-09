#!/usr/bin/env python3
"""Helene: collect interview questions for an offer and store them for Camille.

Live job-board / Glassdoor scraping is a later slice (auth + brittle per-site
selectors). For now this ingests questions from a file (one per line) or a
built-in embedded-role sample, classifies them, and stores them against the
offer. Camille then drafts answers; the user practices them through Clara.

Usage (from the CoverGemini repo root):
    python3 scripts/collect_interview_questions.py <company-or-offer-id> [questions.txt]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coverai.browser_apply import collect_interview_questions  # noqa: E402
from coverai.storage import CoverAiStore  # noqa: E402

# A default set representing what Helene would scrape for an embedded/firmware role.
DEFAULT_QUESTIONS = [
    "Tell me about a challenging embedded debugging problem you solved.",
    "Explain the difference between a mutex and a semaphore.",
    "How would you structure firmware for a low-power sensor node?",
    "How do you prevent and detect race conditions in an RTOS?",
    "Describe a time you disagreed with a teammate and how you resolved it.",
    "Why do you want to work here?",
]


def _resolve_offer(store: CoverAiStore, reference: str) -> dict | None:
    ref = reference.lower()
    for offer in store.list_offers(limit=500):
        if offer["id"] == reference or (offer.get("company") or "").lower() == ref \
                or (reference and ref in (offer.get("company") or "").lower()):
            return offer
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: python3 scripts/collect_interview_questions.py <company-or-offer-id> [questions.txt]")
        return 2
    store = CoverAiStore(str(Path(__file__).resolve().parent.parent / "coverai.db"))
    offer = _resolve_offer(store, argv[0])
    if offer is None:
        print(f"No offer matched {argv[0]!r}. Known companies: "
              f"{sorted({o.get('company') for o in store.list_offers(limit=500) if o.get('company')})}")
        return 1

    if len(argv) > 1:
        raw = [line.strip() for line in Path(argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()]
        source = f"file:{Path(argv[1]).name}"
    else:
        raw = DEFAULT_QUESTIONS
        source = "sample:embedded"

    items = collect_interview_questions(raw, source=source)
    inserted = store.add_interview_questions(offer["id"], items)
    print(f"Helene: collected {len(items)} questions for {offer.get('company')} "
          f"({offer.get('title')}); {len(inserted)} new, stored.")
    for row in store.list_interview_questions(offer["id"]):
        print(f"  [{row['category']:10}] {row['question']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
