"""Submission-packet producer (Marie / coverai.forms).

Marie's one job at the Forms<->Browser seam: assemble the reviewed answers into
the FROZEN contract shape (contracts/submission-packet.schema.json in the
personal-agentic-workflow repo) that Helene's browser_apply.fill_form consumes.

She merges three sources into one packet:

  1. identity  -- the user_profile row, copied straight across (its columns already
                  match the LOGICAL_FIELDS vocabulary in browser_apply.py);
  2. answers   -- application_questions, whose app-specific field_keys are mapped
                  to logical field names via QUESTION_TO_LOGICAL below;
  3. cv        -- the CV artifact (referenced as cv_upload), when one is attached.

Marie deliberately does NOT: set approved_for_autofill (that is a separate human
approval, never Marie's to grant), emit full_name / confirm_email (Helene derives
those at fill time), or fill consent_gdpr (a legal act only the human performs).

This module reads from CoverAiStore; it never touches a browser. Stdlib only.
"""

from __future__ import annotations

from typing import Any

from .storage import CoverAiStore, DEFAULT_USER_ID, utc_now

# Identity fields Marie copies from user_profile. Names are identical to the
# logical form vocabulary, so this is a straight copy with no renaming.
IDENTITY_FIELDS = (
    "first_name", "last_name", "email", "phone",
    "location_city", "location_country", "linkedin_url", "portfolio_url",
)

# Never echo these in SMS or logs -- delivered, but masked in any report.
SENSITIVE_FIELDS = frozenset({"email", "phone"})

# Translate the app's question field_keys (derived from human-readable labels)
# into the logical form vocabulary. Only questions that correspond to a real form
# field appear here; coaching-only questions (e.g. a project example) and internal
# checks (platform_account_login_ready) are intentionally absent -- they are not
# things we type into an application form.
QUESTION_TO_LOGICAL = {
    "cv_tailored_for_this_role": "cv_upload",
    "cover_application_motivation_angle": "motivation",
    "start_date_availability": "start_date",
    "work_authorization_location_constraints": "work_authorization",
}


def _status_for_answer(payload: dict[str, Any]) -> str:
    """Map an application question's state to the contract's status enum.

    Contract statuses: ready | needs_review | missing.
      * no answer text                 -> missing
      * confirmed / already filled     -> ready
      * anything else with text (a
        drafted / AI-generated answer)  -> needs_review (a human should read it
                                          before it is trusted on a real form).
    """
    answer = (payload.get("answer") or "").strip()
    if not answer:
        return "missing"
    if payload.get("status") in ("confirmed", "filled"):
        return "ready"
    return "needs_review"


def _cv_artifact(app_packet: dict[str, Any]) -> dict[str, Any] | None:
    """Find a CV artifact attached to the application, if any.

    The app stores artifacts as a free-form dict in application_tasks.artifacts_json.
    We look for one that names a CV/resume. Returns a contract artifact-ref-shaped
    dict, or None when no CV is attached yet (then cv_upload is reported 'missing').
    """
    artifacts = app_packet.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        return None
    for key, value in artifacts.items():
        blob = f"{key} {value}".lower()
        if any(word in blob for word in ("cv", "resume", "résumé", "curriculum")):
            ref = value if isinstance(value, dict) else {"storage_ref": str(value)}
            return {"artifact_id": ref.get("artifact_id", str(key)), "kind": ref.get("kind", "pdf"),
                    "title": ref.get("title", "CV"), "storage_ref": ref.get("storage_ref", str(value))}
    return None


def build_submission_packet(
    store: CoverAiStore,
    application_id: str,
    user_id: str = DEFAULT_USER_ID,
    cv_artifact: dict[str, Any] | None = None,
    cover_letter_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a contract-shaped submission packet for one application.

    `cv_artifact`, when given, is a rendered CV artifact-ref (from
    cv_render.render_cv) that supplies the cv_upload field. When omitted we fall
    back to any CV attached to the application, and report cv_upload 'missing'
    when there is none -- never faked.

    Read-only against the store. Merges identity + answers + CV into the frozen
    field vocabulary. approved_for_autofill is always False here.
    """
    app_packet = store.application_submission_packet(application_id, user_id=user_id)
    app = app_packet.get("application") or {}
    offer = app_packet.get("offer") or {}
    profile = store.get_profile(user_id) or {}
    answers_by_key = {a["field_key"]: a for a in app_packet.get("answers", [])}
    cv = cv_artifact or _cv_artifact(app_packet)

    fields: list[dict[str, Any]] = []

    # 1. Identity, straight from the profile.
    for logical in IDENTITY_FIELDS:
        value = str(profile.get(logical) or "")
        fields.append({
            "name": logical,
            "value": value,
            "status": "ready" if value else "missing",
            "source": "memory" if value else "unknown",
            "sensitive": logical in SENSITIVE_FIELDS,
        })

    # 2. Application answers, translated to logical fields.
    for field_key, logical in QUESTION_TO_LOGICAL.items():
        answer = answers_by_key.get(field_key)
        if answer is None:
            continue
        if logical == "cv_upload":
            # The stored "answer" here is coaching text, not a file path. The real
            # value is the CV artifact; reference it, else report the CV as missing.
            fields.append({
                "name": "cv_upload",
                "value": f"artifact:{cv['artifact_id']}" if cv else "",
                "status": "needs_review" if cv else "missing",
                "source": "memory" if cv else "unknown",
                "sensitive": False,
            })
            continue
        fields.append({
            "name": logical,
            "value": str(answer.get("answer") or ""),
            "status": _status_for_answer(answer),
            "source": str(answer.get("answer_source") or "unknown"),
            "sensitive": False,
        })

    # 3. Cover letter -- a file field derived from the motivation text, delivered
    # as an artifact exactly like cv_upload. Rendering (motivation -> PDF) happens
    # upstream (cv_render); here we only reference an already-rendered letter, or
    # report it missing. Never faked.
    fields.append({
        "name": "cover_letter_upload",
        "value": f"artifact:{cover_letter_artifact['artifact_id']}" if cover_letter_artifact else "",
        "status": "needs_review" if cover_letter_artifact else "missing",
        "source": "memory" if cover_letter_artifact else "unknown",
        "sensitive": False,
    })

    # 4. GDPR consent -- part of the frozen vocabulary, but HUMAN-ONLY (freeze
    # redline #5). Marie emits the slot so Helene has a stable field to map, but
    # never marks it ready and never sources a value: only the human can consent,
    # at fill time. So it is always needs_review with an empty value.
    fields.append({
        "name": "consent_gdpr",
        "value": "",
        "status": "needs_review",
        "source": "unknown",
        "sensitive": False,
    })

    ready_count = sum(1 for f in fields if f["status"] == "ready")
    missing = [f["name"] for f in fields if f["status"] != "ready"]
    company = str(app.get("company") or offer.get("company") or "This")
    summary = f"{company} packet: {ready_count}/{len(fields)} ready."
    if missing:
        summary += " Missing/review: " + ", ".join(missing) + "."

    return {
        "packet_id": store.new_id("pkt_"),
        "offer_ref": f"offer:{app.get('offer_id')}" if app.get("offer_id") else "",
        "company": company,
        "role": str(app.get("role_title") or offer.get("title") or ""),
        "target_url": str(offer.get("url") or ""),
        "fields": fields,
        "readiness": {
            "ready_count": ready_count,
            "total_count": len(fields),
            "missing": missing,
            "summary": summary,
        },
        "artifacts": [a for a in (cv, cover_letter_artifact) if a],
        "approved_for_autofill": False,
        "created_at": utc_now(),
    }
