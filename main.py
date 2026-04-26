# app.py
import os
import re
import json
import uuid
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY")

app = FastAPI(title="CoverGemini")

SESSIONS: Dict[str, Dict[str, Any]] = {}


class StartSessionRequest(BaseModel):
    job_offer_text: str
    company: Optional[str] = ""
    role_title: Optional[str] = ""
    job_family: Optional[str] = ""   # optional, backend can infer it
    language: Optional[str] = "fr"
    applicant_name: Optional[str] = "Julien Gonzales"


class AnswerRequest(BaseModel):
    session_id: str
    answer: str


class IngestCvTextRequest(BaseModel):
    session_id: str
    cv_text: str


class RenderRequest(BaseModel):
    session_id: str


QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "target_field": {"type": "string"},
        "reason": {"type": "string"}
    },
    "required": ["question", "target_field", "reason"]
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "field_updates": {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "searching_for": {"type": "string"},
                "summary": {"type": "string"},
                "languages": {"type": "array", "items": {"type": "string"}},
                "education": {"type": "array", "items": {"type": "object"}},
                "experiences": {"type": "array", "items": {"type": "object"}},
                "projects": {"type": "array", "items": {"type": "object"}},
                "skills": {"type": "array", "items": {"type": "string"}},
                "soft_skills": {"type": "array", "items": {"type": "string"}},
                "achievements": {"type": "array", "items": {"type": "string"}},
                "constraints": {"type": "object"}
            }
        },
        "still_missing": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"}
    },
    "required": ["field_updates", "still_missing", "notes"]
}

JOB_META_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "role_title": {"type": "string"},
        "job_family": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"}
    },
    "required": ["company", "role_title", "job_family", "keywords", "language"]
}

LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "letter": {"type": "string"},
        "company": {"type": "string"},
        "role_title_masculine": {"type": "string"}
    },
    "required": ["letter", "company", "role_title_masculine"]
}

CV_BLOCKS_SCHEMA = {
    "type": "object",
    "properties": {
        "objectif": {"type": "string"},
        "apl_items": {"type": "array", "items": {"type": "string"}},
        "competences_lines": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["objectif", "apl_items", "competences_lines"]
}


JOB_FAMILY_RULES = {
    "embedded_ai": {
        "required_fields": [
            "headline",
            "searching_for",
            "education",
            "experiences",
            "projects",
            "skills",
            "achievements"
        ]
    },
    "service": {
        "required_fields": [
            "headline",
            "searching_for",
            "experiences",
            "languages",
            "soft_skills",
            "achievements"
        ]
    },
    "general": {
        "required_fields": [
            "headline",
            "searching_for",
            "experiences",
            "skills"
        ]
    }
}


def gemini_generate(
    system_instruction: str,
    contents: List[Dict[str, Any]],
    response_schema: Optional[Dict[str, Any]] = None,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": temperature
        }
    }

    if response_schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = response_schema

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    r = requests.post(GEMINI_URL, headers=headers, json=body, timeout=120)
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Gemini error: {r.text}")

    return r.json()


def extract_text(resp: Dict[str, Any]) -> str:
    try:
        return resp["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Bad Gemini response: {e}")


def merge_profile(profile: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = json.loads(json.dumps(profile))
    for key, value in updates.items():
        if value in (None, "", [], {}):
            continue
        merged[key] = value
    return merged


def infer_job_meta(job_offer_text: str, fallback_family: str = "") -> Dict[str, Any]:
    system = (
        "You extract job metadata from a raw job offer page text. "
        "Return JSON only. "
        "Allowed job_family values are embedded_ai, service, general. "
        "If the role is about embedded systems, firmware, electronics, real-time, AI applied to systems, use embedded_ai. "
        "If the role is about customer-facing service, airline service, hospitality, reception, operations support, use service. "
        "Otherwise use general."
    )
    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_offer_text": job_offer_text,
            "fallback_job_family": fallback_family
        }, ensure_ascii=False)}]
    }]
    resp = gemini_generate(system, contents, response_schema=JOB_META_SCHEMA, temperature=0.1)
    meta = json.loads(extract_text(resp))

    if fallback_family and fallback_family in JOB_FAMILY_RULES:
        meta["job_family"] = fallback_family

    if meta["job_family"] not in JOB_FAMILY_RULES:
        meta["job_family"] = "general"

    return meta


