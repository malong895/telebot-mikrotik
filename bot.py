import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import ai_agent
import mikrotik
from parser import parse_description

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "logs" / "requests.jsonl"

DEFAULTS = {
    "bot_token": "",
    "admin_chat_ids": [],
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "ai_window_min": 30,
    "ai_enabled": True,
    "slow_keywords": [
        "slow", "lag", "lagging", "buffering", "cut", "cutnet",
        "disconnected", "disconnect", "no internet", "internet down",
        "net down", "very slow",
    ],
    "new_connect_keywords": [
        "new connect", "new connection", "connect internet", "install internet",
        "new office", "move office", "apply internet", "subscribe",
        "how much", "price", "quotation",
    ],
    "non_duty_keywords": [],
    "greeting_keywords": [
        "hello", "hi ", "hi!", "hey", "good morning", "good afternoon",
        "good evening", "suosdey", "សួស្តី", "anyone there", "你好", "nihao",
    ],
    "auto_decline_enabled": True,
    "confirm_words": ["ok", "okay", "yes", "yes please", "go ahead", "proceed"],
    "away_notice_keywords": ["admin", "technician", "anyone there", "hello?"],
    "away_notice_cooldown_sec": 300,
    "confirm_timeout_sec": 600,
    "check_host": "8.8.8.8",
    "default_router": {"api_port": 8292, "user": "admin", "password": "REMOVED-EXPOSED-SECRET"},
    "groups": {},
    "templates": {
        "new_connect_reply": (
            "Hello {user}! Thank you for your interest in our internet service "
            "for your new office. I have saved your request and my team will "
            "contact you very soon."
        ),
        "non_duty_reply": (
            "Sorry {user}, this one is not our duty. We only take care of the "
            "internet service here. Thank you for understanding!"
        ),
        "away_notice": (
            "Hello! Our technician is not at the desk right now, I am the auto-assistant.\n"
            "- Slow internet? Just tell me which WiFi name is slow\n"
            "- Need new connection? Type: new connect\n"
            "Your message is saved and we will answer you soon."
        ),
        "checking": [
            "Thank you {user}! Please wait, I am checking {wifi} for you now.",
            "Got it {user}. One moment, I am checking {wifi} right now.",
            "OK {user}, let me check {wifi} for you. A few seconds please.",
        ],
        "report_header": "Here is the result for {group}:",
        "no_issue": [
            "Good news! I checked {wifi} and everything looks normal on our side.",
            "I finished checking {wifi}. No problem found from our network.",
        ],
        "followup": (
            "To help you faster, can you tell me:\n"
            "1. Is it slow on ALL devices or only one phone/PC?\n"
            "2. Which WiFi name is your device connected to?"
        ),
        "propose_pppoe": (
            "I found a problem on router {router}:\n{issues}\n\n"
            "I can reconnect the line now. The network may disconnect for about "
            "1 minute. Reply YES to allow, or /cancel to stop."
        ),
        "propose_reboot": (
            "I found a problem on router {router}:\n{issues}\n\n"
            "I can reboot the router now. The network will be down about 3-5 "
            "minutes. Reply YES to allow, or /cancel to stop."
        ),
        "action_done": "Done! {result}",
        "action_failed": "Sorry, the action failed: {result}",
        "cancelled": "OK, cancelled. Nothing was changed.",
    },
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
log = logging.getLogger("itbot")


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    else:
        data = {}
    cfg = dict(DEFAULTS)
    cfg.update(data)
    tpl_cfg = dict(DEFAULTS["templates"])
    tpl_cfg.update(data.get("templates", {}))
    cfg["templates"] = tpl_cfg
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)


CFG = load_config()

if os.environ.get("BOT_TOKEN"):
    CFG["bot_token"] = os.environ["BOT_TOKEN"]
if os.environ.get("GROQ_API_KEY"):
    CFG["openai_api_key"] = os.environ["GROQ_API_KEY"]
if os.environ.get("ADMIN_CHAT_IDS"):
    CFG["admin_chat_ids"] = [int(x) for x in os.environ["ADMIN_CHAT_IDS"].split(",")]


def detect_language(text):
    if not text:
        return "en"
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    return "zh" if chinese_chars > 0 else "en"


def fmt(key, default="", **kwargs):
    value = CFG["templates"].get(key, default)
    lang = kwargs.pop("lang", None) or detect_language(kwargs.get("text", ""))
    if isinstance(value, list) and len(value) > 0:
        if lang == "zh" and len(value) >= 2:
            value = value[1]
        elif lang == "zh" and len(value) == 1:
            value = value[0]
        else:
            value = value[0]
    try:
        return value.format(**kwargs)
    except Exception:
        return value


def get_group_cfg(chat_id):
    return CFG["groups"].get(str(chat_id))


