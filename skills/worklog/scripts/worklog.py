"""worklog — reconstruct subjects worked on in a time window.

Reads Claude Code session transcripts (~/.claude/projects/<encoded-cwd>/*.jsonl)
plus git history for the current repo, aggregates focused time per subject, and
prints a JSON report to stdout. See SKILL.md for how the report is rendered.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
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
    if under_any(cwd, [repo_root, *live_worktrees]):
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
    "<command-message>",
    "<local-command-",
    "<task-notification>",
    "Another Claude session sent a message",
    "`/",
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
    stripped = text.strip()
    return len(stripped) < 3 or any(stripped.startswith(prefix) for prefix in _NOISE_PREFIXES)


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def inherit_session_branch(events):
    """Give branch-less events the session's dominant feature branch.

    Subagent hand-backs record no `gitBranch`, and a rebase leaves `HEAD`; both
    would otherwise split off into untitled rows. A `main` slice in a session
    that mostly sat on a feature branch is that branch's work too. Sessions with
    no feature branch at all are left untouched.
    """
    counts = {}
    for event in events:
        branch = event.get("branch")
        if branch and branch.lower() not in GENERIC_BRANCHES:
            counts[branch] = counts.get(branch, 0) + 1
    if not counts:
        return events
    # ponytail: one dominant branch per session; a session that truly works two
    # tickets keeps each feature-branch event as-is, only generic ones inherit.
    dominant = max(counts, key=counts.get)
    for event in events:
        if (event.get("branch") or "").lower() in GENERIC_BRANCHES:
            event["branch"] = dominant
    return events


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
        # Only the conversation counts as activity. The harness also writes its
        # own records, such as an `api-request` every ~16 minutes while a request
        # hangs on a sleeping machine, which the idle cap would count in full.
        if timestamp and record.get("type") in ("user", "assistant"):
            if cwd is None and record.get("cwd"):
                cwd = record["cwd"]
            branch = record.get("gitBranch")
            events.append({
                "ts": _parse_ts(timestamp),
                # A `review/X` worktree is a look at branch X: resolve scope,
                # label and ownership against X, which outlives the worktree.
                "branch": branch.removeprefix("review/") if branch else branch,
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
        "events": inherit_session_branch(events),
        "prompts": prompts,
    }


def subagent_tickets(session_path, ticket_prefix):
    """{agent name: ticket} for a session's subagents, None when none is named.

    Subagents live in `<session-id>/subagents/agent-*.jsonl` beside the session
    file. The ticket comes from the agent's `.meta.json` description ("Implement
    BPZ-1682 shell"), because an agent's own gitBranch is noise in a shared epic
    worktree. The name (`agent-<id>`) is also the branch of the agent's worktree.
    """
    if ticket_prefix:
        pattern = re.compile(rf"\b{re.escape(ticket_prefix)}-\d+", re.I)
    else:
        pattern = re.compile(r"\b[A-Z][A-Z0-9]*-\d+")
    tickets = {}
    for meta in Path(session_path).with_suffix("").glob("subagents/*.meta.json"):
        try:
            description = json.loads(meta.read_text(errors="ignore")).get("description") or ""
        except json.JSONDecodeError:
            description = ""
        match = pattern.search(description)
        tickets[meta.name.removesuffix(".meta.json")] = match.group(0).upper() if match else None
    return tickets


def subagent_marks(session_path, agent_tickets):
    """(timestamp, ticket) for every event of a session's subagents, by time.

    An agent that names no ticket marks its moments with None.
    """
    agents_dir = Path(session_path).with_suffix("") / "subagents"
    marks = []
    for name, ticket in agent_tickets.items():
        transcript = agents_dir / f"{name}.jsonl"
        if transcript.exists():
            lines = transcript.read_text(errors="ignore").splitlines()
            marks.extend((event["ts"], ticket) for event in parse_session(lines)["events"])
    return sorted(marks, key=lambda mark: mark[0])


def attribute_to_subagents(events, marks, known_branches, agent_tickets,
                           cap_minutes=IDLE_CAP_MINUTES):
    """Move an orchestrator's time onto the tickets its subagents work on.

    An event on an agent's worktree branch (`agent-<id>`) goes to that agent's
    ticket. Any other event goes to the ticket of the latest subagent event at
    or before it, within the idle cap: the agent whose report the orchestrator
    is handling. Agent time itself is never counted, it is machine time. The
    ticket resolves to its known branch, so the row keeps its slug and its
    ownership. No agent in reach, an agent naming no ticket, or a ticket with no
    branch yet leaves the event where it was.
    """
    tickets = {ticket for _, ticket in marks if ticket} | {t for t in agent_tickets.values() if t}
    branch_for = {
        ticket: next((b for b in sorted(known_branches)
                      if re.match(rf"{re.escape(ticket)}(-|$)", b, re.I)), None)
        for ticket in tickets
    }
    times = [ts for ts, _ in marks]
    cap = timedelta(minutes=cap_minutes)
    for event in events:
        ticket = agent_tickets.get(event["branch"])
        if ticket is None:
            index = bisect.bisect_right(times, event["ts"]) - 1
            if index < 0 or event["ts"] - times[index] > cap:
                continue
            ticket = marks[index][1]
        branch = branch_for.get(ticket)
        if branch:
            event["branch"] = branch
    return events


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
    all_timestamps = []  # every in-scope, in-window event, for the union estimate
    epic_tickets = {}  # epic branch -> tickets its sessions' subagents named

    for session in sessions:
        for event in session["events"]:
            ts = event["ts"]
            if not start <= ts <= end:
                continue
            cwd = event.get("cwd") or session.get("cwd")
            branch = event.get("branch")
            if in_scope(cwd, branch, repo_root, live_worktrees, known_branches):
                key, label = derive_subject(
                    branch, session.get("ai_title"), ticket_prefix, session.get("session_id")
                )
                contributing_sessions.add(session.get("session_id"))
                all_timestamps.append(ts)
                if branch and branch.startswith("epic/"):
                    epic_tickets.setdefault(branch, set()).update(
                        t for t in session.get("agent_tickets", {}).values() if t)
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
            elif under_any(cwd, worktree_parents):
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
            "titles": sorted(agg["titles"]),
            "branches": sorted(agg["branches"]),
            "prompt_samples": agg["prompts"][:4],
            "untitled": key.startswith("untitled:"),
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

    # What stays on an epic branch after attribution is work for the whole stack.
    # It is split over the epic's tickets that have own time in the window.
    own_tickets = {row["ticket"] for row in subject_rows if row["ticket"]}
    for row in subject_rows:
        named = set().union(*(epic_tickets.get(b, set()) for b in row["branches"]))
        row["split_over"] = sorted(named & own_tickets)

    def _sum(rows):
        return {
            "wallclock_min": round(sum(r["wallclock_min"] for r in rows), 1),
            "active_min": round(sum(r["active_min"] for r in rows), 1),
        }
    untitled_rows = [r for r in subject_rows if r["untitled"]]
    subtotals = {
        "ticketed": _sum([r for r in subject_rows if r["ticket"]]),
        "non_ticketed": _sum([r for r in subject_rows if not r["ticket"]]),
        "untitled": {"count": len(untitled_rows), **_sum(untitled_rows)},
    }

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
            "totals": {
                "wallclock_min": round(total_wall, 1),
                "active_min": round(total_active, 1),
                # Parallel worktree sessions are summed above; this merges them
                # onto one timeline, so it is the real "hours at the keyboard".
                "union_active_min": round(active_minutes(all_timestamps), 1),
            },
            "subtotals": subtotals,
            "unattributed": unattributed_rows,
            "reviews": review_rows,
        },
        "subjects": subject_rows,
    }


def count_workdays(start_day, end_day):
    """Number of Mon-Fri dates in the inclusive [start_day, end_day] range."""
    days = (end_day - start_day).days + 1
    return sum(1 for i in range(days) if (start_day + timedelta(days=i)).weekday() < 5)


DAY_START = "09:00"


def suggest_hours(rows, budget_hours, step=0.25):
    """Split `budget_hours` across tickets by active-minute ratio, in `step` units.

    Every ticketed row is its own entry; a row with `split_over` (an epic
    branch) is spread over those tickets by their own active minutes; other
    titled non-ticket rows fold into one `non-ticketed` entry; untitled rows are
    left out (unknown work is assumed to be spread like the known work).
    Largest-remainder rounding keeps the entries summing exactly to the budget.
    Entries are laid out back to back from DAY_START so each carries the
    `start` time to log it at.
    """
    own = {row["ticket"]: row["active_min"] for row in rows if row["ticket"]}
    shares, subjects = {}, {}
    for row in rows:
        if row.get("untitled"):
            continue
        targets = row.get("split_over")
        if targets:
            weight = sum(own[t] for t in targets)
            for target in targets:
                fraction = own[target] / weight if weight else 1 / len(targets)
                shares[target] = shares.get(target, 0.0) + row["active_min"] * fraction
                subjects.setdefault(target, []).append(row["subject"])
            continue
        label = row["ticket"] or "non-ticketed"
        shares[label] = shares.get(label, 0.0) + row["active_min"]
        subjects.setdefault(label, []).append(row["subject"])
    total = sum(shares.values())
    if total <= 0 or budget_hours <= 0:
        return []
    units = round(budget_hours / step)
    exact = {label: minutes / total * units for label, minutes in shares.items()}
    allotted = {label: int(value) for label, value in exact.items()}
    leftover = units - sum(allotted.values())
    for label in sorted(exact, key=lambda k: exact[k] - allotted[k], reverse=True)[:leftover]:
        allotted[label] += 1
    entries = []
    clock = datetime.strptime(DAY_START, "%H:%M")
    for label in sorted(shares, key=shares.get, reverse=True):
        hours = allotted[label] * step
        entries.append({
            "ticket": None if label == "non-ticketed" else label,
            "label": label,
            "subjects": subjects[label],
            "active_min": round(shares[label], 1),
            "hours": hours,
            "start": clock.strftime("%H:%M"),
        })
        clock += timedelta(hours=hours)
    return entries


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
    # A branch only on the remote (an agent pushed it, the local one was never
    # made) resolves through `origin/`; its bare name finds nothing.
    local = set(_git(repo_root, ["for-each-ref", "--format=%(refname:short)", "refs/heads"]))
    owned = set()
    for branch in known_branches:
        if branch in merged_by_user:
            owned.add(branch)
            continue
        ref = branch if branch in local else f"origin/{branch}"
        excludes = [f"^{b}" for b in base if b.replace("origin/", "") != branch]
        # (a) own commits still ahead of the mainline.
        if _git(repo_root, ["rev-list", "--max-count=1",
                            f"--author={author}", ref, *excludes]):
            owned.add(branch)
            continue
        # (c) nothing ahead of main -> already merged; own it iff the tip is ours.
        tip_author = _git(repo_root, ["log", "-1", "--format=%ae", ref])
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
    subjects = []
    by_branch = {}
    for line in raw:
        subject, _, decoration = line.partition("\x00")
        subjects.append(subject)
        for ref in re.split(r"[,\s]+", decoration):
            ref = ref.replace("origin/", "").strip()
            if ref:
                by_branch.setdefault(ref, []).append(subject)
        for ref in parse_merge_branches([subject]):
            by_branch.setdefault(ref, []).append(subject)
    for row in report["subjects"]:
        ticket = row.get("ticket")
        if ticket:
            # A ticket's commits name it in their subject. A branch decoration
            # only marks the tip, which moves on and is shared by stacked branches.
            pattern = re.compile(rf"\b{re.escape(ticket)}\b", re.I)
            commits = [s for s in subjects if pattern.search(s)]
        else:
            commits = []
            for branch in row["branches"]:
                if branch.lower() in GENERIC_BRANCHES:
                    continue
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
    parser.add_argument("--hours", type=float, default=None,
                        help="hour budget to split across tickets (default 8 per weekday in window)")
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
    # live[0] is the primary checkout: its parent holds sibling repos, not worktrees.
    worktree_parents = {str(Path(p).parent) for p in live[1:]}
    worktree_parents.add(str(Path(repo_root) / ".claude" / "worktrees"))

    sessions = []
    for path in discover_session_files(args.projects_root):
        session = parse_session(path.read_text(errors="ignore").splitlines())
        # Only sessions active in the window: reading every agent transcript on
        # disk costs seconds and moves no time inside it.
        if any(window[0] <= event["ts"] <= window[1] for event in session["events"]):
            session["agent_tickets"] = subagent_tickets(path, ticket_prefix)
            if session["agent_tickets"]:
                marks = subagent_marks(path, session["agent_tickets"])
                attribute_to_subagents(session["events"], marks, known, session["agent_tickets"])
        sessions.append(session)

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

    workdays = count_workdays(window[0].astimezone(local_tz).date(),
                              window[1].astimezone(local_tz).date())
    budget = args.hours if args.hours is not None else 8.0 * workdays
    report["meta"]["workdays"] = workdays
    report["meta"]["hours_budget"] = budget
    report["meta"]["suggested_hours"] = suggest_hours(report["subjects"], budget)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
