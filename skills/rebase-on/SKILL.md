---
name: rebase-on
description: Use when the current branch needs rebasing onto its base, either main or the epic branch a story branch is stacked on, including after the epic was force-pushed or re-cut onto a newer main. Takes an optional target or hint (`main`, `epic`, `the epic`, `epic/order`, a full branch name); with none, it rebases onto the MR target. Resolves conflicts, verifies nothing was lost, and runs the post-rebase checks.
---

# Rebase On

## Overview

Replay this branch's own commits onto the latest `origin/<target>`, where the target is main or an epic, then verify nothing was lost.

**Core principle:** replay only your own commits. A force-pushed epic leaves its old commits in your branch under old SHAs, so `git rebase origin/<epic>` replays them too. Git drops a stale copy only when its patch is identical to one on the new epic; an edited one conflicts or lands twice. Rebasing from the fork point avoids that, and costs nothing when the target only moved forward, as main does.

## Config

Optional `AI_SKILLS_*` variables, set up as the README describes:

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_BACKEND_SERVICE` | `backend` | Docker compose service for the checks |
| `AI_SKILLS_LINT_CMD` / `AI_SKILLS_FORMAT_CMD` | _(empty)_ | Lint and format commands. Empty skips the step |
| `AI_SKILLS_TYPECHECK_CMD` | _(empty)_ | Type checker command. Empty skips the step |
| `AI_SKILLS_MIGRATIONS_PATH` / `AI_SKILLS_ALEMBIC_CMD` | _(empty)_ | Alembic migrations dir and command. Empty skips migration verification |

Each Bash call is a fresh shell. **Write every hash literally into later commands**: an unset variable fails silently.

## Pre-flight

1. No rebase in progress: neither `"$(git rev-parse --git-path rebase-merge)"` nor `"$(git rev-parse --git-path rebase-apply)"` exists. In a worktree `.git` is a file, so never test `.git/rebase-merge` literally. Check this first: mid-rebase the tree is dirty and HEAD is detached, so checks 2 and 3 would stop you before you got here. If one exists, ask the user whether to abort or continue it:
   - Abort: `git rebase --abort`, then start this skill over.
   - Continue: recover the values Step 2 would have printed from the rebase dir. `PRE_REBASE_REF` is its `orig-head`. `FORK` is the parent of the first commit in its `done` file (`git rev-parse <sha>^`). The target is `git branch -r --points-at "$(cat <dir>/onto)"`; ask when that names nothing. Then read the replay list, `git log --oneline <FORK>..<PRE_REBASE_REF>`: if it shows commits that are not this branch's own, the rebase is replaying other work (a plain `git rebase origin/<epic>` after a force-push does that), so recommend aborting. Otherwise skip checks 2 and 3, run check 4, and resume at Step 3's conflict handling.
2. Clean working tree. If dirty, stop and ask the user to commit or stash.
3. On a branch: `git branch --show-current` is non-empty.
4. Docker is up, unless `AI_SKILLS_ALEMBIC_CMD`, `AI_SKILLS_LINT_CMD`, `AI_SKILLS_FORMAT_CMD` and `AI_SKILLS_TYPECHECK_CMD` are all empty: `docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" echo ok`. If that fails, tell the user migrations, typecheck and tests need Docker, and ask whether to go on without them or wait.

## Step 1: Settle the Target

The argument is what the user said, not a branch name: never pass it to git as is. Settle it into one remote branch, in this order:

1. **No argument:** the MR target, `glab mr view -F json | jq -r .target_branch`. No MR (a story branch has none until `finalize-branch` opens it), or no `glab`: never assume the default branch, rank the candidates below and take the lowest count. A tie with the default branch goes to the default branch, since every epic cut after this branch's base ties with it. A tie between epics only: ask.
2. **An existing branch** (`git rev-parse --verify --quiet "refs/remotes/origin/<arg>"` succeeds), or `main`: use it.
3. **An epic hint** (`epic`, `the epic`, `epic/order`): rank the candidates below.
   - **Take the top epic** when its count is lower than every other epic's and lower than the default branch's, and the name in the hint (`order` in `epic/order`) is part of its name.
   - **Otherwise ask:** a tie at the top, nothing beats the default branch, the hint names a different epic, or no `epic/*` branch exists. Show the top three with their counts and recommend the first.
4. **Any other word** (`auth`): the one remote branch whose name contains it, `git branch -r --list "origin/*<word>*"`. Several or none: ask.

