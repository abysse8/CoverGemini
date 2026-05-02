import os
import json
import uuid
import shutil
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, request, jsonify, send_file, abort
from werkzeug.utils import secure_filename
from openai import OpenAI

from main import build_cv_tex

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    import paho.mqtt.client as _paho_check  # noqa: F401
    HAS_MQTT = True
except ImportError:
    HAS_MQTT = False


app = Flask(__name__)

BASE_DIR     = Path(__file__).resolve().parent
CONTEXT_DIR  = BASE_DIR / "context"
WORKDIR      = BASE_DIR / "workdir"
LOG_FILE     = BASE_DIR / "server_debug.log"

CONTEXT_DIR.mkdir(exist_ok=True)
WORKDIR.mkdir(exist_ok=True)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
PDFLATEX       = os.environ.get("PDFLATEX_PATH", "pdflatex")
SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://192.168.0.198:9090")

MQTT_HOST   = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER   = os.environ.get("MQTT_USER", "abysse8")
MQTT_PASS   = os.environ.get("MQTT_PASS", "")
MQTT_PREFIX = "abysse8/coverai"

ASSET_FILENAMES = ["photo.jpg", "logo_cefipa.png", "logo_cesi.png"]

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "letter":    {"type": "string"},
        "objective": {"type": "string"},
        "apl_items": {"type": "array", "items": {"type": "string"}},
        "skills": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "languages": {"type": "array", "items": {"type": "string"}},
                "embedded":  {"type": "array", "items": {"type": "string"}},
                "tools":     {"type": "array", "items": {"type": "string"}},
            },
            "required": ["languages", "embedded", "tools"],
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["letter", "objective", "apl_items", "skills", "notes"],
}

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
mqtt_client = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def init_mqtt() -> None:
    global mqtt_client
    if not HAS_MQTT:
        return
    try:
        import paho.mqtt.client as paho
        c = paho.Client(paho.CallbackAPIVersion.VERSION2)
        c.username_pw_set(MQTT_USER, MQTT_PASS)
        c.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        c.loop_start()
        mqtt_client = c
        log(f"MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
    except Exception as e:
        log(f"MQTT unavailable: {e} — continuing without it")


def mqtt_pub(topic: str, payload: dict, retain: bool = False) -> None:
    if mqtt_client is None:
        return
    try:
        mqtt_client.publish(topic, json.dumps(payload), qos=1, retain=retain)
    except Exception as e:
        log(f"MQTT publish failed: {e}")


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------

def job_dir(job_id: str) -> Path:
    return WORKDIR / job_id


def create_job(job_id: str | None = None) -> str:
    jid = job_id or str(uuid.uuid4())
    d = job_dir(jid)
    (d / "input").mkdir(parents=True, exist_ok=True)
    (d / "output").mkdir(parents=True, exist_ok=True)
    write_status(jid, "created")
    return jid


def read_status(job_id: str) -> dict:
    p = job_dir(job_id) / "status.json"
    if not p.exists():
        abort(404, description=f"job {job_id} not found")
    return json.loads(p.read_text())


def write_status(job_id: str, state: str, **extra) -> None:
    p = job_dir(job_id) / "status.json"
    existing = json.loads(p.read_text()) if p.exists() else {}
    existing.update({
        "job_id": job_id,
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    })
    if "created_at" not in existing:
        existing["created_at"] = existing["updated_at"]
    p.write_text(json.dumps(existing, indent=2, ensure_ascii=False))


def safe_file(base: Path, filename: str) -> Path:
    resolved = (base / filename).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        abort(400, description="invalid file path")
    return resolved


def list_dir(path: Path) -> list:
    if not path.exists():
        return []
    return [
        {"name": f.name, "size_bytes": f.stat().st_size}
        for f in sorted(path.iterdir())
        if f.is_file()
    ]


def file_url(job_id: str, slot: str, filename: str) -> str:
    return f"{SERVER_BASE_URL}/jobs/{job_id}/files/{slot}/{filename}"


# ---------------------------------------------------------------------------
# CV generation logic
# ---------------------------------------------------------------------------

def copy_assets(dest: Path) -> None:
    for name in ASSET_FILENAMES:
        for base in [BASE_DIR, CONTEXT_DIR]:
            src = base / name
            if src.exists():
                shutil.copy(src, dest / name)
                break


def load_context_text(max_chars: int = 9000) -> list:
    docs = []
    for path in sorted(CONTEXT_DIR.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            if path.suffix.lower() == ".pdf" and HAS_PYPDF:
                reader = PdfReader(str(path))
                text = "\n\n".join(p.extract_text() or "" for p in reader.pages).strip()
            elif path.suffix.lower() in [".txt", ".md", ".tex", ".json"]:
                text = path.read_text(encoding="utf-8", errors="ignore")
            else:
                continue
            if text.strip():
                docs.append({"filename": path.name, "text": text[:max_chars]})
        except Exception as e:
            log(f"context read error {path.name}: {e}")
    return docs


def call_openai(payload: dict) -> dict:
    if openai_client is None:
        raise RuntimeError("OPENAI_API_KEY not set")
    response = openai_client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": (
                "Tu es CoverAI, un assistant de candidature pour stages en systèmes embarqués et IA appliquée.\n"
                "Produis uniquement des données sémantiques minimales en JSON. Jamais de commandes LaTeX.\n"
                "Utilise uniquement les faits présents dans les documents de contexte et dans l'offre.\n"
                "N'invente rien. La lettre doit être directe, crédible, courte, sans phrases creuses.\n"
                "L'objectif doit tenir en une phrase."
            )},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text={"format": {"type": "json_schema", "name": "coverai_output", "schema": OUTPUT_SCHEMA, "strict": True}},
        store=False,
    )
    return json.loads(response.output_text)


