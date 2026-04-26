import os
import uuid
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_file, send_from_directory, abort
from werkzeug.utils import secure_filename
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

app = Flask(__name__)

WORKDIR = "jobs"
LIBRARY_DIR = "library"
LIBRARY_INDEX_FILE = os.path.join(LIBRARY_DIR, "index.json")
LOG_FILE = "server_debug.log"
PDFLATEX_PATH = "/Library/TeX/texbin/pdflatex"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

os.makedirs(WORKDIR, exist_ok=True)
os.makedirs(LIBRARY_DIR, exist_ok=True)

SESSIONS: Dict[str, Dict[str, Any]] = {}

JOB_FAMILY_RULES = {
    "embedded_ai": {
        "required_fields": [
            "headline",
            "searching_for",
            "education",
            "experiences",
            "projects",
            "skills",
        ]
    },
    "service": {
        "required_fields": [
            "headline",
            "searching_for",
            "experiences",
            "languages",
            "soft_skills",
        ]
    },
    "general": {
        "required_fields": [
            "headline",
            "searching_for",
            "experiences",
            "skills",
        ]
    },
}

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "target_field": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["question", "target_field", "reason"],
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
                "languages": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "education": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "degree": {"type": "string"},
                            "institution": {"type": "string"},
                            "field": {"type": "string"},
                            "start_year": {"type": "string"},
                            "end_year": {"type": "string"},
                            "status": {"type": "string"}
                        }
                    }
                },
                "experiences": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "organization": {"type": "string"},
                            "start_date": {"type": "string"},
                            "end_date": {"type": "string"},
                            "description": {"type": "string"},
                            "tasks": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "tools": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        }
                    }
                },
                "projects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "tasks": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "tools": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        }
                    }
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "soft_skills": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "achievements": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "constraints": {
                    "type": "object",
                    "properties": {
                        "mobility": {"type": "string"},
                        "contract_type": {"type": "string"},
                        "duration": {"type": "string"}
                    }
                }
            },
        },
        "still_missing": {
            "type": "array",
            "items": {"type": "string"}
        },
        "notes": {"type": "string"},
    },
    "required": ["field_updates", "still_missing", "notes"],
}

JOB_META_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "role_title": {"type": "string"},
        "job_family": {"type": "string"},
        "keywords": {
            "type": "array",
            "items": {"type": "string"}
        },
        "language": {"type": "string"},
    },
    "required": ["company", "role_title", "job_family", "keywords", "language"],
}

LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "letter": {"type": "string"},
        "company": {"type": "string"},
        "role_title_masculine": {"type": "string"},
    },
    "required": ["letter", "company", "role_title_masculine"],
}

CV_BLOCKS_SCHEMA = {
    "type": "object",
    "properties": {
        "objectif": {"type": "string"},
        "apl_items": {
            "type": "array",
            "items": {"type": "string"}
        },
        "competences_lines": {
            "type": "array",
            "items": {"type": "string"}
        },
    },
    "required": ["objectif", "apl_items", "competences_lines"],
}


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    print(formatted)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(formatted + "\n")


