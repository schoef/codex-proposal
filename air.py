#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
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
    "proposer": "writeup.txt",
    "critique": "critique.txt",
    "professor": "next.txt",
}


class AirError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "AIR: professor-controlled proposer/critique loop. "
            "The professor session is resumed on every professor turn; if your Codex CLI "
            "supports named session creation through 'resume', the first professor turn "
            "will create it automatically."
        )
    )
    parser.add_argument(
        "professor_session",
        help="Professor session name/ID to use with 'codex exec ... resume <session>'.",
    )
    parser.add_argument(
        "--workdir",
        default=".",
        help="Working directory containing idea.txt, the role .md files, and the AIR outputs.",
    )
    parser.add_argument(
        "--initial",
        default="",
        help="Optional operator note appended only to the first professor turn.",
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
    return parser.parse_args()


def log(message: str) -> None:
    print(f"[AIR] {message}", flush=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require_file(path: Path, description: str) -> None:
    if not path.exists():
        raise AirError(f"Missing required {description}: {path}")


def sha256_of_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def shlex_quote(text: str) -> str:
    if text == "":
        return "''"
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/=:")
    if all(ch in safe for ch in text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def build_role_prompt(role_file: Path, run_message: Optional[str] = None) -> str:
    base = read_text(role_file).rstrip()
    if run_message:
        return (
            f"{base}\n\n"
            "## Run-specific message\n"
            f"{run_message.strip()}\n\n"
            "Act now inside the current repository and then stop."
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


def ensure_git_repo(workdir: Path) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise AirError(f"AIR requires a git repository in workdir: {workdir}")


def git_commit_and_push(workdir: Path, *, step_label: str) -> None:
    tracked_paths = [
        name
        for name in ("idea.txt", "writeup.txt", "critique.txt", "next.txt")
        if (workdir / name).exists()
    ]

    if tracked_paths:
        add_result = subprocess.run(["git", "add", "--", *tracked_paths], cwd=str(workdir))
        if add_result.returncode != 0:
            raise AirError(f"git add failed during {step_label}.")

    commit_message = f"AIR: {step_label}"
    commit_result = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", commit_message],
        cwd=str(workdir),
    )
    if commit_result.returncode != 0:
        raise AirError(f"git commit failed during {step_label}.")
    log(f"git commit created: {commit_message}")

    push_result = subprocess.run(["git", "push"], cwd=str(workdir))
    if push_result.returncode != 0:
        log("git push failed; ignoring and continuing.")
    else:
        log("git push succeeded.")


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
    ensure_git_repo(workdir)

    professor_turn_idx = 0
    actor_turn_idx = 0
    first_professor_turn = True

    while True:
        if actor_turn_idx >= args.max_rounds:
            raise AirError(
                f"Aborting after {args.max_rounds} proposer/critique turns without receiving stop."
            )

        if next_path.exists():
            next_path.unlink()

        professor_message = (
            "Read idea.txt, writeup.txt if present, and critique.txt if present. "
            "Then write next.txt in the strict required format."
        )
        if first_professor_turn and args.initial.strip():
            professor_message += f" Operator note for the first turn only: {args.initial.strip()}"
        first_professor_turn = False

        professor_turn_idx += 1
        log(f"professor turn {professor_turn_idx:03d} using session {args.professor_session}")
        run_codex(
            codex_bin=args.codex_bin,
            prompt=build_role_prompt(professor_md, professor_message),
            workdir=workdir,
            session=args.professor_session,
        )

        if not next_path.exists():
            raise AirError("Professor finished but did not create next.txt.")
        git_commit_and_push(workdir, step_label=f"professor step {professor_turn_idx:03d}")

        actor, message = parse_next(next_path)
        if actor == "stop":
            log("Professor wrote stop. AIR completed.")
            return 0

        actor_turn_idx += 1
        output_name = OUTPUT_FILES[actor]
        output_path = workdir / output_name
        before_hash = sha256_of_file(output_path)

        role_md = proposer_md if actor == "proposer" else critique_md
        log(f"{actor} turn {actor_turn_idx:03d}")
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
        git_commit_and_push(workdir, step_label=f"{actor} step {actor_turn_idx:03d}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AirError as exc:
        print(f"[AIR] error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n[AIR] interrupted.", file=sys.stderr)
        raise SystemExit(130)

