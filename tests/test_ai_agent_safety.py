import unittest
from unittest.mock import patch

import ai_agent


class RouterSelectionSafetyTests(unittest.TestCase):
    def test_unknown_wifi_does_not_fall_back_to_all_routers(self):
        routers = [
            {
                "name": "MOTEL-OUR-PLACE",
                "wifi": "MOTEL-OUR-PLACE",
                "host": "202.57.211.212",
            }
        ]

        self.assertEqual([], ai_agent._match_routers(routers, "UNKNOWN-WIFI"))


class MaintenanceSafetyTests(unittest.TestCase):
    def test_unreachable_router_does_not_create_maintenance_confirmation(self):
        router = {
            "name": "MOTEL-OUR-PLACE",
            "wifi": "MOTEL-OUR-PLACE",
            "host": "202.57.211.212",
        }
        bot_data = {}
        with patch.object(
            ai_agent.mikrotik,
            "check_router",
            return_value={
                "online": False,
                "issues": ["Cannot connect to router (check IP/port/user/password)"],
            },
        ):
            result = ai_agent.execute_tool(
                "request_maintenance",
                {"action": "reboot", "wifi": "MOTEL-OUR-PLACE"},
                {"routers": [router]},
                bot_data,
                42,
                "Admin private chat",
            )

        self.assertEqual("router_management_unreachable", result["status"])
        self.assertNotIn("pending", bot_data)


class StatusHonestyTests(unittest.TestCase):
    def test_unreachable_management_does_not_claim_internet_is_offline(self):
        status = ai_agent._compact_status(
            {
                "name": "MOTEL-OUR-PLACE",
                "wifi": "MOTEL-OUR-PLACE",
                "online": False,
                "error": "connection refused",
                "issues": ["Cannot connect to router"],
            }
        )

        self.assertEqual("unreachable", status.get("monitoring_status"))
        self.assertEqual("unknown", status.get("internet_status"))

    def test_prompt_forbids_equating_monitoring_failure_with_outage(self):
        prompt = ai_agent._system_prompt({"routers": []}, False, "ITFF5BOT")

        self.assertIn("does NOT prove", prompt)
        self.assertIn("Do NOT offer maintenance", prompt)


if __name__ == "__main__":
    unittest.main()
