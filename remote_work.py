"""
Jarvis — Remote Work Sessions
===============================
Lets Jarvis kick off a headless Claude Code session against one of Manav's
other GitHub-hosted projects, driven from natural-language Telegram chat
(the same tool-calling pattern as propose_profile_update/propose_term_update)
instead of a rigid slash command.

Each session works in its own fresh git worktree on a dedicated branch.
By default nothing is ever pushed anywhere — the branch just sits in
remote_work/<project>/<session_id>/ for Manav to review locally. Pass
open_pr=True to have a *finished* session push its branch and open a PR
for review instead (still never merges anything).

SETUP (one-time, on the VPS, as the jarvis user):
  1. Install the Claude Code CLI.
  2. Run `claude login` and complete the browser OAuth flow against your
     Claude Pro/Max subscription. That login is what every remote-work run
     authenticates as — treat it as sensitive as an API key.
  3. Set GITHUB_TOKEN in .env — a fine-grained PAT scoped to your own repos
     with Contents (read/write) + Pull requests (read/write) permissions.
     Used both to look up repos by name and to push/open PRs.
  4. Register a project either via chat/telegram (register_project, which
     looks the repo up on GitHub by name) or directly:
       python3 -c "from remote_work import add_project;
       add_project('myproject', 'https://github.com/you/myproject.git')"

USAGE (from jarvis_telegram.py's chat tools):
  register_project("myproject", "myproject")   # or a search term
  start_session("myproject", "add rate limiting to the API")
  start_session("myproject", "fix the flaky test", open_pr=True)
  check_session("myproject")
"""

import os
import re
import json
import base64
import subprocess
import threading
import datetime
from pathlib import Path

import requests

import config
from json_store import file_lock, atomic_write_json

SCRIPT_DIR    = Path(__file__).parent
WORK_DIR      = SCRIPT_DIR / "remote_work"
PROJECTS_FILE = WORK_DIR / "projects.json"
SESSIONS_FILE = WORK_DIR / "sessions.json"

WORK_DIR.mkdir(exist_ok=True)

SESSION_TIMEOUT_SECONDS = 60 * 60  # a runaway session shouldn't run forever
GITHUB_API = "https://api.github.com"


# ── Project registry ──────────────────────────────────────────────────────────

def load_projects():
    if not PROJECTS_FILE.exists():
        return {}
    try:
        return json.loads(PROJECTS_FILE.read_text())
    except Exception:
        return {}


def add_project(name, git_url, full_name=None, default_branch=None):
    """Registers a project. `git_url` must be the plain https clone URL (no token)."""
    with file_lock(PROJECTS_FILE):
        projects = load_projects()
        projects[name] = {
            "git_url": git_url,
            "full_name": full_name,
            "default_branch": default_branch,
        }
        atomic_write_json(PROJECTS_FILE, projects)