def log_request(kind, update: Update):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    user = update.effective_user
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "chat_id": update.effective_chat.id,
        "chat_title": update.effective_chat.title or update.effective_chat.first_name or "",
        "user_id": getattr(user, "id", None),
        "username": getattr(user, "username", None),
        "full_name": getattr(user, "full_name", None),
        "text": (update.effective_message.text or "")[:500],
    }
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text):
    for admin_id in CFG.get("admin_chat_ids", []):
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            pass


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user and user.id in CFG.get("admin_chat_ids", []):
        return True
    chat = update.effective_chat
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and user:
        try:
            member = await chat.get_member(user.id)
            return member.status in ("administrator", "creator")
        except Exception:
            return False
    return False


def contains_any(text_lower, words):
    return any(w.lower() in text_lower for w in words)


def extract_port(text):
    text_lower = text.lower()
    port_match = re.search(r'port\s*(\d)', text_lower)
    if port_match:
        return int(port_match.group(1))
    ether_match = re.search(r'ether(\d)', text_lower)
    if ether_match:
        return int(ether_match.group(1))
    cn_port = re.search(r'[端端]口\s*(\d)', text_lower)
    if cn_port:
        return int(cn_port.group(1))
    single_digit = re.search(r'\b(\d)\b', text_lower)
    if single_digit:
        num = int(single_digit.group(1))
        if 1 <= num <= 8:
            return num
    return None


def user_display_name(update: Update) -> str:
    user = update.effective_user
    return ((user.first_name if user else "") or "").strip() or "friend"


def build_wifi_keyboard(group_cfg):
    routers = (group_cfg or {}).get("routers", [])
    if not routers:
        return None
    buttons = []
    for r in routers[:8]:
        label = r.get("wifi") or r.get("name") or r.get("host")
        buttons.append([InlineKeyboardButton(str(label), callback_data=f"wifi:{label}")])
    return InlineKeyboardMarkup(buttons)


async def on_wifi_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("wifi:"):
        return
    wifi = data[5:]
    chat = query.message.chat
    user = query.from_user
    if user and user.is_bot:
        return
    group_cfg = get_group_cfg(chat.id) or {"routers": []}
    routers = group_cfg.get("routers", [])
    matched = [r for r in routers if (r.get("wifi") or r.get("name") or "") == wifi]
    if not matched:
        matched = [r for r in routers if wifi.lower() in (r.get("wifi") or r.get("name") or "").lower()]
    if not matched:
        await query.message.reply_text(f"WiFi '{wifi}' not found in config.")
        return
    try:
        await context.bot.send_chat_action(chat.id, "typing")
    except Exception:
        pass
    msg = await query.message.reply_text(f"Checking {wifi}...")
    router = dict(matched[0])
    router.setdefault("check_host", CFG.get("check_host", "8.8.8.8"))
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(mikrotik.check_router, router),
            timeout=90,
        )
    except Exception as exc:
        await msg.edit_text(f"Check failed for {wifi}: {exc}")
        return
    report = format_router_report(res)
    if res["issues"]:
        report += "\n\n" + suggest_action(res)
    await msg.edit_text(report[:4000])


async def on_reboot_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("reboot:"):
        return
    parts = data.split(":")
    if len(parts) < 3:
        return
    action = parts[1]
    router_host = parts[2]
    chat = query.message.chat
    user = query.from_user
    if user and user.is_bot:
        return
    pending = context.bot_data.get("pending", {})
    key = str(chat.id)
    job = pending.get(key)
    if not job:
        lang = detect_language(query.message.text or "")
        if lang == "zh":
            await query.message.reply_text("已过期或已取消。请重新检查网络。")
        else:
            await query.message.reply_text("Expired or already cancelled. Please check again.")
        return
    if time.time() > job["expires"]:
        del pending[key]
        lang = detect_language(query.message.text or "")
        if lang == "zh":
            await query.message.reply_text("已过期。请重新检查网络。")
        else:
            await query.message.reply_text("Expired. Please check again.")
        return
    router = job["router"]
    action = job["action"]
    lang = detect_language(query.message.text or "")
    if lang == "zh":
        await query.message.reply_text("正在执行，请稍候...")
    else:
        await query.message.reply_text("Executing, please wait...")
    try:
        ok, result = await asyncio.wait_for(
            asyncio.to_thread(mikrotik.do_action, router, action), timeout=120
        )
    except Exception as exc:
        ok, result = False, str(exc)
    del pending[key]
    if ok:
        if lang == "zh":
            await query.message.reply_text(f"完成！{result}")
        else:
            await query.message.reply_text(f"Done! {result}")
    else:
        if lang == "zh":
            await query.message.reply_text(f"操作失败：{result}")
        else:
            await query.message.reply_text(f"Action failed: {result}")
    await notify_admins(
        context,
        f"Action '{action}' on {router.get('name')} ({router['host']}) "
        f"in group {job['chat_title']}: {'OK' if ok else 'FAILED'} - {result}",
    )


