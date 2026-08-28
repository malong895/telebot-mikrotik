"""Hidden, single-instance Windows supervisor for ITFF5BOT."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
SERVICE_LOG = LOG_DIR / "service.log"
BOT_LOG = LOG_DIR / "bot.log"
STATE_PATH = LOG_DIR / "service_state.json"
MUTEX_NAME = "Local\\ITFF5BOT_Service_Supervisor"
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183

_MUTEX_HANDLE = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_service_log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with SERVICE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{_utc_now()} {message}\n")


def _write_state(**values) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    state = {"updated_at": _utc_now(), **values}
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def restart_delay(runtime_seconds: float) -> float:
    """Back off crash loops while restarting stable exits quickly."""
    return 10.0 if runtime_seconds < 30.0 else 2.0


def supervise(
    run_once: Callable[[], tuple[int, float]],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
    max_cycles: int | None = None,
) -> None:
    cycles = 0
    while not should_stop():
        exit_code, runtime = run_once()
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        if should_stop():
            return
        sleep_fn(restart_delay(runtime))


def acquire_single_instance() -> bool:
    """Hold a named mutex so duplicate startup entries cannot duplicate polling."""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return False
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


def run_bot_once() -> tuple[int, float]:
    python = BASE_DIR / "venv" / "Scripts" / "python.exe"
    bot_script = BASE_DIR / "bot.py"
    if not python.exists() or not bot_script.exists():
        _append_service_log("ERROR bot executable or bot.py is missing")
        _write_state(service_pid=os.getpid(), state="misconfigured")
        return 2, 0.0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with BOT_LOG.open("ab", buffering=0) as output:
        process = subprocess.Popen(
            [str(python), "-u", str(bot_script)],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        _append_service_log(f"bot child started pid={process.pid}")
        while True:
            _write_state(
                service_pid=os.getpid(),
                child_pid=process.pid,
                state="running",
            )
            try:
                exit_code = process.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                continue

    runtime = time.monotonic() - started
    _append_service_log(
        f"bot child exited code={exit_code} runtime_seconds={runtime:.1f}"
    )
    _write_state(
        service_pid=os.getpid(),
        child_pid=None,
        state="restarting",
        last_exit_code=exit_code,
        last_runtime_seconds=round(runtime, 1),
    )
    return int(exit_code), runtime


def main() -> int:
    if not acquire_single_instance():
        return 0
    _append_service_log(f"supervisor started pid={os.getpid()}")
    _write_state(service_pid=os.getpid(), child_pid=None, state="starting")
    try:
        supervise(run_bot_once)
    except BaseException as exc:
        _append_service_log(f"supervisor stopped: {type(exc).__name__}: {exc}")
        _write_state(service_pid=os.getpid(), child_pid=None, state="stopped")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