**Ranking** scores the local and remote `epic/*` branches and the default branch by how far this branch sits above each:

```bash
for b in $(git for-each-ref --format='%(refname:short)' refs/heads/epic/ refs/remotes/origin/epic/) "$(git symbolic-ref --short refs/remotes/origin/HEAD)"; do
  base=$(git merge-base --fork-point "$b" HEAD || git merge-base "$b" HEAD)
  echo "$(git rev-list --count "$base"..HEAD) $b"
done | sort -n
```

Each count is the commits this branch has above its fork point from the candidate; the base it was built on has the fewest. The fork point comes from the candidate's reflog, so it still finds the tip this branch was built on after the epic was force-pushed and fetched. A local `epic/x` and `origin/epic/x` are one candidate, scored by the lower count. If `git symbolic-ref` fails, `origin/HEAD` is unset: run `git remote set-head origin --auto` and rank again.

Not the current branch itself. Before going on, print the target and why, e.g. `Target: origin/epic/order-memo (5 own commits; next: main at 18)` or `Target: origin/main (MR target)`.

## Step 2: Fetch and Snapshot

```bash
TARGET=<settled target, no origin/ prefix>
git fetch origin "$TARGET" --quiet
echo "PRE_REBASE_REF=$(git rev-parse HEAD)"
echo "FORK=$(git merge-base --fork-point "origin/$TARGET" HEAD || git merge-base "origin/$TARGET" HEAD)"
```

Fetch only the target, and never into a local branch: `main:main` fails whenever main is checked out in another worktree. `--fork-point` reads the remote ref's reflog for the tip this branch was built on, which survives a force-push. Without a reflog entry it falls back to the plain merge-base.

**Check the replay list before rebasing:** `git log --oneline <FORK>..HEAD` must show only this branch's own commits. If it also shows other work (another ticket's subjects, an epic's commits since squash-merged into main, commits you did not write), the reflog had no old tip. Set `FORK` to the commit just below this branch's first own commit, and confirm it with the user when unsure. One exception: when that other work is an epic the target does not have yet, the target is wrong, not the fork point. Moving `FORK` would cut the branch off its epic, so stop and ask whether to rebase onto that epic instead.

## Step 3: Rebase

```bash
git rebase --onto "origin/<TARGET>" <FORK>
```

Never rebase onto a local branch: it may be stale. On conflicts, read each conflicting file:

| Confidence | Examples | Action |
|---|---|---|
| High | imports, formatting, non-overlapping hunks | resolve, `git add`, `GIT_EDITOR=true git rebase --continue` |
| Low | both sides changed the same logic, unclear intent | stop, show both hunks, say what each side wanted, suggest an approach, let the user decide |

## Step 4: Verify the Replay

```bash
git branch --show-current
git range-diff <FORK>..<PRE_REBASE_REF> "origin/<TARGET>"..HEAD
```

The branch name must be non-empty (a worktree rebase can leave HEAD detached). Read `range-diff` line by line:

- `=` everywhere: every commit replayed unchanged. Continue.
- `!` on a commit where you resolved a conflict: show the inner diff to the user and continue once confirmed. `!` showing only context lines the target changed is fine.
- A `<` and `>` pair with the same subject, on a commit where you resolved a conflict: the resolution rewrote most of a small commit. Re-run with `--creation-factor=100` to pair them, then treat it as `!`.
- `<` on a commit the rebase reported as `dropping <sha> <subject> -- patch contents already upstream`: the target already has that change, e.g. a cherry-picked fix. Show it to the user and continue once confirmed.
- Any other `<` (own commit gone) or `>` (a commit that is not yours): **stop** and recover.

## Step 5: Migrations

Skip when `AI_SKILLS_ALEMBIC_CMD` is empty or Docker is down; say so in the report. Run `eval "$AI_SKILLS_ALEMBIC_CMD heads"`. One head is fine. With several, list this branch's migrations with `git log --oneline "origin/<TARGET>"..HEAD -- "$AI_SKILLS_MIGRATIONS_PATH/versions/"`, point the first one's `down_revision` (and its `Revises:` line) at the head that came with the target, and re-run `heads`. Still several: stop and report. One: commit `fix(migrations): re-parent branch migrations after rebase`. Never create a merge migration.

## Step 6: Dependencies

