#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

ROLE_FILES = {
    "proposer": "proposer.md",
    "critique": "critique.md",
    "professor": "professor.md",
}

OUTPUT_FILES = {
    "proposer": "plan.txt",
    "critique": "critique.txt",
    "professor": "next.txt",
}


class AirError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Adversarial Idea Roll-out (AIR): professor-controlled proposer/critique loop."
    )
    parser.add_argument(
        "professor_session",
        help="Session name or ID for the persistent professor Codex run.",
    )
    parser.add_argument(
        "--workdir",
        default=".",
        help="Repository / working directory containing idea.txt and the AIR prompt files.",
    )
    parser.add_argument(
        "--initial",
        default="",
        help="Optional operator note appended only to the first professor call.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=20,
        help="Maximum number of proposer/critique turns before aborting. Default: 20.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex executable to use. Default: codex.",
    )
    parser.add_argument(
        "--git-snapshots",
        action="store_true",
        help="After each proposer/critique turn, create a git snapshot commit if possible.",
    )
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256_of_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def log(message: str) -> None:
    print(f"[AIR] {message}", flush=True)


def build_role_prompt(role_file: Path, run_message: Optional[str] = None) -> str:
    base = read_text(role_file).rstrip()
    if run_message:
        return (
            f"{base}\n\n"
            f"## Run-specific message\n"
            f"{run_message.strip()}\n\n"
            f"Act now inside the current repository and then stop."
        )
    return base


def run_codex(
    *,
    codex_bin: str,
    prompt: str,
    workdir: Path,
    session: Optional[str] = None,
) -> None:
    cmd = [codex_bin, "exec", prompt]
    if session:
        cmd.extend(["resume", session])
    log("running: " + " ".join(shlex_quote(part) for part in cmd))
    result = subprocess.run(cmd, cwd=str(workdir))
    if result.returncode != 0:
        raise AirError(f"Codex exited with code {result.returncode}.")


def shlex_quote(text: str) -> str:
    if text == "":
        return "''"
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/=:")
    if all(ch in safe for ch in text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def require_file(path: Path, description: str) -> None:
    if not path.exists():
        raise AirError(f"Missing required {description}: {path}")


def parse_next(next_path: Path) -> Tuple[str, str]:
    require_file(next_path, "next.txt")
    raw = read_text(next_path)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AirError(
            "next.txt must contain exactly one non-empty line: stop | proposer: <message> | critique: <message>."
        )

    line = lines[0]
    lowered = line.lower()
    if lowered == "stop":
        return ("stop", "")

    for role in ("proposer", "critique"):
        prefix = f"{role}:"
        if lowered.startswith(prefix):
            message = line[len(prefix):].strip()
            if not message:
                raise AirError(f"next.txt selected {role} but did not include a message.")
            return (role, message)

    raise AirError(
        "Malformed next.txt. Expected exactly one line with stop, proposer: <message>, or critique: <message>."
    )


def maybe_git_snapshot(workdir: Path, turn_idx: int, role: str) -> None:
    def git_ok(*args: str) -> bool:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workdir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    if not git_ok("rev-parse", "--is-inside-work-tree"):
        log("git snapshot skipped: not inside a git repository.")
        return

    tracked_paths = [
        name for name in ("idea.txt", "plan.txt", "critique.txt") if (workdir / name).exists()
    ]
    if not tracked_paths:
        log("git snapshot skipped: no AIR files available to snapshot.")
        return

    subprocess.run(["git", "add", "--", *tracked_paths], cwd=str(workdir), check=False)

    diff_index = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(workdir),
    )
    if diff_index.returncode == 0:
        log("git snapshot skipped: no staged changes.")
        return
    if diff_index.returncode not in (0, 1):
        log("git snapshot skipped: unable to inspect staged diff.")
        return

    message = f"AIR turn {turn_idx:03d} {role}"
    commit = subprocess.run(["git", "commit", "-m", message], cwd=str(workdir))
    if commit.returncode != 0:
        log("git snapshot failed; continuing without aborting.")
    else:
        log(f"git snapshot created: {message}")


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir).resolve()

    if not workdir.exists() or not workdir.is_dir():
        raise AirError(f"Workdir does not exist or is not a directory: {workdir}")

    idea_path = workdir / "idea.txt"
    proposer_md = workdir / ROLE_FILES["proposer"]
    critique_md = workdir / ROLE_FILES["critique"]
    professor_md = workdir / ROLE_FILES["professor"]
    next_path = workdir / "next.txt"

    require_file(idea_path, "idea.txt")
    require_file(proposer_md, "proposer.md")
    require_file(critique_md, "critique.md")
    require_file(professor_md, "professor.md")

    turn_idx = 0
    first_professor_turn = True

    while True:
        if turn_idx >= args.max_rounds:
            raise AirError(
                f"Aborting after {args.max_rounds} proposer/critique turns without receiving stop."
            )

        if next_path.exists():
            next_path.unlink()

        professor_message = (
            "Run AIR as the professor. Read idea.txt, plan.txt if present, and critique.txt if present. "
            "Then write next.txt in the strict required format."
        )
        if first_professor_turn and args.initial.strip():
            professor_message += f" Operator note for the first turn only: {args.initial.strip()}"
        first_professor_turn = False

        log("professor turn")
        run_codex(
            codex_bin=args.codex_bin,
            prompt=build_role_prompt(professor_md, professor_message),
            workdir=workdir,
            session=args.professor_session,
        )

        actor, message = parse_next(next_path)
        if actor == "stop":
            log("professor wrote stop. AIR completed.")
            return 0

        turn_idx += 1
        output_name = OUTPUT_FILES[actor]
        output_path = workdir / output_name
        before_hash = sha256_of_file(output_path)

        role_md = proposer_md if actor == "proposer" else critique_md
        log(f"turn {turn_idx:03d}: {actor}")
        run_codex(
            codex_bin=args.codex_bin,
            prompt=build_role_prompt(role_md, message),
            workdir=workdir,
            session=None,
        )

        after_hash = sha256_of_file(output_path)
        if after_hash is None:
            raise AirError(f"{actor} finished but did not create {output_name}.")
        if before_hash == after_hash:
            raise AirError(f"{actor} finished but did not materially change {output_name}.")

        log(f"{actor} updated {output_name}.")
        if args.git_snapshots:
            maybe_git_snapshot(workdir, turn_idx, actor)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AirError as exc:
        print(f"[AIR] error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n[AIR] interrupted.", file=sys.stderr)
        raise SystemExit(130)

