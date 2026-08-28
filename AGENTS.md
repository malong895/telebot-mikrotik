# ITFF Telegram Bot - Project Instructions (auto-loaded every session)

This folder is the USER'S PRODUCTION Telegram bot. It answers ISP customers
and checks MikroTik routers. Edit carefully; an admin runs this bot 24/7.

## Critical rules

- NEVER print, log, or paste the bot token, Groq API key, or router
  passwords into chat or files. config.json is secrets-protected.
- NEVER execute disruptive router actions (reboot, PPPoE reconnect) yourself.
  Only the bot's own confirm-flow does that after the CUSTOMER replies YES.
- Editing config.json or code requires RESTARTING the running bot to take
  effect (restart = rerun start-bot.bat, see below).

## How it runs (Windows)

- One supervised instance: `start-bot.bat` -> `venv\Scripts\pythonw.exe
  service_runner.py` -> supervises bot.py (auto-restart on crash, single
  instance via mutex). Auto-starts at login (registry Run key `ITFFBot`).
- DO NOT start bot.py manually a second time - it makes the bot reply twice.
- Check health: `logs\service.log`, `logs\bot.log` (look for
  "Application started" = online).
- Restart: kill python processes running bot.py/service_runner.py in this
  folder, then run start-bot.bat once. Or reboot the PC.

## Architecture

- `bot.py` - Telegram handlers (group_message, callbacks, commands).
  Admin commands: /setupRT /addnewwifi /deletewifi /status /cancel /away
  /pending /myid.
- `ai_agent.py` - AI brain (Groq, model openai/gpt-oss-120b). Tools:
  list_group_networks, get_router_status, get_customer_plan,
  request_maintenance (needs customer YES). With more than 1 router the
  bot shows WiFi buttons; clicking a button checks ONLY that router.
- `mikrotik.py` - RouterOS API (port 8292) checks + actions.
- `service_runner.py` - supervisor (restart-on-crash, single instance).
- `config.json` - bot_token, admin_chat_ids, openai_api_key, groups map.
- `logs\requests.jsonl` - saved customer requests; `logs\bot.log` - bot log.

## Deployment / 24/7 (VPS)

- Oracle Always Free VPS is the chosen path. See the vps-deploy skill and
  `C:\Users\itfas\OneDrive\Documents\IT.FF\deploy-to-vps.ps1`.
- When deploying: copy THIS folder (not the older IT.FF telegram-mikrotik-bot
  backup). Do not run the PC bot and VPS bot at the same time.

## Router checks (mikrotik.py)

CPU, RAM, uptime, WAN live down/up Mbps, ping loss/latency to 8.8.8.8, users
per SSID, PPPoE status, DHCP leases. Issues -> CPU>=85, loss>10%, avg>150ms,
PPPoE down, SSID empty/crowded.