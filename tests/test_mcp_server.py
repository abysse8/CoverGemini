from __future__ import annotations

import json
import unittest

from coverai.mcp_server import CoverAiMcpServer


class FakeCoverAi:
    def run_offer_explorer(self, config_path: str = "", user_id: str = "") -> dict:
        return {"run": {"status": "completed", "config_path": config_path, "user_id": user_id}, "offers": []}

    def list_offers(self, status: str = "", limit: int = 20, min_score: int | None = None, user_id: str = "") -> dict:
        return {"offers": [{"id": "off_1", "status": status, "limit": limit, "min_score": min_score, "user_id": user_id}]}

    def get_offer(self, offer_id: str) -> dict:
        return {"offer": {"id": offer_id}}

    def send_offer_sms_report(self, offer_id: str, number: str = "", user_id: str = "") -> dict:
        return {"report": {"offer_id": offer_id, "number": number, "user_id": user_id, "status": "sent"}}

    def mark_offer_status(self, offer_id: str, status: str, user_id: str = "") -> dict:
        return {"offer": {"id": offer_id, "status": status, "user_id": user_id}}

    def get_explorer_status(self) -> dict:
        return {"run": {"status": "completed"}}

    def automation_status(self) -> dict:
        return {"automation": {"running": False, "interval_seconds": 900}}

    def ask_coverai(self, message: str, sender: str = "", user_id: str = "") -> dict:
        return {"reply": f"CoverAI heard: {message}", "sender": sender, "user_id": user_id}

    def list_applications(self, status: str = "", limit: int = 20) -> dict:
        return {"applications": [{"id": "app_1", "status": status, "limit": limit}]}

    def create_application_task(self, offer_id: str = "", reference: str = "") -> dict:
        return {"application": {"id": "app_1", "offer_id": offer_id, "reference": reference}}

    def get_application(self, application_id: str) -> dict:
        return {"application": {"id": application_id}, "questions": []}

    def get_submission_packet(self, application_id: str = "", offer_id: str = "", reference: str = "", user_id: str = "") -> dict:
        return {
            "packet": {
                "application": {"id": application_id or "app_1"},
                "offer": {"id": offer_id or "off_1"},
                "reference": reference,
                "user_id": user_id,
                "playwright_payload": {"fields": [{"field_key": "start_date_availability", "value": "September 2026"}]},
            }
        }

    def list_platforms(self) -> dict:
        return {"platforms": [{"id": "linkedin", "name": "LinkedIn"}]}

    def start_platform_login(self, platform_id: str, user_id: str = "julien", launch: bool = False) -> dict:
        return {"platform_id": platform_id, "user_id": user_id, "launched": launch}

    def check_platform_login(self, platform_id: str, user_id: str = "julien") -> dict:
        return {"platform_id": platform_id, "user_id": user_id, "ready": True}


class FakeSms:
    def send_sms(self, number: str, text: str) -> dict:
        return {"ok": True, "number": number, "text": text}


class McpServerTests(unittest.TestCase):
    def test_initialize_and_list_tools(self) -> None:
        server = CoverAiMcpServer(FakeCoverAi(), FakeSms())
        init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

        self.assertEqual(init["result"]["serverInfo"]["name"], "coverai")
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("run_offer_explorer", names)
        self.assertIn("list_platforms", names)
        self.assertIn("start_platform_login", names)
        self.assertIn("automation_status", names)
        self.assertIn("ask_coverai", names)
        self.assertIn("list_applications", names)
        self.assertIn("create_application_task", names)
        self.assertIn("get_application", names)
        self.assertIn("get_submission_packet", names)
        self.assertIn("send_sms", names)

    def test_calls_tool_and_returns_json_text(self) -> None:
        server = CoverAiMcpServer(FakeCoverAi(), FakeSms())
        response = server.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_offer", "arguments": {"offer_id": "off_1"}},
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["offer"]["id"], "off_1")

    def test_calls_platform_tool_with_user_context(self) -> None:
        server = CoverAiMcpServer(FakeCoverAi(), FakeSms())
        response = server.handle({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "start_platform_login",
                "arguments": {"platform_id": "linkedin", "user_id": "julien", "launch": False},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["platform_id"], "linkedin")
        self.assertEqual(payload["user_id"], "julien")

    def test_calls_ask_coverai_tool(self) -> None:
        server = CoverAiMcpServer(FakeCoverAi(), FakeSms())
        response = server.handle({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "ask_coverai",
                "arguments": {"message": "STATUS", "sender": "+33600000000", "user_id": "julien"},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["reply"], "CoverAI heard: STATUS")
        self.assertEqual(payload["sender"], "+33600000000")

    def test_calls_application_tools(self) -> None:
        server = CoverAiMcpServer(FakeCoverAi(), FakeSms())
        create_response = server.handle({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "create_application_task", "arguments": {"reference": "Netatmo"}},
        })
        list_response = server.handle({
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "list_applications", "arguments": {"limit": 5}},
        })
        get_response = server.handle({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "get_application", "arguments": {"application_id": "app_1"}},
        })
        packet_response = server.handle({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "get_submission_packet", "arguments": {"reference": "Netatmo", "user_id": "julien"}},
        })

        self.assertEqual(json.loads(create_response["result"]["content"][0]["text"])["application"]["reference"], "Netatmo")
        self.assertEqual(json.loads(list_response["result"]["content"][0]["text"])["applications"][0]["limit"], 5)
        self.assertEqual(json.loads(get_response["result"]["content"][0]["text"])["application"]["id"], "app_1")
        self.assertEqual(json.loads(packet_response["result"]["content"][0]["text"])["packet"]["reference"], "Netatmo")

    def test_missing_required_argument_errors(self) -> None:
        server = CoverAiMcpServer(FakeCoverAi(), FakeSms())
        response = server.handle({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "send_sms", "arguments": {"number": "+331"}},
        })
        self.assertIn("error", response)
        self.assertIn("text", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
