from __future__ import annotations

import json
import os
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .explorer import load_config, report_offer_by_sms
from .storage import DEFAULT_USER_ID, CoverAiStore


class CoverAiAgentTools:
    def __init__(
        self,
        store: CoverAiStore,
        sender: str,
        config_path: str | Path,
        sms_client: Any,
        automation_runner: Any = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> None:
        self.store = store
        self.sender = sender
        self.config_path = Path(config_path)
        self.sms_client = sms_client
        self.automation_runner = automation_runner
        self.user_id = user_id

    def list_recent_offers(self, status: str = "", limit: int = 5) -> dict[str, Any]:
        offers = self.store.list_offers(status=status, limit=limit, user_id=self.user_id)
        return {"offers": [self.offer_summary(offer) for offer in offers]}

    def resolve_offer(self, reference: str = "") -> dict[str, Any]:
        offer = self.store.find_offer_by_reference(reference, user_id=self.user_id, phone=self.sender)
        return {"offer": self.offer_summary(offer) if offer else None}

    def get_offer_context(self, reference: str = "") -> dict[str, Any]:
        offer = self.store.find_offer_by_reference(reference, user_id=self.user_id, phone=self.sender)
        if not offer:
            return {"error": "offer_not_found"}
        app = self.store.get_application_for_offer(offer["id"], user_id=self.user_id)
        return {
            "offer": self.offer_summary(offer, include_text=True),
            "application": self.application_summary(app) if app else None,
            "questions": self.store.list_application_questions(app["id"], user_id=self.user_id) if app else [],
        }

    def scout_now(self) -> dict[str, Any]:
        if self.automation_runner is None:
            return {"started": False, "reason": "automation_unavailable"}
        return self.automation_runner.run_async("agent")

    def send_more_offer_reports(self, limit: int = 5) -> dict[str, Any]:
        config = self.safe_load_config()
        sms_config = config.get("sms") if isinstance(config.get("sms"), dict) else {}
        min_score = int(sms_config.get("min_score") or config.get("minimum_score") or config.get("min_score") or 0)
        sent: list[dict[str, Any]] = []
        for offer in self.store.list_offers(status="new", limit=max(1, min(limit, 10)) * 4, min_score=min_score, user_id=self.user_id):
            if len(sent) >= max(1, min(limit, 10)):
                break
            report = report_offer_by_sms(self.store, offer["id"], self.sender, self.sms_client, user_id=self.user_id)
            if report.get("status") == "sent":
                self.store.mark_offer_status(offer["id"], "reported", user_id=self.user_id)
                sent.append(self.offer_summary(offer))
        return {"sent": sent, "count": len(sent)}

    def create_application_task(self, reference: str = "") -> dict[str, Any]:
        offer = self.store.find_offer_by_reference(reference, user_id=self.user_id, phone=self.sender)
        if not offer:
            return {"error": "offer_not_found"}
        app, created = self.store.upsert_application_task(offer["id"], user_id=self.user_id)
        questions = self.store.list_application_questions(app["id"], user_id=self.user_id)
        return {
            "created": created,
            "offer": self.offer_summary(offer),
            "application": self.application_summary(app),
            "questions": [self.question_summary(question) for question in questions],
        }

    def get_application_readiness(self, reference: str = "") -> dict[str, Any]:
        app = self.resolve_application(reference)
        if not app:
            offer = self.store.find_offer_by_reference(reference, user_id=self.user_id, phone=self.sender) if reference else None
            return {"error": "application_not_found", "offer": self.offer_summary(offer) if offer else None}
        app = self.store.recalculate_application_readiness(app["id"], user_id=self.user_id)
        questions = self.store.list_application_questions(app["id"], user_id=self.user_id)
        offer = self.store.get_offer(app["offer_id"])
        return {
            "offer": self.offer_summary(offer) if offer else None,
            "application": self.application_summary(app),
            "questions": [self.question_summary(question) for question in questions],
            "next_question": next((self.question_summary(q) for q in questions if q.get("status") == "needs_user"), None),
        }

    def get_submission_packet(self, reference: str = "") -> dict[str, Any]:
        app = self.resolve_application(reference)
        if not app:
            offer = self.store.find_offer_by_reference(reference, user_id=self.user_id, phone=self.sender) if reference else None
            return {"error": "application_not_found", "offer": self.offer_summary(offer) if offer else None}
        packet = self.store.application_submission_packet(app["id"], user_id=self.user_id)
        return {
            "packet": packet,
            "summary": self.submission_packet_summary(packet),
        }

    def answer_next_application_question(self, answer: str, reference: str = "") -> dict[str, Any]:
        app = self.resolve_application(reference)
        if not app:
            return {"error": "application_not_found"}
        question = self.store.answer_next_application_question(app["id"], answer, user_id=self.user_id)
        app = self.store.recalculate_application_readiness(app["id"], user_id=self.user_id)
        return {
            "answered": self.question_summary(question) if question else None,
            "application": self.application_summary(app),
            "next_question": next(
                (self.question_summary(q) for q in self.store.list_application_questions(app["id"], user_id=self.user_id) if q.get("status") == "needs_user"),
                None,
            ),
        }

    def coach_offer(self, reference: str = "", focus: str = "") -> dict[str, Any]:
        offer = self.store.find_offer_by_reference(reference, user_id=self.user_id, phone=self.sender)
        if not offer:
            return {"error": "offer_not_found"}
        title = str(offer.get("title") or "this role")
        company = str(offer.get("company") or "the company")
        summary = str(offer.get("summary") or offer.get("snippet") or "")
        raw = str(offer.get("raw_text") or "")
        signals = []
        for word in ("Zephyr", "Linux", "C", "C++", "RTOS", "firmware", "embedded", "IoT", "FPGA", "microcontroller"):
            if word.lower() in f"{summary} {raw}".lower():
                signals.append(word)
        angle = (
            f"For {company}, sell yourself as a practical embedded/applied-AI profile. "
            f"Anchor on {', '.join(signals[:5]) or 'embedded systems, C/C++, and engineering curiosity'}. "
            "Ask me to start an application task when you want me to track missing answers."
        )
        return {"offer": self.offer_summary(offer), "focus": focus, "coaching": angle}

    def queue_company_research(self, reference: str = "") -> dict[str, Any]:
        offer = self.store.find_offer_by_reference(reference, user_id=self.user_id, phone=self.sender)
        if not offer:
            return {"error": "offer_not_found"}
        queue = self.store.create_queue_item(
            "company.research",
            "offer",
            offer["id"],
            {"company": offer.get("company"), "url": offer.get("url"), "needs": ["culture", "news", "market_position"]},
            user_id=self.user_id,
        )
        return {"queued": queue, "offer": self.offer_summary(offer)}

    def market_status(self) -> dict[str, Any]:
        apps = self.store.list_application_tasks(limit=5, user_id=self.user_id)
        return {
            "latest_run": self.store.latest_explorer_run(self.user_id),
            "new_offers": len(self.store.list_offers(status="new", limit=200, user_id=self.user_id)),
            "reported_offers": len(self.store.list_offers(status="reported", limit=200, user_id=self.user_id)),
            "applications": [self.application_summary(app) for app in apps],
            "automation": self.automation_runner.status() if self.automation_runner is not None else None,
        }

    def resolve_application(self, reference: str = "") -> dict[str, Any] | None:
        ref = str(reference or "").strip()
        if ref.startswith("app_"):
            return self.store.get_application_task(ref, user_id=self.user_id)
        if ref:
            offer = self.store.find_offer_by_reference(ref, user_id=self.user_id, phone=self.sender)
            if not offer:
                return None
            return self.store.get_application_for_offer(offer["id"], user_id=self.user_id)
        apps = self.store.list_application_tasks(limit=1, user_id=self.user_id)
        return apps[0] if apps else None

    def safe_load_config(self) -> dict[str, Any]:
        try:
            return load_config(self.config_path)
        except Exception:
            return {}

    @staticmethod
    def offer_summary(offer: dict[str, Any] | None, include_text: bool = False) -> dict[str, Any] | None:
        if not offer:
            return None
        result = {
            "id": offer.get("id"),
            "company": offer.get("company"),
            "title": offer.get("title"),
            "location": offer.get("location"),
            "score": offer.get("score"),
            "status": offer.get("status"),
            "summary": offer.get("summary"),
            "url": offer.get("url"),
        }
        if include_text:
            result["snippet"] = compact(str(offer.get("snippet") or ""), 800)
            result["raw_text_excerpt"] = compact(str(offer.get("raw_text") or ""), 1600)
        return result

    @staticmethod
    def application_summary(app: dict[str, Any] | None) -> dict[str, Any] | None:
        if not app:
            return None
        return {
            "id": app.get("id"),
            "offer_id": app.get("offer_id"),
            "company": app.get("company"),
            "role_title": app.get("role_title"),
            "status": app.get("status"),
            "readiness_percent": app.get("readiness_percent"),
            "questions_total": app.get("questions_total"),
            "questions_answered": app.get("questions_answered"),
            "questions_needs_user": app.get("questions_needs_user"),
            "questions_low_confidence": app.get("questions_low_confidence"),
            "strategy_text": app.get("strategy_text"),
            "last_action": app.get("last_action"),
        }

    @staticmethod
    def question_summary(question: dict[str, Any] | None) -> dict[str, Any] | None:
        if not question:
            return None
        return {
            "id": question.get("id"),
            "label": question.get("label"),
            "required": bool(question.get("required")),
            "status": question.get("status"),
            "answer_source": question.get("answer_source"),
            "confidence": question.get("confidence"),
            "has_answer": bool(str(question.get("answer") or "").strip()),
        }

    @staticmethod
    def submission_packet_summary(packet: dict[str, Any]) -> dict[str, Any]:
        application = packet.get("application") or {}
        return {
            "application_id": application.get("id"),
            "company": application.get("company"),
            "role_title": application.get("role_title"),
            "ready_for_form_fill": packet.get("ready_for_form_fill"),
            "readiness": packet.get("readiness"),
            "ready_fields": [
                {
                    "field_key": answer.get("field_key"),
                    "label": answer.get("label"),
                    "field_type": answer.get("field_type"),
                    "answer": compact(str(answer.get("answer") or ""), 220),
                    "confidence": answer.get("confidence"),
                    "source": answer.get("answer_source"),
                }
                for answer in packet.get("ready_answers", [])
            ],
            "missing_required": [
                {
                    "field_key": answer.get("field_key"),
                    "label": answer.get("label"),
                    "status": answer.get("status"),
                    "confidence": answer.get("confidence"),
                }
                for answer in packet.get("missing_required", [])
            ],
        }


def handle_coverai_sms_agent(
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
    message = " ".join(str(text or "").strip().split())
    tools = CoverAiAgentTools(store, sender, config_path, sms_client, automation_runner=automation_runner, user_id=user_id)
    if not message:
        reply = agent_help()
        intent = "agent.help"
    else:
        reply, intent = answer_with_ai_or_fallback(message, tools, openai_client=openai_client, model=model)
    store.record_sms_message("inbound", sender, message, reply, intent, user_id=user_id)
    return {"ok": True, "reply": reply, "command": intent, "mode": "agent"}


def answer_with_ai_or_fallback(message: str, tools: CoverAiAgentTools, openai_client: Any = None, model: str = "gpt-4o-mini") -> tuple[str, str]:
    pending_answer = maybe_record_pending_answer(message, tools)
    if pending_answer is not None:
        return pending_answer
    hardcoded = hardcoded_agent_command(message, tools)
    if hardcoded is not None:
        return hardcoded
    if has_system_pipeline_intent(message):
        return compact(answer_system_pipeline_question(message, openai_client, model), 600), "agent.system_review"
    if openai_client is not None:
        try:
            answer = answer_with_openai(message, tools, openai_client, model)
            if answer:
                return compact(answer, 600), "agent.ai"
        except Exception:
            pass
    return fallback_agent_answer(message, tools)


def hardcoded_agent_command(message: str, tools: CoverAiAgentTools) -> tuple[str, str] | None:
    normalized = " ".join(str(message or "").strip().lower().replace("?", "").split())
    if normalized in {"help", "commands", "menu", "start"}:
        return agent_help(), "agent.help"
    if normalized in {"capabilities", "capability", "capabilties", "capablities", "caps", "skills", "what can you do", "what do you do", "agent"}:
        return capabilities_reply(), "agent.capabilities"
    if normalized in {"status", "state", "summary", "what are we doing", "whats going on", "what is going on"}:
        return status_reply(tools), "agent.status"
    if normalized in {"offers", "offer status", "jobs", "job status", "opportunities", "opportunity status"}:
        return offers_reply(tools), "agent.offers"
    if normalized in {"queue", "applications", "application status", "apps", "readiness"}:
        return queue_reply(tools), "agent.queue"
    if has_submission_intent(normalized):
        result = tools.get_submission_packet(message)
        return submission_packet_reply(result), "agent.submission_packet"
    if has_review_intent(normalized):
        result = tools.get_submission_packet(review_reference(message, tools))
        return submission_packet_reply(result), "agent.submission_packet"
    if has_readiness_intent(normalized):
        result = tools.get_application_readiness(message)
        if result.get("error"):
            offer = result.get("offer") or {}
            if offer:
                company = offer.get("company") or "that offer"
                return f"I found {company}, but there is no application task for it yet. Say 'start applying to {company}' to create one.", "agent.readiness"
            return "No application task yet. Tell me which opportunity to apply to and I will create one.", "agent.readiness"
        return readiness_reply(result), "agent.readiness"
    return None


def has_submission_intent(normalized: str) -> bool:
    return any(word in normalized for word in ("submit", "submission", "inject", "playwright", "autofill", "form fill", "fill form", "fields"))


def has_review_intent(normalized: str) -> bool:
    return any(
        phrase in normalized
        for phrase in (
            "show draft",
            "show me the draft",
            "show me what",
            "see what",
            "written so far",
            "written up",
            "application so far",
            "draft so far",
            "what do we have",
            "what have we got",
            "current application",
        )
    )


def has_readiness_intent(normalized: str) -> bool:
    return any(word in normalized for word in ("ready", "readiness", "missing", "progress", "percent", "percentage"))


def has_system_pipeline_intent(message: str) -> bool:
    normalized = " ".join(str(message or "").strip().lower().replace("?", "").split())
    product_refs = ("coverai", "sms bridge", "rut241", "mcp", "agent pipeline", "sms flow")
    review_refs = ("pipeline", "architecture", "implementation", "code", "system", "design", "fix", "modification", "improve")
    return any(ref in normalized for ref in product_refs) and any(ref in normalized for ref in review_refs)


def system_pipeline_context() -> dict[str, Any]:
    return {
        "purpose": "CoverAI is a human-in-the-loop job-market agent controlled by SMS.",
        "stages": [
            "RUT241 SMS webhook/workbench receives and sends texts",
            "CoverAI SMS agent resolves natural-language intent",
            "SQLite stores offers, application tasks, questions, answers, SMS history, and events",
            "deterministic reads handle factual readiness/submission-packet questions",
            "AI handles coaching, drafting, research direction, and normal conversation",
            "future Playwright worker should consume submission packets after human review",
        ],
        "current_limits": [
            "browser form fill is not live yet",
            "final submission is intentionally blocked until human approval",
            "company research queues are shallow and need a real research worker",
        ],
        "recommended_shape": [
            "keep DB facts and state changes behind explicit tools",
            "route product/engineering chat away from application readiness context",
            "use short SMS acknowledgements and chunked replies for longer answers",
            "model the apply flow as a queue with approval gates before browser automation",
        ],
    }


def answer_system_pipeline_question(message: str, openai_client: Any = None, model: str = "gpt-4o-mini") -> str:
    if openai_client is not None:
        try:
            response = openai_client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are CoverAI discussing your own SMS/product/engineering pipeline with Julien. "
                            "Speak normally and practically, under 600 characters. "
                            "Do not discuss Agixis, KELENN, application readiness, or missing application answers unless explicitly asked. "
                            "Use the supplied pipeline context to suggest concrete implementation or UX fixes."
                        ),
                    },
                    {"role": "user", "content": json.dumps({"pipeline": system_pipeline_context(), "question": message}, ensure_ascii=False)},
                ],
            )
            answer = str(response.choices[0].message.content or "").strip()
            if answer and not looks_like_application_readiness_reply(answer):
                return answer
        except Exception:
            pass
    return (
        "CoverAI pipeline review: keep deterministic tools for DB truth and state changes, but keep normal AI chat for coaching, "
        "architecture, UX, and strategy. Strong fixes: split meta/system questions away from application readiness context, log each SMS "
        "routing decision, keep ack+chunked replies, and make the submit worker consume reviewed submission packets from a queue."
    )