def compile_latex(job_id: str, cv_tex: str) -> Path | None:
    out_dir = job_dir(job_id) / "output"
    tex_path = out_dir / "CV.tex"
    tex_path.write_text(cv_tex, encoding="utf-8")
    copy_assets(out_dir)

    cmd = [PDFLATEX, "-interaction=nonstopmode", "CV.tex"]
    subprocess.run(cmd, cwd=out_dir, capture_output=True)
    result = subprocess.run(cmd, cwd=out_dir, capture_output=True)

    pdf = out_dir / "CV.pdf"
    if pdf.exists():
        return pdf

    log(f"[{job_id}] pdflatex failed:\n{result.stdout.decode()[-2000:]}")
    return None


def run_generation(job_id: str, offer: str, company: str, role: str, language: str) -> None:
    try:
        write_status(job_id, "processing")
        mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/status", {"job_id": job_id, "state": "processing"})

        log(f"[{job_id}] calling OpenAI...")
        context = load_context_text()
        result = call_openai({
            "company": company,
            "role_title": role,
            "language": language,
            "job_offer_text": offer[:6000],
            "context_documents": context,
        })

        semantic_path = job_dir(job_id) / "output" / "semantic.json"
        semantic_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

        log(f"[{job_id}] compiling LaTeX...")
        cv_tex = build_cv_tex(result, company, role)
        pdf = compile_latex(job_id, cv_tex)

        if pdf:
            outputs = {
                "tex": file_url(job_id, "output", "CV.tex"),
                "pdf": file_url(job_id, "output", "CV.pdf"),
            }
            write_status(job_id, "done", outputs=outputs)
            mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/status", {"job_id": job_id, "state": "done"})
            mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/result", {"job_id": job_id, "outputs": outputs})
            log(f"[{job_id}] done → {outputs['pdf']}")
        else:
            write_status(job_id, "failed", error="pdflatex compilation failed")
            mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/error", {"job_id": job_id, "error": "pdflatex failed"})

    except Exception as e:
        log(f"[{job_id}] error: {e}")
        write_status(job_id, "failed", error=str(e))
        mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/error", {"job_id": job_id, "error": str(e)})


# ---------------------------------------------------------------------------
# Routes — job lifecycle
# ---------------------------------------------------------------------------

@app.route("/jobs", methods=["POST"])
def jobs_create():
    jid = create_job()
    mqtt_pub(f"{MQTT_PREFIX}/jobs/new", {"job_id": jid})
    return jsonify({
        "job_id": jid,
        "status_url": f"{SERVER_BASE_URL}/jobs/{jid}",
        "files_url": f"{SERVER_BASE_URL}/jobs/{jid}/files",
    }), 201


