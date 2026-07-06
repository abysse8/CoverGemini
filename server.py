import os
import json
import uuid
import shutil
import subprocess
import threading
import time
import sys
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, request, jsonify, send_file, abort
from werkzeug.utils import secure_filename
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

from coverai.automation import OfferAutomationRunner
from coverai.explorer import load_config, report_offer_by_sms, run_offer_explorer
from coverai.platforms import check_platform_session, prepare_login_session
from coverai.sms_bridge import RutWorkbenchSmsClient
from coverai.sms_commands import handle_coverai_sms
from coverai.storage import DEFAULT_USER_ID, CoverAiStore
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


def load_local_env(path: Path) -> None:
    if not path.exists():
        return
    if load_dotenv is not None:
        load_dotenv(path)
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


load_local_env(BASE_DIR / ".env")

CONTEXT_DIR  = BASE_DIR / "context"
WORKDIR      = BASE_DIR / "workdir"
LOG_FILE     = BASE_DIR / "server_debug.log"
DB_PATH      = Path(os.environ.get("COVERAI_DB_PATH", str(BASE_DIR / "coverai.db"))).expanduser()
DEFAULT_JOB_SEARCH_CONFIG = Path(os.environ.get("COVERAI_JOB_SEARCH_CONFIG", str(BASE_DIR / "config" / "job_search.json"))).expanduser()

CONTEXT_DIR.mkdir(exist_ok=True)
WORKDIR.mkdir(exist_ok=True)

# Extract the key, strip surrounding whitespace, and filter out non-ASCII characters
_raw_key = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_API_KEY = "".join(c for c in _raw_key if ord(c) < 128)
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
PDFLATEX       = os.environ.get("PDFLATEX_PATH", "pdflatex")
SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "")

MQTT_HOST   = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER   = "".join(c for c in os.environ.get("MQTT_USER", "abysse8").strip() if ord(c) < 128)
MQTT_PASS   = "".join(c for c in os.environ.get("MQTT_PASS", "").strip() if ord(c) < 128)
MQTT_PREFIX = "abysse8/coverai"

ASSET_FILENAMES = ["photo.jpg", "logo_cefipa.png", "logo_cesi.png"]

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "company":    {"type": "string"},
        "role_title": {"type": "string"},
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
    "required": ["company", "role_title", "letter", "objective", "apl_items", "skills", "notes"],
}

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY and OpenAI is not None else None
mqtt_client = None
coverai_store = CoverAiStore(DB_PATH)
automation_runner: OfferAutomationRunner | None = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    try:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # Fallback for encoding errors during logging
        try:
            line = f"[{datetime.now().strftime('%H:%M:%S')}] {message.encode('ascii', errors='replace').decode('ascii')}"
            print(line, flush=True)
        except Exception:
            pass


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
# CoverAI explorer helpers
# ---------------------------------------------------------------------------

def selected_config_path(value: str = "") -> Path:
    return Path(value).expanduser() if value else DEFAULT_JOB_SEARCH_CONFIG


def build_sms_client() -> RutWorkbenchSmsClient:
    return RutWorkbenchSmsClient()


def get_automation_runner() -> OfferAutomationRunner:
    global automation_runner
    if automation_runner is None:
        automation_runner = OfferAutomationRunner(
            coverai_store,
            DEFAULT_JOB_SEARCH_CONFIG,
            openai_client_getter=lambda: openai_client,
            model_getter=lambda: OPENAI_MODEL,
            sms_client_factory=build_sms_client,
            logger=log,
            user_id=DEFAULT_USER_ID,
        )
    return automation_runner


def default_sms_number(config_path: Path) -> str:
    try:
        config = load_config(config_path)
    except Exception:
        return ""
    sms_config = config.get("sms") if isinstance(config.get("sms"), dict) else {}
    return str(sms_config.get("number") or "")


def request_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def require_user(user_id: str):
    user = coverai_store.get_user(user_id)
    if not user:
        return None
    return user


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