def filter_routers_by_text(group_cfg, text_lower):
    routers = group_cfg.get("routers", [])
    matched = []
    for router in routers:
        names = set()
        for key in ("wifi", "name"):
            value = str(router.get(key) or "").strip().lower()
            if value:
                names.add(value)
        host = str(router.get("host") or "").strip().lower()
        hit = bool(host) and host in text_lower
        if not hit:
            for name in names:
                if len(name) >= 3:
                    if name in text_lower:
                        hit = True
                        break
                elif name:
                    pattern = r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])"
                    if re.search(pattern, text_lower):
                        hit = True
                        break
        if hit:
            matched.append(router)
    return matched


def wifi_label_of(routers):
    labels = sorted(
        {
            str(r.get("wifi") or r.get("name") or r.get("host") or "").strip()
            for r in routers
        }
        - {""}
    )
    return ", ".join(labels) if labels else "the WiFi"


async def run_group_checks(group_cfg):
    lines = []
    problems = []
    routers = group_cfg.get("routers", [])
    for index, router in enumerate(routers):
        r = dict(router)
        r.setdefault("check_host", CFG.get("check_host", "8.8.8.8"))
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(mikrotik.check_router, r), timeout=90
            )
        except Exception as exc:
            lines.append(f"Router {r.get('name', r['host'])} ({r['host']}): check failed ({exc})")
            problems.append({"index": index, "router": r, "issues": ["check failed"], "action": None})
            continue
        lines.append(format_router_report(res))
        if res["issues"]:
            action = suggest_action(res)
            problems.append({"index": index, "router": r, "issues": res["issues"], "action": action})
    if not lines:
        lines.append("No router configured for this group. Use /setupRT first.")
    return "\n".join(lines), problems


def format_router_report(res):
    parts = [f"Router: {res['name']} ({res['host']})"]
    if not res["online"]:
        parts.append("Status: OFFLINE")
        if res.get("error"):
            parts.append(f"Error: {res['error']}")
        issues = "; ".join(res["issues"]) if res["issues"] else "cannot reach router"
        parts.append(f"Problems: {issues}")
        return "\n".join(parts)
    parts.append("Status: ONLINE")
    parts.append(
        f"CPU: {res['cpu']}% | RAM free: {res['mem_free_mb']} MB | Uptime: {res['uptime']}"
    )
    parts.append(f"RouterOS: {res.get('version', '?')}")
    wan_txt = f"WAN: {res['wan']}"
    if res["rx_mbps"] is not None:
        wan_txt += f" | now down {res['rx_mbps']} Mbps / up {res['tx_mbps']} Mbps"
    parts.append(wan_txt)
    if res["ping"]:
        avg = res["ping"]["avg_ms"]
        avg_txt = f"{avg:.0f} ms" if avg else "-"
        parts.append(
            f"Ping {router.get('check_host', '8.8.8.8')}: avg {avg_txt}, loss {res['ping']['loss_pct']:.0f}%"
        )
    if res["users_total"] is not None:
        wifi_part = f"WiFi users: {res['users_total']}"
        if res.get("users_on_wifi") is not None:
            wifi_part += f" on SSID '{res.get('wifi')}': {res['users_on_wifi']}"
        parts.append(wifi_part)
    if res["pppoe"]:
        pppoe_txt = ", ".join(
            f"{p['name']}={'running' if p['running'] else 'DOWN'}" for p in res["pppoe"]
        )
        parts.append(f"PPPoE: {pppoe_txt}")
    if res["dhcp_leases"] is not None:
        parts.append(f"DHCP leases: {res['dhcp_leases']}")
    if res.get("interfaces"):
        down_ifaces = [i["name"] for i in res["interfaces"] if not i["running"] and not i["disabled"]]
        up_ifaces = [i["name"] for i in res["interfaces"] if i["running"]]
        parts.append(f"Interfaces: {len(up_ifaces)} up, {len(down_ifaces)} down")
        if down_ifaces:
            parts.append(f"  Down: {', '.join(down_ifaces[:8])}")
    if res.get("dns", {}).get("servers"):
        parts.append(f"DNS: {', '.join(res['dns']['servers'][:3])}")
    elif res["online"]:
        parts.append("DNS: NOT configured")
    if res.get("hotspot_active"):
        parts.append(f"Hotspot users: {len(res['hotspot_active'])}")
    if res.get("temperature"):
        temp_warn = " HIGH" if res["temperature"] > 60 else ""
        parts.append(f"Temperature: {res['temperature']}C{temp_warn}")
    if res.get("disk_free"):
        parts.append(f"Disk free: {res['disk_free']}")
    if res.get("active_conns"):
        conn_warn = " HIGH" if res["active_conns"] > 5000 else ""
        parts.append(f"Active connections: {res['active_conns']}{conn_warn}")
    if res.get("routes_count"):
        parts.append(f"Routes: {res['routes_count']}")
    if res.get("queues"):
        parts.append(f"Queues: {len(res['queues'])}")
    if res["issues"]:
        parts.append("Problems found:")
        for issue in res["issues"]:
            parts.append(f"- {issue}")
    else:
        parts.append(fmt("no_issue", "No problem found.", wifi=str(res.get("wifi") or "this WiFi")))
    return "\n".join(parts)


