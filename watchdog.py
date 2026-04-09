#!/usr/bin/env python3
"""Alternate two existing Codex sessions in strict turn-taking mode.

This runner launches one Codex session with the exact calling convention that
matches the user's working environment:

    codex exec <MESSAGE> resume <SESSION>

It waits until that run exits, then launches the other side, and repeats until
interrupted.

Usage:
    python watchdog.py proposer_session referee_session \
        [--workdir PATH] [--start proposer|referee] [--initial MESSAGE] \
        [--to-proposer MESSAGE] [--to-referee MESSAGE]
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_MESSAGES = {
    "proposer": "The referee is done. Do as instructed. Update and commit.",
    "referee": "The proposer is done. Do as instructed. Update and commit.",
}


class Runner:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.proc: Optional[subprocess.Popen] = None
        self.role: Optional[str] = None

    @staticmethod
    def build_argv(session: str, message: str) -> list[str]:
        return ["codex", "exec", message, "resume", session]

    def start(self, session: str, role: str, message: str) -> None:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError(f"Cannot start {role}: {self.role} is still running")

        argv = self.build_argv(session, message)
        print(f"[watchdog] starting {role}: {' '.join(shlex.quote(x) for x in argv)}", flush=True)

        kwargs = {"cwd": str(self.workdir)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True

        self.proc = subprocess.Popen(argv, **kwargs)
        self.role = role

    def wait(self) -> int:
        if self.proc is None:
            raise RuntimeError("No process is running")
        return self.proc.wait()

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            self.proc = None
            self.role = None
            return

        print(f"[watchdog] stopping {self.role}", flush=True)
        try:
            if os.name == "nt":
                self.proc.kill()
            else:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except Exception:
            self.proc.kill()
        finally:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None
            self.role = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alternate two Codex sessions by waiting for each run to finish."
    )
    parser.add_argument("proposer_session", help="Existing Codex proposer session name or id")
    parser.add_argument("referee_session", help="Existing Codex referee session name or id")
    parser.add_argument("--workdir", default=".", help="Working directory for both agents (default: current directory)")
    parser.add_argument(
        "--start",
        choices=("proposer", "referee"),
        default="proposer",
        help="Which side runs first (default: proposer)",
    )
    parser.add_argument(
        "--initial",
        default=None,
        help="Optional message to use only for the first run; later turns use the configured handoff messages.",
    )
    parser.add_argument(
        "--to-proposer",
        default=DEFAULT_MESSAGES["proposer"],
        help="Message used on later turns when launching the proposer.",
    )
    parser.add_argument(
        "--to-referee",
        default=DEFAULT_MESSAGES["referee"],
        help="Message used on later turns when launching the referee.",
    )
    return parser.parse_args()


def other_role(role: str) -> str:
    return "referee" if role == "proposer" else "proposer"


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    sessions = {
        "proposer": args.proposer_session,
        "referee": args.referee_session,
    }
    handoff_messages = {
        "proposer": args.to_proposer,
        "referee": args.to_referee,
    }

    runner = Runner(workdir)

    def shutdown(signum, _frame):
        print(f"\n[watchdog] received signal {signum}, shutting down", flush=True)
        runner.stop()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    current_role = args.start
    first_turn = True

    while True:
        message = args.initial if first_turn and args.initial is not None else handoff_messages[current_role]

        runner.start(sessions[current_role], current_role, message)
        code = runner.wait()
        finished_role = current_role
        runner.proc = None
        runner.role = None

        if code != 0:
            print(f"[watchdog] {finished_role} exited with code {code}", flush=True)
            return code

        print(f"[watchdog] {finished_role} finished successfully", flush=True)
        current_role = other_role(finished_role)
        first_turn = False


if __name__ == "__main__":
    sys.exit(main())