@app.route("/jobs/<job_id>", methods=["GET"])
def jobs_status(job_id):
    return jsonify(read_status(job_id))


@app.route("/jobs/<job_id>/files", methods=["GET"])
def jobs_files(job_id):
    d = job_dir(job_id)
    if not d.exists():
        abort(404)
    return jsonify({
        "job_id": job_id,
        "input":  list_dir(d / "input"),
        "output": list_dir(d / "output"),
    })


@app.route("/jobs/<job_id>/files/input", methods=["POST"])
def jobs_upload_input(job_id):
    d = job_dir(job_id) / "input"
    if not d.exists():
        abort(404)
    saved = []
    for key in request.files:
        for f in request.files.getlist(key):
            if not f.filename:
                continue
            name = secure_filename(f.filename)
            f.save(d / name)
            saved.append({"name": name, "url": file_url(job_id, "input", name)})
    return jsonify({"saved": saved})


@app.route("/jobs/<job_id>/files/output", methods=["POST"])
def jobs_upload_output(job_id):
    d = job_dir(job_id) / "output"
    if not d.exists():
        abort(404)
    saved = []
    for key in request.files:
        for f in request.files.getlist(key):
            if not f.filename:
                continue
            name = secure_filename(f.filename)
            f.save(d / name)
            saved.append({"name": name, "url": file_url(job_id, "output", name)})
    return jsonify({"saved": saved})


@app.route("/jobs/<job_id>/files/<slot>/<path:filename>", methods=["GET"])
def jobs_download(job_id, slot, filename):
    if slot not in ("input", "output"):
        abort(400)
    base = job_dir(job_id) / slot
    if not base.exists():
        abort(404)
    path = safe_file(base, filename)
    if not path.exists():
        abort(404)
    return send_file(str(path), as_attachment=True, download_name=path.name)


# ---------------------------------------------------------------------------
# Routes — generation
# ---------------------------------------------------------------------------

@app.route("/generate-job", methods=["POST"])
def generate_job():
    data = request.get_json(force=True)
    offer = data.get("job_offer_text", "")
    if not offer:
        return jsonify({"error": "job_offer_text is required"}), 400

    company  = data.get("company", "")
    role     = data.get("role_title", "")
    language = data.get("language", "fr")

    jid = create_job()
    (job_dir(jid) / "input" / "job_offer.txt").write_text(offer, encoding="utf-8")

    mqtt_pub(f"{MQTT_PREFIX}/jobs/new", {
        "job_id": jid,
        "action": "coverai.generate_pdf",
        "input_url": file_url(jid, "input", "job_offer.txt"),
    })

    threading.Thread(
        target=run_generation,
        args=(jid, offer, company, role, language),
        daemon=True,
    ).start()

    return jsonify({
        "job_id": jid,
        "state": "processing",
        "status_url": f"{SERVER_BASE_URL}/jobs/{jid}",
        "poll_url":   f"{SERVER_BASE_URL}/jobs/{jid}",
        "pdf_url":    f"{SERVER_BASE_URL}/jobs/{jid}/files/output/CV.pdf",
    }), 202


# ---------------------------------------------------------------------------
# Utility routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "app": "CoverAI",
        "status": "online",
        "model": OPENAI_MODEL,
        "routes": [
            "POST /generate-job",
            "POST /jobs",
            "GET  /jobs/<id>",
            "GET  /jobs/<id>/files",
            "POST /jobs/<id>/files/input",
            "POST /jobs/<id>/files/output",
            "GET  /jobs/<id>/files/<slot>/<filename>",
            "GET  /health",
            "GET  /logs",
        ],
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "openai_configured": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "mqtt_connected": mqtt_client is not None,
    })


@app.route("/logs", methods=["GET"])
def logs():
    if not LOG_FILE.exists():
        return "No logs yet.", 404
    return LOG_FILE.read_text(encoding="utf-8", errors="ignore"), 200, {"Content-Type": "text/plain"}


if __name__ == "__main__":
    init_mqtt()
    app.run(host="0.0.0.0", port=9090, debug=False, threaded=True)