def load_library_index() -> Dict[str, Any]:
    if not os.path.exists(LIBRARY_INDEX_FILE):
        return {"documents": {}}
    with open(LIBRARY_INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_library_index(index_data: Dict[str, Any]) -> None:
    with open(LIBRARY_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)


def safe_filename(filename: str) -> str:
    cleaned = "".join(c for c in filename if c.isalnum() or c in ("-", "_", "."))
    return cleaned or "document"


def cache_path_for_label(label: str) -> str:
    return os.path.join(LIBRARY_DIR, f"{safe_filename(label)}.profile.json")


def save_library_file(file_storage, label: str) -> Dict[str, Any]:
    index_data = load_library_index()
    original_name = file_storage.filename or "document"
    extension = os.path.splitext(original_name)[1]
    stored_name = safe_filename(f"{label}{extension}")
    file_path = os.path.join(LIBRARY_DIR, stored_name)
    file_storage.save(file_path)

    previous = index_data["documents"].get(label, {})
    doc_record = {
        "label": label,
        "original_name": original_name,
        "stored_name": stored_name,
        "path": file_path,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "cached_profile_path": previous.get("cached_profile_path", cache_path_for_label(label)),
        "cached_profile_updated_at": previous.get("cached_profile_updated_at", ""),
    }
    index_data["documents"][label] = doc_record
    save_library_index(index_data)
    return doc_record


def get_library_document(label: str) -> Dict[str, Any]:
    index_data = load_library_index()
    doc = index_data.get("documents", {}).get(label)
    if not doc:
        raise RuntimeError(f"Unknown library label: {label}")
    if not os.path.exists(doc["path"]):
        raise RuntimeError(f"Stored file missing for label: {label}")
    return doc


def update_library_cache_metadata(label: str, cached_profile_path: str) -> None:
    index_data = load_library_index()
    doc = index_data.get("documents", {}).get(label)
    if not doc:
        raise RuntimeError(f"Unknown library label: {label}")
    doc["cached_profile_path"] = cached_profile_path
    doc["cached_profile_updated_at"] = datetime.now().isoformat(timespec="seconds")
    index_data["documents"][label] = doc
    save_library_index(index_data)


def save_cached_profile(label: str, field_updates: Dict[str, Any]) -> str:
    cache_path = cache_path_for_label(label)
    payload = {
        "label": label,
        "field_updates": field_updates,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    update_library_cache_metadata(label, cache_path)
    return cache_path


def load_cached_profile(label: str) -> Optional[Dict[str, Any]]:
    try:
        doc = get_library_document(label)
    except Exception:
        return None

    cache_path = doc.get("cached_profile_path") or cache_path_for_label(label)
    if not os.path.exists(cache_path):
        return None

    with open(cache_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    field_updates = payload.get("field_updates")
    if not isinstance(field_updates, dict):
        return None
    return field_updates


def gemini_generate(
    system_instruction: str,
    contents: List[Dict[str, Any]],
    response_schema: Optional[Dict[str, Any]] = None,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        raise RuntimeError("Missing GEMINI_API_KEY")

    body: Dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature},
    }

    if response_schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = response_schema

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    max_attempts = 3
    backoff_seconds = 3

    for attempt in range(1, max_attempts + 1):
        response = requests.post(GEMINI_URL, headers=headers, json=body, timeout=120)

        if response.status_code == 429:
            if attempt < max_attempts:
                wait_time = backoff_seconds * attempt
                log(f"Gemini rate limit hit. Waiting {wait_time}s before retry {attempt + 1}/{max_attempts}.")
                time.sleep(wait_time)
                continue
            raise RuntimeError(
                "Gemini rate limit hit. Wait a bit before retrying. "
                f"Response: {response.text}"
            )

        if not response.ok:
            raise RuntimeError(f"Gemini API error {response.status_code}: {response.text}")

        return response.json()

    raise RuntimeError("Gemini request failed after retries.")


def extract_text(resp: Dict[str, Any]) -> str:
    return resp["candidates"][0]["content"]["parts"][0]["text"]


def make_empty_profile(applicant_name: str, language: str) -> Dict[str, Any]:
    default_languages = ["Français natif", "Anglais courant"]
    if language != "fr":
        default_languages = ["French native", "English fluent"]

    return {
        "name": applicant_name,
        "headline": "",
        "searching_for": "",
        "summary": "",
        "languages": default_languages,
        "education": [],
        "experiences": [],
        "projects": [],
        "skills": [],
        "soft_skills": [],
        "achievements": [],
        "constraints": {
            "mobility": "",
            "contract_type": "",
            "duration": "",
        },
    }


def merge_profile(profile: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = json.loads(json.dumps(profile))
    for key, value in updates.items():
        if value in (None, "", [], {}):
            continue
        merged[key] = value
    return merged


def normalize_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    for key in ["education", "experiences", "projects"]:
        cleaned = []
        for item in profile.get(key, []):
            if isinstance(item, dict) and any(v not in (None, "", [], {}) for v in item.values()):
                cleaned.append(item)
        profile[key] = cleaned

    for key in ["skills", "soft_skills", "achievements", "languages"]:
        cleaned_list = []
        for item in profile.get(key, []):
            if isinstance(item, str) and item.strip():
                cleaned_list.append(item.strip())
        profile[key] = cleaned_list

    return profile


def infer_job_meta(job_offer_text: str, fallback_family: str = "") -> Dict[str, Any]:
    if fallback_family and fallback_family in JOB_FAMILY_RULES:
        return {
            "company": "",
            "role_title": "",
            "job_family": fallback_family,
            "keywords": [],
            "language": "fr",
        }

    system = (
        "You extract job metadata from raw job offer page text. "
        "Return JSON only. "
        "Allowed job_family values are embedded_ai, service, general. "
        "If the role is about embedded systems, firmware, electronics, real-time, or AI applied to systems, use embedded_ai. "
        "If the role is customer-facing service, airline service, hospitality, or reception, use service. "
        "Otherwise use general."
    )
    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_offer_text": job_offer_text,
            "fallback_job_family": fallback_family,
        }, ensure_ascii=False)}],
    }]
    resp = gemini_generate(system, contents, response_schema=JOB_META_SCHEMA, temperature=0.1)
    meta = json.loads(extract_text(resp))

    if fallback_family and fallback_family in JOB_FAMILY_RULES:
        meta["job_family"] = fallback_family
    if meta.get("job_family") not in JOB_FAMILY_RULES:
        meta["job_family"] = "general"
    return meta


