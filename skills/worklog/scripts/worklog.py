"""worklog — reconstruct subjects worked on in a time window.

Reads Claude Code session transcripts (~/.claude/projects/<encoded-cwd>/*.jsonl)
plus git history for the current repo, aggregates focused time per subject, and
prints a JSON report to stdout. See SKILL.md for how the report is rendered.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

IDLE_CAP_MINUTES = 15
GENERIC_BRANCHES = {"main", "master", "head", "develop", ""}


def active_minutes(timestamps, cap_minutes=IDLE_CAP_MINUTES):
    """Sum gaps between consecutive (sorted) timestamps, each gap capped."""
    if len(timestamps) < 2:
        return 0.0
    ordered = sorted(timestamps)
    cap_seconds = cap_minutes * 60
    total = 0.0
    for earlier, later in zip(ordered, ordered[1:]):
        total += min((later - earlier).total_seconds(), cap_seconds)
    return total / 60.0


def parse_window(arg, today, tz):
    """Parse a window arg into (start_utc, end_utc) aware datetimes.

    arg: None -> today; "YYYY-MM-DD" -> that day; "YYYY-MM-DD..YYYY-MM-DD" -> range.
    Day boundaries are local (tz), then converted to UTC.
    """

    def bounds(day):
        start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
        end = datetime(day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=tz)
        return start, end

    if not arg:
        start, end = bounds(today)
    elif ".." in arg:
        left, right = arg.split("..", 1)
        start, _ = bounds(date.fromisoformat(left.strip()))
        _, end = bounds(date.fromisoformat(right.strip()))
    else:
        start, end = bounds(date.fromisoformat(arg.strip()))
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


_MERGE_RE = re.compile(r"Merge branch '([^']+)'")


def parse_merge_branches(subjects):
    """Extract branch names from `Merge branch 'X' ...` commit subjects."""
    found = set()
    for subject in subjects:
        match = _MERGE_RE.match(subject)
        if match:
            found.add(match.group(1))
    return found


def normalize_refs(lines):
    """Ref short-names -> set; strip a leading `origin/`, drop HEAD/blank."""
    names = set()
    for line in lines:
        name = line.strip()
        if name.startswith("origin/"):
            name = name[len("origin/"):]
        if name in ("", "HEAD"):
            continue
        names.add(name)
    return names


_TICKET_KEY_RE = re.compile(r"^[A-Z0-9]+-\d+$")


def is_ticket_key(key):
    """True if a subject key looks like a normalized ticket id (e.g. BPZ-756)."""
    return bool(_TICKET_KEY_RE.match(key))


def derive_subject(branch, ai_title, ticket_prefix, session_id):
    """Map a branch (+ session context) to a (subject_key, display_label).

    Labeling only — does not affect scope. Ticket branches normalize to
    `PREFIX-### slug`; other feature branches use the branch name; main/empty
    falls back to the session's ai-title, then to an untitled marker.
    """
    branch = (branch or "").strip()
    if branch:
        if ticket_prefix:
            match = re.match(rf"^{re.escape(ticket_prefix)}-(\d+)(?:-(.*))?$", branch, re.I)
            if match:
                ticket = f"{ticket_prefix.upper()}-{match.group(1)}"
                slug = match.group(2) or ""
                return ticket, f"{ticket} {slug}".strip()
        else:
            match = re.match(r"^([a-z0-9]+-\d+)(?:-(.*))?$", branch, re.I)
            if match:
                ticket = match.group(1).upper()
                slug = match.group(2) or ""
                return ticket, f"{ticket} {slug}".strip()
        if branch.lower() not in GENERIC_BRANCHES:
            return branch, branch.replace("-", " ").replace("_", " ")
    if ai_title:
        return f"title:{ai_title}", ai_title
    short = (session_id or "????????")[:8]
    return f"untitled:{short}", f"main (untitled {short})"


def _norm(path):
    return (path or "").rstrip("/")


def in_scope(cwd, branch, repo_root, live_worktrees, known_branches):
    """True if a session record belongs to this repo (rules a/b/c)."""
    cwd = _norm(cwd)
    if cwd == _norm(repo_root):
        return True
    if cwd in {_norm(w) for w in live_worktrees}:
        return True
    if branch and branch in known_branches:
        return True
    return False


def under_any(cwd, parents):
    """True if cwd is under any of the given parent directories."""
    cwd = _norm(cwd)
    for parent in parents:
        parent = _norm(parent)
        if parent and (cwd == parent or cwd.startswith(parent + "/")):
            return True
    return False


_NOISE_PREFIXES = (
    "Base directory for this skill:",
    "Caveat:",
    "<system-reminder",
    "[Request interrupted",
    "<command-name>",
)


def _user_text(message):
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text")
    return None


def _is_noise(text):
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in _NOISE_PREFIXES)


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_session(lines):
    """Parse jsonl lines of one session into a structured dict."""
    session_id = ai_title = cwd = None
    events = []
    prompts = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = session_id or record.get("sessionId")
        if record.get("type") == "ai-title" and record.get("aiTitle"):
            ai_title = record["aiTitle"]
        timestamp = record.get("timestamp")
        if timestamp:
            if cwd is None and record.get("cwd"):
                cwd = record["cwd"]
            events.append({
                "ts": _parse_ts(timestamp),
                "branch": record.get("gitBranch"),
                "cwd": record.get("cwd"),
            })
        if record.get("type") == "user":
            text = _user_text(record.get("message"))
            if text and not _is_noise(text):
                prompts.append(text.strip()[:120])
    return {
        "session_id": session_id,
        "ai_title": ai_title,
        "cwd": cwd,
        "events": events,
        "prompts": prompts,
    }


def build_report(sessions, *, window, repo_root, live_worktrees,
                 known_branches, worktree_parents, ticket_prefix,
                 owned_branches=None):
    """Turn parsed sessions into the full report dict (subjects + meta).

    `owned_branches`, when given, is the set of feature branches the user
    personally authored commits on. Branch-derived subjects whose branches
    are all absent from it are treated as reviews (someone else's branch that
    was merely checked out) and routed to `meta.reviews` instead of counting
    as own work. `None` disables the split (every subject stays as work)."""
    start, end = window
    subjects = {}
    unattributed = {}
    contributing_sessions = set()
    # Earliest-ever timestamp per subject key, in scope, regardless of window —
    # lets us tell "worked on again today" apart from "picked up today".
    first_seen = {}

    for session in sessions:
        for event in session["events"]:
            ts = event["ts"]
            cwd = event.get("cwd") or session.get("cwd")
            branch = event.get("branch")
            in_window = start <= ts <= end
            if in_scope(cwd, branch, repo_root, live_worktrees, known_branches):
                key, label = derive_subject(
                    branch, session.get("ai_title"), ticket_prefix, session.get("session_id")
                )
                if key not in first_seen or ts < first_seen[key]:
                    first_seen[key] = ts
                if not in_window:
                    continue
                contributing_sessions.add(session.get("session_id"))
                agg = subjects.setdefault(key, {
                    "subject": label,
                    "ticket": key if is_ticket_key(key) else None,
                    "timestamps": [],
                    "sessions": set(),
                    "titles": set(),
                    "branches": set(),
                    "prompts": [],
                })
                agg["timestamps"].append(ts)
                if session.get("session_id"):
                    agg["sessions"].add(session["session_id"])
                if session.get("ai_title"):
                    agg["titles"].add(session["ai_title"])
                if branch:
                    agg["branches"].add(branch)
                for prompt in session.get("prompts", [])[:2]:
                    if prompt not in agg["prompts"]:
                        agg["prompts"].append(prompt)
            elif in_window and under_any(cwd, worktree_parents):
                bucket = unattributed.setdefault((cwd, branch), {
                    "branch": branch, "cwd": cwd, "timestamps": [],
                })
                bucket["timestamps"].append(ts)

    subject_rows = []
    review_rows = []
    total_wall = total_active = 0.0
    for key, agg in subjects.items():
        timeline = sorted(agg["timestamps"])
        wall = (timeline[-1] - timeline[0]).total_seconds() / 60.0 if len(timeline) > 1 else 0.0
        active = active_minutes(timeline)
        row = {
            "subject": agg["subject"],
            "ticket": agg["ticket"],
            "wallclock_min": round(wall, 1),
            "active_min": round(active, 1),
            "session_count": len(agg["sessions"]),
            "first": timeline[0].isoformat(),
            "last": timeline[-1].isoformat(),
            "started_in_window": first_seen[key] >= start,
            "titles": sorted(agg["titles"]),
            "branches": sorted(agg["branches"]),
            "prompt_samples": agg["prompts"][:4],
            "commits": [],  # filled by main() via git enrichment
            "merged_commits": [],  # filled by main() via git enrichment
        }
        # A branch-derived subject (ticket or plain feature branch, not a
        # main/title/untitled row) with no branch the user authored on is a
        # review of someone else's work, not work the user did.
        branch_derived = not key.startswith(("title:", "untitled:"))
        is_review = (
            owned_branches is not None
            and branch_derived
            and not any(b in owned_branches for b in agg["branches"])
        )
        if is_review:
            review_rows.append(row)
        else:
            total_wall += wall
            total_active += active
            subject_rows.append(row)
    subject_rows.sort(key=lambda row: row["active_min"], reverse=True)
    review_rows.sort(key=lambda row: row["active_min"], reverse=True)

    unattributed_rows = []
    for bucket in unattributed.values():
        timeline = sorted(bucket["timestamps"])
        wall = (timeline[-1] - timeline[0]).total_seconds() / 60.0 if len(timeline) > 1 else 0.0
        unattributed_rows.append({
            "branch": bucket["branch"],
            "cwd": bucket["cwd"],
            "first": timeline[0].isoformat(),
            "last": timeline[-1].isoformat(),
            "wallclock_min": round(wall, 1),
        })

    return {
        "meta": {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "ticket_prefix": ticket_prefix or None,
            "idle_cap_min": IDLE_CAP_MINUTES,
            "known_branch_count": len(known_branches),
            "sessions_counted": len(contributing_sessions),
            "totals": {"wallclock_min": round(total_wall, 1), "active_min": round(total_active, 1)},
            "unattributed": unattributed_rows,
            "reviews": review_rows,
        },
        "subjects": subject_rows,
    }


def _git(repo_root, args):
    """Run a git command in repo_root; return stdout lines (empty on failure)."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return out.splitlines()