def get_base_url():
    """Returns the base URL, prioritising environment variable, then the current request's host."""
    if SERVER_BASE_URL:
        return SERVER_BASE_URL.rstrip('/')
    if request:
        # request.url_root includes scheme and host (e.g., https://xyz.ngrok-free.app/)
        return request.url_root.rstrip('/')
    return "http://localhost:9090"


def file_url(job_id: str, slot: str, filename: str) -> str:
    return f"{get_base_url()}/jobs/{job_id}/files/{slot}/{filename}"


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
    response = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": (
                "Tu es CoverAI, un assistant de candidature pour stages en systèmes embarqués et IA appliquée.\n"
                "Produis uniquement des données sémantiques minimales en JSON. Jamais de commandes LaTeX.\n"
                "Extrais impérativement le nom de l'entreprise ('company') et le titre du poste ('role_title') de l'offre.\n"
                "Utilise uniquement les faits présents dans les documents de contexte (CV, DC, etc.) et dans l'offre.\n"
                "N'invente rien. La lettre doit être structurée en exactement 3 paragraphes distincts, professionnelle et percutante.\n"
                "L'objectif doit tenir en une phrase."
            )},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "coverai_output", "schema": OUTPUT_SCHEMA, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)


def compile_latex(job_id: str, cv_tex: str) -> Path | None:
    out_dir = job_dir(job_id) / "output"
    tex_path = out_dir / "CV.tex"
    tex_path.write_text(cv_tex, encoding="utf-8")
    copy_assets(out_dir)

    # Added --enable-installer for MiKTeX and captured stderr
    cmd = [PDFLATEX, "-interaction=nonstopmode", "--enable-installer", "CV.tex"]
    
    # Run once
    subprocess.run(cmd, cwd=out_dir, capture_output=True)
    # Run twice (standard for LaTeX to resolve references)
    result = subprocess.run(cmd, cwd=out_dir, capture_output=True)

    pdf = out_dir / "CV.pdf"
    if pdf.exists():
        return pdf

    # Log full output for debugging
    log(f"[{job_id}] pdflatex failed. STDOUT:\n{result.stdout.decode()[-1000:]}")
    log(f"[{job_id}] pdflatex failed. STDERR:\n{result.stderr.decode()[-1000:]}")
    return None


def run_generation(job_id: str, offer: str, language: str) -> None:
    try:
        write_status(job_id, "processing")
        mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/status", {"job_id": job_id, "state": "processing"})

        log(f"[{job_id}] calling OpenAI...")
        context = load_context_text()
        
        # Ensure payload is UTF-8 encoded. The OpenAI client handles UTF-8 by default.
        payload = {
            "language": language,
            "job_offer_text": offer[:6000],
            "context_documents": context,
        }
        
        result = call_openai(payload)

        company = result.get("company", "Unknown Company")
        role = result.get("role_title", "Unknown Role")

        semantic_path = job_dir(job_id) / "output" / "semantic.json"
        semantic_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

        log(f"[{job_id}] compiling LaTeX for {company} - {role}...")
        cv_tex = build_cv_tex(result, company, role)
        pdf = compile_latex(job_id, cv_tex)

        if pdf:
            pdf_url = file_url(job_id, "output", "CV.pdf")
            outputs = {
                "tex": file_url(job_id, "output", "CV.tex"),
                "pdf": pdf_url,
            }
            letter_text = result.get("letter", "")
            
            # Shortcut-friendly list: [job_title, company, pdf_url, letter_text]
            shortcut_list = [role, company, pdf_url, letter_text]

            write_status(job_id, "done", 
                         outputs=outputs, 
                         letter=letter_text, 
                         company=company, 
                         role_title=role,
                         shortcut_list=shortcut_list)
            
            mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/status", {"job_id": job_id, "state": "done", "letter": letter_text})
            mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/result", {"job_id": job_id, "outputs": outputs, "letter": letter_text})
            log(f"[{job_id}] done → {outputs['pdf']}")
        else:
            write_status(job_id, "failed", error="pdflatex compilation failed")
            mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/error", {"job_id": job_id, "error": "pdflatex failed"})

    except Exception as e:
        log(f"[{job_id}] error: {type(e).__name__}: {e}")
        import traceback
        log(traceback.format_exc())
        write_status(job_id, "failed", error=str(e))
        mqtt_pub(f"{MQTT_PREFIX}/jobs/{job_id}/error", {"job_id": job_id, "error": str(e)})


