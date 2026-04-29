import os
import json
import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List

from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename
from openai import OpenAI

from main import build_latex_patch, build_cv_tex

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONTEXT_DIR = BASE_DIR / "context"
WORKDIR = BASE_DIR / "workdir"
LOG_FILE = BASE_DIR / "server_debug.log"

CONTEXT_DIR.mkdir(exist_ok=True)
WORKDIR.mkdir(exist_ok=True)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
PDFLATEX_PATH = os.environ.get("PDFLATEX_PATH", "pdflatex")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

ASSET_FILENAMES = [
    "photo.jpg",
    "logo_cefipa.png",
    "logo_cesi.png",
]


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "letter": {
            "type": "string"
        },
        "objective": {
            "type": "string"
        },
        "apl_items": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "skills": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "languages": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                },
                "embedded": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                },
                "tools": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            },
            "required": [
                "languages",
                "embedded",
                "tools"
            ]
        },
        "notes": {
            "type": "array",
            "items": {
                "type": "string"
            }
        }
    },
    "required": [
        "letter",
        "objective",
        "apl_items",
        "skills",
        "notes"
    ]
}


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def copy_latex_assets(job_dir: Path) -> None:
    for filename in ASSET_FILENAMES:
        candidates = [
            BASE_DIR / filename,
            CONTEXT_DIR / filename,
        ]

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                shutil.copy(candidate, job_dir / filename)
                break


def compile_tex_string(cv_tex: str, output_name: str = "CV.pdf"):
    job_id = str(uuid.uuid4())
    job_dir = WORKDIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    tex_path = job_dir / "CV.tex"
    tex_path.write_text(cv_tex, encoding="utf-8")
    copy_latex_assets(job_dir)

    cmd = [PDFLATEX_PATH, "-interaction=nonstopmode", "CV.tex"]

    subprocess.run(cmd, cwd=str(job_dir), capture_output=True, text=True)
    result = subprocess.run(cmd, cwd=str(job_dir), capture_output=True, text=True)

    pdf_path = job_dir / "CV.pdf"

    if pdf_path.exists():
        return send_file(
            str(pdf_path),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=output_name,
        )

    return jsonify({
        "error": "LaTeX build failed",
        "stdout": result.stdout[-3000:],
        "stderr": result.stderr[-3000:],
        "job_dir": str(job_dir),
        "job_files": [p.name for p in job_dir.iterdir()],
    }), 500


def read_pdf(path: Path) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed. Run: pip install pypdf")

    reader = PdfReader(str(path))
    pages = []

    for page in reader.pages:
        pages.append(page.extract_text() or "")

    return "\n\n".join(pages).strip()


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def read_context_file(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return read_pdf(path)

    if suffix in [".txt", ".md", ".tex", ".json", ".csv"]:
        return read_text_file(path)

    return ""


def list_context_files() -> List[Dict[str, Any]]:
    files = []

    for path in sorted(CONTEXT_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name.startswith("."):
            continue

        files.append({
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "suffix": path.suffix.lower(),
        })

    return files


def load_context_text(max_chars_per_file: int = 9000) -> List[Dict[str, str]]:
    docs = []

    for path in sorted(CONTEXT_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name.startswith("."):
            continue

        try:
            text = read_context_file(path)
        except Exception as e:
            docs.append({
                "filename": path.name,
                "error": str(e),
                "text": "",
            })
            continue

        if text.strip():
            docs.append({
                "filename": path.name,
                "text": text[:max_chars_per_file],
            })

    return docs


def generate_with_openai(payload: Dict[str, Any]) -> Dict[str, Any]:
    if client is None:
        raise RuntimeError("Missing OPENAI_API_KEY")

    system_prompt = """
Tu es CoverAI, un assistant de candidature pour stages en systèmes embarqués et IA appliquée.

Tu dois produire uniquement des données sémantiques minimales en JSON.
Tu ne dois jamais produire de commande LaTeX.
Tu ne dois jamais produire de champ nommé cv_blocks ou latex_patch.
Python transformera localement tes champs en LaTeX.

Utilise uniquement les faits présents dans les documents de contexte et dans l'offre.
N'invente pas d'expérience, d'école, de diplôme, de date, de compétence ou de projet.
Favorise Python, C, C++, Linux, capteurs, MQTT, ESP8266, ESP32, Raspberry Pi, Flask, SQLite, Git, automatisation, systèmes embarqués et IA appliquée seulement si ces éléments sont présents dans le contexte.

La lettre doit être directe, crédible, courte, sans phrases creuses.
L'objectif doit tenir en une phrase.
Les trois items APL doivent être courts et orientés action.
Les compétences doivent être classées en trois listes plates: languages, embedded, tools.
""".strip()

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "coverai_output",
                "schema": OUTPUT_SCHEMA,
                "strict": True,
            }
        },
        store=False,
    )

    return json.loads(response.output_text)


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "app": "CoverAI",
        "status": "online",
        "model": OPENAI_MODEL,
        "routes": [
            "/health",
            "/context/list",
            "/context/upload",
            "/latex/preview",
            "/latex/compile",
            "/generate",
            "/generate/pdf",
            "/compile",
            "/logs",
        ],
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "openai_configured": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "context_files": len(list_context_files()),
        "assets": {
            filename: any((base / filename).exists() for base in [BASE_DIR, CONTEXT_DIR])
            for filename in ASSET_FILENAMES
        },
    })


