#!/usr/bin/env python3
"""
Monitor orchestrate.py from outside: tail its log, infer phase, write status to D1.

Launched by entrypoint.sh in parallel with orchestrate.py. Does not modify the orchestrator.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import requests

Status = Literal["crawling", "reviewing", "done", "crashed"]

TERMINAL: frozenset[Status] = frozenset({"done", "crashed"})

LOG_MARKERS: tuple[tuple[str, Status], ...] = (
    ("Step 1 — Crawl", "crawling"),
    ("Step 2 —", "reviewing"),
    ("Step 3 —", "reviewing"),
    ("Step 4 —", "reviewing"),
    ("Step 5 —", "reviewing"),
    ("Step 6 —", "reviewing"),
)

POLL_INTERVAL_SEC = 0.5
PID_WAIT_TIMEOUT_SEC = 120
EXIT_FILE_WAIT_SEC = 10
D1_RETRIES = 3
D1_RETRY_BACKOFF_SEC = 2.0


def _log(msg: str) -> None:
    print(f"[status-relay] {msg}", flush=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_run_id() -> str:
    host = socket.gethostname()
    return f"{host}-{int(time.time())}"


def _env_enabled() -> bool:
    raw = os.environ.get("STATUS_RELAY_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _d1_config() -> tuple[str, str, str] | None:
    token = os.environ.get("D1_TOKEN", "").strip()
    account_id = os.environ.get("D1_ACCOUNT_ID", "").strip()
    database_id = os.environ.get("D1_DATABASE_ID", "").strip()
    if not token or not account_id or not database_id:
        return None
    return token, account_id, database_id


def _infer_status_from_line(line: str) -> Status | None:
    for marker, status in LOG_MARKERS:
        if marker in line:
            return status
    return None


def _read_pid(pid_file: Path) -> int | None:
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    return int(raw)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _read_exit_code(exit_file: Path) -> int | None:
    try:
        raw = exit_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


class D1Client:
    def __init__(self, token: str, account_id: str, database_id: str) -> None:
        self._url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def execute(self, sql: str, params: list[object] | None = None) -> None:
        payload: dict[str, object] = {"sql": sql}
        if params is not None:
            payload["params"] = params

        last_err: Exception | None = None
        for attempt in range(1, D1_RETRIES + 1):
            try:
                resp = requests.post(
                    self._url,
                    headers=self._headers,
                    json=payload,
                    timeout=30,
                )
                data = resp.json()
                if not resp.ok or not data.get("success"):
                    errors = data.get("errors") or resp.text
                    raise RuntimeError(f"D1 query failed ({resp.status_code}): {errors}")
                return
            except (requests.RequestException, RuntimeError) as exc:
                last_err = exc
                if attempt < D1_RETRIES:
                    time.sleep(D1_RETRY_BACKOFF_SEC * attempt)
        assert last_err is not None
        raise last_err


class StatusRelay:
    def __init__(
        self,
        *,
        log_file: Path,
        pid_file: Path,
        exit_file: Path,
        run_id: str,
        host: str,
        d1: D1Client | None,
    ) -> None:
        self.log_file = log_file
        self.pid_file = pid_file
        self.exit_file = exit_file
        self.run_id = run_id
        self.host = host
        self.d1 = d1
        self.started_at = _utc_now()
        self.current: Status | None = None
        self._log_offset = 0

    def _write_status(self, status: Status, *, exit_code: int | None = None) -> None:
        if status == self.current:
            return
        self.current = status
        now = _utc_now()
        _log(f"status -> {status}" + (f" (exit {exit_code})" if exit_code is not None else ""))

        if self.d1 is None:
            return

        try:
            self.d1.execute(
                """
                INSERT OR REPLACE INTO orchestrator_runs
                  (run_id, status, started_at, updated_at, host, exit_code)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [self.run_id, status, self.started_at, now, self.host, exit_code],
            )
            self.d1.execute(
                """
                INSERT INTO orchestrator_status_events (run_id, status, recorded_at)
                VALUES (?, ?, ?)
                """,
                [self.run_id, status, now],
            )
        except Exception as exc:
            _log(f"D1 write failed (best-effort): {exc}")

    def _tail_new_lines(self) -> list[str]:
        if not self.log_file.is_file():
            return []
        try:
            with self.log_file.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._log_offset)
                chunk = fh.read()
                self._log_offset = fh.tell()
        except OSError as exc:
            _log(f"log read error: {exc}")
            return []
        if not chunk:
            return []
        return chunk.splitlines()

    def _process_log_lines(self, lines: list[str]) -> None:
        for line in lines:
            inferred = _infer_status_from_line(line)
            if inferred is not None:
                self._write_status(inferred)

    def _wait_for_pid(self) -> int | None:
        deadline = time.monotonic() + PID_WAIT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            pid = _read_pid(self.pid_file)
            if pid is not None and _pid_alive(pid):
                return pid
            time.sleep(POLL_INTERVAL_SEC)
        return None

    def _wait_for_exit_code(self) -> int:
        deadline = time.monotonic() + EXIT_FILE_WAIT_SEC
        while time.monotonic() < deadline:
            code = _read_exit_code(self.exit_file)
            if code is not None:
                return code
            time.sleep(POLL_INTERVAL_SEC)
        _log("exit file missing after orchestrator stopped; treating as crashed")
        return 1

    def run(self) -> int:
        _log(f"monitoring run_id={self.run_id} log={self.log_file}")

        pid = self._wait_for_pid()
        if pid is None:
            _log("orchestrator pid not found; exiting")
            return 1

        _log(f"watching orchestrator pid={pid}")

        while _pid_alive(pid):
            self._process_log_lines(self._tail_new_lines())
            time.sleep(POLL_INTERVAL_SEC)

        self._process_log_lines(self._tail_new_lines())
        exit_code = self._wait_for_exit_code()
        final: Status = "done" if exit_code == 0 else "crashed"
        self._write_status(final, exit_code=exit_code)
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Relay orchestrator status to Cloudflare D1.")
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--pid-file", required=True, type=Path)
    parser.add_argument("--exit-file", required=True, type=Path)
    parser.add_argument("--run-id", default=None, help="Override ORCHESTRATOR_RUN_ID")
    args = parser.parse_args()

    if not _env_enabled():
        _log("disabled (STATUS_RELAY_ENABLED=0)")
        return

    cfg = _d1_config()
    d1: D1Client | None = None
    if cfg is None:
        _log("D1_TOKEN / D1_ACCOUNT_ID / D1_DATABASE_ID not set; monitoring only (no D1 writes)")
    else:
        d1 = D1Client(*cfg)

    run_id = (args.run_id or os.environ.get("ORCHESTRATOR_RUN_ID") or _default_run_id()).strip()
    relay = StatusRelay(
        log_file=args.log_file,
        pid_file=args.pid_file,
        exit_file=args.exit_file,
        run_id=run_id,
        host=socket.gethostname(),
        d1=d1,
    )
    raise SystemExit(relay.run())


if __name__ == "__main__":
    main()
