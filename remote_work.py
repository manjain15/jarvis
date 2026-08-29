"""
Jarvis — Remote Work Sessions
===============================
Lets Jarvis kick off a headless Claude Code session against one of Manav's
other GitHub-hosted projects, driven from natural-language Telegram chat
(the same tool-calling pattern as propose_profile_update/propose_term_update)
instead of a rigid slash command.

Phase 1 scope: trigger + isolated execution + a completion notification.
Each session works in its own fresh git worktree on a dedicated branch —
nothing is ever pushed anywhere by this module. Pushing a reviewed branch
and opening a PR is a deliberately separate, later phase.

SETUP (one-time, on the VPS, as the jarvis user):
  1. Install the Claude Code CLI.
  2. Run `claude login` and complete the browser OAuth flow against your
     Claude Pro/Max subscription. That login is what every remote-work run
     authenticates as — treat it as sensitive as an API key.
  3. Register each project: python3 -c "from remote_work import add_project;
     add_project('myproject', 'git@github.com:you/myproject.git')"

USAGE (from jarvis_telegram.py's chat tools):
  start_session("myproject", "add rate limiting to the API")
  check_session("myproject")
"""

import json
import re
import subprocess
import threading
import datetime
from pathlib import Path

from json_store import file_lock, atomic_write_json

SCRIPT_DIR    = Path(__file__).parent
WORK_DIR      = SCRIPT_DIR / "remote_work"
PROJECTS_FILE = WORK_DIR / "projects.json"
SESSIONS_FILE = WORK_DIR / "sessions.json"

WORK_DIR.mkdir(exist_ok=True)

SESSION_TIMEOUT_SECONDS = 60 * 60  # a runaway session shouldn't run forever


# ── Project registry ──────────────────────────────────────────────────────────

def load_projects():
    if not PROJECTS_FILE.exists():
        return {}
    try:
        return json.loads(PROJECTS_FILE.read_text())
    except Exception:
        return {}


def add_project(name, git_url):
    """Registers a project. Run once per project via SSH, not from chat."""
    with file_lock(PROJECTS_FILE):
        projects = load_projects()
        projects[name] = {"git_url": git_url}
        atomic_write_json(PROJECTS_FILE, projects)


# ── Session tracking ──────────────────────────────────────────────────────────

def load_sessions():
    if not SESSIONS_FILE.exists():
        return []
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except Exception:
        return []


def _update_session(session_id, **fields):
    with file_lock(SESSIONS_FILE):
        sessions = load_sessions()
        for s in sessions:
            if s["id"] == session_id:
                s.update(fields)
        atomic_write_json(SESSIONS_FILE, sessions)


def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "task"


# ── Running a session ─────────────────────────────────────────────────────────

def _run_claude_code(session_id, project, worktree_dir, instruction):
    """
    Runs headless Claude Code in `worktree_dir`, blocking — call this from a
    background thread, never on the Telegram bot's main thread.
    """
    log_path = worktree_dir / "session.log"
    try:
        with open(log_path, "w") as log_file:
            result = subprocess.run(
                ["claude", "-p", instruction, "--dangerously-skip-permissions"],
                cwd=worktree_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=SESSION_TIMEOUT_SECONDS,
            )
        status = "done" if result.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        status = "failed"
        with open(log_path, "a") as log_file:
            log_file.write(f"\n\n[jarvis] Timed out after {SESSION_TIMEOUT_SECONDS}s — stopped.")
    except Exception as e:
        status = "failed"
        with open(log_path, "a") as log_file:
            log_file.write(f"\n\n[jarvis] Failed to run: {e}")

    # Last ~40 lines as a cheap summary; a real diff-stat summary is Phase 2.
    try:
        tail = log_path.read_text().splitlines()[-40:]
        summary = "\n".join(tail)
    except Exception:
        summary = ""

    _update_session(
        session_id, status=status, summary=summary,
        finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )

    try:
        from jarvis_telegram import send_message
        icon = "✅" if status == "done" else "❌"
        send_message(f"{icon} Remote work [{session_id}] on {project} {status}.\n/ or just ask me how it went.")
    except Exception:
        pass  # notification is best-effort — the session result is already saved either way


def start_session(project, instruction):
    """
    Clones/fetches `project`, creates a fresh branch + worktree, and kicks
    off a background Claude Code run. Returns (session_id, error) — error
    is None on success.
    """
    projects = load_projects()
    if project not in projects:
        known = ", ".join(sorted(projects)) or "(none registered)"
        return None, f"Unknown project '{project}'. Known: {known}"

    git_url      = projects[project]["git_url"]
    timestamp    = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    session_id   = f"{project}-{timestamp}"
    branch       = f"jarvis/{_slugify(instruction)}-{timestamp}"
    repo_dir     = WORK_DIR / project / "repo"
    worktree_dir = WORK_DIR / project / session_id

    try:
        if not repo_dir.exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", git_url, str(repo_dir)], check=True, timeout=300)
        else:
            subprocess.run(["git", "fetch", "origin"], cwd=repo_dir, check=True, timeout=300)

        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree_dir), "origin/HEAD"],
            cwd=repo_dir, check=True, timeout=60,
        )
    except Exception as e:
        return None, f"Setup failed: {e}"

    with file_lock(SESSIONS_FILE):
        sessions = load_sessions()
        sessions.append({
            "id": session_id, "project": project, "instruction": instruction,
            "branch": branch, "status": "running",
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "summary": "",
        })
        atomic_write_json(SESSIONS_FILE, sessions)

    thread = threading.Thread(
        target=_run_claude_code, args=(session_id, project, worktree_dir, instruction), daemon=True,
    )
    thread.start()

    return session_id, None


def check_session(project):
    """Returns a formatted status string for the most recent session on `project`."""
    sessions = [s for s in load_sessions() if s["project"] == project]
    if not sessions:
        return f"No remote-work sessions found for '{project}'."

    s = sessions[-1]
    lines = [
        f"[{s['id']}] {s['status']} — {s['instruction']}",
        f"  branch: {s['branch']}",
        f"  started: {s['started_at']}",
    ]
    if s["status"] != "running":
        lines.append(f"  finished: {s['finished_at']}")
    if s["summary"]:
        lines.append(f"  last output:\n{s['summary'][-1000:]}")
    return "\n".join(lines)