def profile_gaps(session: Dict[str, Any]) -> List[str]:
    family = session["job_target"]["job_family"]
    required = JOB_FAMILY_RULES[family]["required_fields"]
    profile = normalize_profile(session["profile"])
    gaps = []
    for field in required:
        value = profile.get(field)
        if value in ("", [], {}, None):
            gaps.append(field)
    return gaps


def build_next_question(session: Dict[str, Any]) -> Dict[str, Any]:
    system = (
        "You are an intake interviewer for building a job application package. "
        "Ask exactly one short next question in French. "
        "Do not ask vague questions. "
        "Choose the most useful missing field first. "
        "The user is answering from a phone. "
        "If education is already present, do not ask about education. "
        "If experiences are already present, do not ask about experiences."
    )
    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_target": session["job_target"],
            "profile": session["profile"],
            "missing_fields": profile_gaps(session),
        }, ensure_ascii=False)}],
    }]
    resp = gemini_generate(system, contents, response_schema=QUESTION_SCHEMA, temperature=0.2)
    return json.loads(extract_text(resp))


def extract_answer_to_profile(session: Dict[str, Any], answer: str) -> Dict[str, Any]:
    system = (
        "You extract structured applicant information from a French answer. "
        "Return JSON only. "
        "Do not invent details. "
        "Avoid empty placeholder objects."
    )
    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_target": session["job_target"],
            "current_profile": session["profile"],
            "answer": answer,
        }, ensure_ascii=False)}],
    }]
    resp = gemini_generate(system, contents, response_schema=EXTRACTION_SCHEMA, temperature=0.1)
    return json.loads(extract_text(resp))


def extract_text_from_pdf(file_path: str) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed. Run: pip install pypdf")

    reader = PdfReader(file_path)
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages).strip()


def extract_text_from_file(file_path: str, filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return extract_text_from_pdf(file_path)
    if lower.endswith(".txt") or lower.endswith(".tex") or lower.endswith(".md") or lower.endswith(".json"):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    raise RuntimeError(f"Unsupported file type for ingestion: {filename}")


def ingest_text_with_gemini(text: str, source_name: str) -> Dict[str, Any]:
    system = (
        "Extract structured applicant data from the provided CV, dossier de compétences, or profile document. "
        "Return JSON only. "
        "Do not invent facts. "
        "Avoid empty placeholder objects. "
        "Education is mandatory when present in the document. "
        "If education is present, extract it into field_updates.education as a non-empty array of objects. "
        "Each education object should include degree, institution, field, start_year, end_year, and status when available. "
        "If professional experiences are present, extract them into field_updates.experiences as a non-empty array of objects with title, organization, start_date, end_date, description, tasks, and tools when available. "
        "If projects are present, extract them into field_updates.projects as a non-empty array of objects with title, description, tasks, and tools when available. "
        "If skills are present, extract them into field_updates.skills as a flat list of strings. "
        "If the document explicitly includes language levels, extract them into field_updates.languages. "
        "Preserve applicant data exactly as stated in the source text. "
        "Do not output empty objects in education, experiences, or projects."
    )
    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "source_name": source_name,
            "document_text": text,
        }, ensure_ascii=False)}],
    }]
    resp = gemini_generate(system, contents, response_schema=EXTRACTION_SCHEMA, temperature=0.1)
    return json.loads(extract_text(resp))


def ingest_text_into_profile(session: Dict[str, Any], text: str, source_name: str) -> Dict[str, Any]:
    result = ingest_text_with_gemini(text, source_name)
    session["profile"] = normalize_profile(merge_profile(session["profile"], result["field_updates"]))
    return result