def suggest_action(res):
    joined = " ".join(res["issues"]).lower()
    if "cannot connect" in joined:
        return None
    if "pppoe down" in joined:
        return "reconnect_pppoe"
    if "cpu high" in joined:
        return "reboot"
    return None


async def cmd_always(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Use /always inside the group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    arg = (context.args or [""])[0].lower()
    entry = CFG["groups"].setdefault(str(chat.id), {"routers": []})
    if arg == "on":
        entry["always_reply"] = True
        save_config(CFG)
        await update.message.reply_text("✅ ALWAYS-REPLY ON: I now answer every message in this group.")
    elif arg == "off":
        entry["always_reply"] = False
        save_config(CFG)
        await update.message.reply_text("✅ ALWAYS-REPLY OFF: back to smart keyword mode.")
    else:
        state = entry.get("always_reply", False)
        await update.message.reply_text(f"Always-reply is {'ON' if state else 'OFF'}. Use /always on or /always off")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text())


def help_text():
    return (
        "IT Service assistant bot.\n\n"
        "Just type normal messages:\n"
        "- 'internet is slow' -> I check the router(s) of this group\n"
        "- 'KTV is slow' or 'wifi OFFICE-F2 lag' -> I check ONLY that WiFi\n"
        "- 'need new connect for new office' -> I save the request and reply\n\n"
        "Commands:\n"
        "/status - check all routers of this group now\n"
        "/setupRT - setup router (see format below)\n"
        "/cancel - cancel a pending fix approval\n"
        "/myid - show chat id and your user id\n"
        "Admin only:\n"
        "/away on|off - away mode notice\n"
        "/pending - recent saved requests\n\n"
        "/setupRT format:\n"
        "/setupRT\n"
        "IP : 203.171.252.87\n"
        "WIFI NAME : KTV\n"
        "User login : admin\n"
        "password : Myteacher@123\n"
        "SSH port : 44222"
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"chat_id: {chat.id}\nuser_id: {user.id if user else '-'}\n"
        "Put your user id into admin_chat_ids in config.json to use admin commands."
    )


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Use /setupRT inside the customer group.")
        return
    text = (update.effective_message.text or "").strip()
    lines = text.split("\n")
    data = {}
    for line in lines:
        line = line.strip()
        if line.startswith("/setupRT"):
            rest = line[len("/setupRT"):].strip()
            if rest:
                data["ip"] = rest
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if "ip" in key and "wifi" not in key:
                data["ip"] = val
            elif "wifi" in key or "name" in key:
                data["wifi"] = val
            elif "user" in key or "login" in key:
                data["user"] = val
            elif "pass" in key:
                data["password"] = val
            elif "ssh" in key or "port" in key:
                try:
                    data["ssh_port"] = int(val)
                except ValueError:
                    pass
    if not data.get("ip"):
        await update.message.reply_text(
            "Please enter router info like this:\n\n"
            "/setupRT\n"
            "IP : 203.171.252.87\n"
            "WIFI NAME : KTV\n"
            "User login : admin\n"
            "password : Myteacher@123\n"
            "SSH port : 44222"
        )
        return
    defaults = CFG.get("default_router", {})
    router = {
        "name": data.get("wifi", data["ip"]),
        "wifi": data.get("wifi", data["ip"]),
        "host": data["ip"],
        "api_port": defaults.get("api_port", 52743),
        "ssh_port": data.get("ssh_port", defaults.get("ssh_port", 44222)),
        "user": data.get("user", defaults.get("user", "admin")),
        "password": data.get("password", defaults.get("password", "")),
    }
    entry = CFG["groups"].setdefault(str(chat.id), {"title": chat.title or "", "routers": []})
    existing = [r for r in entry.get("routers", []) if r.get("host") == data["ip"]]
    if existing:
        existing[0].update(router)
    else:
        entry.setdefault("routers", []).append(router)
    save_config(CFG)
    await update.message.reply_text(
        f"Router saved for this group:\n"
        f"WiFi: {router['wifi']}\n"
        f"IP: {router['host']}\n"
        f"SSL port: {router['api_port']}\n"
        f"SSH port: {router['ssh_port']}\n"
        f"User: {router['user']}"
    )