```bash
git diff --name-only <FORK> "origin/<TARGET>" | grep -E '(^|/)(uv\.lock|pyproject\.toml|package\.json|pnpm-lock\.yaml|Dockerfile)$'
```

Any match means the target changed dependencies, so the running image is stale. Tell the user, and get the image rebuilt (`docker compose up --build -d`) before Step 7. Lint, typecheck and tests against a stale image fail on imports that have nothing to do with the rebase. Skip this step if the project doesn't run in Docker.

## Step 7: Checks

Run in order. Any failure: stop and report.

1. **Lint and format**, when configured: `if [ -n "${AI_SKILLS_LINT_CMD:-}" ]; then eval "$AI_SKILLS_LINT_CMD"; fi`, the same for format. Commit any fixes as `style: fix lint/format issues after rebase`, then re-run to confirm they pass clean.
2. **Server health:** `docker compose logs --tail=20 "${AI_SKILLS_BACKEND_SERVICE:-backend}"`, looking for startup errors or a crash loop. Skip if the project doesn't run a server in Docker.
3. **Typecheck**, when configured: `if [ -n "${AI_SKILLS_TYPECHECK_CMD:-}" ]; then eval "$AI_SKILLS_TYPECHECK_CMD"; fi`. Git merges text, not meaning: a function the target renamed reaches your code without a conflict, and the type checker finds that in seconds.
4. **Tests:** run only the tests whose outcome the rebase can have changed. Your diff was green on the old base and the target is green in CI, so the risk sits where the two meet. List what the target brought in, then the files both sides changed:
   ```bash
   git diff --name-only <FORK> "origin/<TARGET>"
   comm -12 <(git diff --name-only <FORK> "origin/<TARGET>" | sort) <(git diff --name-only <FORK> <PRE_REBASE_REF> | sort)
   ```

   | What the target brought in | Tests |
   |---|---|
   | Nothing, or only files no test exercises (docs, CI config, infrastructure) | None. Say so in the report |
   | Code, but no file changed on both sides | The test files this branch added or changed |
   | Files changed on both sides, whether they conflicted or merged cleanly | The test files this branch added or changed, plus the tests for each file changed on both sides |

   A file both sides changed can merge cleanly and still break, so a rebase without conflicts does not shrink the list. Run it as a Tier 1 run of the `pytest-docker` skill (or the project's test skill), which says how to find the tests for a changed file. Never run pytest directly, and never the full suite here: CI and `finalize-branch` own it. Never auto-fix a failing test; report it.

## Recovery

Mid-rebase: `git rebase --abort`. After it: `git reset --keep <PRE_REBASE_REF>`. Both restore the exact pre-rebase branch, and `--keep` refuses to discard uncommitted changes.

## Report

The target and how it was settled (named, MR target, or the ranking with its counts), the fork point (and whether you corrected it by hand), the pre-rebase tip for recovery, commits replayed, conflicts and how they were resolved, the `range-diff` verdict, migration and dependency changes (and whether the image was rebuilt), lint/format fixes, the typecheck result, and the test scope you picked (which row, and the files) with its results.

All passed: remind the user the branch needs `git push --force-with-lease`, and push only when they confirm. Anything failed: the failure output and the recovery command, and no push or further changes.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Passing the hint to git (`git fetch origin epic`) | Step 1 settles it into one remote branch |
| No argument and no MR, so rebasing onto main | Rank the candidates (Step 1): a story branch has no MR until `finalize-branch` |
| Moving `FORK` above epic commits the target doesn't have | The branch belongs on that epic: ask (Step 2) |
| `git rebase origin/<target>` after the target was force-pushed | Rebase `--onto` from the fork point (Step 2) |
| Trusting the fork point without reading the replay list | Reflogs expire and fresh clones have none. Read `<FORK>..HEAD` first |
| Rebasing onto a local branch | Use `origin/<target>` straight after fetching it |
| Testing on the old image after the target changed dependencies | Step 6, then rebuild |
| Skipping tests because the rebase had no conflicts | A file both sides changed can merge cleanly and still break, so it keeps its tests |
| Running the full suite after the rebase | Step 7 picks the scope; CI and `finalize-branch` own the full suite |
| Auto-fixing a failing test | Lint and format can be auto-fixed; a failing test needs investigating |
| Snapshots or hashes kept in shell variables or `/tmp` | Write hashes literally; `/tmp` is shared across worktrees |
| Force-pushing without asking | Remind, confirm, then `--force-with-lease` |
