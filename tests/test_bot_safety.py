import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot


class DummyMessage:
    def __init__(self, text="", chat=None):
        self.text = text
        self.chat = chat
        self.replies = []
        self.edits = []
        self.reply_to_message = None

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return self


class DummyChat:
    def __init__(self, chat_id=-100123, title="Test Group", chat_type="supergroup"):
        self.id = chat_id
        self.title = title
        self.type = chat_type
        self.first_name = None


class DummyUser:
    def __init__(self, user_id=123, username="customer", is_bot=False):
        self.id = user_id
        self.username = username
        self.is_bot = is_bot
        self.first_name = "Customer"
        self.full_name = "Customer"


class DummyUpdate:
    def __init__(self, text="", chat=None, user=None):
        self.effective_chat = chat or DummyChat()
        self.effective_user = user or DummyUser()
        self.effective_message = DummyMessage(text, self.effective_chat)
        self.message = self.effective_message
        self.callback_query = None


class DummyQuery:
    def __init__(self, data, chat, user):
        self.data = data
        self.message = DummyMessage("maintenance requested", chat)
        self.from_user = user
        self.answered = False

    async def answer(self):
        self.answered = True


class DummyContext:
    def __init__(self):
        self.args = []
        self.bot_data = {"pending": {}}
        self.bot = Mock()
        self.bot.id = 8577833475
        self.bot.username = "ITFF5BOT"
        self.bot.send_message = AsyncMock()
        self.bot.send_chat_action = AsyncMock()


class RouterReportTests(unittest.TestCase):
    def _result(self, issues=None):
        return {
            "name": "MOTEL-OUR-PLACE",
            "wifi": "MOTEL-OUR-PLACE",
            "host": "202.57.211.212",
            "check_host": "8.8.8.8",
            "online": True,
            "cpu": 10,
            "mem_free_mb": 128.0,
            "uptime": "1d2h",
            "version": "7.24",
            "wan": "ether1",
            "rx_mbps": 12.3,
            "tx_mbps": 2.1,
            "ping": {"avg_ms": 14.2, "loss_pct": 0.0},
            "users_total": 3,
            "users_on_wifi": 3,
            "pppoe": [],
            "dhcp_leases": 4,
            "interfaces": [],
            "dns": {"servers": ["1.1.1.1"]},
            "hotspot_active": [],
            "temperature": None,
            "disk_free": None,
            "active_conns": 10,
            "routes_count": 2,
            "queues": [],
            "issues": list(issues or []),
        }

    def test_format_router_report_uses_result_check_host(self):
        report = bot.format_router_report(self._result())
        self.assertIn("Ping 8.8.8.8", report)

    def test_render_router_check_response_handles_non_actionable_issue(self):
        report = bot.render_router_check_response(
            self._result(["DNS not configured"])
        )
        self.assertIn("DNS not configured", report)


class RouterValidationTests(unittest.TestCase):
    def test_default_router_password_is_not_hardcoded(self):
        self.assertEqual("", bot.DEFAULTS["default_router"]["password"])

    def test_http_client_logs_do_not_expose_bot_token_urls(self):
        import logging

        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)

    def test_rejects_telegram_username_as_router_host(self):
        self.assertFalse(bot.is_valid_router_host("@ITFF5BOT"))

    def test_accepts_public_ipv4_router_host(self):
        self.assertTrue(bot.is_valid_router_host("202.57.211.212"))


