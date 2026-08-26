import re
import time

import paramiko
import routeros_api


def _ros_time_to_ms(value):
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(us|ms|s)", str(value).lower()):
        v = float(num)
        if unit == "us":
            total += v / 1000.0
        elif unit == "ms":
            total += v
        elif unit == "s":
            total += v * 1000.0
    return total


class RouterError(Exception):
    pass


def _dec(value):
    return value.decode(errors="ignore") if isinstance(value, bytes) else value


def _connect(router):
    host = router["host"]
    user = router.get("user", "admin")
    password = router.get("password", "")
    ssl_port = int(router.get("api_port", 52743))
    ssh_port = int(router.get("ssh_port", 44222))

    try:
        pool = routeros_api.RouterOsApiPool(
            host,
            username=user,
            password=password,
            port=ssl_port,
            plaintext_login=True,
            use_ssl=True,
        )
        api = pool.get_api()
        return pool, api
    except Exception:
        pass

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=ssh_port, username=user, password=password,
                timeout=10, allow_agent=False, look_for_keys=False)
    return ssh, None


def _detect_wan(api):
    if api is None:
        return None
    try:
        routes = api.get_resource("/ip/route").get()
        for route in routes:
            dst = str(route.get("dst-address", ""))
            if dst in ("0.0.0.0/0", ""):
                raw = route.get("immediate-gw") or route.get("gateway-status") or ""
                tokens = _dec(raw).replace("%", " ").split()
                for token in reversed(tokens):
                    if token and token[0].isalpha():
                        return token
                for token in tokens:
                    if token:
                        return token
    except Exception:
        pass
    return None


