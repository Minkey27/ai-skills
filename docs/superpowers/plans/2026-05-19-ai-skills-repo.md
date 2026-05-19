# ai-skills Repo Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move 5 dev workflow skills from `~/.claude/skills/` into `~/Projects/ai-skills/skills/`, replace the originals with symlinks, and provide an `install.sh` other users can run after cloning.

**Architecture:** Repo becomes source of truth. Each in-scope skill is a real directory under `skills/`. The user's `~/.claude/skills/<name>` becomes a symlink pointing into the repo. Other users clone, then run `./install.sh` to symlink any subset of skills into their own `~/.claude/skills/`.

**Tech Stack:** bash (install script), git, filesystem symlinks. No build system, no tests beyond install.sh shell-level verification.

---

## File structure

```
ai-skills/
├── README.md                          # MODIFY: install instructions
├── install.sh                         # CREATE: symlink installer
├── docs/superpowers/                  # exists
│   ├── specs/2026-05-19-ai-skills-repo-design.md
│   └── plans/2026-05-19-ai-skills-repo.md  # this file
└── skills/                            # CREATE
    ├── branch-test-review/            # MOVE from ~/.claude/skills/
    ├── finalize-branch/               # MOVE
    ├── pytest-docker/                 # MOVE
    ├── rebase-on-main/                # MOVE
    └── squash/                        # MOVE
```

---

### Task 1: Move skills into the repo and symlink back

**Goal:** Flip source-of-truth for all 5 in-scope skills in one atomic batch. After this task, `~/.claude/skills/<name>` is a symlink for each in-scope skill, and `~/Projects/ai-skills/skills/<name>` holds the real content.

**Files:**
- Create: `~/Projects/ai-skills/skills/{branch-test-review,finalize-branch,pytest-docker,rebase-on-main,squash}/`
- Replace with symlink: `~/.claude/skills/{branch-test-review,finalize-branch,pytest-docker,rebase-on-main,squash}/`

- [ ] **Step 1: Snapshot current state for rollback safety**

Run:
```bash
ls -la ~/.claude/skills/ > /tmp/ai-skills-pre-move.txt
cat /tmp/ai-skills-pre-move.txt
```
Expected: a directory listing showing 9 real skill directories. Keep this file until Task 6 verification passes.

- [ ] **Step 2: Create the skills/ directory in the repo**

Run:
```bash
mkdir -p ~/Projects/ai-skills/skills
```
Expected: no output. Verify with `ls ~/Projects/ai-skills/skills` (empty).

- [ ] **Step 3: Move each in-scope skill, then symlink back**

Run one skill at a time so any failure is contained:
```bash
for skill in branch-test-review finalize-branch pytest-docker rebase-on-main squash; do
  src="$HOME/.claude/skills/$skill"
  dst="$HOME/Projects/ai-skills/skills/$skill"
  if [ ! -d "$src" ] || [ -L "$src" ]; then
    echo "SKIP $skill (not a real dir at $src)"
    continue
  fi
  if [ -e "$dst" ]; then
    echo "ABORT: $dst already exists" >&2
    exit 1
  fi
  mv "$src" "$dst"
  ln -s "$dst" "$src"
  echo "OK   $skill"
done
```

Expected output:
```
OK   branch-test-review
OK   finalize-branch
OK   pytest-docker
OK   rebase-on-main
OK   squash
```

- [ ] **Step 4: Verify symlinks resolve correctly**

Run:
```bash
ls -la ~/.claude/skills/ | grep -E 'branch-test-review|finalize-branch|pytest-docker|rebase-on-main|squash'
```
Expected: each entry shows `lrwxr-xr-x ... <name> -> /Users/yyung/Projects/ai-skills/skills/<name>`.

Also verify content is readable through the link:
```bash
cat ~/.claude/skills/squash/SKILL.md | head -3
```
Expected: the actual first three lines of the squash SKILL.md (frontmatter or heading).

- [ ] **Step 5: Confirm peon-ping-* skills are untouched**

Run:
```bash
ls -la ~/.claude/skills/ | grep peon-ping
```
Expected: 4 entries, all `drwx...` (real directories, no `l` prefix, no `->`).

- [ ] **Step 6: Commit the migrated skills**

```bash
cd ~/Projects/ai-skills
git add skills/
git status
```
Expected: `git status` shows 5 new directories with their contents staged under `skills/`.

```bash
git commit -m "Add 5 dev workflow skills as source of truth

Migrated from ~/.claude/skills/ — those paths are now symlinks
into this repo.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```
Expected: commit succeeds with ~5 new directories' worth of file additions.

---

### Task 2: Verify a skill still loads correctly through the symlink

**Goal:** Confirm Claude Code's skill loader treats a symlinked skill identically to a real one. Pick the smallest skill (`squash`, single SKILL.md) for the check.