async def cmd_setupnewwifi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text(            "Use /addnewwifi inside the customer group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    group_cfg = get_group_cfg(chat.id)
    if not group_cfg:
        await update.message.reply_text("This group has no router yet. Use /setupRT first to add the first one.")
        return
    text = (update.effective_message.text or "").strip()
    lines = text.split("\n")
    has_info = any(":" in line and ("ip" in line.lower() or "wifi" in line.lower() or "name" in line.lower()) for line in lines if not line.strip().startswith("/"))
    if not has_info:
        await update.message.reply_text(
            "Add new WiFi to this group.\n\n"
            "Single example:\n"
            "/addnewwifi\n"
            "IP : 203.171.252.90\n"
            "WIFI NAME : NEW-WIFI\n\n"
            "Multiple example:\n"
            "/addnewwifi\n"
            "IP : 203.171.252.90\n"
            "WIFI NAME : WIFI-A\n\n"
            "IP : 203.171.252.91\n"
            "WIFI NAME : WIFI-B\n\n"
            "IP : 203.171.252.92\n"
            "WIFI NAME : WIFI-C"
        )
        return
    entries = []
    current = {}
    for line in lines:
        line = line.strip()
        if line.startswith("/addnewwifi"):
            continue
        if not line:
            if current.get("ip") and current.get("wifi"):
                entries.append(current)
                current = {}
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if "ip" in key and "wifi" not in key:
                if current.get("ip") and current.get("wifi"):
                    entries.append(current)
                    current = {}
                current["ip"] = val
            elif "wifi" in key or "name" in key:
                current["wifi"] = val
            elif "user" in key or "login" in key:
                current["user"] = val
            elif "pass" in key:
                current["password"] = val
            elif "ssh" in key or "port" in key:
                try:
                    current["ssh_port"] = int(val)
                except ValueError:
                    pass
    if current.get("ip") and current.get("wifi"):
        entries.append(current)
    if not entries:
        await update.message.reply_text(
            "Please enter IP and WiFi name.\n\n"
            "Example:\n"
            "/addnewwifi\n"
            "IP : 203.171.252.90\n"
            "WIFI NAME : NEW-WIFI"
        )
        return
    defaults = CFG.get("default_router", {})
    added = []
    updated = []
    for data in entries:
        router = {
            "name": data["wifi"],
            "wifi": data["wifi"],
            "host": data["ip"],
            "api_port": defaults.get("api_port", 52743),
            "ssh_port": data.get("ssh_port", defaults.get("ssh_port", 44222)),
            "user": data.get("user", defaults.get("user", "admin")),
            "password": data.get("password", defaults.get("password", "")),
        }
        existing = [r for r in group_cfg.get("routers", []) if r.get("host") == data["ip"] and r.get("wifi") == data["wifi"]]
        if existing:
            existing[0].update(router)
            updated.append(data["wifi"])
        else:
            group_cfg.setdefault("routers", []).append(router)
            added.append(data["wifi"])
    CFG["groups"][str(chat.id)] = group_cfg
    save_config(CFG)
    wifi_list = "\n".join(
        f"  - {r.get('wifi', r.get('name', r.get('host')))}" for r in group_cfg.get("routers", [])
    )
    parts = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if updated:
        parts.append(f"Updated: {', '.join(updated)}")
    parts.append(f"\nAll WiFi in this group:\n{wifi_list}")
    await update.message.reply_text("\n".join(parts))


async def cmd_deletewifi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Use /deletewifi inside the customer group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    group_cfg = get_group_cfg(chat.id)
    if not group_cfg or not group_cfg.get("routers"):
        await update.message.reply_text("No WiFi configured in this group.")
        return
    buttons = []
    for r in group_cfg["routers"]:
        label = r.get("wifi") or r.get("name") or r.get("host")
        buttons.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"dwifi:{label}")])
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Select WiFi to delete:", reply_markup=keyboard)


