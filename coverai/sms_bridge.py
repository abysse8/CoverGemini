from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class RutWorkbenchSmsClient:
    def __init__(self, base_url: str = "", token: str = "") -> None:
        self.base_url = (base_url or os.environ.get("WORKBENCH_PUBLIC_URL", "http://127.0.0.1:8765")).rstrip("/")
        self.token = token or os.environ.get("WORKBENCH_TOKEN", "")

    def send_sms(self, number: str, text: str) -> dict[str, Any]:
        number = number.strip()
        text = text.strip()
        if not number:
            raise ValueError("number is required")
        if not text:
            raise ValueError("text is required")
        payload = json.dumps({"number": number, "text": text}).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}/api/sms/send", data=payload, method="POST")
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                parsed_error = json.loads(raw)
                message = parsed_error.get("error") or raw
            except json.JSONDecodeError:
                message = raw or error.reason
            raise RuntimeError(f"RUT workbench HTTP {error.code}: {message}") from error
        if isinstance(parsed, dict) and "error" in parsed:
            raise RuntimeError(str(parsed["error"]))
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
            return parsed["data"]
        return {"ok": True, "response": parsed}