def looks_like_application_readiness_reply(answer: str) -> bool:
    lowered = str(answer or "").lower()
    return "agixis" in lowered or "kelenn" in lowered or ("80%" in lowered and "ready" in lowered)


def answer_with_openai(message: str, tools: CoverAiAgentTools, openai_client: Any, model: str) -> str:
    recent_messages = tools.store.recent_sms_messages(user_id=tools.user_id, phone=tools.sender, limit=6)
    context = {
        "recent_offers": tools.list_recent_offers(limit=5),
        "recent_reported_offers": [tools.offer_summary(offer) for offer in tools.store.recent_reported_offers(tools.sender, limit=5, user_id=tools.user_id)],
        "applications": tools.market_status()["applications"],
        "known_system_pipeline": system_pipeline_context(),
        "recent_sms": [
            {"text": item.get("text"), "reply": item.get("response_text"), "intent": item.get("command")}
            for item in reversed(recent_messages)
        ],
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are CoverAI, Julien's SMS job-market agent. Use tools to scout, coach, operate, and communicate. "
                "You can also speak normally; tools are optional and should only be used when they improve accuracy or change state. "
                "The user should not need internal ids. Resolve natural references like 'the Netatmo one' or 'that one'. "
                "Keep SMS replies under 600 characters. Give practical coaching. If applying, create an application task and report readiness. "
                "When asked what is ready for submission, form fill, Playwright, injection, or answers in memory, call get_submission_packet. "
                "When asked to show the draft, current application, or what is written so far, call get_submission_packet. "
                "When asked to review the CoverAI pipeline, architecture, UX, SMS flow, or implementation approach, answer as a product/engineering reviewer using known_system_pipeline; do not reinterpret that as job-application readiness. "
                "Never claim an application was submitted. Submissions require explicit approval in a future queue stage."
            ),
        },
        {"role": "user", "content": json.dumps({"context": context, "message": message}, ensure_ascii=False)},
    ]
    first = openai_client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tool_specs(),
        tool_choice="auto",
    )
    assistant_message = first.choices[0].message
    tool_calls = getattr(assistant_message, "tool_calls", None) or []
    if not tool_calls:
        return str(getattr(assistant_message, "content", "") or "").strip()
    messages.append(assistant_message.model_dump() if hasattr(assistant_message, "model_dump") else {"role": "assistant", "content": getattr(assistant_message, "content", ""), "tool_calls": tool_calls})
    for call in tool_calls:
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        result = execute_tool(tools, name, args)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)})
    second = openai_client.chat.completions.create(model=model, messages=messages)
    return str(second.choices[0].message.content or "").strip()


