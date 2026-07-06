# CoverAI: Job Explorer + LaTeX Resume Pipeline

**CoverAI** is a mobile-to-local automation suite for finding job offers, reporting good matches by SMS, and generating tailored LaTeX CV/application material. The existing Apple Shortcuts flow still posts extracted job-offer text to the Flask server and receives a ready-to-preview PDF.

![CoverGemini Demo](assets/demo.gif)

## Current Workflow
1. **Extraction**: An iOS Shortcut captures a job offer's HTML and extracts text via JavaScript.
2. **Intelligence**: Content is sent to OpenAI with a strict JSON schema to generate tailored LaTeX sections (Objectif, Expériences, Compétences).
3. **Transmission**: The Shortcut uses Regex to split the AI response into LaTeX-safe components.
4. **Compilation**: Files are POSTed to a local Flask server that manages a dedicated build directory, handling assets (photo.jpg, logos) and multi-pass `pdflatex` compilation.
5. **Delivery**: The compiled PDF is returned to the iPhone for immediate preview.

## Job Explorer

The first agentic layer is a persistent job-offer explorer. It stores discovered offers in SQLite, scores them against the configured target profile, and can report high-scoring offers by SMS through the RUT241 Codex Workbench.

Configure `config/job_search.json`:

```json
{
  "keywords": ["alternance systemes embarques"],
  "locations": ["Ile-de-France"],
  "source_urls": [],
  "minimum_score": 65,
  "sms": {
    "enabled": false,
    "number": "+33123456789",
    "min_score": 70,
    "max_reports_per_run": 3
  }
}
```

Run the server:

```bash
python3 server.py
```

Run the explorer directly:

```bash
python3 -m coverai.explorer
```

Or through HTTP:

```bash
curl -X POST http://127.0.0.1:9090/explorer/run
curl http://127.0.0.1:9090/offers
curl http://127.0.0.1:9090/automation/status
curl -X POST http://127.0.0.1:9090/automation/run-now -H 'Content-Type: application/json' -d '{"async": true}'
```

SMS is not sent directly by CoverAI. CoverAI calls the existing RUT241 workbench:

```bash
export WORKBENCH_PUBLIC_URL=http://127.0.0.1:8765
export WORKBENCH_TOKEN=<token-printed-by-workbench>
```

To keep phone numbers out of git, put live SMS reporting settings in ignored `.env`:

```bash
COVERAI_SMS_ENABLED=true
COVERAI_SMS_NUMBER=+<your-phone-number>
COVERAI_SMS_MIN_SCORE=70
COVERAI_SMS_MAX_REPORTS_PER_RUN=5
COVERAI_AUTOMATION_ENABLED=true
COVERAI_AUTOMATION_INTERVAL_SECONDS=900
COVERAI_AUTOMATION_RUN_ON_START=false
```

When the RUT241 workbench is configured to forward inbound SMS to CoverAI, plain texts use the CoverAI job-market agent. The normal interface is natural language:

- `CAPABILITIES`: show what the agent can currently do
- `STATUS`: show scout loop, offer counts, latest application readiness, and current activity
- `OFFERS`: show offer counts and recent/top opportunities
- `QUEUE`: show application tasks and missing answers
- `scout for new embedded roles`
- `tell me about the Netatmo one`
- `is that worth applying to?`
- `start applying to the last one`
- `how ready is the application?`
- `research the company culture`

Internal offer ids still work as a fallback, but SMS cards no longer require the user to type them.

Codex CLI control remains available through the RUT workbench by prefixing commands with `codex`, for example `codex status`.

## Agent Queue and Readiness

CoverAI keeps deterministic tools and durable queue state underneath the AI conversation:

- **Scout**: discover offers, deduplicate them, score fit, and report strong matches.
- **Coach**: explain fit, culture signals from stored context, and application strategy.
- **Operator**: create application tasks and track missing answers. Browser fill/submission is not enabled yet.
- **Communicator**: keep SMS concise, resolve natural references like `the last one`, and ask one missing question at a time.

Application tasks are stored separately from offers and linked to queue items. Each task has child questions so readiness can be measured:

```text
Application readiness = required questions handled / total required questions
```

Useful HTTP endpoints:

```bash
curl http://127.0.0.1:9090/applications
curl -X POST http://127.0.0.1:9090/applications \
  -H 'Content-Type: application/json' \
  -d '{"reference": "Netatmo"}'
curl http://127.0.0.1:9090/applications/<application_id>
curl -X POST http://127.0.0.1:9090/applications/<application_id>/questions/next-answer \
  -H 'Content-Type: application/json' \
  -d '{"answer": "September 2026"}'
```

## MCP

Register CoverAI as a Codex MCP server:

```bash
codex mcp add coverai \
  --env COVERAI_BASE_URL=http://127.0.0.1:9090 \
  --env WORKBENCH_PUBLIC_URL=http://127.0.0.1:8765 \
  --env WORKBENCH_TOKEN=<rut-workbench-token> \
  -- python3 -m coverai.mcp_server
```

MCP tools:

- `run_offer_explorer`
- `list_offers`
- `get_offer`
- `send_offer_sms_report`
- `mark_offer_status`
- `get_explorer_status`
- `automation_status`
- `ask_coverai`
- `list_applications`
- `create_application_task`
- `get_application`
- `send_sms`

## 🛠️ Technical Stack
* **Language**: Python 3.11 (Flask)
* **Typesetting**: TeX Live / MacTeX (pdflatex)
* **Automation**: iOS Shortcuts + JavaScript + Playwright
* **AI**: OpenAI API
* **Networking**: ngrok (for local-to-mobile tunneling)

## 🔧 Technical Challenges & Solutions
* **Dynamic Data Partitioning**: Implemented custom Regular Expression (Regex) patterns within iOS Shortcuts to split non-deterministic AI responses into structured LaTeX components.
* **Headless Compilation**: Configured `pdflatex` in `nonstopmode` to handle compilation errors gracefully in a server-side environment.
* **Bilingual Signal Processing**: Optimized the pipeline for bilingual (French/English) technical profiles, ensuring UTF-8 encoding support for engineering terminology.

## Project Structure

- `server.py`: Flask server for CV generation, explorer endpoints, file downloads, and logs.
- `main.py`: LaTeX rendering helpers and CV template.
- `coverai/`: Agent, explorer, SQLite storage, RUT SMS bridge, automation runner, and MCP server.
- `config/job_search.json`: Seed keywords, locations, sources, and SMS reporting settings.
- `archive/legacy-20260630/`: Older prototype server files kept for reference.

## Development

```bash
python3 -m unittest discover -s tests
```
