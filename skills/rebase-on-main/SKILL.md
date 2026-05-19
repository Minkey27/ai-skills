---
name: rebase-on-main
description: Use when the current feature branch needs to be rebased onto main, with conflict resolution and post-rebase verification
---

# Rebase on Main

## Overview

Rebase current branch onto main, resolve merge conflicts intelligently, and verify the codebase afterwards.

**Core principle:** Automate the routine, pause for the ambiguous, verify everything.

## Config

This skill reads optional config via the `AI_SKILLS_*` env vars. Recommended setup is one line in `~/.zshenv`:

```sh
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
```

That makes the variables available to every shell Claude spawns. Commands below use `${VAR:-default}` syntax or guard each step on whether the relevant variable is set.

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_BACKEND_SERVICE` | `backend` | Docker-compose service for migration/lint commands |
| `AI_SKILLS_LINT_CMD` | _(empty)_ | Command to run lint. Empty → skip lint step |
| `AI_SKILLS_FORMAT_CMD` | _(empty)_ | Command to run formatter. Empty → skip format step |
| `AI_SKILLS_MIGRATIONS_PATH` | _(empty)_ | Path to alembic migrations dir. Empty → skip migration verification |
| `AI_SKILLS_ALEMBIC_CMD` | _(empty)_ | Shell command that invokes `alembic` in the project env. Empty → skip migration verification |

## Pre-flight Checks

Before starting, verify ALL of these:

1. **Clean working tree** — `git status` must show no uncommitted changes. If dirty: stop, tell user to commit or stash.
2. **Not on main** — refuse to run if current branch is `main`.
3. **No rebase in progress** — check for `.git/rebase-merge` or `.git/rebase-apply`. If found: ask user whether to `--continue` or `--abort` the existing rebase.
   - If user says `--abort`: run `git rebase --abort`, then restart this skill from the beginning.
   - If user says `--continue`: resume at **Conflict Resolution** (the rebase is mid-flight, so conflicts may still appear).
4. **Docker is running** (only if migration/lint steps will run) — verify the service container is reachable:
   ```bash
   docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" echo "ok"
   ```
   If this fails: warn the user that migration verification and tests require Docker. Ask whether to proceed without them or wait. Skip this check entirely if `AI_SKILLS_ALEMBIC_CMD`, `AI_SKILLS_LINT_CMD`, and `AI_SKILLS_FORMAT_CMD` are all empty.

## Step 0: Fetch and Snapshot

### Fetch latest main

Ensure local `main` reflects the remote before rebasing:

```bash
git fetch origin main:main
```

If this fails (e.g., because the current branch tracks `main`), fall back to:
```bash
git fetch origin main
```

### Save recovery point and diff snapshot

```bash
PRE_REBASE_REF=$(git rev-parse HEAD)
MERGE_BASE=$(git merge-base HEAD main)
git diff main...$PRE_REBASE_REF > /tmp/rebase-before.diff
```

`$PRE_REBASE_REF` allows instant recovery. The diff snapshot allows verification that no changes were lost.

## Rebase

Run `git rebase main`.

- If no conflicts: proceed to **Diff Verification**.
- If conflicts: proceed to **Conflict Resolution**.

## Conflict Resolution

For each conflicting commit during the rebase:

1. Read each conflicting file.
2. Assess confidence:

| Confidence | Examples | Action |
|------------|----------|--------|
| **High** | Import reordering, formatting, non-overlapping changes, added lines in different sections | Resolve automatically, continue |
| **Low** | Both sides changed same function/logic, ambiguous intent, semantic conflicts | Pause, show the conflict to the user, ask for guidance |

3. After resolving all files in a commit: `git add` resolved files, then `git rebase --continue`.
4. Repeat until rebase completes.

**When asking the user about a low-confidence conflict:**
- Show the conflicting hunks clearly
- Explain what each side (main vs branch) was trying to do
- Suggest an approach if you have one, but let the user decide

## Diff Verification

Verify no changes were lost or mangled during the rebase:

```bash
git diff main > /tmp/rebase-after.diff
diff /tmp/rebase-before.diff /tmp/rebase-after.diff
```

- **No output** = diffs are identical. All branch changes preserved. Proceed to **Migration Verification**.
- **Any output** = something changed during rebase. **STOP.** Show the difference to the user.
  - If the difference is expected (e.g., a conflict resolution intentionally changed logic): user confirms, proceed.
  - If unexpected: recover immediately:
    ```bash
    git reset --hard $PRE_REBASE_REF
    ```
    Report what happened and stop.

## Migration Verification

**Skip this whole section entirely if `AI_SKILLS_ALEMBIC_CMD` is empty** — the project doesn't use Alembic or hasn't configured the skill for it.

After rebase completes (and after diff verification passes), check that Alembic migrations form a single linear chain.

**Skip this section if Docker is not running** (warned during pre-flight). In that case, use the fallback grep approach in step 1.

### 1. Check for multiple heads

```bash
eval "$AI_SKILLS_ALEMBIC_CMD heads"
```

**Fallback without Docker** — grep migration files directly:
```bash
# Get all revision IDs and down_revisions from migration files
grep -r "^revision = \|^down_revision = " "$AI_SKILLS_MIGRATIONS_PATH/versions/" | sort
```
If any `down_revision` points to a revision that is NOT another branch migration AND is not main's head, there may be a fork.

- **Single head** → migrations are fine, proceed to **Verification**.
- **Multiple heads** → the branch's migrations now share a parent with migrations that landed on main. Proceed to step 2.

### 2. Identify which migrations belong to this branch

```bash
git log main..HEAD --oneline -- "$AI_SKILLS_MIGRATIONS_PATH/versions/"
```

This shows which migration files were added or modified on this branch. Cross-reference with:

```bash
eval "$AI_SKILLS_ALEMBIC_CMD history"
```

### 3. Re-parent branch migrations onto main's head

For each migration file that belongs to **this branch** (in chronological order):

1. Read the migration file.
2. Find the current `down_revision`.
3. Update `down_revision` to point to main's latest head (for the first branch migration) or to the previous branch migration (for subsequent ones).
4. **Do NOT create a merge migration** — instead, rewrite the `down_revision` chain so branch migrations sit on top of main's migrations linearly.

Example: if main's head is `abc123` and the branch has two migrations `mig_A` (down=`old_parent`) -> `mig_B` (down=`mig_A`):
- Update `mig_A`: `down_revision = "abc123"`
- `mig_B` stays unchanged (already points to `mig_A`)

### 4. Verify single head

```bash
eval "$AI_SKILLS_ALEMBIC_CMD heads"
```

Must show exactly one head. If not, stop and report.

### 5. Commit the fix

If migration files were modified, create a new commit:
```
fix(migrations): re-parent branch migrations after rebase
```

## Verification

Run these steps in order.

### 1. Lint and format

Run lint and format **only if the corresponding config variable is set**. Skip the step entirely when the variable is empty.

```bash
[ -n "${AI_SKILLS_LINT_CMD:-}" ] && eval "$AI_SKILLS_LINT_CMD"
[ -n "${AI_SKILLS_FORMAT_CMD:-}" ] && eval "$AI_SKILLS_FORMAT_CMD"
```

If lint/format made changes (check `git status`): auto-commit the fix:
```
style: fix lint/format issues after rebase
```

Then re-run to confirm they pass clean. If they still fail after auto-fix: stop and report.

### 2. Server health

```bash
docker compose logs --tail=20 "${AI_SKILLS_BACKEND_SERVICE:-backend}"
```

Check for startup errors or crash loops. If the server is down: stop and report. Skip this step if the project doesn't run a server in Docker.

### 3. Tests

Invoke the **pytest-docker** skill to run the test suite (or a project-appropriate test skill). Do not run pytest directly.

If tests fail: stop and report. Do not attempt to auto-fix test failures.

## Recovery

If anything goes wrong after the rebase started, the branch can be restored:

```bash
git reset --hard $PRE_REBASE_REF
```

This restores the branch to its exact pre-rebase state. Use this when:
- Diff verification shows unexpected changes and the user doesn't confirm
- The rebase leaves the codebase in an unrecoverable state
- The user asks to abort at any point

## Cleanup

After successful verification, remove temporary files:

```bash
rm -f /tmp/rebase-before.diff /tmp/rebase-after.diff
```

## Report

**All pass:** Summarize what happened — number of commits rebased, conflicts resolved (and how), diff verification result, migration changes (if any), lint/format fixes (if any), test results.

**Any failure:** Report the failure output and the recovery command (`git reset --hard <ref>`). Do not force-push or make further changes.

## Red Flags — STOP

- Diff verification shows unexpected changes — changes may have been lost
- Multiple Alembic heads persist after re-parenting — migration chain is broken
- Tests fail after rebase — investigate before proceeding
- User hasn't confirmed ambiguous conflict resolution — never guess on low-confidence conflicts

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Not fetching main first | Local main may be stale — always `git fetch origin main:main` |
| No recovery snapshot | Always save `PRE_REBASE_REF` before starting |
| Skipping diff verification | Silent conflict resolution errors are the #1 source of post-rebase bugs |
| Running pytest directly | Always invoke the pytest-docker skill first |
| Auto-fixing test failures | Lint/format can be auto-fixed; test failures cannot — they need investigation |
| Force-pushing without asking | After rebase, remind user the branch needs force-push and confirm before doing it |
| Not checking Docker is running | Migration verification and tests need Docker — check during pre-flight |
| Hardcoding migration paths or alembic commands | Use `$AI_SKILLS_MIGRATIONS_PATH` and `$AI_SKILLS_ALEMBIC_CMD`; skip the step when they are empty |