def load_source_into_session(session: Dict[str, Any], label: str) -> Dict[str, Any]:
    cached = load_cached_profile(label)
    if cached is not None:
        session["profile"] = normalize_profile(merge_profile(session["profile"], cached))
        source_entry = {
            "label": label,
            "original_name": get_library_document(label)["original_name"],
            "notes": "Loaded from cached structured profile.",
            "cache_used": True,
        }
        if not any(existing.get("label") == label for existing in session["sources"]):
            session["sources"].append(source_entry)
        return source_entry

    doc = get_library_document(label)
    text = extract_text_from_file(doc["path"], doc["original_name"])
    ingest_result = ingest_text_into_profile(session, text, label)
    save_cached_profile(label, ingest_result["field_updates"])

    source_entry = {
        "label": label,
        "original_name": doc["original_name"],
        "notes": ingest_result["notes"],
        "cache_used": False,
    }
    if not any(existing.get("label") == label for existing in session["sources"]):
        session["sources"].append(source_entry)
    return source_entry


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
        "Conclusion directe vers l'entretien."
    )
    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_target": session["job_target"],
            "profile": session["profile"],
        }, ensure_ascii=False)}],
    }]
    resp = gemini_generate(system, contents, response_schema=LETTER_SCHEMA, temperature=0.35)
    return json.loads(extract_text(resp))


def generate_cv_blocks(session: Dict[str, Any]) -> Dict[str, Any]:
    system = (
        "Tu réécris trois blocs de CV adaptés à une offre. "
        "Retourne un JSON seulement. "
        "Le bloc objectif doit être un paragraphe court. "
        "Le bloc APL doit contenir trois items maximum. "
        "Le bloc compétences doit être une liste de lignes prêtes à injecter dans LaTeX."
    )
    contents = [{
        "role": "user",
        "parts": [{"text": json.dumps({
            "job_target": session["job_target"],
            "profile": session["profile"],
        }, ensure_ascii=False)}],
    }]
    resp = gemini_generate(system, contents, response_schema=CV_BLOCKS_SCHEMA, temperature=0.25)
    return json.loads(extract_text(resp))