def profile_gaps(session: Dict[str, Any]) -> List[str]:
    family = session["job_target"]["job_family"]
    required = JOB_FAMILY_RULES[family]["required_fields"]
    profile = session["profile"]
    gaps = []

    for field in required:
        value = profile.get(field)
        if value in ("", [], {}, None):
            gaps.append(field)

    return gaps


def build_next_question(session: Dict[str, Any]) -> Dict[str, Any]:
    gaps = profile_gaps(session)

    system = (
        "You are an intake interviewer for building a job application package. "
        "Ask exactly one short next question in French. "
        "Do not ask vague questions. "
        "Choose the most useful missing field first. "
        "The user is answering from a phone. "
        "Keep the question practical."
    )

    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_target": session["job_target"],
            "profile": session["profile"],
            "missing_fields": gaps
        }, ensure_ascii=False)}]
    }]

    resp = gemini_generate(system, contents, response_schema=QUESTION_SCHEMA, temperature=0.2)
    return json.loads(extract_text(resp))


def extract_answer_to_profile(session: Dict[str, Any], answer: str) -> Dict[str, Any]:
    system = (
        "You extract structured applicant information from a French answer. "
        "Return JSON only. "
        "Do not invent details. "
        "If the user gives one experience, preserve it as one experience object. "
        "If the user gives one project, preserve it as one project object. "
        "Keep wording concise."
    )

    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_target": session["job_target"],
            "current_profile": session["profile"],
            "answer": answer
        }, ensure_ascii=False)}]
    }]

    resp = gemini_generate(system, contents, response_schema=EXTRACTION_SCHEMA, temperature=0.1)
    return json.loads(extract_text(resp))


def ingest_cv_text(session: Dict[str, Any], cv_text: str) -> Dict[str, Any]:
    system = (
        "Extract structured applicant data from the CV text. "
        "Return JSON only. "
        "Do not invent facts. "
        "Use the same field structure as the profile."
    )

    contents = [{
        "role": "user",
        "parts": [{"text": cv_text}]
    }]

    resp = gemini_generate(system, contents, response_schema=EXTRACTION_SCHEMA, temperature=0.1)
    result = json.loads(extract_text(resp))
    session["profile"] = merge_profile(session["profile"], result["field_updates"])
    return session["profile"]


def latex_escape(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)

    replacements = {
        "\\": "\\textbackslash{}",
        "&": "\\&",
        "%": "\\%",
        "$": "\\$",
        "#": "\\#",
        "_": "\\_",
        "{": "\\{",
        "}": "\\}",
        "~": "\\textasciitilde{}",
        "^": "\\textasciicircum{}",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def generate_letter(session: Dict[str, Any]) -> Dict[str, Any]:
    system = (
        "Tu rédiges une lettre de motivation en français. "
        "Ton direct, orienté offre de service. "
        "Pas de listes dans le corps. "
        "Trois paragraphes maximum. "
        "Inclure au moins une constante numérique crédible si pertinente, par exemple 10 jours ou 10 ms, sans inventer de contexte faux. "
        "Conclusion directe vers l'entretien."
    )

    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_target": session["job_target"],
            "profile": session["profile"]
        }, ensure_ascii=False)}]
    }]

    resp = gemini_generate(system, contents, response_schema=LETTER_SCHEMA, temperature=0.35)
    return json.loads(extract_text(resp))


def generate_cv_blocks(session: Dict[str, Any]) -> Dict[str, Any]:
    system = (
        "Tu réécris trois blocs de CV adaptés à une offre. "
        "Retourne un JSON seulement. "
        "Le bloc objectif doit être un paragraphe court. "
        "Le bloc APL doit contenir trois items maximum. "
        "Le bloc compétences doit être une liste de lignes prêtes à injecter dans LaTeX. "
        "Ne change pas la structure du template global."
    )

    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_target": session["job_target"],
            "profile": session["profile"]
        }, ensure_ascii=False)}]
    }]

    resp = gemini_generate(system, contents, response_schema=CV_BLOCKS_SCHEMA, temperature=0.25)
    return json.loads(extract_text(resp))