# ---------------------------------------------------------------------------
# Routes — job lifecycle
# ---------------------------------------------------------------------------

@app.route("/jobs", methods=["POST"])
def jobs_create():
    jid = create_job()
    mqtt_pub(f"{MQTT_PREFIX}/jobs/new", {"job_id": jid})
    base_url = get_base_url()
    return jsonify({
        "job_id": jid,
        "status_url": f"{base_url}/jobs/{jid}",
        "files_url": f"{base_url}/jobs/{jid}/files",
    }), 201


@app.route("/jobs/<job_id>", methods=["GET"])
def jobs_status(job_id):
    wait = request.args.get("wait") == "1"
    timeout = 60
    start_time = time.time()
    
    while wait:
        status = read_status(job_id)
        if status.get("state") in ("done", "failed"):
            return jsonify(status)
        
        if (time.time() - start_time) > timeout:
            return jsonify(status)
            
        time.sleep(2)
        
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

    language = data.get("language", "fr")
    sync = request.args.get("sync") == "1"

    jid = create_job()
    (job_dir(jid) / "input" / "job_offer.txt").write_text(offer, encoding="utf-8")

    mqtt_pub(f"{MQTT_PREFIX}/jobs/new", {
        "job_id": jid,
        "action": "coverai.generate_pdf",
        "input_url": file_url(jid, "input", "job_offer.txt"),
    })

    if sync:
        # Run in-thread for synchronous response
        run_generation(jid, offer, language)
        status = read_status(jid)
        if status.get("state") == "done":
            return jsonify(status.get("shortcut_list", []))
        return jsonify({"error": status.get("error", "Generation failed")}), 500

    threading.Thread(
        target=run_generation,
        args=(jid, offer, language),
        daemon=True,
    ).start()

    base_url = get_base_url()
    return jsonify({
        "job_id": jid,
        "state": "processing",
        "status_url": f"{base_url}/jobs/{jid}",
        "poll_url":   f"{base_url}/jobs/{jid}",
        "pdf_url":    f"{base_url}/jobs/{jid}/files/output/CV.pdf",
        "tex_url":    f"{base_url}/jobs/{jid}/files/output/CV.tex",
        "shortcut_friendly": f"{base_url}/jobs/{jid}?wait=1"
    }), 202


# ---------------------------------------------------------------------------
# Routes — users and platforms
# ---------------------------------------------------------------------------

@app.route("/users/me", methods=["GET"])
def users_me():
    return jsonify({"user": coverai_store.get_user(DEFAULT_USER_ID)})


@app.route("/platforms", methods=["GET"])
def platforms_list():
    return jsonify({"platforms": coverai_store.list_platforms()})


@app.route("/users/<user_id>/platforms", methods=["GET"])
def user_platforms(user_id):
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    return jsonify({"user": coverai_store.get_user(user_id), "accounts": coverai_store.user_platform_accounts(user_id)})


@app.route("/users/<user_id>/platforms/<platform_id>/login-session", methods=["POST"])
def platform_login_session(user_id, platform_id):
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    if not coverai_store.get_platform(platform_id):
        return jsonify({"error": "platform not found"}), 404
    data = request.get_json(silent=True) or {}
    result = prepare_login_session(
        coverai_store,
        user_id,
        platform_id,
        BASE_DIR,
        launch=bool(data.get("launch", False)),
    )
    return jsonify(result)


