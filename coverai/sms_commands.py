from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .agent import handle_coverai_sms_agent
from .explorer import load_config, report_offer_by_sms
from .storage import DEFAULT_USER_ID, CoverAiStore


def handle_coverai_sms(
    store: CoverAiStore,
    sender: str,
    text: str,
    config_path: str | Path,
    sms_client: Any,
    openai_client: Any = None,
    model: str = "gpt-4o-mini",
    automation_runner: Any = None,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, Any]:
    return handle_coverai_sms_agent(
        store,
        sender,
        text,
        config_path,
        sms_client,
        openai_client=openai_client,
        model=model,
        automation_runner=automation_runner,
        user_id=user_id,
    )


def route_message(
    store: CoverAiStore,
    sender: str,
    message: str,
    config_path: str | Path,
    sms_client: Any,
    openai_client: Any = None,
    model: str = "gpt-4o-mini",
    automation_runner: Any = None,
    user_id: str = DEFAULT_USER_ID,
) -> tuple[str, str]:
    upper = message.upper()
    if upper in {"HELP", "COMMANDS"}:
        return "help", coverai_help()
    if upper == "RUN":
        if automation_runner is None:
            return "run", "CoverAI automation is not available in this process."
        result = automation_runner.run_async("sms")
        if result.get("started"):
            return "run", "CoverAI run started. I will text new matching offers as they are found."
        return "run", "CoverAI is already running. Reply STATUS for progress."
    if upper == "STATUS":
        status = automation_runner.status() if automation_runner is not None else None
        return "status", coverai_status_text(store, status, user_id=user_id)
    if upper == "MORE":
        reports = send_more_offer_reports(store, sender, config_path, sms_client, user_id=user_id)
        if reports:
            return "more", f"Sent {len(reports)} more offer SMS. Reply VIEW <id> for details or SKIP <id> to hide one."
        return "more", "No unreported high-score offers are waiting. Reply RUN to search again."
    if upper.startswith("VIEW "):
        offer_id = message.split(maxsplit=1)[1].strip()
        return "view", view_offer_text(store, offer_id, user_id=user_id)
    if upper.startswith("SKIP "):
        offer_id = message.split(maxsplit=1)[1].strip()
        try:
            offer = store.mark_offer_status(offer_id, "skipped", user_id=user_id)
        except KeyError:
            return "skip", f"I could not find offer {offer_id}."
        title = compact(str(offer.get("title") or "offer"), 80)
        return "skip", f"Skipped {offer_id}: {title}."
    return "ask", answer_coverai_question(store, message, config_path, openai_client=openai_client, model=model, user_id=user_id)


def coverai_help() -> str:
    return (
        "CoverAI SMS: RUN searches now, STATUS shows the queue, MORE sends up to 5 waiting offers, "
        "VIEW <id> shows details, SKIP <id> hides one. You can also ask a question about stored offers."
    )


def coverai_status_text(store: CoverAiStore, automation_status: dict[str, Any] | None = None, user_id: str = DEFAULT_USER_ID) -> str:
    latest = store.latest_explorer_run(user_id)
    new_count = len(store.list_offers(status="new", limit=200, user_id=user_id))
    reported_count = len(store.list_offers(status="reported", limit=200, user_id=user_id))
    selected_count = len(store.list_offers(status="selected", limit=200, user_id=user_id))
    running = bool((automation_status or {}).get("running"))
    interval = int((automation_status or {}).get("interval_seconds") or 0)
    latest_bits = "no runs yet"
    if latest:
        latest_bits = (
            f"last {latest.get('status')} new={latest.get('offers_new')} "
            f"reported={latest.get('offers_reported')} at {latest.get('completed_at') or latest.get('started_at')}"
        )
    loop = f"loop every {interval // 60} min" if interval else "loop unavailable"
    return f"CoverAI status: {'running now' if running else 'idle'}, {loop}. Offers: {new_count} new, {reported_count} reported, {selected_count} selected. {latest_bits}."