def _github_headers():
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def register_project(name, repo_query=None):
    """
    Looks up `repo_query` (or `name` if not given) among Manav's own GitHub
    repos and registers it under `name`. Returns (info, error).
    """
    if not config.GITHUB_TOKEN:
        return None, "GITHUB_TOKEN isn't configured — set it in .env first."

    query = (repo_query or name).lower()
    matches = []
    page = 1
    try:
        while True:
            resp = requests.get(
                f"{GITHUB_API}/user/repos",
                headers=_github_headers(),
                params={"per_page": 100, "page": page, "affiliation": "owner"},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            matches.extend(r for r in batch if query in r["name"].lower())
            page += 1
            if len(batch) < 100:
                break
    except Exception as e:
        return None, f"GitHub lookup failed: {e}"

    if not matches:
        return None, f"No repo matching '{query}' found among your GitHub repos."

    exact = [r for r in matches if r["name"].lower() == query]
    chosen = exact[0] if exact else matches[0]
    if not exact and len(matches) > 1:
        names = ", ".join(r["name"] for r in matches[:8])
        return None, f"Multiple repos match '{query}': {names}. Be more specific."

    add_project(name, chosen["clone_url"], chosen["full_name"], chosen["default_branch"])
    return {"full_name": chosen["full_name"], "default_branch": chosen["default_branch"]}, None


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


# ── GitHub auth for git itself (never persisted to disk) ───────────────────────

def _auth_args():
    """
    Extra `git` args that inject a one-off Authorization header for this
    invocation only — never written into .git/config, never embedded in a
    remote URL, so the token can't leak via a stored repo or a stray `git
    remote -v`.
    """
    basic = base64.b64encode(f"x-access-token:{config.GITHUB_TOKEN}".encode()).decode()
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


def _git(args, **kwargs):
    return subprocess.run(["git"] + _auth_args() + args, **kwargs)


# ── Running a session ─────────────────────────────────────────────────────────

def _open_pull_request(project, full_name, branch, base_branch, instruction, summary):
    """Pushes `branch` and opens a PR. Returns (pr_url, error)."""
    git_url = load_projects()[project]["git_url"]
    try:
        # Push by branch name (not HEAD) — repo_dir's own checkout may be sitting
        # on a different ref; the worktree's branch exists in the shared refs
        # regardless of what repo_dir itself has checked out.
        push = _git(
            ["push", git_url, f"{branch}:{branch}"],
            cwd=WORK_DIR / project / "repo",
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        return None, f"push failed: {e}"
    if push.returncode != 0:
        return None, f"push failed: {push.stderr.strip()[-500:]}"

    try:
        resp = requests.post(
            f"{GITHUB_API}/repos/{full_name}/pulls",
            headers=_github_headers(),
            json={
                "title": instruction[:200],
                "head": branch,
                "base": base_branch,
                "body": f"Opened automatically by Jarvis remote-work.\n\n**Task:** {instruction}\n\n**Last output:**\n```\n{summary[-1500:]}\n```",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["html_url"], None
    except Exception as e:
        return None, f"PR creation failed: {e}"


def _run_claude_code(session_id, project, worktree_dir, instruction, open_pr, branch, full_name, base_branch):
    """
    Runs headless Claude Code in `worktree_dir`, blocking — call this from a
    background thread, never on the Telegram bot's main thread.
    """
    full_instruction = instruction
    if open_pr:
        full_instruction += (
            "\n\nWhen you're finished, stage and commit all your changes with a "
            "clear commit message. Do not push or open a PR yourself."
        )

    # Strip Jarvis's own ANTHROPIC_API_KEY from the child's environment — its mere
    # presence makes the `claude` CLI silently prefer pay-per-token API billing
    # over the `claude login` subscription these sessions are meant to run under.
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    log_path = worktree_dir / "session.log"
    try:
        with open(log_path, "w") as log_file:
            result = subprocess.run(
                ["claude", "-p", full_instruction, "--dangerously-skip-permissions"],
                cwd=worktree_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=SESSION_TIMEOUT_SECONDS,
                env=env,
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

    # Last ~40 lines as a cheap summary; a real diff-stat summary is a later phase.
    try:
        tail = log_path.read_text().splitlines()[-40:]
        summary = "\n".join(tail)
    except Exception:
        summary = ""

    pr_url, pr_error = None, None
    if status == "done" and open_pr:
        try:
            ahead = subprocess.run(
                ["git", "rev-list", "--count", f"origin/{base_branch}..HEAD"],
                cwd=worktree_dir, capture_output=True, text=True, timeout=15,
            )
            has_commits = ahead.returncode == 0 and int(ahead.stdout.strip() or "0") > 0
        except Exception:
            has_commits = False

        if has_commits:
            pr_url, pr_error = _open_pull_request(project, full_name, branch, base_branch, instruction, summary)
        else:
            pr_error = "no commits made — nothing to open a PR for"

    _update_session(
        session_id, status=status, summary=summary,
        finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
        pr_url=pr_url, pr_error=pr_error,
    )

    try:
        from jarvis_telegram import send_message
        icon = "✅" if status == "done" else "❌"
        msg = f"{icon} Remote work [{session_id}] on {project} {status}."
        if pr_url:
            msg += f"\n🔀 PR opened: {pr_url}"
        elif open_pr and pr_error:
            msg += f"\n⚠️ No PR: {pr_error}"
        msg += "\n/ or just ask me how it went."
        send_message(msg)
    except Exception:
        pass  # notification is best-effort — the session result is already saved either way


def start_session(project, instruction, open_pr=False):
    """
    Clones/fetches `project`, creates a fresh branch + worktree, and kicks
    off a background Claude Code run. Returns (session_id, error) — error
    is None on success. If `open_pr` is True, a successful session with
    commits will push its branch and open a PR when it finishes.
    """
    projects = load_projects()
    if project not in projects:
        known = ", ".join(sorted(projects)) or "(none registered)"
        return None, f"Unknown project '{project}'. Known: {known}"

    info = projects[project]
    git_url  = info["git_url"]
    full_name = info.get("full_name")
    if open_pr and not full_name:
        return None, f"'{project}' was registered without GitHub metadata — re-register via register_project to enable PRs."

    timestamp    = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    session_id   = f"{project}-{timestamp}"
    branch       = f"jarvis/{_slugify(instruction)}-{timestamp}"
    repo_dir     = WORK_DIR / project / "repo"
    worktree_dir = WORK_DIR / project / session_id
    base_branch  = info.get("default_branch") or "HEAD"

    try:
        if not repo_dir.exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            _git(["clone", git_url, str(repo_dir)], check=True, timeout=300)
        else:
            _git(["fetch", "origin"], cwd=repo_dir, check=True, timeout=300)

        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree_dir), f"origin/{base_branch}"],
            cwd=repo_dir, check=True, timeout=60,
        )
    except Exception as e:
        return None, f"Setup failed: {e}"

    with file_lock(SESSIONS_FILE):
        sessions = load_sessions()
        sessions.append({
            "id": session_id, "project": project, "instruction": instruction,
            "branch": branch, "status": "running", "open_pr": open_pr,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "summary": "", "pr_url": None, "pr_error": None,
        })
        atomic_write_json(SESSIONS_FILE, sessions)

    thread = threading.Thread(
        target=_run_claude_code,
        args=(session_id, project, worktree_dir, instruction, open_pr, branch, full_name, base_branch),
        daemon=True,
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
    if s.get("pr_url"):
        lines.append(f"  PR: {s['pr_url']}")
    elif s.get("pr_error"):
        lines.append(f"  PR: not opened ({s['pr_error']})")
    if s["summary"]:
        lines.append(f"  last output:\n{s['summary'][-1000:]}")
    return "\n".join(lines)