@app.route("/users/<user_id>/platforms/<platform_id>/check-session", methods=["POST"])
def platform_check_session(user_id, platform_id):
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    if not coverai_store.get_platform(platform_id):
        return jsonify({"error": "platform not found"}), 404
    result = check_platform_session(coverai_store, user_id, platform_id, BASE_DIR)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Routes — offer explorer
# ---------------------------------------------------------------------------

@app.route("/explorer/run", methods=["POST"])
def explorer_run():
    return user_explorer_run(DEFAULT_USER_ID)


@app.route("/users/<user_id>/explorer/run", methods=["POST"])
def user_explorer_run(user_id):
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    data = request.get_json(silent=True) or {}
    config_path = selected_config_path(str(data.get("config_path") or ""))
    result = run_offer_explorer(
        coverai_store,
        config_path,
        openai_client=openai_client,
        model=OPENAI_MODEL,
        sms_client=build_sms_client(),
        user_id=user_id,
    )
    status = result.get("run", {}).get("status")
    code = 500 if status == "failed" else 200
    return jsonify(result), code


@app.route("/explorer/status", methods=["GET"])
def explorer_status():
    latest = coverai_store.latest_explorer_run(DEFAULT_USER_ID)
    return jsonify({"run": latest})


@app.route("/automation/status", methods=["GET"])
def automation_status():
    runner = get_automation_runner()
    return jsonify({
        "automation": runner.status(),
        "latest_explorer_run": coverai_store.latest_explorer_run(DEFAULT_USER_ID),
    })


@app.route("/automation/run-now", methods=["POST"])
def automation_run_now():
    data = request.get_json(silent=True) or {}
    runner = get_automation_runner()
    trigger = str(data.get("trigger") or "manual")
    if request_truthy(data.get("async", False)):
        result = runner.run_async(trigger)
        code = 202 if result.get("started") else 409
        return jsonify(result), code
    result = runner.run_once(trigger)
    code = 409 if result.get("skipped") == "already_running" else 200
    if result.get("result", {}).get("run", {}).get("status") == "failed":
        code = 500
    return jsonify(result), code


@app.route("/sms/inbound", methods=["POST"])
def sms_inbound():
    data = request.get_json(force=True)
    sender = str(data.get("sender") or data.get("from") or data.get("number") or data.get("phone") or "")
    message = str(data.get("message") or data.get("text") or data.get("sms") or data.get("body") or "")
    user_id = str(data.get("user_id") or DEFAULT_USER_ID)
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    if not sender or not message:
        return jsonify({"error": "sender and message are required"}), 400
    result = handle_coverai_sms(
        coverai_store,
        sender,
        message,
        DEFAULT_JOB_SEARCH_CONFIG,
        build_sms_client(),
        openai_client=openai_client,
        model=OPENAI_MODEL,
        automation_runner=get_automation_runner(),
        user_id=user_id,
    )
    return jsonify(result)


@app.route("/offers", methods=["GET"])
def offers_list():
    return user_offers_list(DEFAULT_USER_ID)


@app.route("/users/<user_id>/offers", methods=["GET"])
def user_offers_list(user_id):
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    status = request.args.get("status", "")
    limit = int(request.args.get("limit", "50") or "50")
    min_score_arg = request.args.get("min_score", "")
    min_score = int(min_score_arg) if min_score_arg else None
    return jsonify({"offers": coverai_store.list_offers(status=status, limit=limit, min_score=min_score, user_id=user_id)})


@app.route("/offers/<offer_id>", methods=["GET"])
def offer_get(offer_id):
    offer = coverai_store.get_offer(offer_id)
    if not offer:
        return jsonify({"error": "offer not found"}), 404
    return jsonify({"offer": offer})


@app.route("/offers/<offer_id>/sms-report", methods=["POST"])
def offer_sms_report(offer_id):
    return user_offer_sms_report(DEFAULT_USER_ID, offer_id)