def view_offer_text(store: CoverAiStore, offer_id: str, user_id: str = DEFAULT_USER_ID) -> str:
    offer = store.get_offer(offer_id)
    if not offer or offer.get("user_id") != user_id:
        return f"I could not find offer {offer_id}."
    title = compact(str(offer.get("title") or "Untitled offer"), 110)
    company = compact(str(offer.get("company") or "Unknown company"), 60)
    location = compact(str(offer.get("location") or "Location unknown"), 60)
    score = int(offer.get("score") or 0)
    summary = compact(str(offer.get("summary") or offer.get("snippet") or ""), 260)
    url = str(offer.get("url") or "")
    return f"{offer_id}: {score}% {company} - {title}. {location}. {summary} {url}".strip()


def send_more_offer_reports(
    store: CoverAiStore,
    number: str,
    config_path: str | Path,
    sms_client: Any,
    limit: int | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> list[dict[str, Any]]:
    config = safe_load_config(config_path)
    sms_config = config.get("sms") if isinstance(config.get("sms"), dict) else {}
    effective_limit = limit if limit is not None else int(os.environ.get("COVERAI_SMS_MORE_LIMIT", "") or 5)
    effective_limit = max(1, min(effective_limit, 10))
    min_score = int(sms_config.get("min_score") or config.get("minimum_score") or config.get("min_score") or 0)
    reports: list[dict[str, Any]] = []
    for offer in store.list_offers(status="new", limit=effective_limit * 4, min_score=min_score, user_id=user_id):
        if len(reports) >= effective_limit:
            break
        report = report_offer_by_sms(store, offer["id"], number, sms_client, user_id=user_id)
        if report.get("status") == "sent":
            store.mark_offer_status(offer["id"], "reported", user_id=user_id)
            reports.append(report)
    return reports


def answer_coverai_question(
    store: CoverAiStore,
    question: str,
    config_path: str | Path,
    openai_client: Any = None,
    model: str = "gpt-4o-mini",
    user_id: str = DEFAULT_USER_ID,
) -> str:
    offers = store.list_offers(limit=8, user_id=user_id)
    if not offers:
        return "I do not have stored offers yet. Reply RUN and I will search, score, and text the best matches."
    if openai_client is None:
        return fallback_answer(question, offers)
    payload = {
        "question": question,
        "latest_run": store.latest_explorer_run(user_id),
        "target": summarize_config(safe_load_config(config_path)),
        "offers": [offer_context(offer) for offer in offers],
    }
    try:
        response = openai_client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are CoverAI over SMS for Julien's job search. Answer in <= 600 characters. "
                        "Use only the provided offers, scores, and target config. Stay in job-search scope. "
                        "Do not claim to submit applications or browse live sites from SMS."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        answer = str(response.choices[0].message.content or "").strip()
        return compact(answer, 600) or fallback_answer(question, offers)
    except Exception:
        return fallback_answer(question, offers)


def fallback_answer(question: str, offers: list[dict[str, Any]]) -> str:
    top = sorted(offers, key=lambda offer: int(offer.get("score") or 0), reverse=True)[:3]
    if any(word in question.lower() for word in ("best", "meilleur", "top", "prior")):
        offer = top[0]
        return (
            f"Best stored match: {offer.get('id')} at {int(offer.get('score') or 0)}%, "
            f"{offer.get('company') or 'Unknown'} - {compact(str(offer.get('title') or 'Untitled'), 120)}. "
            f"Reply VIEW {offer.get('id')} for details."
        )
    parts = [
        f"{offer.get('id')} {int(offer.get('score') or 0)}% {compact(str(offer.get('company') or 'Unknown'), 24)}"
        for offer in top
    ]
    return "Top stored offers: " + "; ".join(parts) + ". Reply VIEW <id> for one."


def offer_context(offer: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": offer.get("id"),
        "status": offer.get("status"),
        "score": offer.get("score"),
        "company": offer.get("company"),
        "title": offer.get("title"),
        "location": offer.get("location"),
        "source": offer.get("source"),
        "summary": offer.get("summary"),
        "snippet": compact(str(offer.get("snippet") or ""), 500),
        "url": offer.get("url"),
    }


def summarize_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "keywords": config.get("keywords", []),
        "locations": config.get("locations", []),
        "companies": config.get("companies", []),
        "minimum_score": config.get("minimum_score", config.get("min_score")),
    }


def safe_load_config(config_path: str | Path) -> dict[str, Any]:
    try:
        return load_config(config_path)
    except Exception:
        return {}


def compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."
