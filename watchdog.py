#!/usr/bin/env python3
"""Simple watchdog for alternating two pre-existing Codex sessions.

Usage:
    python watchdog.py "<proposer command>" "<referee command>" [--workdir PATH] [--poll-interval 0.25]

Behavior:
- Starts the proposer command first.
- When `done.txt` appears, waits 1 second, stops proposer, removes `done.txt`, and starts referee.
- When `referee_done.txt` appears, waits 1 second, stops referee, removes `referee_done.txt`, and starts proposer.
- When `proposer_done.txt` appears, waits 1 second, stops proposer, and exits.

This script assumes the agents create their sentinel files in `workdir`.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


class ProcessManager:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.proc: Optional[subprocess.Popen] = None
        self.role: Optional[str] = None

    def start(self, command: str, role: str) -> None:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError(f"Cannot start {role}: process for {self.role} is still running")

        print(f"[watchdog] starting {role}: {command}", flush=True)

        kwargs = {
            "cwd": str(self.workdir),
            "shell": True,
            "stdin": subprocess.DEVNULL,
        }

        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True

        self.proc = subprocess.Popen(command, **kwargs)
        self.role = role

    def stop_current(self) -> None:
        if self.proc is None:
            return

        if self.proc.poll() is not None:
            print(f"[watchdog] {self.role} already exited with code {self.proc.returncode}", flush=True)
            self.proc = None
            self.role = None
            return

        print(f"[watchdog] stopping {self.role} (pid={self.proc.pid})", flush=True)

        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception as exc:
            print(f"[watchdog] warning: graceful stop failed: {exc}", flush=True)
            self.proc.terminate()

        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"[watchdog] escalating kill for {self.role}", flush=True)
            try:
                if os.name == "nt":
                    self.proc.kill()
                else:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception as exc:
                print(f"[watchdog] warning: force kill failed: {exc}", flush=True)
                self.proc.kill()
            self.proc.wait(timeout=5)

        print(f"[watchdog] {self.role} stopped with code {self.proc.returncode}", flush=True)
        self.proc = None
        self.role = None


def remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
        print(f"[watchdog] removed trigger file: {path.name}", flush=True)
    except FileNotFoundError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alternate two Codex agent commands based on sentinel files.")
    parser.add_argument("proposer_cmd", help="Command line for the proposer session")
    parser.add_argument("referee_cmd", help="Command line for the referee session")
    parser.add_argument(
        "--workdir",
        default=".",
        help="Directory where the agents run and where sentinel files are created (default: current directory)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Polling interval in seconds for checking sentinel files (default: 0.25)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    done = workdir / "done.txt"
    referee_done = workdir / "referee_done.txt"
    proposer_done = workdir / "proposer_done.txt"

    manager = ProcessManager(workdir)

    def shutdown(signum, _frame):
        print(f"\n[watchdog] received signal {signum}, shutting down", flush=True)
        manager.stop_current()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Clear stale handoff files that would otherwise immediately retrigger a switch.
    remove_file_if_exists(done)
    remove_file_if_exists(referee_done)

    if proposer_done.exists():
        print("[watchdog] proposer_done.txt already exists; nothing to do", flush=True)
        return 0

    manager.start(args.proposer_cmd, "proposer")

    while True:
        time.sleep(args.poll_interval)

        if proposer_done.exists():
            print("[watchdog] detected proposer_done.txt", flush=True)
            time.sleep(1.0)
            manager.stop_current()
            return 0

        if manager.role == "proposer" and done.exists():
            print("[watchdog] detected done.txt from proposer", flush=True)
            time.sleep(1.0)
            manager.stop_current()
            remove_file_if_exists(done)
            remove_file_if_exists(referee_done)
            manager.start(args.referee_cmd, "referee")
            continue

        if manager.role == "referee" and referee_done.exists():
            print("[watchdog] detected referee_done.txt", flush=True)
            time.sleep(1.0)
            manager.stop_current()
            remove_file_if_exists(referee_done)
            remove_file_if_exists(done)
            manager.start(args.proposer_cmd, "proposer")
            continue

        if manager.proc is not None and manager.proc.poll() is not None:
            code = manager.proc.returncode
            role = manager.role
            print(f"[watchdog] {role} exited unexpectedly with code {code}", flush=True)
            manager.proc = None
            manager.role = None
            return code if code is not None else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

