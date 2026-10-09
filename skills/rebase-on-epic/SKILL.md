---
name: rebase-on-epic
description: Use when the current story branch is stacked on an epic branch (its MR targets `epic/*` rather than main) and needs restacking onto the epic's latest tip, including after the epic was force-pushed or re-cut onto a newer main.
---

# Rebase on Epic

## Overview

Replay this branch's own commits onto the latest `origin/<epic>`, then verify nothing was lost.

**Core principle:** replay only your own commits. A force-pushed epic leaves its old commits in your branch under old SHAs, so `git rebase origin/<epic>` replays them too. Git drops a stale copy only when its patch is identical to one on the new epic; an edited one conflicts or lands twice. Rebasing from the fork point avoids that, and costs nothing when the epic only moved forward.

## Config

Reads the same optional `AI_SKILLS_*` variables as `rebase-on-main`:

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_BACKEND_SERVICE` | `backend` | Docker compose service for the checks |
| `AI_SKILLS_LINT_CMD` / `AI_SKILLS_FORMAT_CMD` | _(empty)_ | Lint and format commands. Empty skips the step |
| `AI_SKILLS_MIGRATIONS_PATH` / `AI_SKILLS_ALEMBIC_CMD` | _(empty)_ | Alembic migrations dir and command. Empty skips migration verification |

Each Bash call is a fresh shell. **Write every hash literally into later commands**: an unset variable fails silently.

## Pre-flight

1. Clean working tree. If dirty, stop and ask the user to commit.
2. No rebase in progress: neither `"$(git rev-parse --git-path rebase-merge)"` nor `"$(git rev-parse --git-path rebase-apply)"` exists. In a worktree `.git` is a file, so never test `.git/rebase-merge` literally.
3. Settle the epic, in this order: the branch the user named; else the MR target, `glab mr view -F json | jq -r .target_branch`; else ask. An MR that targets the default branch means this skill is the wrong one: use `rebase-on-main`.
4. Not on the epic itself.

## Step 1: Fetch and Snapshot

```bash
EPIC=<settled epic, no origin/ prefix>
git fetch origin "$EPIC" --quiet
echo "PRE_REBASE_REF=$(git rev-parse HEAD)"
echo "FORK=$(git merge-base --fork-point "origin/$EPIC" HEAD || git merge-base "origin/$EPIC" HEAD)"
```

Fetch only the epic. `--fork-point` reads the remote ref's reflog for the epic tip this branch was built on, which survives a force-push. Without a reflog entry it falls back to the plain merge-base.

**Check the replay list before rebasing:** `git log --oneline <FORK>..HEAD` must show only this branch's own commits. If it also shows epic work (another ticket's subjects, or commits you did not write), the reflog had no old tip. Set `FORK` to the commit just below this branch's first own commit, and confirm it with the user when unsure.

## Step 2: Rebase

```bash
git rebase --onto "origin/<EPIC>" <FORK>
```

Never rebase onto the local epic branch: it may be stale. On conflicts:

| Confidence | Examples | Action |
|---|---|---|
| High | imports, formatting, non-overlapping hunks | resolve, `git add`, `GIT_EDITOR=true git rebase --continue` |
| Low | both sides changed the same logic, unclear intent | stop, show both hunks, say what each side wanted, let the user decide |

## Step 3: Verify the Replay

```bash
git branch --show-current
git range-diff <FORK>..<PRE_REBASE_REF> "origin/<EPIC>"..HEAD
```

The branch name must be non-empty (a worktree rebase can leave HEAD detached). Read `range-diff` line by line:

- `=` everywhere: every commit replayed unchanged. Continue.
- `!` on a commit where you resolved a conflict: show the inner diff to the user and continue once confirmed. `!` showing only context lines the epic changed is fine.
- `<` (own commit gone) or `>` (a commit that is not yours): **stop** and recover.

## Step 4: Migrations

Skip when `AI_SKILLS_ALEMBIC_CMD` is empty. Run `eval "$AI_SKILLS_ALEMBIC_CMD heads"`. One head is fine. With several, list this branch's migrations with `git log --oneline "origin/<EPIC>"..HEAD -- "$AI_SKILLS_MIGRATIONS_PATH/versions/"`, point the first one's `down_revision` (and its `Revises:` line) at the head that came with the epic, re-run `heads` until there is one, and commit `fix(migrations): re-parent branch migrations after rebase`. Never create a merge migration.

## Step 5: Dependencies

```bash
git diff --name-only <FORK> "origin/<EPIC>" | grep -E '(^|/)(uv\.lock|pyproject\.toml|package\.json|pnpm-lock\.yaml|Dockerfile)$'
```

Any match means the epic changed dependencies, so the running image is stale. Tell the user, and get the image rebuilt (`docker compose up --build -d`) before Step 6. Tests against a stale image fail on imports that have nothing to do with the rebase.

## Step 6: Checks

1. Lint and format, when configured: `[ -n "${AI_SKILLS_LINT_CMD:-}" ] && eval "$AI_SKILLS_LINT_CMD"`, the same for format. Commit any fixes as `style: fix lint/format issues after rebase`.
2. Server health: `docker compose logs --tail=20 "${AI_SKILLS_BACKEND_SERVICE:-backend}"`, looking for startup errors.
3. Tests: invoke the `pytest-docker` skill (or the project's test skill). Never auto-fix a failing test; report it.

## Recovery

Mid-rebase: `git rebase --abort`. After it: `git reset --keep <PRE_REBASE_REF>`. Both restore the exact pre-rebase branch.

## Report

The epic and fork point used (and whether you corrected the fork point by hand), the pre-rebase tip for recovery, commits replayed, conflicts and how they were resolved, the `range-diff` verdict, migration and dependency changes, and test results. Then remind the user the branch needs `git push --force-with-lease`, and push only when they confirm.

## Common Mistakes

| Mistake | Fix |
|---|---|
| `git rebase origin/<epic>` after the epic was force-pushed | Rebase `--onto` from the fork point (Step 1) |
| Trusting the fork point without reading the replay list | Reflogs expire and fresh clones have none. Read `<FORK>..HEAD` first |
| Rebasing onto the local epic branch | Use `origin/<epic>` straight after fetching it |
| Diffing against main | Main is irrelevant here. `range-diff` compares your commits before and after |
| Testing on the old image after the epic changed dependencies | Step 5, then rebuild |
| Snapshots or hashes kept in shell variables or `/tmp` | Write hashes literally; `/tmp` is shared across worktrees |
| Force-pushing without asking | Remind, confirm, then `--force-with-lease` |