def build_known_branches(repo_root):
    """Union of current ref short-names and merged-branch names, minus generics."""
    refs = normalize_refs(_git(repo_root, [
        "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes",
    ]))
    merged = parse_merge_branches(_git(repo_root, ["log", "--merges", "--format=%s"]))
    return {b for b in (refs | merged) if b.lower() not in GENERIC_BRANCHES}


_BASE_REF_CANDIDATES = (
    "main", "master", "develop",
    "origin/main", "origin/master", "origin/develop",
)


def build_owned_branches(repo_root, author, known_branches):
    """Feature branches the user personally authored commits on.

    A branch is "owned" if ANY of:
    (a) the user authored a commit reachable from the branch but NOT from the
        mainline — real unmerged work on the branch, not commits inherited from
        main;
    (b) the user authored the `Merge branch '<branch>'` commit that landed it
        (merge-commit workflows); or
    (c) the branch has no commits ahead of main (already fast-forward/rebase
        merged, or empty) and its tip commit was authored by the user — the
        linear-history equivalent of (b), where merged commits now live on main
        so (a) finds nothing.
    A teammate's branch merely checked out to review matches none: its unique
    commits and its tip are authored by someone else, and the user never merged
    it. When the author is unknown, every branch is owned so nothing is
    misclassified."""
    if not author:
        return set(known_branches)
    base = [ref for ref in _BASE_REF_CANDIDATES
            if _git(repo_root, ["rev-parse", "--verify", "--quiet", ref])]
    # (b) branches the user personally merged, from their own merge commits.
    merged_by_user = set(parse_merge_branches(
        _git(repo_root, ["log", "--merges", f"--author={author}", "--format=%s"])
    ))
    owned = set()
    for branch in known_branches:
        if branch in merged_by_user:
            owned.add(branch)
            continue
        excludes = [f"^{b}" for b in base if b.replace("origin/", "") != branch]
        # (a) own commits still ahead of the mainline.
        if _git(repo_root, ["rev-list", "--max-count=1",
                            f"--author={author}", branch, *excludes]):
            owned.add(branch)
            continue
        # (c) nothing ahead of main -> already merged; own it iff the tip is ours.
        tip_author = _git(repo_root, ["log", "-1", "--format=%ae", branch])
        if tip_author and tip_author[0].strip() == author:
            owned.add(branch)
    return owned