def render_latex_patch(session: Dict[str, Any], cv_blocks: Dict[str, Any], letter: Dict[str, Any]) -> str:
    company = latex_escape(session["job_target"].get("company") or letter.get("company") or "Entreprise")
    role_title = latex_escape(session["job_target"].get("role_title") or letter.get("role_title_masculine") or "Poste")
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
% {latex_escape(letter['letter'])}
""".strip()


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "message": "CoverMaker server is running",
        "endpoints": [
            "/",
            "/health",
            "/logs",
            "/library/list",
            "/library/upload",
            "/library/build_cache",
            "/session/start",
            "/session/load_sources",
            "/session/answer",
            "/session/ingest_text",
            "/session/ingest_file",
            "/session/debug/<session_id>",
            "/session/render",
            "/compile",
        ],
    })


@app.route("/logs", methods=["GET"])
def get_logs():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/plain"}
    return "No logs found.", 404


@app.route("/health", methods=["GET"])
def health():
    index_data = load_library_index()
    return jsonify({
        "ok": True,
        "gemini_configured": bool(GEMINI_API_KEY),
        "model": GEMINI_MODEL,
        "sessions": len(SESSIONS),
        "library_documents": len(index_data.get("documents", {})),
    })


@app.route("/library/list", methods=["GET"])
def list_library_documents():
    index_data = load_library_index()
    return jsonify(index_data)


@app.route("/library/upload", methods=["POST"])
def upload_library_document():
    label = request.form.get("label", "").strip()
    if not label:
        return jsonify({"error": "label is required"}), 400

    if "file" not in request.files:
        return jsonify({"error": "file is required"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "filename is required"}), 400

    try:
        doc_record = save_library_file(file, label)
        return jsonify({
            "ok": True,
            "document": doc_record,
        })
    except Exception as e:
        log(f"Library upload failed for label {label}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/library/build_cache", methods=["POST"])
def build_library_cache():
    data = request.get_json(force=True)
    label = data.get("label", "")
    if not label:
        return jsonify({"error": "label is required"}), 400

    try:
        doc = get_library_document(label)
        text = extract_text_from_file(doc["path"], doc["original_name"])
        result = ingest_text_with_gemini(text, label)
        cache_path = save_cached_profile(label, result["field_updates"])
        return jsonify({
            "ok": True,
            "label": label,
            "cache_path": cache_path,
            "notes": result["notes"],
            "field_updates": result["field_updates"],
        })
    except Exception as e:
        log(f"build_library_cache failed for label {label}: {e}")
        return jsonify({"error": str(e)}), 429


@app.route("/session/start", methods=["POST"])
def start_session():
    data = request.get_json(force=True)
    job_offer_text = data.get("job_offer_text", "")
    if not job_offer_text:
        return jsonify({"error": "job_offer_text is required"}), 400

    applicant_name = data.get("applicant_name", "Julien Gonzales")
    language = data.get("language", "fr")
    fallback_family = data.get("job_family", "")

    try:
        meta = infer_job_meta(job_offer_text, fallback_family)
    except Exception as e:
        log(f"start_session failed during infer_job_meta: {e}")
        return jsonify({"error": str(e)}), 429

    company = data.get("company") or meta.get("company", "")
    role_title = data.get("role_title") or meta.get("role_title", "")
    job_family = meta.get("job_family", "general")

    session_id = str(uuid.uuid4())
    session = {
        "session_id": session_id,
        "job_target": {
            "company": company,
            "role_title": role_title,
            "job_family": job_family,
            "keywords": meta.get("keywords", []),
            "language": language,
            "job_offer_text": job_offer_text,
        },
        "profile": make_empty_profile(applicant_name, language),
        "conversation": [],
        "sources": [],
    }
    SESSIONS[session_id] = session

    source_labels = data.get("source_labels", [])
    if isinstance(source_labels, list):
        for label in source_labels:
            try:
                load_source_into_session(session, label)
            except Exception as e:
                log(f"Library source load failed for session {session_id}, label {label}: {e}")

    gaps = profile_gaps(session)
    if not gaps:
        return jsonify({
            "session_id": session_id,
            "job_target": session["job_target"],
            "sources": session["sources"],
            "done": True,
            "message": "Profile already complete enough for rendering.",
            "profile": session["profile"],
        })

    try:
        next_q = build_next_question(session)
    except Exception as e:
        log(f"start_session failed during build_next_question: {e}")
        return jsonify({
            "session_id": session_id,
            "job_target": session["job_target"],
            "sources": session["sources"],
            "profile": session["profile"],
            "still_missing": gaps,
            "error": str(e),
        }), 429

    return jsonify({
        "session_id": session_id,
        "job_target": session["job_target"],
        "sources": session["sources"],
        "next_question": next_q["question"],
        "target_field": next_q["target_field"],
        "reason": next_q["reason"],
        "profile": session["profile"],
        "still_missing": gaps,
    })


@app.route("/session/load_sources", methods=["POST"])
def load_sources_into_session():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    source_labels = data.get("source_labels", [])

    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Unknown session_id"}), 404
    if not isinstance(source_labels, list) or not source_labels:
        return jsonify({"error": "source_labels must be a non-empty list"}), 400

    loaded = []
    for label in source_labels:
        try:
            loaded.append(load_source_into_session(session, label))
        except Exception as e:
            log(f"Session source load failed for session {session_id}, label {label}: {e}")
            loaded.append({
                "label": label,
                "error": str(e),
            })

    return jsonify({
        "session_id": session_id,
        "loaded_sources": loaded,
        "profile": session["profile"],
        "still_missing": profile_gaps(session),
    })


@app.route("/session/answer", methods=["POST"])
def answer_session():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    answer = data.get("answer", "")

    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Unknown session_id"}), 404
    if not answer:
        return jsonify({"error": "answer is required"}), 400

    try:
        extracted = extract_answer_to_profile(session, answer)
    except Exception as e:
        log(f"answer_session failed during extract_answer_to_profile: {e}")
        return jsonify({"error": str(e)}), 429

    session["profile"] = normalize_profile(merge_profile(session["profile"], extracted["field_updates"]))
    session["conversation"].append({"role": "user", "text": answer})

    gaps = profile_gaps(session)
    if gaps:
        try:
            next_q = build_next_question(session)
        except Exception as e:
            log(f"answer_session failed during build_next_question: {e}")
            return jsonify({
                "done": False,
                "profile": session["profile"],
                "notes": extracted["notes"],
                "still_missing": gaps,
                "error": str(e),
            }), 429

        return jsonify({
            "done": False,
            "profile": session["profile"],
            "notes": extracted["notes"],
            "still_missing": gaps,
            "next_question": next_q["question"],
            "target_field": next_q["target_field"],
        })

    return jsonify({
        "done": True,
        "profile": session["profile"],
        "notes": extracted["notes"],
        "still_missing": [],
    })


@app.route("/session/ingest_text", methods=["POST"])
def ingest_text_route():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    document_text = data.get("document_text", "")
    source_name = data.get("source_name", "document")

    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Unknown session_id"}), 404
    if not document_text:
        return jsonify({"error": "document_text is required"}), 400

    try:
        result = ingest_text_into_profile(session, document_text, source_name)
    except Exception as e:
        log(f"ingest_text_route failed: {e}")
        return jsonify({"error": str(e)}), 429

    return jsonify({
        "session_id": session_id,
        "profile": session["profile"],
        "notes": result["notes"],
        "still_missing": profile_gaps(session),
    })


@app.route("/session/ingest_file", methods=["POST"])
def ingest_file_route():
    session_id = request.form.get("session_id")
    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Unknown session_id"}), 404

    if "file" not in request.files:
        return jsonify({"error": "file is required"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "filename is required"}), 400

    ingest_dir = os.path.join(WORKDIR, session_id, "ingest")
    os.makedirs(ingest_dir, exist_ok=True)
    file_path = os.path.join(ingest_dir, file.filename)
    file.save(file_path)

    try:
        text = extract_text_from_file(file_path, file.filename)
        result = ingest_text_into_profile(session, text, file.filename)
        return jsonify({
            "session_id": session_id,
            "filename": file.filename,
            "profile": session["profile"],
            "notes": result["notes"],
            "still_missing": profile_gaps(session),
        })
    except Exception as e:
        log(f"Ingest failed for session {session_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/session/debug/<session_id>", methods=["GET"])
def debug_session(session_id):
    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Unknown session_id"}), 404
    return jsonify(session)


@app.route("/session/render", methods=["POST"])
def render_session():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Unknown session_id"}), 404

    try:
        letter = generate_letter(session)
        cv_blocks = generate_cv_blocks(session)
    except Exception as e:
        log(f"render_session failed: {e}")
        return jsonify({"error": str(e)}), 429

    latex_patch = render_latex_patch(session, cv_blocks, letter)

    return jsonify({
        "session_id": session_id,
        "job_target": session["job_target"],
        "sources": session.get("sources", []),
        "letter": letter["letter"],
        "cv_blocks": cv_blocks,
        "latex_patch": latex_patch,
        "profile": session["profile"],
    })


@app.route("/compile", methods=["POST"])
def compile_tex():
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(WORKDIR, job_id)
    os.makedirs(job_dir)

    log(f"New compile job {job_id} receiving form data...")

    try:
        if not request.files:
            log(f"Job {job_id} failed: No files in request.")
            return jsonify({"error": "No file content detected"}), 400

        for key in request.files:
            file_list = request.files.getlist(key)
            for file in file_list:
                if file.filename == "":
                    continue
                destination = os.path.join(job_dir, file.filename)
                file.save(destination)
                log(f"Saved for compile job {job_id}: {file.filename}")

        tex_filename = "CV.tex"
        tex_path = os.path.join(job_dir, tex_filename)

        if not os.path.exists(tex_path):
            log(f"Job {job_id} failed: {tex_filename} not found.")
            return jsonify({
                "error": f"{tex_filename} not found",
                "received_files": os.listdir(job_dir),
            }), 400

        cmd = [PDFLATEX_PATH, "-interaction=nonstopmode", tex_filename]
        log(f"Compiling {tex_filename} for job {job_id}...")
        subprocess.run(cmd, cwd=job_dir, capture_output=True, text=True)
        result = subprocess.run(cmd, cwd=job_dir, capture_output=True, text=True)

        pdf_path = os.path.join(job_dir, tex_filename.replace(".tex", ".pdf"))
        if os.path.exists(pdf_path):
            log(f"Job {job_id}: Success! Sending PDF.")
            return send_file(pdf_path, mimetype="application/pdf")

        log(f"Job {job_id}: LaTeX Error. Output: {result.stdout[-500:]}")
        return jsonify({
            "error": "LaTeX build failed",
            "details": result.stdout[-500:],
            "stderr": result.stderr[-500:],
        }), 500

    except Exception as e:
        log(f"Critical error in compile job {job_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
