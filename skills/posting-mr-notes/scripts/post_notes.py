"""Post a batch of GitLab MR notes from one JSON file, through glab (never a raw token).

Usage:
    python3 <home>/.claude/skills/posting-mr-notes/scripts/post_notes.py [--dry-run] /abs/path/batch.json

<home> is the home directory written out, never ~ or $HOME, so the command matches its allow rule.

batch.json:
    {
      "project": "12345678",
      "iid": 42,
      "web_url": "https://gitlab.com/group/project/-/merge_requests/42",
      "notes": [
        {"id": "F1", "kind": "diff", "body": "...", "position": {...GitLab position...}},
        {"id": "F4", "kind": "thread", "body": "..."},
        {"id": "F9", "kind": "general", "body": "..."},
        {"id": "T3", "kind": "reply", "discussion_id": "abc...", "body": "...", "resolve": true}
      ]
    }

A "thread" is a resolvable thread on no line; a "general" note is a plain comment, which GitLab
never lets anyone resolve.

Prints one receipt line per note and keeps going past a failed one.
Exit status is 1 when any note or resolve failed.
"""

import json
import subprocess
import sys


def glab(method, path, payload=None):
    cmd = ["glab", "api", path, "--method", method]
    if payload is not None:
        cmd += ["--header", "Content-Type: application/json", "--input", "-"]
    out = subprocess.run(
        cmd,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:300])
    return json.loads(out.stdout) if out.stdout.strip() else {}


def request_for(note, base):
    kind = note["kind"]
    if kind == "diff":
        return "POST", f"{base}/discussions", {"body": note["body"], "position": note["position"]}
    if kind == "thread":
        return "POST", f"{base}/discussions", {"body": note["body"]}
    if kind == "general":
        return "POST", f"{base}/notes", {"body": note["body"]}
    if kind == "reply":
        return "POST", f"{base}/discussions/{note['discussion_id']}/notes", {"body": note["body"]}
    raise ValueError(f"unknown kind {kind!r}")


def note_id(kind, response):
    return response["notes"][0]["id"] if kind in ("diff", "thread") else response["id"]


def main(argv):
    dry_run = "--dry-run" in argv
    paths = [a for a in argv if a != "--dry-run"]
    if len(paths) != 1:
        sys.exit(__doc__)
    batch = json.load(open(paths[0]))
    base = f"projects/{batch['project']}/merge_requests/{batch['iid']}"
    failed = unresolved = 0
    for note in batch["notes"]:
        method, path, payload = request_for(note, base)
        label = f"{note['id']} | {note['kind']}"
        if dry_run:
            resolve = " + resolve" if note.get("resolve") else ""
            print(f"[DRY-RUN] {label} | {method} {path}{resolve}\n{json.dumps(payload, indent=2)}\n")
            continue
        try:
            response = glab(method, path, payload)
        except (RuntimeError, ValueError) as exc:
            failed += 1
            print(f"{label} | FAILED: {exc}")
            continue
        line = f"{label} | {batch['web_url']}#note_{note_id(note['kind'], response)}"
        if note.get("resolve"):
            # The note is already on the MR here, so a failed resolve must not read as FAILED.
            try:
                glab("PUT", f"{base}/discussions/{note['discussion_id']}?resolved=true")
                line += " | resolved"
            except (RuntimeError, KeyError) as exc:
                unresolved += 1
                line += f" | RESOLVE FAILED: {exc}"
        print(line)
    total = len(batch["notes"])
    left_open = f", {unresolved} left unresolved" if unresolved else ""
    print(f"\n{'Would post' if dry_run else 'Posted'} {total - failed}/{total} to MR !{batch['iid']}{left_open}")
    sys.exit(1 if failed or unresolved else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
