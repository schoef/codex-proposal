#!/usr/bin/env python3
"""Alternate two Codex agent processes using file-based handoff.

Usage:
    python watchdog.py "<proposer command>" "<referee command>" \
        [--workdir PATH] [--poll-interval 0.25] [--start proposer|referee]

Behavior:
- Starts either the proposer or referee command, selected with `--start`
  (default: proposer).
- When `proposer_done.txt` appears while proposer is running, waits 1 second,
  stops proposer, deletes `proposer_done.txt`, and starts referee.
- When `referee_done.txt` appears while referee is running, waits 1 second,
  stops referee, deletes `referee_done.txt`, and starts proposer.
- Runs until interrupted externally.

Convenience:
- If a command starts with `codex`, the script appends the default flags
  `--ask-for-approval never --sandbox danger-full-access` unless they are
  already present.
- On startup, stale `proposer_done.txt` and `referee_done.txt` are removed.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
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
        }

        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True

        try:
            argv = shlex.split(command)
            use_shell = False
        except ValueError:
            argv = command
            use_shell = True

        self.proc = subprocess.Popen(argv, shell=use_shell, **kwargs)
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


def maybe_add_codex_defaults(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return command

    if not parts:
        return command

    exe = os.path.basename(parts[0])
    if exe != "codex":
        return command

    has_ask = "--ask-for-approval" in parts
    has_sandbox = "--sandbox" in parts

    if not has_ask:
        parts.extend(["--ask-for-approval", "never"])
    if not has_sandbox:
        parts.extend(["--sandbox", "danger-full-access"])

    return shlex.join(parts)


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
    parser.add_argument(
        "--start",
        choices=("proposer", "referee"),
        default="proposer",
        help="Which process to start first (default: proposer)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    proposer_done = workdir / "proposer_done.txt"
    referee_done = workdir / "referee_done.txt"

    proposer_cmd = maybe_add_codex_defaults(args.proposer_cmd)
    referee_cmd = maybe_add_codex_defaults(args.referee_cmd)

    manager = ProcessManager(workdir)

    def shutdown(signum, _frame):
        print(f"\n[watchdog] received signal {signum}, shutting down", flush=True)
        manager.stop_current()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    remove_file_if_exists(proposer_done)
    remove_file_if_exists(referee_done)

    if args.start == "proposer":
        manager.start(proposer_cmd, "proposer")
    else:
        manager.start(referee_cmd, "referee")

    while True:
        time.sleep(args.poll_interval)

        if manager.role == "proposer" and proposer_done.exists():
            print("[watchdog] detected proposer_done.txt", flush=True)
            time.sleep(1.0)
            manager.stop_current()
            remove_file_if_exists(proposer_done)
            manager.start(referee_cmd, "referee")
            continue

        if manager.role == "referee" and referee_done.exists():
            print("[watchdog] detected referee_done.txt", flush=True)
            time.sleep(1.0)
            manager.stop_current()
            remove_file_if_exists(referee_done)
            manager.start(proposer_cmd, "proposer")
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