class AdminSafetyTests(unittest.TestCase):
    def test_setup_router_requires_admin(self):
        update = DummyUpdate(
            "/setupRT\nIP : 202.57.211.212\nWIFI NAME : MOTEL-OUR-PLACE"
        )
        context = DummyContext()
        original_groups = copy.deepcopy(bot.CFG.get("groups", {}))
        with patch.object(bot, "is_admin", AsyncMock(return_value=False)), patch.object(
            bot, "save_config"
        ) as save_config:
            asyncio.run(bot.cmd_setup(update, context))
        self.assertEqual(original_groups, bot.CFG.get("groups", {}))
        save_config.assert_not_called()
        self.assertEqual("Admin only.", update.message.replies[-1][0])

    def test_setup_rejects_invalid_router_host(self):
        update = DummyUpdate(
            "/setupRT\nIP : @ITFF5BOT\nWIFI NAME : BAD"
        )
        context = DummyContext()
        with patch.object(bot, "is_admin", AsyncMock(return_value=True)), patch.object(
            bot, "save_config"
        ) as save_config:
            asyncio.run(bot.cmd_setup(update, context))
        save_config.assert_not_called()
        self.assertEqual(
            "Invalid router IP address or hostname.",
            update.message.replies[-1][0],
        )

    def test_delete_wifi_button_requires_admin(self):
        chat = DummyChat()
        user = DummyUser()
        update = DummyUpdate(chat=chat, user=user)
        update.callback_query = DummyQuery("dwifi:MOTEL-OUR-PLACE", chat, user)
        group_cfg = {
            "routers": [
                {
                    "name": "MOTEL-OUR-PLACE",
                    "wifi": "MOTEL-OUR-PLACE",
                    "host": "202.57.211.212",
                }
            ]
        }
        with patch.object(bot, "is_admin", AsyncMock(return_value=False)), patch.object(
            bot, "get_group_cfg", return_value=group_cfg
        ), patch.object(bot, "save_config") as save_config:
            asyncio.run(bot.on_delete_wifi_button(update, DummyContext()))
        save_config.assert_not_called()
        self.assertEqual(1, len(group_cfg["routers"]))
        self.assertEqual(
            "Admin only.", update.callback_query.message.replies[-1][0]
        )

    def test_reboot_button_requires_admin(self):
        chat = DummyChat()
        user = DummyUser()
        update = DummyUpdate(chat=chat, user=user)
        update.callback_query = DummyQuery(
            "reboot:reboot:202.57.211.212", chat, user
        )
        context = DummyContext()
        context.bot_data["pending"][str(chat.id)] = {
            "router": {"name": "MOTEL-OUR-PLACE", "host": "202.57.211.212"},
            "action": "reboot",
            "expires": 9999999999,
            "chat_title": chat.title,
        }
        with patch.object(bot, "is_admin", AsyncMock(return_value=False)), patch.object(
            bot.mikrotik, "do_action", Mock(return_value=(True, "done"))
        ) as do_action:
            asyncio.run(bot.on_reboot_button(update, context))
        do_action.assert_not_called()
        self.assertEqual("Admin only.", update.callback_query.message.replies[-1][0])

    def test_text_confirmation_requires_admin(self):
        update = DummyUpdate("YES")
        context = DummyContext()
        context.bot_data["pending"][str(update.effective_chat.id)] = {
            "router": {"name": "MOTEL-OUR-PLACE", "host": "202.57.211.212"},
            "action": "reboot",
            "expires": 9999999999,
            "chat_title": update.effective_chat.title,
        }
        with patch.object(bot, "is_admin", AsyncMock(return_value=False)), patch.object(
            bot.mikrotik, "do_action", Mock(return_value=(True, "done"))
        ) as do_action:
            handled = asyncio.run(bot.handle_confirm(update, context))
        self.assertTrue(handled)
        do_action.assert_not_called()
        self.assertEqual("Admin only.", update.message.replies[-1][0])

    def test_add_wifi_confirmation_requires_admin(self):
        update = DummyUpdate("YES")
        context = DummyContext()
        group_cfg = {"routers": []}
        context.bot_data["pending_add"] = {
            str(update.effective_chat.id): {
                "expires": 9999999999,
                "data": {"wifi": "MOTEL-OUR-PLACE", "ip": "202.57.211.212"},
                "group_cfg": group_cfg,
            }
        }
        with patch.object(bot, "is_admin", AsyncMock(return_value=False)), patch.object(
            bot, "save_config"
        ) as save_config:
            asyncio.run(bot.group_message(update, context))

        self.assertEqual([], group_cfg["routers"])
        save_config.assert_not_called()
        self.assertEqual("Admin only.", update.message.replies[-1][0])