async def on_delete_wifi_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("dwifi:"):
        return
    wifi = data[6:]
    chat = query.message.chat
    group_cfg = get_group_cfg(chat.id)
    if not group_cfg or not group_cfg.get("routers"):
        await query.message.reply_text("No WiFi configured.")
        return
    routers = group_cfg["routers"]
    found = None
    for i, r in enumerate(routers):
        label = r.get("wifi") or r.get("name") or r.get("host")
        if label == wifi:
            found = i
            break
    if found is None:
        await query.message.reply_text(f"WiFi '{wifi}' not found.")
        return
    removed = routers.pop(found)
    CFG["groups"][str(chat.id)] = group_cfg
    save_config(CFG)
    remaining = "\n".join(
        f"  - {r.get('wifi', r.get('name', r.get('host')))}" for r in routers
    ) or "  (none)"
    await query.message.reply_text(
        f"WiFi '{wifi}' deleted successfully!\n\nRemaining WiFi:\n{remaining}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    group_cfg = get_group_cfg(chat.id)
    if not group_cfg:
        await update.message.reply_text(
            "This group is not configured yet. Use /setup first."
        )
        return
    msg = await update.message.reply_text(
        fmt("checking", user=user_display_name(update), wifi="ALL WiFi of this group")
    )
    report, _problems = await run_group_checks(group_cfg)
    text = fmt("report_header", group=chat.title or "") + "\n\n" + report
    await msg.edit_text(text[:4000])


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.bot_data.get("pending", {})
    key = str(update.effective_chat.id)
    if key in pending:
        del pending[key]
        await update.message.reply_text(fmt("cancelled"))
    else:
        await update.message.reply_text("Nothing pending.")


async def cmd_away(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    arg = (context.args or [""])[0].lower()
    if arg == "on":
        context.bot_data["away_mode"] = True
        await update.message.reply_text("Away mode ON.")
    elif arg == "off":
        context.bot_data["away_mode"] = False
        await update.message.reply_text("Away mode OFF.")
    else:
        state = context.bot_data.get("away_mode", False)
        await update.message.reply_text(
            f"Away mode is {'ON' if state else 'OFF'}. Use /away on or /away off"
        )


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    if not LOG_PATH.exists():
        await update.message.reply_text("No requests saved yet.")
        return
    rows = LOG_PATH.read_text(encoding="utf-8").strip().splitlines()[-20:]
    if not rows:
        await update.message.reply_text("No requests saved yet.")
        return
    out = []
    for row in reversed(rows):
        try:
            item = json.loads(row)
            out.append(
                f"[{item['time']}] {item['kind']} | {item['chat_title']} | "
                f"{item['full_name'] or item['username']}: {item['text'][:80]}"
            )
        except Exception:
            continue
    await update.message.reply_text("\n".join(out)[:4000])


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.bot_data.setdefault("pending", {})
    key = str(update.effective_chat.id)
    job = pending.get(key)
    if not job:
        return False
    if time.time() > job["expires"]:
        del pending[key]
        return False
    user_text = (update.effective_message.text or "").strip().lower()
    if contains_any(user_text, CFG.get("confirm_words", [])):
        del pending[key]
        router = job["router"]
        action = job["action"]
        try:
            ok, result = await asyncio.wait_for(
                asyncio.to_thread(mikrotik.do_action, router, action), timeout=120
            )
        except Exception as exc:
            ok, result = False, str(exc)
        msg_text = update.effective_message.text or ""
        if ok:
            await update.effective_message.reply_text(fmt("action_done", result=result, text=msg_text))
        else:
            await update.effective_message.reply_text(fmt("action_failed", result=result, text=msg_text))
        await notify_admins(
            context,
            f"Action '{action}' on {router.get('name')} ({router['host']}) "
            f"in group {job['chat_title']}: {'OK' if ok else 'FAILED'} - {result}",
        )
        return True
    return False


async def handle_slow(update: Update, context: ContextTypes.DEFAULT_TYPE, group_cfg, text_lower):
    chat = update.effective_chat
    name = user_display_name(update)
    user_text = update.effective_message.text or ""

    matched = filter_routers_by_text(group_cfg, text_lower)
    if matched:
        target_cfg = dict(group_cfg)
        target_cfg["routers"] = matched
        label = wifi_label_of(matched)
    else:
        target_cfg = group_cfg
        label = "your connection"

    await update.message.reply_text(fmt("checking", user=name, wifi=label, text=user_text))

    log_request("slow_complaint", update)
    scope = f"WiFi: {label}" if matched else "all routers"
    await notify_admins(
        context,
        f"SLOW complaint in '{chat.title}' ({scope}):\n"
        f"{user_text[:200]}",
    )

    report, problems = await run_group_checks(target_cfg)
    text = fmt("report_header", group=chat.title or "", text=user_text) + "\n\n" + report

    actionable = [p for p in problems if p["action"]]
    if actionable:
        first = actionable[0]
        lang = detect_language(user_text)
        if first["action"] == "reconnect_pppoe":
            if lang == "zh":
                btn_text = "重新连接PPPoE"
                desc = f"发现路由器 {first['router'].get('name')} 有问题：\n" + "\n".join("- " + i for i in first["issues"]) + "\n\n我可以重新连接线路。网络可能会断开约1分钟。"
            else:
                btn_text = "Reconnect PPPoE"
                desc = f"I found a problem on router {first['router'].get('name')}:\n" + "\n".join("- " + i for i in first["issues"]) + "\n\nI can reconnect the line now. The network may disconnect for about 1 minute."
        else:
            if lang == "zh":
                btn_text = "重启路由器"
                desc = f"发现路由器 {first['router'].get('name')} 有问题：\n" + "\n".join("- " + i for i in first["issues"]) + "\n\n我可以重启路由器。网络会断开约3-5分钟。"
            else:
                btn_text = "Restart Router"
                desc = f"I found a problem on router {first['router'].get('name')}:\n" + "\n".join("- " + i for i in first["issues"]) + "\n\nI can reboot the router now. The network will be down about 3-5 minutes."
        callback_data = f"reboot:{first['action']}:{first['router'].get('host', '')}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, callback_data=callback_data)]])
        context.bot_data.setdefault("pending", {})[str(chat.id)] = {
            "router": first["router"],
            "action": first["action"],
            "expires": time.time() + CFG.get("confirm_timeout_sec", 600),
            "chat_title": chat.title or "",
        }
        await update.effective_message.reply_text(desc[:4000], reply_markup=keyboard)
    elif not problems:
        text += "\n\n" + fmt("followup", text=user_text)
        await update.effective_message.reply_text(text[:4000])
    else:
        await update.effective_message.reply_text(text[:4000])


async def handle_port_check(update: Update, context: ContextTypes.DEFAULT_TYPE, group_cfg, text):
    chat = update.effective_chat
    name = user_display_name(update)
    user_text = update.effective_message.text or ""
    port_num = extract_port(user_text)
    if not port_num:
        await update.message.reply_text("Please specify port number (1-8). Example: check port 3")
        return
    if not group_cfg or not group_cfg.get("routers"):
        await update.message.reply_text("No router configured. Use /setupRT first.")
        return
    await update.message.reply_text(f"Checking port {port_num} on your router...")
    router = group_cfg["routers"][0]
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(mikrotik.check_port, router, port_num), timeout=30
        )
    except Exception as exc:
        await update.message.reply_text(f"Cannot check port: {exc}")
        return
    parts = [
        f"Port {port_num} ({res['port_name']}) on {res['name']}:",
        f"Status: {res['status']}",
    ]
    if res["speed"] and res["speed"] != "unknown":
        parts.append(f"Speed: {res['speed']}")
    if res["rx_byte"] or res["tx_byte"]:
        rx_mb = round(res["rx_byte"] / (1024 * 1024), 1)
        tx_mb = round(res["tx_byte"] / (1024 * 1024), 1)
        parts.append(f"Traffic: down {rx_mb} MB / up {tx_mb} MB")
    if res.get("error"):
        parts.append(f"Error: {res['error']}")
    if res["issues"]:
        parts.append("Problems:")
        for issue in res["issues"]:
            parts.append(f"- {issue}")
    if res["status"] == "UP":
        parts.append("Port is working normally.")
    elif res["status"] == "DOWN":
        parts.append("Port is DOWN - check cable connection.")
    elif res["status"] == "NO CABLE":
        parts.append("No cable plugged in on this port.")
    await update.message.reply_text("\n".join(parts))


async def handle_new_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = (user.full_name if user else "") or ""
    tag = f" (@{user.username})" if user and user.username else ""
    user_text = update.effective_message.text or ""
    await update.message.reply_text(fmt("new_connect_reply", user=name, text=user_text))
    log_request("new_connect", update)
    await notify_admins(
        context,
        f"NEW CONNECT request in '{update.effective_chat.title}':\n"
        f"From: {name}{tag}\n"
        f"Text: {user_text[:200]}",
    )


async def handle_non_duty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.effective_message.text or ""
    await update.message.reply_text(
        fmt("non_duty_reply", user=user_display_name(update), text=user_text)
    )
    log_request("non_duty", update)


async def handle_away_notice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.bot_data.get("away_mode", False):
        return False
    cooldown = int(CFG.get("away_notice_cooldown_sec", 300))
    seen = context.bot_data.setdefault("away_last", {})
    key = str(update.effective_chat.id)
    if time.time() - seen.get(key, 0) < cooldown:
        return False
    user_text = (update.effective_message.text or "").lower()
    if contains_any(user_text, CFG.get("away_notice_keywords", [])):
        seen[key] = time.time()
        await update.message.reply_text(fmt("away_notice", text=update.effective_message.text or ""))
        log_request("away_notice", update)
        return True
    return False


async def group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if user and user.is_bot:
        return
    chat = update.effective_chat
    text = message.text or ""
    text_lower = text.lower()

    if await handle_confirm(update, context):
        return

    group_cfg = get_group_cfg(chat.id)

    slow_hit = contains_any(text_lower, CFG.get("slow_keywords", []))
    new_hit = contains_any(text_lower, CFG.get("new_connect_keywords", []))
    greet_hit = contains_any(text_lower, CFG.get("greeting_keywords", []))
    port_hit = contains_any(text_lower, CFG.get("port_keywords", [])) or extract_port(text) is not None
    duty_hit = (
        CFG.get("auto_decline_enabled")
        and bool(CFG.get("non_duty_keywords"))
        and contains_any(text_lower, CFG.get("non_duty_keywords", []))
    )

    mentions_bot = bool(
        context.bot.username and f"@{context.bot.username.lower()}" in text_lower
    )
    reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == context.bot.id
    )
    active_chat = ai_agent.recent_activity(chat.id, int(CFG.get("ai_window_min", 30)))

    should_reply = (
        slow_hit or new_hit or greet_hit or port_hit or mentions_bot
        or reply_to_bot or active_chat
        or bool(group_cfg and group_cfg.get("always_reply"))
    )

    if not should_reply:
        await handle_away_notice(update, context)
        return

    if slow_hit:
        log_request("slow_complaint", update)
        await notify_admins(
            context,
            f"SLOW complaint in '{chat.title}': {text[:200]}",
        )
    if new_hit:
        log_request("new_connect", update)
        await notify_admins(
            context,
            f"NEW CONNECT request in '{chat.title}': {text[:200]}",
        )
    if duty_hit:
        log_request("non_duty", update)

    if not group_cfg:
        if chat.type == ChatType.PRIVATE:
            group_cfg = {"routers": []}
        else:
            await update.message.reply_text(
                "This group is not configured yet. Use /setupRT first."
            )
            return

    if port_hit:
        routers = group_cfg.get("routers", [])
        if len(routers) > 1:
            lang = detect_language(text)
            if lang == "zh":
                await update.message.reply_text("请问要检查哪个WiFi？", reply_markup=build_wifi_keyboard(group_cfg))
            else:
                await update.message.reply_text("Which WiFi would you like me to check?", reply_markup=build_wifi_keyboard(group_cfg))
            return
        elif routers:
            r = dict(routers[0])
            r.setdefault("check_host", CFG.get("check_host", "8.8.8.8"))
            await handle_port_check(update, context, group_cfg, text)
            return
        await handle_port_check(update, context, group_cfg, text)
        return

    if slow_hit and group_cfg and len(group_cfg.get("routers", [])) > 0:
        lang = detect_language(text)
        if len(group_cfg.get("routers", [])) > 1:
            if lang == "zh":
                await update.message.reply_text("请问哪个WiFi有问题？请选择：", reply_markup=build_wifi_keyboard(group_cfg))
            else:
                await update.message.reply_text("Which WiFi has a problem? Please select:", reply_markup=build_wifi_keyboard(group_cfg))
        else:
            router = group_cfg["routers"][0]
            wifi = router.get("wifi") or router.get("name") or "WiFi"
            msg = await update.message.reply_text(f"Checking {wifi}...")
            r = dict(router)
            r.setdefault("check_host", CFG.get("check_host", "8.8.8.8"))
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(mikrotik.check_router, r), timeout=90
                )
            except Exception as exc:
                await msg.edit_text(f"Check failed: {exc}")
                return
            report = format_router_report(res)
            if res["issues"]:
                report += "\n\n" + suggest_action(res)
            await msg.edit_text(report[:4000])
        return

    if CFG.get("ai_enabled", True) and ai_agent.ai_ready():
        name = user_display_name(update)
        away = context.bot_data.get("away_mode", False)
        try:
            await context.bot.send_chat_action(chat.id, "typing")
        except Exception:
            pass
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    ai_agent.run_agent,
                    chat.id,
                    name,
                    text,
                    group_cfg,
                    context.bot_data,
                    away,
                    context.bot.username,
                    chat.title or "",
                ),
                timeout=180,
            )
        except Exception as exc:
            log.error(f"AI agent failed: {exc}")
            reply = "😓 The AI assistant is temporarily unavailable. Please try again in a minute."
        if reply:
            keyboard = None
            pending = context.bot_data.get("pending", {})
            pending_job = pending.get(str(chat.id))
            if pending_job and time.time() < pending_job.get("expires", 0):
                action = pending_job.get("action", "")
                router_host = pending_job.get("router", {}).get("host", "")
                lang = detect_language(text)
                if action == "reconnect_pppoe":
                    btn_text = "重新连接PPPoE" if lang == "zh" else "Reconnect PPPoE"
                else:
                    btn_text = "重启路由器" if lang == "zh" else "Restart Router"
                callback_data = f"reboot:{action}:{router_host}"
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, callback_data=callback_data)]])
            elif group_cfg.get("routers") and ("?" in reply or "wifi" in reply.lower()):
                keyboard = build_wifi_keyboard(group_cfg)
            await message.reply_text(reply[:4000], reply_markup=keyboard)
            ai_agent.mark_activity(chat.id)
        return

    await legacy_flow(update, context, group_cfg, text_lower, slow_hit, new_hit, duty_hit)


