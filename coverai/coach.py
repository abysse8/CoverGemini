"""Camille (coverai.coach) -- interview answer coaching.

Camille takes interview questions Helene collected for an offer and drafts a
job-specific *suggested* answer for each, in Julien's voice, using AI. The user
then refines each into a final answer. This module is the pure drafting logic:
it takes an injected OpenAI-style client (so tests pass a fake and no network is
touched) and returns suggestions; persisting them is the store's job.

Design choices:
  * one call per question -- simpler and more robust than parsing a batched JSON
    blob, and cheap for the ~5-8 questions an interview prep has;
  * a deterministic template fallback when no client is given, so the pipeline
    still runs (and tests stay hermetic) without an API key.
"""

from __future__ import annotations

import json
from typing import Any

_SYSTEM = (
    "You are Camille, an interview coach for Julien, an embedded / software engineer "
    "(C/C++, Linux/RTOS, firmware, applied AI). Draft a concise, confident, first-person "
    "suggested answer to the interview question, tailored to the specific company and role. "
    "Ground it in embedded/software engineering. 2-4 sentences. Return only the answer text."
)


def _offer_context(offer: dict[str, Any]) -> dict[str, Any]:
    return {
        "company": offer.get("company"),
        "role": offer.get("title") or offer.get("role_title"),
        "summary": offer.get("summary"),
    }


def _fallback_answer(offer: dict[str, Any], question: str, category: str) -> str:
    """Template answer used when no AI client is available. Honest and generic."""
    company = offer.get("company") or "this company"
    role = offer.get("title") or "this role"
    return (
        f"[draft -- no AI client] Tie your embedded/software experience (C/C++, RTOS, "
        f"firmware) to {role} at {company}. Question was: {question} ({category})."
    )


def draft_interview_answers(
    offer: dict[str, Any],
    questions: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    openai_client: Any = None,
    model: str = "gpt-4o-mini",
) -> list[dict[str, Any]]:
    """Draft a suggested answer per question, tailored to the offer.

    Returns a list of {question_id, question, suggested_answer}. Never raises on a
    single failed call -- that one question falls back to the template so the rest
    still get coached.
    """
    context = _offer_context(offer)
    profile = profile or {}
    results: list[dict[str, Any]] = []
    for q in questions:
        question = str(q.get("question") or "")
        category = str(q.get("category") or "general")
        if openai_client is None:
            answer = _fallback_answer(offer, question, category)
        else:
            payload = {"offer": context, "candidate": {
                "name": f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
                "location": profile.get("location_city"),
            }, "question": question, "category": category}
            try:
                resp = openai_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                )
                answer = str(resp.choices[0].message.content or "").strip() or _fallback_answer(offer, question, category)
            except Exception:  # noqa: BLE001 -- one bad call must not sink the whole prep
                answer = _fallback_answer(offer, question, category)
        results.append({"question_id": q.get("id"), "question": question, "suggested_answer": answer})
    return results