class GroupAutoReplyTests(unittest.TestCase):
    def test_always_command_reports_default_on_state(self):
        update = DummyUpdate("/always")
        context = DummyContext()
        cfg = {
            "groups": {
                str(update.effective_chat.id): {
                    "title": update.effective_chat.title,
                    "routers": [],
                }
            }
        }

        with patch.object(bot, "CFG", cfg), patch.object(
            bot, "is_admin", AsyncMock(return_value=True)
        ):
            asyncio.run(bot.cmd_always(update, context))

        self.assertIn("Always-reply is ON.", update.message.replies[-1][0])

    def test_configured_group_replies_to_ordinary_message_by_default(self):
        update = DummyUpdate("Please answer my question.")
        context = DummyContext()
        cfg = {
            "admin_chat_ids": [],
            "groups": {
                str(update.effective_chat.id): {
                    "title": update.effective_chat.title,
                    "routers": [],
                }
            },
            "ai_enabled": True,
            "ai_window_min": 30,
            "slow_keywords": [],
            "new_connect_keywords": [],
            "greeting_keywords": [],
            "port_keywords": [],
            "non_duty_keywords": [],
        }

        with patch.object(bot, "CFG", cfg), patch.object(
            bot.ai_agent, "recent_activity", return_value=False
        ), patch.object(bot.ai_agent, "ai_ready", return_value=True), patch.object(
            bot.ai_agent, "run_agent", return_value="Automatic support reply"
        ) as run_agent:
            asyncio.run(bot.group_message(update, context))

        run_agent.assert_called_once()
        self.assertEqual("Automatic support reply", update.message.replies[-1][0])

    def test_explicit_always_reply_off_keeps_ordinary_messages_silent(self):
        update = DummyUpdate("Please answer my question.")
        context = DummyContext()
        cfg = {
            "admin_chat_ids": [],
            "groups": {
                str(update.effective_chat.id): {
                    "title": update.effective_chat.title,
                    "routers": [],
                    "always_reply": False,
                }
            },
            "ai_enabled": True,
            "ai_window_min": 30,
            "slow_keywords": [],
            "new_connect_keywords": [],
            "greeting_keywords": [],
            "port_keywords": [],
            "non_duty_keywords": [],
        }

        with patch.object(bot, "CFG", cfg), patch.object(
            bot.ai_agent, "recent_activity", return_value=False
        ), patch.object(bot.ai_agent, "ai_ready", return_value=True), patch.object(
            bot.ai_agent, "run_agent", return_value="Unexpected reply"
        ) as run_agent:
            asyncio.run(bot.group_message(update, context))

        run_agent.assert_not_called()
        self.assertEqual([], update.message.replies)


class PrivateAdminRoutingTests(unittest.TestCase):
    def test_admin_private_chat_receives_configured_router_context(self):
        chat = DummyChat(chat_id=42, title="", chat_type="private")
        update = DummyUpdate(
            "Please inspect MOTEL-OUR-PLACE", chat=chat, user=DummyUser(user_id=42)
        )
        context = DummyContext()
        cfg = {
            "admin_chat_ids": [42],
            "groups": {
                "-100123": {
                    "title": "Motel",
                    "routers": [
                        {
                            "name": "MOTEL-OUR-PLACE",
                            "wifi": "MOTEL-OUR-PLACE",
                            "host": "202.57.211.212",
                        }
                    ],
                }
            },
            "ai_enabled": True,
            "ai_window_min": 30,
            "slow_keywords": [],
            "new_connect_keywords": [],
            "greeting_keywords": [],
            "port_keywords": [],
            "non_duty_keywords": [],
        }
        with patch.object(bot, "CFG", cfg), patch.object(
            bot.ai_agent, "recent_activity", return_value=True
        ), patch.object(bot.ai_agent, "ai_ready", return_value=True), patch.object(
            bot.ai_agent, "run_agent", return_value="I found the router."
        ) as run_agent:
            asyncio.run(bot.group_message(update, context))

        router_context = run_agent.call_args.args[3]
        self.assertTrue(router_context["routers"], "admin private chat has no routers")
        self.assertEqual("MOTEL-OUR-PLACE", router_context["routers"][0]["wifi"])
        self.assertEqual("202.57.211.212", router_context["routers"][0]["host"])


if __name__ == "__main__":
    unittest.main()