def render_latex_patch(session: Dict[str, Any], cv_blocks: Dict[str, Any], letter: Dict[str, Any]) -> str:
    company = latex_escape(session["job_target"]["company"] or letter["company"] or "Entreprise")
    role_title = latex_escape(session["job_target"]["role_title"] or letter["role_title_masculine"] or "Poste")
    headline = latex_escape(session["profile"].get("headline") or "Apprenti Ingénieur")
    searching_for = latex_escape(session["profile"].get("searching_for") or "Recherche d'opportunité")
    objectif = latex_escape(cv_blocks["objectif"])

    apl_items_tex = "\n".join(
        f"    \\item {latex_escape(item)}" for item in cv_blocks["apl_items"]
    )

    competences_tex = " \\\\\n".join(
        latex_escape(line) for line in cv_blocks["competences_lines"]
    )

    return f"""
% ===== PATCH GENERATED =====
\\newcommand{{\\company}}{{{company}}}
\\newcommand{{\\jobtitle}}{{{role_title}}}

% HEADER REPLACEMENT
\\textbf{{{headline}}}\\\\
\\textit{{{searching_for}}}\\\\[0.1cm]
\\textit{{\\jobtitle\\ – \\company}}

% OBJECTIF REPLACEMENT
\\section*{{Objectif}}
{objectif}

% APL BLOCK REPLACEMENT
\\textbf{{Research Intern – RISE@APL (Johns Hopkins APL)}} \\hfill \\textit{{2024 – 2025}}
\\begin{{itemize}}[leftmargin=*]
{apl_items_tex}
\\end{{itemize}}

% COMPETENCES REPLACEMENT
\\section*{{Compétences clés}}
\\noindent
{competences_tex}

% LETTER
% {latex_escape(letter["letter"])}
""".strip()


def make_empty_profile(applicant_name: str, language: str) -> Dict[str, Any]:
    return {
        "name": applicant_name,
        "headline": "",
        "searching_for": "",
        "summary": "",
        "languages": ["Français natif", "Anglais courant"] if language == "fr" else ["French native", "English fluent"],
        "education": [],
        "experiences": [],
        "projects": [],
        "skills": [],
        "soft_skills": [],
        "achievements": [],
        "constraints": {
            "mobility": "",
            "contract_type": "",
            "duration": ""
        }
    }


@app.post("/session/start")
def start_session(req: StartSessionRequest):
    meta = infer_job_meta(req.job_offer_text, req.job_family or "")

    company = req.company or meta["company"]
    role_title = req.role_title or meta["role_title"]
    job_family = meta["job_family"]

    session_id = str(uuid.uuid4())

    session = {
        "session_id": session_id,
        "job_target": {
            "company": company,
            "role_title": role_title,
            "job_family": job_family,
            "keywords": meta["keywords"],
            "language": req.language,
            "job_offer_text": req.job_offer_text
        },
        "profile": make_empty_profile(req.applicant_name, req.language),
        "conversation": []
    }

    SESSIONS[session_id] = session
    next_q = build_next_question(session)

    return {
        "session_id": session_id,
        "job_target": session["job_target"],
        "next_question": next_q["question"],
        "target_field": next_q["target_field"],
        "reason": next_q["reason"]
    }


@app.post("/session/answer")
def answer_session(req: AnswerRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    extracted = extract_answer_to_profile(session, req.answer)
    session["profile"] = merge_profile(session["profile"], extracted["field_updates"])
    session["conversation"].append({
        "role": "user",
        "text": req.answer
    })

    gaps = profile_gaps(session)

    if gaps:
        next_q = build_next_question(session)
        return {
            "done": False,
            "profile": session["profile"],
            "notes": extracted["notes"],
            "still_missing": gaps,
            "next_question": next_q["question"],
            "target_field": next_q["target_field"]
        }

    return {
        "done": True,
        "profile": session["profile"],
        "notes": extracted["notes"],
        "still_missing": []
    }


@app.post("/session/ingest_cv_text")
def session_ingest_cv_text(req: IngestCvTextRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    profile = ingest_cv_text(session, req.cv_text)

    return {
        "session_id": req.session_id,
        "profile": profile,
        "still_missing": profile_gaps(session)
    }


@app.post("/session/render")
def render_session(req: RenderRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    letter = generate_letter(session)
    cv_blocks = generate_cv_blocks(session)
    latex_patch = render_latex_patch(session, cv_blocks, letter)

    return {
        "session_id": req.session_id,
        "job_target": session["job_target"],
        "letter": letter["letter"],
        "cv_blocks": cv_blocks,
        "latex_patch": latex_patch,
        "profile": session["profile"]
    }