def execute_tool(tools: CoverAiAgentTools, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "list_recent_offers":
        return tools.list_recent_offers(str(args.get("status") or ""), int(args.get("limit") or 5))
    if name == "resolve_offer":
        return tools.resolve_offer(str(args.get("reference") or ""))
    if name == "get_offer_context":
        return tools.get_offer_context(str(args.get("reference") or ""))
    if name == "scout_now":
        return tools.scout_now()
    if name == "send_more_offer_reports":
        return tools.send_more_offer_reports(int(args.get("limit") or 5))
    if name == "create_application_task":
        return tools.create_application_task(str(args.get("reference") or ""))
    if name == "get_application_readiness":
        return tools.get_application_readiness(str(args.get("reference") or ""))
    if name == "get_submission_packet":
        return tools.get_submission_packet(str(args.get("reference") or ""))
    if name == "answer_next_application_question":
        return tools.answer_next_application_question(str(args.get("answer") or ""), str(args.get("reference") or ""))
    if name == "coach_offer":
        return tools.coach_offer(str(args.get("reference") or ""), str(args.get("focus") or ""))
    if name == "queue_company_research":
        return tools.queue_company_research(str(args.get("reference") or ""))
    if name == "market_status":
        return tools.market_status()
    return {"error": f"unknown_tool:{name}"}


def tool_specs() -> list[dict[str, Any]]:
    string_ref = {"type": "string", "description": "Natural reference such as company name, 'the last one', 'that one', or an internal id if known."}
    return [
        {"type": "function", "function": {"name": "list_recent_offers", "description": "List recent stored offers.", "parameters": {"type": "object", "properties": {"status": {"type": "string"}, "limit": {"type": "integer"}}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "resolve_offer", "description": "Resolve a natural offer reference.", "parameters": {"type": "object", "properties": {"reference": string_ref}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_offer_context", "description": "Get offer details and application state.", "parameters": {"type": "object", "properties": {"reference": string_ref}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "scout_now", "description": "Start a background scouting run.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "send_more_offer_reports", "description": "Send more offer SMS cards.", "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "create_application_task", "description": "Create or get an application task for an offer.", "parameters": {"type": "object", "properties": {"reference": string_ref}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_application_readiness", "description": "Get readiness and missing questions for an application.", "parameters": {"type": "object", "properties": {"reference": string_ref}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_submission_packet", "description": "Get database answers that are ready to inject into a browser application form, plus missing required answers.", "parameters": {"type": "object", "properties": {"reference": string_ref}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "answer_next_application_question", "description": "Use the user's SMS as the answer to the next missing application question.", "parameters": {"type": "object", "properties": {"reference": string_ref, "answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "coach_offer", "description": "Coach the user on fit, positioning, and application strategy.", "parameters": {"type": "object", "properties": {"reference": string_ref, "focus": {"type": "string"}}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "queue_company_research", "description": "Queue deeper company culture/news research.", "parameters": {"type": "object", "properties": {"reference": string_ref}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "market_status", "description": "Get overall job-market agent status.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    ]


def fallback_agent_answer(message: str, tools: CoverAiAgentTools) -> tuple[str, str]:
    lowered = message.lower()
    if lowered in {"help", "commands"}:
        return agent_help(), "agent.help"
    latest_app = tools.resolve_application("")
    if latest_app and is_likely_user_answer(lowered):
        result = tools.answer_next_application_question(message)
        return readiness_reply(result), "agent.answer_question"
    if any(word in lowered for word in ("scout", "search", "find", "run")):
        result = tools.scout_now()
        started = bool(result.get("started"))
        return ("I started scouting. I will text strong new matches." if started else "Scouting is already running or unavailable."), "agent.scout"
    if any(word in lowered for word in ("more", "opportunit", "offer", "jobs")) and not any(word in lowered for word in ("apply", "ready", "missing")):
        result = tools.send_more_offer_reports()
        count = int(result.get("count") or 0)
        if count:
            return f"I sent {count} opportunities. You can reply naturally, e.g. 'tell me about the Netatmo one' or 'start applying to that one'.", "agent.more"
        offers = tools.list_recent_offers(limit=3)["offers"]
        return "Top stored opportunities: " + "; ".join(f"{o['company']} ({o['score']}%)" for o in offers), "agent.more"
    if has_submission_intent(lowered):
        result = tools.get_submission_packet(message)
        return submission_packet_reply(result), "agent.submission_packet"
    if has_review_intent(lowered):
        result = tools.get_submission_packet(review_reference(message, tools))
        return submission_packet_reply(result), "agent.submission_packet"
    if any(word in lowered for word in ("apply", "application", "candidature")):
        result = tools.create_application_task(message)
        if result.get("error"):
            return "I could not identify which opportunity you mean. Name the company or say 'the last one'.", "agent.apply"
        return application_created_reply(result), "agent.apply"
    if lowered.startswith("skip") or "not interested" in lowered:
        offer = tools.store.find_offer_by_reference(message, user_id=tools.user_id, phone=tools.sender)
        if not offer:
            return "I could not identify which opportunity to skip.", "agent.skip"
        tools.store.mark_offer_status(offer["id"], "skipped", user_id=tools.user_id)
        return f"Skipped {offer.get('company') or 'that company'} - {compact(str(offer.get('title') or 'opportunity'), 120)}.", "agent.skip"
    if any(word in lowered for word in ("queue",)) or has_readiness_intent(lowered):
        result = tools.get_application_readiness(message)
        if result.get("error"):
            offer = result.get("offer") or {}
            if offer:
                company = offer.get("company") or "that offer"
                return f"I found {company}, but there is no application task for it yet. Say 'start applying to {company}' to create one.", "agent.readiness"
            return "No application task yet. Tell me which opportunity to apply to and I will create one.", "agent.readiness"
        return readiness_reply(result), "agent.readiness"
    if any(word in lowered for word in ("culture", "news", "company", "market")):
        queued = tools.queue_company_research(message)
        coached = tools.coach_offer(message, "culture/news")
        if queued.get("error") or coached.get("error"):
            return "Which company or opportunity do you mean?", "agent.company"
        return compact(f"I queued company research. For now: {coached['coaching']}", 600), "agent.company"
    if any(word in lowered for word in ("view", "tell", "about", "why", "worth", "coach", "prep", "prepare")):
        coached = tools.coach_offer(message)
        if coached.get("error"):
            return "Which opportunity do you want to discuss? You can say the company name or 'the last one'.", "agent.coach"
        return compact(coached["coaching"], 600), "agent.coach"
    status = tools.market_status()
    return (
        f"I am tracking {status['new_offers']} new offers and {status['reported_offers']} reported offers. "
        "Ask me to scout, explain a company, or start applying to a specific opportunity."
    ), "agent.status"


def is_likely_user_answer(lowered: str) -> bool:
    if lowered.endswith("?"):
        return False
    return len(lowered) <= 160 and not any(word in lowered for word in ("scout", "apply", "offer", "ready", "status", "news", "culture", "why"))


def maybe_record_pending_answer(message: str, tools: CoverAiAgentTools) -> tuple[str, str] | None:
    app = tools.resolve_application("")
    if not app:
        return None
    questions = tools.store.list_application_questions(app["id"], user_id=tools.user_id)
    next_question = next((question for question in questions if question.get("status") == "needs_user"), None)
    if not next_question or not message_answers_question(message, next_question):
        return None
    answer_text = extract_answer_clause(message)
    result = tools.answer_next_application_question(answer_text, str(app["id"]))
    if asks_to_review_application(message):
        packet = tools.get_submission_packet(str(app["id"]))
        reply = f"Saved {next_question['label']}. " + submission_packet_reply(packet)
        return compact(reply, 600), "agent.answer_and_review"
    return readiness_reply(result), "agent.answer_question"


def review_reference(message: str, tools: CoverAiAgentTools) -> str:
    lowered = message.lower()
    if "app_" in lowered or "off_" in lowered:
        return message
    return message if message_has_company_reference(message, tools) else ""


def message_has_company_reference(message: str, tools: CoverAiAgentTools) -> bool:
    message_words = tools.store.search_words(message, min_length=4)
    if not message_words:
        return False
    candidates = tools.store.recent_reported_offers(tools.sender, limit=20, user_id=tools.user_id) + tools.store.list_offers(limit=100, user_id=tools.user_id)
    for offer in candidates:
        company = str(offer.get("company") or "").strip()
        if not company or company.lower() in {"n/a", "na", "unknown"}:
            continue
        company_words = tools.store.search_words(company, min_length=4)
        if any(word in message_words for word in company_words):
            return True
        if any(SequenceMatcher(None, word, company_word).ratio() >= 0.82 for word in message_words for company_word in company_words):
            return True
    return False


def message_answers_question(message: str, question: dict[str, Any]) -> bool:
    lowered = message.lower()
    label = str(question.get("label") or "").lower()
    if "work authorization" in label or "location constraints" in label:
        return any(
            phrase in lowered
            for phrase in (
                "nationality",
                "citizen",
                "allowed to work",
                "authorised to work",
                "authorized to work",
                "work permit",
                "visa",
                "french",
                "france",
            )
        )
    if "start date" in label or "availability" in label:
        months = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
        return "available" in lowered or any(month in lowered for month in months) or any(char.isdigit() for char in lowered)
    if "project" in label or "embedded" in label:
        return len(lowered) > 40 and any(
            word in lowered
            for word in ("project", "embedded", "firmware", "linux", "rtos", "iot", "stm32", "esp32", "arduino", "c++", " c ", "microcontroller")
        )
    return is_likely_user_answer(lowered)


def extract_answer_clause(message: str) -> str:
    lowered = message.lower()
    for marker in (". can i ", ". could i ", ". show me ", ". what ", "? can i ", "? could i ", "? show me ", "? what "):
        index = lowered.find(marker)
        if index > 0:
            return message[: index + 1].strip()
    return message.strip()


def asks_to_review_application(message: str) -> bool:
    lowered = message.lower()
    return any(
        phrase in lowered
        for phrase in (
            "see what",
            "show me",
            "written",
            "so far",
            "ready for submission",
            "ready to submit",
            "ready to inject",
            "application so far",
        )
    )


def application_created_reply(result: dict[str, Any]) -> str:
    app = result["application"]
    next_question = next((q for q in result["questions"] if q["status"] == "needs_user"), None)
    reply = (
        f"Application task ready for {app['company']}: {app['questions_answered']}/{app['questions_total']} required answers handled "
        f"({app['readiness_percent']}%)."
    )
    if next_question:
        reply += f" First missing answer: {next_question['label']}."
    return compact(reply, 600)


def readiness_reply(result: dict[str, Any]) -> str:
    app = result.get("application") or {}
    next_question = result.get("next_question")
    answered = app.get("questions_answered", 0)
    total = app.get("questions_total", 0)
    reply = (
        f"{app.get('company', 'Application')} readiness: {answered}/{total} required answers handled "
        f"({app.get('readiness_percent', 0)}%). {app.get('questions_needs_user', 0)} need you."
    )
    if result.get("answered"):
        reply = f"Saved that answer. " + reply
    if next_question:
        reply += f" Next: {next_question['label']}?"
    else:
        reply += " No missing required answers; next stage is form fill/review."
    return compact(reply, 600)


def submission_packet_reply(result: dict[str, Any]) -> str:
    if result.get("error"):
        offer = result.get("offer") or {}
        if offer:
            company = offer.get("company") or "that offer"
            title = compact(str(offer.get("title") or "opportunity"), 80)
            return f"I found {company} - {title}, but there is no application task for it yet. Say 'start applying to {company}' and I will create the answer queue."
        return "No application task yet. Tell me which opportunity to apply to and I will create one."
    packet = result.get("packet") or {}
    summary = result.get("summary") or {}
    readiness = summary.get("readiness") or {}
    company = summary.get("company") or "Application"
    ready_fields = summary.get("ready_fields") or []
    missing_required = summary.get("missing_required") or []
    ready_text = "; ".join(ready_field_sms_text(field) for field in ready_fields[:4])
    missing_text = "; ".join(short_field_label(field) for field in missing_required[:4])
    reply = (
        f"{company}: {readiness.get('required_ready', 0)}/{readiness.get('required_total', 0)} required fields ready "
        f"({readiness.get('percent', 0)}%)."
    )
    if ready_text:
        reply += f" Ready to inject: {ready_text}."
    else:
        reply += " No stored answers are ready to inject yet."
    if missing_text:
        reply += f" Missing: {missing_text}."
    elif packet.get("ready_for_form_fill"):
        reply += " No missing required fields; next is browser fill and human review."
    return compact(reply, 600)


def ready_field_sms_text(field: dict[str, Any]) -> str:
    label = short_field_label(field)
    answer = " ".join(str(field.get("answer") or "").split()).rstrip(".")
    source = str(field.get("source") or "")
    field_type = str(field.get("field_type") or "")
    if source == "user_sms" and answer and len(answer) <= 90:
        return f"{label}: {answer}"
    if field_type in {"text", "checkbox"} and answer and len(answer) <= 90:
        return f"{label}: {answer}"
    return f"{label}: ready"


def short_field_label(field: dict[str, Any]) -> str:
    label = str(field.get("label") or field.get("field_key") or "field")
    lowered = label.lower()
    if "cv" in lowered:
        return "CV"
    if "motivation" in lowered or "cover/application" in lowered:
        return "Motivation"
    if "start date" in lowered or "availability" in lowered:
        return "Start"
    if "work authorization" in lowered or "location constraints" in lowered:
        return "Work auth"
    if "project" in lowered or "embedded" in lowered:
        return "Project example"
    if "platform account" in lowered or "login" in lowered:
        return "Platform login"
    return compact(label, 38)


def capabilities_reply() -> str:
    return (
        "Capabilities: SCOUT finds roles; COACH explains fit and prep; RESEARCH queues company culture/news; "
        "APPLY creates application tasks; SUBMISSION shows database answers ready for browser injection; "
        "QUEUE tracks readiness and missing answers; SMS asks you one question at a time. "
        "Not live yet: browser form fill or final submit."
    )


def status_reply(tools: CoverAiAgentTools) -> str:
    status = tools.market_status()
    automation = status.get("automation") or {}
    latest = status.get("latest_run") or {}
    apps = status.get("applications") or []
    running = "running" if automation.get("running") else "idle"
    interval = int(automation.get("interval_seconds") or 0)
    loop = f"{interval // 60}m loop" if interval else "loop unavailable"
    latest_text = "no scout runs yet"
    if latest:
        latest_text = (
            f"last scout {latest.get('status')} found={latest.get('offers_found')} "
            f"new={latest.get('offers_new')} reported={latest.get('offers_reported')}"
        )
    app_text = "no application tasks"
    if apps:
        app = apps[0]
        app_text = (
            f"latest app {app.get('company')}: {app.get('readiness_percent')}% ready "
            f"({app.get('questions_answered')}/{app.get('questions_total')})"
        )
    return compact(
        f"Status: scout {running}, {loop}. Offers: {status['new_offers']} new, {status['reported_offers']} reported. "
        f"{app_text}. {latest_text}. Text CAPABILITIES, OFFERS, QUEUE, or natural questions.",
        600,
    )


def offers_reply(tools: CoverAiAgentTools) -> str:
    counts = {
        "new": len(tools.store.list_offers(status="new", limit=200, user_id=tools.user_id)),
        "reported": len(tools.store.list_offers(status="reported", limit=200, user_id=tools.user_id)),
        "selected": len(tools.store.list_offers(status="selected", limit=200, user_id=tools.user_id)),
        "skipped": len(tools.store.list_offers(status="skipped", limit=200, user_id=tools.user_id)),
    }
    recent_reported = tools.store.recent_reported_offers(tools.sender, limit=3, user_id=tools.user_id)
    if not recent_reported:
        recent_reported = tools.store.list_offers(limit=3, user_id=tools.user_id)
    top = "; ".join(
        f"{offer.get('company') or 'Unknown'} {int(offer.get('score') or 0)}%"
        for offer in recent_reported
    ) or "none yet"
    return compact(
        f"Offers: {counts['new']} new, {counts['reported']} reported, {counts['selected']} selected, {counts['skipped']} skipped. "
        f"Recent/top: {top}. Text 'send more opportunities' or ask about a company.",
        600,
    )


def queue_reply(tools: CoverAiAgentTools) -> str:
    apps = tools.store.list_application_tasks(limit=5, user_id=tools.user_id)
    if not apps:
        return "Queue: no application tasks yet. Text 'start applying to the last one' or name a company to create one."
    parts = [
        f"{app.get('company') or 'Unknown'} {app.get('readiness_percent')}% ({app.get('questions_answered')}/{app.get('questions_total')}) {app.get('status')}"
        for app in apps[:4]
    ]
    next_app = apps[0]
    questions = tools.store.list_application_questions(str(next_app["id"]), user_id=tools.user_id)
    next_question = next((q for q in questions if q.get("status") == "needs_user"), None)
    suffix = f" Next missing: {next_question['label']}." if next_question else " No missing required answers on the latest task."
    return compact("Queue: " + "; ".join(parts) + "." + suffix, 600)


def agent_help() -> str:
    return (
        "Hard commands: CAPABILITIES, STATUS, OFFERS, QUEUE. Also talk naturally: 'scout for new roles', "
        "'tell me about Netatmo', 'start applying to that', 'how ready is it?', or 'research the company culture'."
    )


def compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."
