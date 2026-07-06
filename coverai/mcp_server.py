from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .sms_bridge import RutWorkbenchSmsClient

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]


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


class CoverAiHttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def run_offer_explorer(self, config_path: str = "", user_id: str = "") -> Any:
        payload = {"config_path": config_path} if config_path else {}
        path = f"/users/{urllib.parse.quote(user_id)}/explorer/run" if user_id else "/explorer/run"
        return self._request("POST", path, payload)

    def list_offers(self, status: str = "", limit: int = 20, min_score: int | None = None, user_id: str = "") -> Any:
        query: dict[str, str] = {"limit": str(limit)}
        if status:
            query["status"] = status
        if min_score is not None:
            query["min_score"] = str(min_score)
        path = f"/users/{urllib.parse.quote(user_id)}/offers" if user_id else "/offers"
        return self._request("GET", path, query=query)

    def get_offer(self, offer_id: str) -> Any:
        return self._request("GET", f"/offers/{urllib.parse.quote(offer_id)}")

    def send_offer_sms_report(self, offer_id: str, number: str = "", user_id: str = "") -> Any:
        payload = {"number": number} if number else {}
        if user_id:
            path = f"/users/{urllib.parse.quote(user_id)}/offers/{urllib.parse.quote(offer_id)}/sms-report"
        else:
            path = f"/offers/{urllib.parse.quote(offer_id)}/sms-report"
        return self._request("POST", path, payload)

    def mark_offer_status(self, offer_id: str, status: str, user_id: str = "") -> Any:
        if user_id:
            path = f"/users/{urllib.parse.quote(user_id)}/offers/{urllib.parse.quote(offer_id)}/status"
        else:
            path = f"/offers/{urllib.parse.quote(offer_id)}/status"
        return self._request("POST", path, {"status": status})

    def get_explorer_status(self) -> Any:
        return self._request("GET", "/explorer/status")

    def automation_status(self) -> Any:
        return self._request("GET", "/automation/status")

    def ask_coverai(self, message: str, sender: str = "", user_id: str = "") -> Any:
        payload = {"message": message, "sender": sender or os.environ.get("COVERAI_SMS_NUMBER", "mcp")}
        if sender:
            payload["sender"] = sender
        if user_id:
            payload["user_id"] = user_id
        return self._request("POST", "/sms/inbound", payload)

    def list_applications(self, status: str = "", limit: int = 20) -> Any:
        query = {"limit": str(limit)}
        if status:
            query["status"] = status
        return self._request("GET", "/applications", query=query)

    def create_application_task(self, offer_id: str = "", reference: str = "") -> Any:
        payload: dict[str, Any] = {}
        if offer_id:
            payload["offer_id"] = offer_id
        if reference:
            payload["reference"] = reference
        return self._request("POST", "/applications", payload)

    def get_application(self, application_id: str) -> Any:
        return self._request("GET", f"/applications/{urllib.parse.quote(application_id)}")

    def get_submission_packet(self, application_id: str = "", offer_id: str = "", reference: str = "", user_id: str = "") -> Any:
        query: dict[str, str] = {}
        if application_id:
            query["application_id"] = application_id
        if offer_id:
            query["offer_id"] = offer_id
        if reference:
            query["reference"] = reference
        if user_id:
            query["user_id"] = user_id
        return self._request("GET", "/submission-packets", query=query)

    def list_platforms(self) -> Any:
        return self._request("GET", "/platforms")

    def start_platform_login(self, platform_id: str, user_id: str = "julien", launch: bool = False) -> Any:
        return self._request(
            "POST",
            f"/users/{urllib.parse.quote(user_id)}/platforms/{urllib.parse.quote(platform_id)}/login-session",
            {"launch": launch},
        )

    def check_platform_login(self, platform_id: str, user_id: str = "julien") -> Any:
        return self._request(
            "POST",
            f"/users/{urllib.parse.quote(user_id)}/platforms/{urllib.parse.quote(platform_id)}/check-session",
            {},
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, query: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        body = None if method == "GET" else json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                parsed_error = json.loads(raw)
                message = parsed_error.get("error") or raw
            except json.JSONDecodeError:
                message = raw or error.reason
            raise RuntimeError(f"CoverAI HTTP {error.code}: {message}") from error
        if isinstance(parsed, dict) and "error" in parsed:
            raise RuntimeError(str(parsed["error"]))
        return parsed


class CoverAiMcpServer:
    def __init__(self, coverai_client: Any, sms_client: Any) -> None:
        self.coverai = coverai_client
        self.sms = sms_client

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if request_id is None and method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                result = self.initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                result = self.call_tool(params)
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                return self.error(request_id, -32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as error:
            return self.error(request_id, -32000, str(error))

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        protocol = str(params.get("protocolVersion") or "2024-11-05")
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "coverai", "version": "0.1.0"},
            "instructions": (
                "Use CoverAI tools to scout job offers, coach the user, create application tasks, "
                "and inspect readiness. This server does not submit applications. SMS is sent through the RUT241 workbench allowlist."
            ),
        }

    def call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name == "run_offer_explorer":
            result = self.coverai.run_offer_explorer(str(arguments.get("config_path") or ""), str(arguments.get("user_id") or ""))
        elif name == "list_offers":
            min_score = arguments.get("min_score")
            result = self.coverai.list_offers(
                status=str(arguments.get("status") or ""),
                limit=self.integer(arguments.get("limit"), 20),
                min_score=None if min_score in (None, "") else self.integer(min_score, 0),
                user_id=str(arguments.get("user_id") or ""),
            )
        elif name == "get_offer":
            result = self.coverai.get_offer(self.required(arguments, "offer_id"))
        elif name == "send_offer_sms_report":
            result = self.coverai.send_offer_sms_report(
                self.required(arguments, "offer_id"),
                str(arguments.get("number") or ""),
                str(arguments.get("user_id") or ""),
            )
        elif name == "mark_offer_status":
            result = self.coverai.mark_offer_status(
                self.required(arguments, "offer_id"),
                self.required(arguments, "status"),
                str(arguments.get("user_id") or ""),
            )
        elif name == "get_explorer_status":
            result = self.coverai.get_explorer_status()
        elif name == "automation_status":
            result = self.coverai.automation_status()
        elif name == "ask_coverai":
            result = self.coverai.ask_coverai(
                self.required(arguments, "message"),
                str(arguments.get("sender") or ""),
                str(arguments.get("user_id") or ""),
            )
        elif name == "list_applications":
            result = self.coverai.list_applications(
                status=str(arguments.get("status") or ""),
                limit=self.integer(arguments.get("limit"), 20),
            )
        elif name == "create_application_task":
            result = self.coverai.create_application_task(
                str(arguments.get("offer_id") or ""),
                str(arguments.get("reference") or ""),
            )
        elif name == "get_application":
            result = self.coverai.get_application(self.required(arguments, "application_id"))
        elif name == "get_submission_packet":
            result = self.coverai.get_submission_packet(
                str(arguments.get("application_id") or ""),
                str(arguments.get("offer_id") or ""),
                str(arguments.get("reference") or ""),
                str(arguments.get("user_id") or ""),
            )
        elif name == "list_platforms":
            result = self.coverai.list_platforms()
        elif name == "start_platform_login":
            result = self.coverai.start_platform_login(
                self.required(arguments, "platform_id"),
                str(arguments.get("user_id") or "julien"),
                bool(arguments.get("launch", False)),
            )
        elif name == "check_platform_login":
            result = self.coverai.check_platform_login(self.required(arguments, "platform_id"), str(arguments.get("user_id") or "julien"))
        elif name == "send_sms":
            result = self.sms.send_sms(self.required(arguments, "number"), self.required(arguments, "text"))
        else:
            raise ValueError(f"Unknown tool: {name}")
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2, sort_keys=True)}]}

    @staticmethod
    def required(arguments: dict[str, Any], key: str) -> str:
        value = str(arguments.get(key) or "").strip()
        if not value:
            raise ValueError(f"Missing required argument: {key}")
        return value

    @staticmethod
    def integer(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def tools() -> list[dict[str, Any]]:
        return [
            {
                "name": "run_offer_explorer",
                "description": "Run the configured CoverAI job-offer explorer and store discovered offers.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config_path": {"type": "string", "description": "Optional path to a job search JSON config."},
                        "user_id": {"type": "string", "description": "Optional CoverAI user id. Defaults to julien."},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "list_offers",
                "description": "List stored job offers ordered by fit score.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                        "min_score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "user_id": {"type": "string", "description": "Optional CoverAI user id. Defaults to julien."},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_offer",
                "description": "Get a stored job offer by id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"offer_id": {"type": "string"}},
                    "required": ["offer_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "send_offer_sms_report",
                "description": "Send a concise SMS report for one offer through the RUT241 workbench allowlist.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "offer_id": {"type": "string"},
                        "number": {"type": "string"},
                        "user_id": {"type": "string", "description": "Optional CoverAI user id. Defaults to julien."},
                    },
                    "required": ["offer_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mark_offer_status",
                "description": "Mark an offer status, for example new, reported, viewed, skipped, selected.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "offer_id": {"type": "string"},
                        "status": {"type": "string"},
                        "user_id": {"type": "string", "description": "Optional CoverAI user id. Defaults to julien."},
                    },
                    "required": ["offer_id", "status"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_explorer_status",
                "description": "Return the latest CoverAI explorer run status.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "automation_status",
                "description": "Return CoverAI SMS automation status, including interval and latest scheduled run.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "ask_coverai",
                "description": "Ask the CoverAI job-market SMS agent a natural-language question, for example scouting, coaching, company research, or application readiness.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "sender": {"type": "string", "description": "Optional phone number context. Defaults to the CoverAI user."},
                        "user_id": {"type": "string", "description": "Optional CoverAI user id. Defaults to julien."},
                    },
                    "required": ["message"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "list_applications",
                "description": "List application tasks with readiness percentages and queue status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "create_application_task",
                "description": "Create or return an application task for an offer using an offer id or natural reference.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "offer_id": {"type": "string"},
                        "reference": {"type": "string", "description": "Natural reference such as company name or 'the last one'."},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_application",
                "description": "Get an application task and its readiness questions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"application_id": {"type": "string"}},
                    "required": ["application_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_submission_packet",
                "description": "Get database answers ready for Playwright/browser injection for an application or offer, plus missing required fields.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "application_id": {"type": "string"},
                        "offer_id": {"type": "string"},
                        "reference": {"type": "string", "description": "Natural reference such as company name or 'the last one'."},
                        "user_id": {"type": "string", "description": "Optional CoverAI user id. Defaults to julien."},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "list_platforms",
                "description": "List CoverAI job-platform registry entries.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "start_platform_login",
                "description": "Prepare a per-user browser profile for a job platform login, optionally launching a headed login browser.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "platform_id": {"type": "string", "description": "Platform id such as linkedin, jobteaser, apec, welcome_to_the_jungle."},
                        "user_id": {"type": "string", "description": "CoverAI user id. Defaults to julien."},
                        "launch": {"type": "boolean", "description": "If true, launch a headed Playwright browser in the background."},
                    },
                    "required": ["platform_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "check_platform_login",
                "description": "Check whether a per-user browser profile appears logged in for a job platform.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "platform_id": {"type": "string", "description": "Platform id such as linkedin, jobteaser, apec, welcome_to_the_jungle."},
                        "user_id": {"type": "string", "description": "CoverAI user id. Defaults to julien."},
                    },
                    "required": ["platform_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "send_sms",
                "description": "Send an SMS through the RUT241 workbench. Destination must be allowlisted there.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "string", "description": "Full phone number including country code."},
                        "text": {"type": "string", "description": "SMS body."},
                    },
                    "required": ["number", "text"],
                    "additionalProperties": False,
                },
            },
        ]


def main() -> None:
    load_local_env(Path(__file__).resolve().parent.parent / ".env")
    coverai_base = os.environ.get("COVERAI_BASE_URL", "http://127.0.0.1:9090")
    server = CoverAiMcpServer(CoverAiHttpClient(coverai_base), RutWorkbenchSmsClient())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            response = server.handle(json.loads(line))
        except json.JSONDecodeError as error:
            response = CoverAiMcpServer.error(None, -32700, f"Parse error: {error}")
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