async def legacy_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    group_cfg,
    text_lower,
    slow_hit,
    new_hit,
    duty_hit,
):
    if slow_hit:
        await handle_slow(update, context, group_cfg, text_lower)
        return
    if new_hit:
        await handle_new_connect(update, context)
        return
    if duty_hit:
        await handle_non_duty(update, context)
        return
    await handle_away_notice(update, context)


def main():
    token = CFG.get("bot_token", "")
    if not token or "PUT" in token.upper():
        raise SystemExit("Put your bot token into config.json first (bot_token).")
    app = Application.builder().token(token).build()
    app.bot_data["pending"] = {}
    app.add_handler(CommandHandler(["start"], cmd_start))
    app.add_handler(CommandHandler(["help"], cmd_help))
    app.add_handler(CommandHandler(["myid"], cmd_myid))
    app.add_handler(CommandHandler(["setupRT"], cmd_setup))
    app.add_handler(CommandHandler(["addnewwifi"], cmd_setupnewwifi))
    app.add_handler(CommandHandler(["deletewifi"], cmd_deletewifi))
    app.add_handler(CommandHandler(["status"], cmd_status))
    app.add_handler(CommandHandler(["cancel"], cmd_cancel))
    app.add_handler(CommandHandler(["away"], cmd_away))
    app.add_handler(CommandHandler(["pending"], cmd_pending))
    app.add_handler(CommandHandler(["always"], cmd_always))
    app.add_handler(CallbackQueryHandler(on_wifi_button, pattern="^wifi:"))
    app.add_handler(CallbackQueryHandler(on_delete_wifi_button, pattern="^dwifi:"))
    app.add_handler(CallbackQueryHandler(on_reboot_button, pattern="^reboot:"))
    app.add_handler(
        MessageHandler(
            (filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
            & filters.TEXT
            & ~filters.COMMAND,
            group_message,
        )
    )
    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