@app.route("/users/<user_id>/offers/<offer_id>/sms-report", methods=["POST"])
def user_offer_sms_report(user_id, offer_id):
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    offer = coverai_store.get_offer(offer_id)
    if not offer or offer.get("user_id") != user_id:
        return jsonify({"error": "offer not found"}), 404
    data = request.get_json(silent=True) or {}
    number = str(data.get("number") or default_sms_number(DEFAULT_JOB_SEARCH_CONFIG) or "")
    if not number:
        return jsonify({"error": "SMS number is required in request body or config/job_search.json"}), 400
    report = report_offer_by_sms(coverai_store, offer_id, number, build_sms_client(), user_id=user_id)
    if report.get("status") == "sent":
        coverai_store.mark_offer_status(offer_id, "reported", user_id=user_id)
    code = 502 if report.get("status") == "failed" else 200
    return jsonify({"report": report, "offer": coverai_store.get_offer(offer_id)}), code


@app.route("/offers/<offer_id>/status", methods=["POST"])
def offer_status_update(offer_id):
    return user_offer_status_update(DEFAULT_USER_ID, offer_id)


@app.route("/users/<user_id>/offers/<offer_id>/status", methods=["POST"])
def user_offer_status_update(user_id, offer_id):
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    data = request.get_json(force=True)
    status = str(data.get("status") or "").strip()
    if not status:
        return jsonify({"error": "status is required"}), 400
    try:
        offer = coverai_store.mark_offer_status(offer_id, status, user_id=user_id)
    except KeyError:
        return jsonify({"error": "offer not found"}), 404
    return jsonify({"offer": offer})


def resolve_submission_application(user_id: str, application_id: str = "", offer_id: str = "", reference: str = ""):
    if application_id:
        return coverai_store.get_application_task(application_id, user_id=user_id)
    if offer_id:
        return coverai_store.get_application_for_offer(offer_id, user_id=user_id)
    if reference:
        if reference.startswith("app_"):
            return coverai_store.get_application_task(reference, user_id=user_id)
        offer = coverai_store.find_offer_by_reference(reference, user_id=user_id)
        if offer:
            return coverai_store.get_application_for_offer(offer["id"], user_id=user_id)
    applications = coverai_store.list_application_tasks(limit=1, user_id=user_id)
    return applications[0] if applications else None


@app.route("/submission-packets", methods=["GET"])
def submission_packet_get():
    user_id = str(request.args.get("user_id") or DEFAULT_USER_ID)
    if not require_user(user_id):
        return jsonify({"error": "user not found"}), 404
    application_id = str(request.args.get("application_id") or "").strip()
    offer_id = str(request.args.get("offer_id") or "").strip()
    reference = str(request.args.get("reference") or "").strip()
    app_task = resolve_submission_application(user_id, application_id, offer_id, reference)
    if not app_task:
        return jsonify({"error": "application not found"}), 404
    return jsonify({"packet": coverai_store.application_submission_packet(app_task["id"], user_id=user_id)})


@app.route("/offers/<offer_id>/submission-packet", methods=["GET"])
def offer_submission_packet_get(offer_id):
    app_task = coverai_store.get_application_for_offer(offer_id, user_id=DEFAULT_USER_ID)
    if not app_task:
        return jsonify({"error": "application not found"}), 404
    return jsonify({"packet": coverai_store.application_submission_packet(app_task["id"], user_id=DEFAULT_USER_ID)})


@app.route("/applications", methods=["GET"])
def applications_list():
    status = request.args.get("status", "")
    limit = int(request.args.get("limit", "20") or "20")
    return jsonify({"applications": coverai_store.list_application_tasks(status=status, limit=limit, user_id=DEFAULT_USER_ID)})


@app.route("/applications", methods=["POST"])
def application_create():
    data = request.get_json(force=True)
    offer_id = str(data.get("offer_id") or "").strip()
    reference = str(data.get("reference") or "").strip()
    offer = coverai_store.get_offer(offer_id) if offer_id else coverai_store.find_offer_by_reference(reference, user_id=DEFAULT_USER_ID)
    if not offer:
        return jsonify({"error": "offer not found"}), 404
    app_task, created = coverai_store.upsert_application_task(offer["id"], user_id=DEFAULT_USER_ID)
    questions = coverai_store.list_application_questions(app_task["id"], user_id=DEFAULT_USER_ID)
    return jsonify({"application": app_task, "questions": questions, "created": created}), 201 if created else 200