def live_worktree_paths(repo_root):
    """Absolute paths of all current worktrees (incl. the primary)."""
    paths = []
    for line in _git(repo_root, ["worktree", "list", "--porcelain"]):
        if line.startswith("worktree "):
            paths.append(line[len("worktree "):].strip())
    return paths


def discover_session_files(projects_root):
    """All `*/*.jsonl` session files under ~/.claude/projects."""
    return list(Path(projects_root).glob("*/*.jsonl"))


def _enrich_commits(report, repo_root, start_utc, end_utc, author):
    """Attach commit subjects to subjects whose branch set the commit references."""
    raw = _git(repo_root, [
        "log", "--all", f"--author={author}",
        f"--since={start_utc.isoformat()}", f"--until={end_utc.isoformat()}",
        "--format=%s%x00%D",
    ])
    by_branch = {}
    for line in raw:
        subject, _, decoration = line.partition("\x00")
        for ref in re.split(r"[,\s]+", decoration):
            ref = ref.replace("origin/", "").strip()
            if ref:
                by_branch.setdefault(ref, []).append(subject)
        for ref in parse_merge_branches([subject]):
            by_branch.setdefault(ref, []).append(subject)
    for row in report["subjects"]:
        commits = []
        for branch in row["branches"]:
            commits.extend(by_branch.get(branch, []))
        unique_sorted = sorted(set(commits))
        row["commits"] = unique_sorted[:8]
        row["merged_commits"] = [c for c in unique_sorted if _MERGE_RE.match(c)]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconstruct worklog for a time window.")
    parser.add_argument("window", nargs="?", default=None,
                        help="YYYY-MM-DD, or YYYY-MM-DD..YYYY-MM-DD; default today")
    parser.add_argument("--ticket-prefix", default=None,
                        help="override AI_SKILLS_TICKET_PREFIX")
    parser.add_argument("--projects-root", default=str(Path.home() / ".claude" / "projects"))
    args = parser.parse_args(argv)

    ticket_prefix = args.ticket_prefix
    if ticket_prefix is None:
        ticket_prefix = os.environ.get("AI_SKILLS_TICKET_PREFIX", "")

    repo_root = (_git(".", ["rev-parse", "--show-toplevel"]) or [""])[0].strip()
    if not repo_root:
        print(json.dumps({"error": "not inside a git repository"}))
        return 1

    local_tz = datetime.now().astimezone().tzinfo
    window = parse_window(args.window, datetime.now(local_tz).date(), local_tz)
    live = live_worktree_paths(repo_root)
    known = build_known_branches(repo_root)
    worktree_parents = {str(Path(p).parent) for p in live}
    worktree_parents.add(str(Path(repo_root) / ".claude" / "worktrees"))

    sessions = [parse_session(p.read_text(errors="ignore").splitlines())
                for p in discover_session_files(args.projects_root)]

    author = (_git(repo_root, ["config", "user.email"]) or
              _git(repo_root, ["config", "user.name"]) or [""])[0].strip()
    owned = build_owned_branches(repo_root, author, known)

    report = build_report(
        sessions, window=window, repo_root=repo_root, live_worktrees=set(live),
        known_branches=known, worktree_parents=worktree_parents, ticket_prefix=ticket_prefix,
        owned_branches=owned,
    )
    report["meta"]["repo_root"] = repo_root

    if author:
        _enrich_commits(report, repo_root, *window, author)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