**Files:**
- Read-only verification, no file changes.

- [ ] **Step 1: Resolve the symlink and confirm path target**

Run:
```bash
readlink ~/.claude/skills/squash
```
Expected: `/Users/yyung/Projects/ai-skills/skills/squash` (or equivalent absolute path).

- [ ] **Step 2: Read SKILL.md through both paths and diff**

Run:
```bash
diff ~/.claude/skills/squash/SKILL.md ~/Projects/ai-skills/skills/squash/SKILL.md
```
Expected: no output (files identical because they ARE the same file).

- [ ] **Step 3: Verify frontmatter is intact**

Run:
```bash
head -5 ~/.claude/skills/squash/SKILL.md
```
Expected: shows the `---` frontmatter header with `name:` and `description:` fields. If frontmatter is missing or malformed, the skill won't be discoverable — stop and investigate.

No commit for this task — it's verification only.

---

### Task 3: Write install.sh

**Goal:** Provide a small bash script that other users run after cloning to symlink the repo's skills into their `~/.claude/skills/`. Idempotent, refuses to clobber by default, supports `--force`.

**Files:**
- Create: `~/Projects/ai-skills/install.sh`

- [ ] **Step 1: Create install.sh with the script body**

Write to `~/Projects/ai-skills/install.sh`:

```bash
#!/usr/bin/env bash
# Symlink every skill under skills/ into ~/.claude/skills/.
# Usage: ./install.sh [--force]
#   --force  Replace any existing ~/.claude/skills/<name> entry.

set -euo pipefail

force=0
if [ "${1:-}" = "--force" ]; then
  force=1
elif [ -n "${1:-}" ]; then
  echo "Usage: $0 [--force]" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src_dir="$repo_root/skills"
dst_dir="$HOME/.claude/skills"

if [ ! -d "$src_dir" ]; then
  echo "No skills/ directory at $src_dir" >&2
  exit 1
fi

mkdir -p "$dst_dir"

installed=0
skipped=0
replaced=0

for skill_path in "$src_dir"/*/; do
  name="$(basename "$skill_path")"
  target="$dst_dir/$name"

  if [ -e "$target" ] || [ -L "$target" ]; then
    if [ "$force" = "1" ]; then
      rm -rf "$target"
      ln -s "${skill_path%/}" "$target"
      echo "REPLACED $name"
      replaced=$((replaced + 1))
    else
      echo "SKIP     $name (already exists; use --force to replace)"
      skipped=$((skipped + 1))
    fi
  else
    ln -s "${skill_path%/}" "$target"
    echo "INSTALL  $name"
    installed=$((installed + 1))
  fi
done

echo
echo "Done. installed=$installed replaced=$replaced skipped=$skipped"
```

- [ ] **Step 2: Make it executable**

Run:
```bash
chmod +x ~/Projects/ai-skills/install.sh
ls -l ~/Projects/ai-skills/install.sh
```
Expected: mode shows `-rwxr-xr-x`.

- [ ] **Step 3: Sanity-check syntax**

Run:
```bash
bash -n ~/Projects/ai-skills/install.sh
```
Expected: no output (means no syntax errors).

---

### Task 4: Verify install.sh against a simulated fresh user

**Goal:** Confirm install.sh installs, skips, and force-replaces correctly without touching the real `~/.claude/skills/`. We do this by overriding `$HOME` to a temp directory.

**Files:**
- Read-only verification using a temp directory. No changes to the repo.

- [ ] **Step 1: Set up a fake HOME**

Run:
```bash
fake_home="$(mktemp -d)"
echo "$fake_home"
ls -la "$fake_home"
```
Expected: empty temp dir is created. Capture the path for later steps.

- [ ] **Step 2: Run a clean install into the fake HOME**

Run (in the same shell, with `fake_home` still set):
```bash
HOME="$fake_home" ~/Projects/ai-skills/install.sh
```
Expected output: 5 `INSTALL` lines (one per skill) and `Done. installed=5 replaced=0 skipped=0`.

- [ ] **Step 3: Verify symlinks landed in the fake HOME**

Run:
```bash
ls -la "$fake_home/.claude/skills/"
```
Expected: 5 symlink entries (`l...`), each pointing to `/Users/yyung/Projects/ai-skills/skills/<name>`.

- [ ] **Step 4: Re-run without --force — should skip all**

Run:
```bash
HOME="$fake_home" ~/Projects/ai-skills/install.sh
```
Expected: 5 `SKIP` lines, `Done. installed=0 replaced=0 skipped=5`.

- [ ] **Step 5: Plant a fake real-directory and verify SKIP protects it**

Run:
```bash
rm "$fake_home/.claude/skills/squash"
mkdir "$fake_home/.claude/skills/squash"
echo "user content" > "$fake_home/.claude/skills/squash/SKILL.md"
HOME="$fake_home" ~/Projects/ai-skills/install.sh
cat "$fake_home/.claude/skills/squash/SKILL.md"
```
Expected: install run outputs `SKIP squash`; the cat afterward still prints `user content` (unchanged).