@app.route("/applications/<application_id>", methods=["GET"])
def application_get(application_id):
    app_task = coverai_store.get_application_task(application_id, user_id=DEFAULT_USER_ID)
    if not app_task:
        return jsonify({"error": "application not found"}), 404
    app_task = coverai_store.recalculate_application_readiness(application_id, user_id=DEFAULT_USER_ID)
    questions = coverai_store.list_application_questions(application_id, user_id=DEFAULT_USER_ID)
    return jsonify({"application": app_task, "questions": questions})


@app.route("/applications/<application_id>/submission-packet", methods=["GET"])
def application_submission_packet_get(application_id):
    app_task = coverai_store.get_application_task(application_id, user_id=DEFAULT_USER_ID)
    if not app_task:
        return jsonify({"error": "application not found"}), 404
    return jsonify({"packet": coverai_store.application_submission_packet(application_id, user_id=DEFAULT_USER_ID)})


@app.route("/applications/<application_id>/questions/next-answer", methods=["POST"])
def application_answer_next(application_id):
    data = request.get_json(force=True)
    answer = str(data.get("answer") or "").strip()
    if not answer:
        return jsonify({"error": "answer is required"}), 400
    if not coverai_store.get_application_task(application_id, user_id=DEFAULT_USER_ID):
        return jsonify({"error": "application not found"}), 404
    question = coverai_store.answer_next_application_question(application_id, answer, user_id=DEFAULT_USER_ID)
    app_task = coverai_store.recalculate_application_readiness(application_id, user_id=DEFAULT_USER_ID)
    questions = coverai_store.list_application_questions(application_id, user_id=DEFAULT_USER_ID)
    return jsonify({"answered": question, "application": app_task, "questions": questions})


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
            "GET  /users/me",
            "GET  /platforms",
            "GET  /users/<id>/platforms",
            "POST /users/<id>/platforms/<platform>/login-session",
            "POST /users/<id>/platforms/<platform>/check-session",
            "POST /explorer/run",
            "POST /users/<id>/explorer/run",
            "GET  /explorer/status",
            "GET  /automation/status",
            "POST /automation/run-now",
            "POST /sms/inbound",
            "GET  /offers",
            "GET  /users/<id>/offers",
            "GET  /offers/<id>",
            "POST /offers/<id>/sms-report",
            "POST /offers/<id>/status",
            "GET  /applications",
            "POST /applications",
            "GET  /applications/<id>",
            "GET  /applications/<id>/submission-packet",
            "GET  /offers/<id>/submission-packet",
            "GET  /submission-packets",
            "POST /applications/<id>/questions/next-answer",
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
        "db_path": str(DB_PATH),
        "job_search_config": str(DEFAULT_JOB_SEARCH_CONFIG),
        "default_user": coverai_store.get_user(DEFAULT_USER_ID),
        "platforms": len(coverai_store.list_platforms()),
        "latest_explorer_run": coverai_store.latest_explorer_run(DEFAULT_USER_ID),
        "automation": get_automation_runner().status(),
        "applications": len(coverai_store.list_application_tasks(limit=100, user_id=DEFAULT_USER_ID)),
    })


@app.route("/logs", methods=["GET"])
def logs():
    if not LOG_FILE.exists():
        return "No logs yet.", 404
    return LOG_FILE.read_text(encoding="utf-8", errors="ignore"), 200, {"Content-Type": "text/plain"}


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    init_mqtt()
    stale_runs = coverai_store.mark_stale_explorer_runs()
    if stale_runs:
        log(f"Marked {stale_runs} stale explorer run(s) as failed")
    get_automation_runner().start()
    app.run(host="0.0.0.0", port=9090, debug=False, threaded=True)