def _detect_wan_ssh(ssh):
    try:
        stdin, stdout, stderr = ssh.exec_command("/ip/route/print where dst-address=0.0.0.0/0", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if "0.0.0.0/0" in line:
                parts = line.split()
                for part in reversed(parts):
                    if part and part[0].isalpha() and "%" not in part:
                        return part
    except Exception:
        pass
    return None


def _iface_bytes(api, wan_name):
    if api is None:
        return 0, 0
    try:
        interfaces = api.get_resource("/interface").get()
        for iface in interfaces:
            name = _dec(iface.get("name", ""))
            if name == wan_name or (wan_name is None and name.startswith("ether")):
                rx = int(_dec(iface.get("rx-byte", 0)) or 0)
                tx = int(_dec(iface.get("tx-byte", 0)) or 0)
                return rx, tx
        if interfaces:
            first = interfaces[0]
            rx = int(_dec(first.get("rx-byte", 0)) or 0)
            tx = int(_dec(first.get("tx-byte", 0)) or 0)
            return rx, tx
    except Exception:
        pass
    return 0, 0


def _iface_bytes_ssh(ssh, wan_name):
    try:
        stdin, stdout, stderr = ssh.exec_command("/interface/print", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if wan_name and wan_name in line:
                parts = line.split()
                rx = int(parts[5]) if len(parts) > 5 else 0
                tx = int(parts[6]) if len(parts) > 6 else 0
                return rx, tx
    except Exception:
        pass
    return 0, 0


def _ping_stats(api, check_host, count=5):
    if api is None:
        return None
    try:
        rows = api.get_binary_resource("/").call(
            "ping", {"address": str(check_host).encode(), "count": str(count).encode()}
        )
        sent = 0
        received = 0
        times = []
        for row in rows:
            clean = {_dec(k): _dec(v) for k, v in row.items()}
            sent += 1
            if "time" in clean:
                try:
                    ms = _ros_time_to_ms(clean["time"])
                    times.append(ms)
                    received += 1
                except ValueError:
                    pass
        loss = 100.0 * (sent - received) / sent if sent else 100.0
        avg = sum(times) / len(times) if times else None
        worst = max(times) if times else None
        return {"avg_ms": avg, "loss_pct": loss, "worst_ms": worst}
    except Exception:
        return None


def _ping_stats_ssh(ssh, check_host, count=5):
    try:
        stdin, stdout, stderr = ssh.exec_command(
            f"/ping {check_host} count={count}", timeout=30
        )
        out = stdout.read().decode("utf-8", errors="ignore")
        sent = 0
        received = 0
        times = []
        for line in out.splitlines():
            if "time=" in line:
                sent += 1
                m = re.search(r"time=([\d.]+)(ms|s)", line)
                if m:
                    val = float(m.group(1))
                    unit = m.group(2)
                    ms = val * 1000 if unit == "s" else val
                    times.append(ms)
                    received += 1
            elif "sent=" in line:
                m = re.search(r"sent=(\d+).*received=(\d+)", line)
                if m:
                    sent = int(m.group(1))
                    received = int(m.group(2))
        loss = 100.0 * (sent - received) / sent if sent else 100.0
        avg = sum(times) / len(times) if times else None
        worst = max(times) if times else None
        return {"avg_ms": avg, "loss_pct": loss, "worst_ms": worst}
    except Exception:
        return None


def _wifi_users(api, expected_wifi):
    total = 0
    on_wifi = 0
    if api is None:
        return total, on_wifi
    sources = [
        "/interface/wireless/registration-table",
        "/interface/wifi/registration-table",
    ]
    for source in sources:
        try:
            rows = api.get_resource(source).get()
            total = len(rows)
            if expected_wifi:
                target = expected_wifi.strip().lower()
                on_wifi = sum(
                    1 for r in rows if str(_dec(r.get("ssid", ""))).strip().lower() == target
                )
            return total, on_wifi
        except Exception:
            continue
    return total, on_wifi


def _wifi_users_ssh(ssh, expected_wifi):
    total = 0
    on_wifi = 0
    for source in ["/interface/wireless/registration-table", "/interface/wifi/registration-table"]:
        try:
            stdin, stdout, stderr = ssh.exec_command(f"{source} print", timeout=10)
            out = stdout.read().decode("utf-8", errors="ignore")
            lines = [l for l in out.strip().splitlines() if l.strip() and not l.startswith("Columns")]
            total = len(lines)
            if expected_wifi:
                target = expected_wifi.strip().lower()
                for line in lines:
                    if target in line.lower():
                        on_wifi += 1
            return total, on_wifi
        except Exception:
            continue
    return total, on_wifi


def _pppoe_status(api):
    states = []
    if api is None:
        return states
    try:
        clients = api.get_resource("/interface/pppoe-client").get()
        for client in clients:
            running = client.get("running")
            running = running in (True, "true", "yes", 1) or str(running).lower() == "true"
            states.append({"name": _dec(client.get("name", "?")), "running": running})
    except Exception:
        pass
    return states


def _pppoe_status_ssh(ssh):
    states = []
    try:
        stdin, stdout, stderr = ssh.exec_command("/interface/pppoe-client print", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.strip().splitlines():
            if "pppoe" in line.lower():
                parts = line.split()
                name = parts[0] if parts else "?"
                running = "R" in parts
                states.append({"name": name, "running": running})
    except Exception:
        pass
    return states


def _all_interfaces(api):
    ifaces = []
    try:
        rows = api.get_resource("/interface").get()
        for r in rows:
            name = _dec(r.get("name", ""))
            running = r.get("running")
            running = running in (True, "true", "yes", 1) or str(running).lower() == "true"
            disabled = r.get("disabled")
            disabled = disabled in (True, "true", "yes", 1) or str(disabled).lower() == "true"
            tx_rate = _dec(r.get("tx-rate", ""))
            rx_rate = _dec(r.get("rx-rate", ""))
            ifaces.append({"name": name, "running": running, "disabled": disabled, "tx_rate": tx_rate, "rx_rate": rx_rate})
    except Exception:
        pass
    return ifaces


def _all_interfaces_ssh(ssh):
    ifaces = []
    try:
        stdin, stdout, stderr = ssh.exec_command("/interface print", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if not line.strip() or line.startswith("Columns"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            flags = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            running = "R" in flags
            disabled = "X" in flags
            ifaces.append({"name": name, "running": running, "disabled": disabled, "tx_rate": "", "rx_rate": ""})
    except Exception:
        pass
    return ifaces


def _dns_status(api):
    result = {"servers": [], "dynamic": False}
    try:
        dns = api.get_resource("/ip/dns").get()
        if dns:
            d = dns[0]
            servers_str = _dec(d.get("servers", ""))
            result["servers"] = [s.strip() for s in servers_str.split(",") if s.strip()]
            dynamic = d.get("dynamic-dns")
            result["dynamic"] = dynamic in (True, "true", "yes", 1) or str(dynamic).lower() == "true"
    except Exception:
        pass
    return result


def _dns_status_ssh(ssh):
    result = {"servers": [], "dynamic": False}
    try:
        stdin, stdout, stderr = ssh.exec_command("/ip/dns print", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if "servers:" in line.lower():
                servers_part = line.split(":", 1)
                if len(servers_part) > 1:
                    result["servers"] = [s.strip() for s in servers_part[1].split(",") if s.strip()]
            if "dynamic-dns:" in line.lower() and "yes" in line.lower():
                result["dynamic"] = True
    except Exception:
        pass
    return result


def _hotspot_active(api):
    users = []
    try:
        rows = api.get_resource("/ip/hotspot/active").get()
        for r in rows:
            user = _dec(r.get("user", r.get("mac-address", "")))
            ip = _dec(r.get("ip", ""))
            uptime = _dec(r.get("uptime", ""))
            users.append({"user": user, "ip": ip, "uptime": uptime})
    except Exception:
        pass
    return users


def _hotspot_active_ssh(ssh):
    users = []
    try:
        stdin, stdout, stderr = ssh.exec_command("/ip/hotspot/active print", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if not line.strip() or line.startswith("Columns"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                users.append({"user": parts[0], "ip": parts[1] if len(parts) > 1 else "", "uptime": parts[-1] if len(parts) > 2 else ""})
    except Exception:
        pass
    return users


def _system_logs(ssh, count=5):
    logs = []
    try:
        stdin, stdout, stderr = ssh.exec_command(f"/log print stats-only where topics~\"error\" do={{;}} count={count}", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.strip().splitlines()[:count]:
            if line.strip():
                logs.append(line.strip())
    except Exception:
        pass
    if not logs:
        try:
            stdin, stdout, stderr = ssh.exec_command("/log print where topics~\"error\"", timeout=10)
            out = stdout.read().decode("utf-8", errors="ignore")
            lines = [l.strip() for l in out.strip().splitlines() if l.strip() and not l.startswith("Columns")]
            logs = lines[-count:]
        except Exception:
            pass
    return logs


def _system_temp(ssh):
    try:
        stdin, stdout, stderr = ssh.exec_command("/system health print", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if "temperature" in line.lower() and "=" in line:
                _, _, val = line.partition("=")
                temp = val.strip().replace("C", "").strip()
                if temp.replace(".", "").isdigit():
                    return float(temp)
            elif line.strip().startswith("temperature"):
                parts = line.split()
                for p in parts:
                    if p.replace(".", "").isdigit():
                        return float(p)
    except Exception:
        pass
    return None


def _disk_space(ssh):
    try:
        stdin, stdout, stderr = ssh.exec_command("/system disk print", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if "free" in line.lower() and "=" in line:
                _, _, val = line.partition("=")
                free = val.strip()
                return free
    except Exception:
        pass
    return None


def _active_connections(ssh):
    count = 0
    try:
        stdin, stdout, stderr = ssh.exec_command("/ip/firewall/connection print count-only", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        if out.strip().isdigit():
            count = int(out.strip())
    except Exception:
        pass
    return count


def _queues_status(ssh):
    queues = []
    try:
        stdin, stdout, stderr = ssh.exec_command("/queue simple print stats", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if not line.strip() or line.startswith("Columns"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                queues.append({"name": parts[0], "target": parts[1] if len(parts) > 1 else ""})
    except Exception:
        pass
    return queues


def _routes_count(ssh):
    count = 0
    try:
        stdin, stdout, stderr = ssh.exec_command("/ip/route print count-only", timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        if out.strip().isdigit():
            count = int(out.strip())
    except Exception:
        pass
    return count


def check_router(router):
    result = {
        "name": router.get("name") or router["host"],
        "host": router["host"],
        "online": False,
        "cpu": None,
        "mem_free_mb": None,
        "uptime": None,
        "version": None,
        "wan": None,
        "rx_mbps": None,
        "tx_mbps": None,
        "ping": None,
        "users_total": None,
        "users_on_wifi": None,
        "pppoe": [],
        "dhcp_leases": None,
        "interfaces": [],
        "dns": {},
        "hotspot_active": [],
        "logs_errors": [],
        "temperature": None,
        "disk_free": None,
        "active_conns": 0,
        "queues": [],
        "routes_count": 0,
        "issues": [],
    }
    conn = None
    api = None
    ssh = None
    use_ssh = False
    try:
        conn, api = _connect(router)
        if api is None:
            ssh = conn
            use_ssh = True
    except Exception as exc:
        result["error"] = str(exc)
        result["issues"].append("Cannot connect to router (check IP/port/user/password)")
        return result
    try:
        if use_ssh:
            stdin, stdout, stderr = ssh.exec_command("/system/resource print", timeout=10)
            res_out = stdout.read().decode("utf-8", errors="ignore")
            res = {}
            for line in res_out.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    res[k.strip()] = v.strip()
        else:
            res = api.get_resource("/system/resource").get()[0]
        result["online"] = True
        result["cpu"] = int(float(res.get("cpu-load", 0)))
        free_mem = int(res.get("free-memory", 0))
        result["mem_free_mb"] = round(free_mem / (1024 * 1024), 1)
        result["uptime"] = res.get("uptime", "?")
        result["version"] = res.get("version", "?")

        wan = _detect_wan_ssh(ssh) if use_ssh else _detect_wan(api)
        result["wan"] = wan
        if wan:
            if use_ssh:
                rx1, tx1 = _iface_bytes_ssh(ssh, wan)
                time.sleep(2)
                rx2, tx2 = _iface_bytes_ssh(ssh, wan)
            else:
                rx1, tx1 = _iface_bytes(api, wan)
                time.sleep(2)
                rx2, tx2 = _iface_bytes(api, wan)
            result["rx_mbps"] = round(max(0, rx2 - rx1) * 8 / 2 / 1_000_000, 2)
            result["tx_mbps"] = round(max(0, tx2 - tx1) * 8 / 2 / 1_000_000, 2)

        if use_ssh:
            result["ping"] = _ping_stats_ssh(ssh, router.get("check_host", "8.8.8.8"))
            result["users_total"], result["users_on_wifi"] = _wifi_users_ssh(ssh, router.get("wifi"))
            result["pppoe"] = _pppoe_status_ssh(ssh)
            result["interfaces"] = _all_interfaces_ssh(ssh)
            result["dns"] = _dns_status_ssh(ssh)
            result["hotspot_active"] = _hotspot_active_ssh(ssh)
            result["logs_errors"] = _system_logs(ssh, 5)
            result["temperature"] = _system_temp(ssh)
            result["disk_free"] = _disk_space(ssh)
            result["active_conns"] = _active_connections(ssh)
            result["queues"] = _queues_status(ssh)
            result["routes_count"] = _routes_count(ssh)
        else:
            try:
                result["ping"] = _ping_stats(api, router.get("check_host", "8.8.8.8"))
            except Exception:
                result["ping"] = None
            result["users_total"], result["users_on_wifi"] = _wifi_users(api, router.get("wifi"))
            result["pppoe"] = _pppoe_status(api)
            result["interfaces"] = _all_interfaces(api)
            result["dns"] = _dns_status(api)
            result["hotspot_active"] = _hotspot_active(api)

        try:
            if use_ssh:
                stdin, stdout, stderr = ssh.exec_command("/ip/dhcp-server/lease print count-only", timeout=10)
                dhcp_out = stdout.read().decode("utf-8", errors="ignore")
                result["dhcp_leases"] = int(dhcp_out.strip()) if dhcp_out.strip().isdigit() else None
            else:
                result["dhcp_leases"] = len(api.get_resource("/ip/dhcp-server/lease").get())
        except Exception:
            result["dhcp_leases"] = None

        max_users = int(router.get("max_users", 40))
        if result["cpu"] is not None and result["cpu"] >= 85:
            result["issues"].append(f"CPU high: {result['cpu']}%")
        if result["ping"]:
            if result["ping"]["loss_pct"] > 10:
                result["issues"].append(
                    f"Packet loss {result['ping']['loss_pct']:.0f}% to {router.get('check_host', '8.8.8.8')}"
                )
            elif result["ping"]["avg_ms"] and result["ping"]["avg_ms"] > 150:
                result["issues"].append(
                    f"High latency avg {result['ping']['avg_ms']:.0f} ms"
                )
        if result["pppoe"]:
            down = [p["name"] for p in result["pppoe"] if not p["running"]]
            if down:
                result["issues"].append("PPPoE down: " + ", ".join(down))
        if router.get("wifi") and result["users_total"]:
            if result["users_on_wifi"] == 0:
                result["issues"].append(f"No client connected on SSID '{router['wifi']}'")
            elif result["users_on_wifi"] >= max_users:
                result["issues"].append(
                    f"SSID '{router['wifi']}' crowded: {result['users_on_wifi']} users"
                )
        if result.get("interfaces"):
            down_ifaces = [i["name"] for i in result["interfaces"] if not i["running"] and not i["disabled"] and i["name"] not in ("lo", "bridge1")]
            if down_ifaces:
                result["issues"].append(f"Interfaces down: {', '.join(down_ifaces[:5])}")
        if result.get("temperature") and result["temperature"] > 60:
            result["issues"].append(f"Temperature high: {result['temperature']}C")
        if result.get("dns", {}).get("servers"):
            pass
        elif result["online"]:
            result["issues"].append("DNS not configured")
        if result.get("logs_errors"):
            error_count = len(result["logs_errors"])
            if error_count > 0:
                result["issues"].append(f"{error_count} recent errors in logs")
        if result["active_conns"] > 5000:
            result["issues"].append(f"High connection count: {result['active_conns']}")
    finally:
        try:
            if use_ssh and ssh:
                ssh.close()
            elif not use_ssh and conn:
                conn.disconnect()
        except Exception:
            pass
    return result


def check_port(router, port_num):
    result = {
        "name": router.get("name") or router["host"],
        "host": router["host"],
        "port": port_num,
        "port_name": f"ether{port_num}",
        "online": False,
        "status": "unknown",
        "speed": "unknown",
        "rx_byte": 0,
        "tx_byte": 0,
        "rx_rate": "0",
        "tx_rate": "0",
        "issues": [],
    }
    conn = None
    api = None
    ssh = None
    use_ssh = False
    try:
        conn, api = _connect(router)
        if api is None:
            ssh = conn
            use_ssh = True
    except Exception as exc:
        result["error"] = str(exc)
        result["issues"].append("Cannot connect to router")
        return result
    try:
        port_name = f"ether{port_num}"
        if use_ssh:
            stdin, stdout, stderr = ssh.exec_command(f"/interface/ethernet print where name={port_name}", timeout=10)
            out = stdout.read().decode("utf-8", errors="ignore")
        else:
            out = str(api.get_resource("/interface/ethernet").get(**{"name": port_name}))
        result["online"] = True
        if "running" in out.lower() or "R" in out:
            result["status"] = "UP"
        elif "no-save" in out.lower() or not out.strip():
            result["status"] = "NO CABLE"
        else:
            result["status"] = "DOWN"
        if use_ssh:
            stdin, stdout, stderr = ssh.exec_command(f"/interface print where name={port_name}", timeout=10)
            iface_out = stdout.read().decode("utf-8", errors="ignore")
        else:
            iface_out = str(api.get_resource("/interface").get(**{"name": port_name}))
        for line in (iface_out if isinstance(iface_out, str) else str(iface_out)).splitlines():
            if port_name in line:
                parts = line.split()
                if len(parts) > 5:
                    try:
                        result["rx_byte"] = int(parts[5])
                        result["tx_byte"] = int(parts[6]) if len(parts) > 6 else 0
                    except (ValueError, IndexError):
                        pass
        if use_ssh:
            stdin, stdout, stderr = ssh.exec_command(f"/interface/ethernet monitor {port_name} once", timeout=10)
            monitor_out = stdout.read().decode("utf-8", errors="ignore")
            for line in monitor_out.splitlines():
                if "rate" in line.lower() or "speed" in line.lower():
                    result["speed"] = line.split(":")[-1].strip() if ":" in line else line.strip()
                if "status" in line.lower():
                    result["status"] = line.split(":")[-1].strip() if ":" in line else result["status"]
        else:
            try:
                monitor = api.get_resource("/interface/ethernet").get(**{"name": port_name})
                if monitor:
                    m = monitor[0]
                    result["speed"] = str(m.get("speed", "unknown"))
                    result["status"] = str(m.get("status", result["status"]))
            except Exception:
                pass
    except Exception as exc:
        result["issues"].append(f"Error checking port: {exc}")
    finally:
        try:
            if use_ssh and ssh:
                ssh.close()
            elif not use_ssh and conn:
                conn.disconnect()
        except Exception:
            pass
    return result


def get_plan(router):
    conn = None
    api = None
    ssh = None
    use_ssh = False
    try:
        conn, api = _connect(router)
        if api is None:
            ssh = conn
            use_ssh = True
    except Exception as exc:
        return {"error": str(exc)}
    try:
        queues = []
        ppp_active = []
        if use_ssh:
            try:
                stdin, stdout, stderr = ssh.exec_command("/queue/simple print", timeout=10)
                out = stdout.read().decode("utf-8", errors="ignore")
                for line in out.strip().splitlines():
                    if line.strip() and not line.startswith("Columns"):
                        parts = line.split()
                        if len(parts) >= 4:
                            queues.append({"name": parts[0], "target": parts[1], "max_limit": parts[3], "rate_now": parts[4] if len(parts) > 4 else ""})
            except Exception:
                pass
            try:
                stdin, stdout, stderr = ssh.exec_command("/ppp/active print", timeout=10)
                out = stdout.read().decode("utf-8", errors="ignore")
                for line in out.strip().splitlines():
                    if line.strip() and not line.startswith("Columns"):
                        parts = line.split()
                        if len(parts) >= 2:
                            ppp_active.append({"user": parts[0], "profile": parts[1], "service": parts[2] if len(parts) > 2 else ""})
            except Exception:
                pass
        else:
            try:
                for q in api.get_resource("/queue/simple").get():
                    queues.append({"name": _dec(q.get("name")), "target": _dec(q.get("target", "")), "max_limit": _dec(q.get("max-limit", "")), "rate_now": _dec(q.get("rate", ""))})
            except Exception:
                pass
            try:
                for p in api.get_resource("/ppp/active").get():
                    ppp_active.append({"user": _dec(p.get("name")), "profile": _dec(p.get("profile", "")), "service": _dec(p.get("service", ""))})
            except Exception:
                pass
        return {"router": router.get("name"), "queues": queues[:20], "ppp_active": ppp_active[:20]}
    finally:
        try:
            if use_ssh and ssh:
                ssh.close()
            elif not use_ssh and conn:
                conn.disconnect()
        except Exception:
            pass


def do_action(router, action):
    conn = None
    api = None
    ssh = None
    use_ssh = False
    try:
        conn, api = _connect(router)
        if api is None:
            ssh = conn
            use_ssh = True
    except Exception as exc:
        return False, f"Cannot connect: {exc}"
    try:
        if action == "reconnect_pppoe":
            if use_ssh:
                stdin, stdout, stderr = ssh.exec_command("/interface/pppoe-client print", timeout=10)
                out = stdout.read().decode("utf-8", errors="ignore")
                names = []
                for line in out.strip().splitlines():
                    if "pppoe" in line.lower():
                        name = line.split()[0]
                        ssh.exec_command(f"/interface/pppoe-client disable {name}")
                        time.sleep(1)
                        ssh.exec_command(f"/interface/pppoe-client enable {name}")
                        names.append(name)
            else:
                resource = api.get_resource("/interface/pppoe-client")
                names = []
                for client in resource.get():
                    pid = _dec(client.get(".id"))
                    name = _dec(client.get("name", "?"))
                    resource.call("disable", {"numbers": str(pid)})
                    resource.call("enable", {"numbers": str(pid)})
                    names.append(name)
            if not names:
                return False, "No PPPoE client found on this router."
            return True, "Reconnected PPPoE: " + ", ".join(names)
        if action == "reboot":
            try:
                if use_ssh:
                    ssh.exec_command("/system reboot")
                else:
                    api.get_binary_resource("/system").call("reboot")
            except Exception:
                pass
            return True, "Reboot command sent."
        return False, f"Unknown action: {action}"
    finally:
        try:
            if use_ssh and ssh:
                ssh.close()
            elif not use_ssh and conn:
                conn.disconnect()
        except Exception:
            pass