- [ ] **Step 6: Run with --force and verify replacement**

Run:
```bash
HOME="$fake_home" ~/Projects/ai-skills/install.sh --force
ls -la "$fake_home/.claude/skills/squash"
```
Expected: output includes `REPLACED squash` for at least the planted dir, summary line shows `replaced>=1`. The `ls -la` confirms `squash` is now a symlink pointing into the repo.

- [ ] **Step 7: Clean up the fake HOME**

Run:
```bash
rm -rf "$fake_home"
```
Expected: no output. The real `~/.claude/skills/` was never touched.

No commit for this task — verification only.

---

### Task 5: Update README.md with install instructions

**Goal:** Rewrite the placeholder README so a stranger landing on the GitHub repo understands what these skills are and how to install them.

**Files:**
- Modify: `~/Projects/ai-skills/README.md` (currently 2 lines)

- [ ] **Step 1: Replace README contents**

Write to `~/Projects/ai-skills/README.md`:

```markdown
# ai-skills

A personal collection of [Claude Code](https://docs.claude.com/en/docs/claude-code) skills I use day to day. Sharing them here so others can install them with a single command.

## Skills included

- **branch-test-review** — generate a styled HTML report of pytest tests added/modified on the current branch.
- **finalize-branch** — review, simplify, squash, and open an MR for the current branch.
- **pytest-docker** — run pytest inside the project's docker-compose backend container.
- **rebase-on-main** — rebase the current feature branch onto `main` with guided conflict resolution.
- **squash** — reorganize messy or fixup commits into clean logical commits.

Each skill is a directory under [`skills/`](./skills) containing a `SKILL.md` (and optional helper scripts).

## Install

```sh
git clone https://github.com/<your-user>/ai-skills.git
cd ai-skills
./install.sh
```

This symlinks every skill in `skills/` into `~/.claude/skills/`. Existing entries are left alone — pass `--force` to replace them:

```sh
./install.sh --force
```

### Manual install (single skill)

```sh
ln -s "$PWD/skills/<name>" ~/.claude/skills/<name>
```

## Uninstall

Symlinks only — safe to delete directly:

```sh
rm ~/.claude/skills/<name>
```

## Updating

```sh
cd ai-skills
git pull
```

Because the installed paths are symlinks into this clone, `git pull` is the entire update step. No reinstall needed.
```

- [ ] **Step 2: Eyeball the rendered output**

Run:
```bash
cat ~/Projects/ai-skills/README.md
```
Expected: the file matches the markdown above, no stray characters.

---

### Task 6: Final commit and end-to-end verification

**Goal:** Commit install.sh + README and confirm the user's own machine still has working skills through the symlinks.

**Files:**
- Stage: `install.sh`, `README.md`

- [ ] **Step 1: Review what's about to be committed**

Run:
```bash
cd ~/Projects/ai-skills
git status
git diff README.md
```
Expected: `install.sh` listed as new file (executable), `README.md` shown as modified with the full new content.

- [ ] **Step 2: Commit**

Run:
```bash
git add install.sh README.md
git commit -m "Add install.sh and update README with install instructions

install.sh symlinks every skill under skills/ into ~/.claude/skills/.
Idempotent; refuses to clobber existing entries unless --force.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```
Expected: commit succeeds.

- [ ] **Step 3: Confirm git log is clean and ordered**

Run:
```bash
git log --oneline
```
Expected: at least 4 commits — initial commit, design doc, skills migration, install + README.

- [ ] **Step 4: Final end-to-end check on the user's machine**

Run:
```bash
ls -la ~/.claude/skills/ | grep -E 'branch-test-review|finalize-branch|pytest-docker|rebase-on-main|squash'
readlink ~/.claude/skills/finalize-branch
test -f ~/.claude/skills/finalize-branch/SKILL.md && echo "SKILL.md reachable through symlink"
```
Expected:
- 5 symlinks shown.
- `readlink` prints the repo path.
- The final echo prints `SKILL.md reachable through symlink`.

- [ ] **Step 5: Remove the rollback snapshot**

Run:
```bash
rm /tmp/ai-skills-pre-move.txt
```
Expected: no output. Migration is complete and verified.

---

## Self-review notes

- All 5 in-scope skills covered in Task 1; peon-ping-* explicitly excluded and verified untouched.
- install.sh tested via temp HOME (Task 4) covering install / skip / force-replace / protection of real dirs.
- README install + uninstall + update paths covered in Task 5.
- No type/signature consistency to check — there's no code beyond install.sh, and its flags (`--force`) are referenced consistently.
- No placeholders left (`<your-user>` in README is a documentation placeholder users substitute, not a plan gap).