@app.route("/logs", methods=["GET"])
def logs():
    if not LOG_FILE.exists():
        return "No logs yet.", 404

    return LOG_FILE.read_text(encoding="utf-8", errors="ignore"), 200, {
        "Content-Type": "text/plain"
    }


@app.route("/context/list", methods=["GET"])
def context_list():
    return jsonify({
        "context_dir": str(CONTEXT_DIR),
        "files": list_context_files(),
    })


@app.route("/context/upload", methods=["POST"])
def context_upload():
    if "file" not in request.files:
        return jsonify({"error": "file is required"}), 400

    uploaded = request.files["file"]

    if not uploaded.filename:
        return jsonify({"error": "filename is required"}), 400

    filename = secure_filename(uploaded.filename)

    if not filename:
        return jsonify({"error": "invalid filename"}), 400

    destination = CONTEXT_DIR / filename
    uploaded.save(destination)

    return jsonify({
        "ok": True,
        "filename": filename,
        "saved_to": str(destination),
        "size_bytes": destination.stat().st_size,
    })


@app.route("/latex/preview", methods=["POST"])
def latex_preview():
    data = request.get_json(force=True)

    company = data.get("company", "")
    role_title = data.get("role_title", "")
    semantic_data = data.get("data", data)

    try:
        latex_patch = build_latex_patch(semantic_data, company, role_title)
        cv_tex = build_cv_tex(semantic_data, company, role_title)

        return jsonify({
            "latex_patch": latex_patch,
            "cv_tex": cv_tex,
        })

    except Exception as e:
        log(f"latex preview failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/latex/compile", methods=["POST"])
def latex_compile_from_json():
    data = request.get_json(force=True)

    company = data.get("company", "")
    role_title = data.get("role_title", "")
    semantic_data = data.get("data", data)

    try:
        cv_tex = build_cv_tex(semantic_data, company, role_title)
        return compile_tex_string(cv_tex)

    except Exception as e:
        log(f"latex compile failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True)

    job_offer_text = data.get("job_offer_text", "")
    company = data.get("company", "")
    role_title = data.get("role_title", "")
    language = data.get("language", "fr")

    if not job_offer_text:
        return jsonify({"error": "job_offer_text is required"}), 400

    try:
        context_documents = load_context_text()

        payload = {
            "company": company,
            "role_title": role_title,
            "language": language,
            "job_offer_text": job_offer_text[:6000],
            "context_documents": context_documents,
        }

        log(f"Generating application with {len(context_documents)} context files.")
        result = generate_with_openai(payload)

        result["cv_blocks"] = {
            "objectif": result.get("objective", ""),
            "apl_items": result.get("apl_items", []),
            "competences": result.get("skills", {}),
        }
        result["latex_patch"] = build_latex_patch(result, company, role_title)

        return jsonify(result)

    except Exception as e:
        log(f"generate failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/generate/pdf", methods=["POST"])
def generate_pdf():
    data = request.get_json(force=True)

    job_offer_text = data.get("job_offer_text", "")
    company = data.get("company", "")
    role_title = data.get("role_title", "")
    language = data.get("language", "fr")

    if not job_offer_text:
        return jsonify({"error": "job_offer_text is required"}), 400

    try:
        context_documents = load_context_text()

        payload = {
            "company": company,
            "role_title": role_title,
            "language": language,
            "job_offer_text": job_offer_text[:6000],
            "context_documents": context_documents,
        }

        log(f"Generating PDF application with {len(context_documents)} context files.")
        result = generate_with_openai(payload)
        cv_tex = build_cv_tex(result, company, role_title)
        return compile_tex_string(cv_tex)

    except Exception as e:
        log(f"generate_pdf failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/compile", methods=["POST"])
def compile_tex():
    job_id = str(uuid.uuid4())
    job_dir = WORKDIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        if not request.files:
            return jsonify({"error": "No file content detected"}), 400

        for key in request.files:
            for uploaded in request.files.getlist(key):
                if not uploaded.filename:
                    continue

                filename = secure_filename(uploaded.filename)
                destination = job_dir / filename
                uploaded.save(destination)

        copy_latex_assets(job_dir)
        tex_path = job_dir / "CV.tex"

        if not tex_path.exists():
            return jsonify({
                "error": "CV.tex not found",
                "received_files": [p.name for p in job_dir.iterdir()],
            }), 400

        cmd = [PDFLATEX_PATH, "-interaction=nonstopmode", "CV.tex"]

        subprocess.run(cmd, cwd=str(job_dir), capture_output=True, text=True)
        result = subprocess.run(cmd, cwd=str(job_dir), capture_output=True, text=True)

        pdf_path = job_dir / "CV.pdf"

        if pdf_path.exists():
            return send_file(
                str(pdf_path),
                mimetype="application/pdf",
                as_attachment=True,
                download_name="CV.pdf",
            )

        return jsonify({
            "error": "LaTeX build failed",
            "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-3000:],
            "job_dir": str(job_dir),
        }), 500

    except Exception as e:
        log(f"compile failed: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9090, debug=True)