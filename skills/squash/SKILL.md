---
name: squash
description: Use when the current branch has messy or fixup commits that need to be reorganized into clean logical commits before merging or creating a PR
---

# Squash Branch Commits

## Overview

Interactive-rebase the current branch's commits on its merge-base, grouping them into clean, logical commits. Verify no changes are lost by diffing before and after.

**Core principle:** Ask first, squash second, verify always.

## Pre-flight Checks

1. **Clean working tree** — `git status` must show no uncommitted changes. If dirty: stop, tell user to commit or stash.
2. **Not on main** — refuse to run if current branch is `main`.
3. **No rebase in progress** — check for `.git/rebase-merge` or `.git/rebase-apply`.

## Step 0: Determine the Base Branch

The branch may be based off `main` or off another feature branch. Using the wrong base will destroy commits that don't belong to this branch.

**Detection algorithm:**

```bash
# 1. List candidate bases: main + any local branches whose tip is an ancestor of HEAD
CANDIDATES=$(git branch --format='%(refname:short)' | while read b; do
  [ "$b" != "$(git branch --show-current)" ] && git merge-base --is-ancestor "$b" HEAD 2>/dev/null && echo "$b"
done)

# 2. For each candidate, compute merge-base distance (fewer commits = closer base)
for b in $CANDIDATES; do
  echo "$(git rev-list --count $(git merge-base HEAD $b)..HEAD) $b"
done | sort -n
# The branch with the FEWEST commits between merge-base and HEAD is the likely base
```

If only `main` is an ancestor, use `main`. If another branch is a closer ancestor (fewer commits), it's likely the real base. **When ambiguous, ask the user:**

> What branch is this based off? (default: `main`)

Store the result and save a recovery point:
```bash
BASE_BRANCH=<confirmed base>
MERGE_BASE=$(git merge-base HEAD $BASE_BRANCH)
PRE_SQUASH_REF=$(git rev-parse HEAD)
```

**`PRE_SQUASH_REF` is the single most important safety mechanism** — it enables instant recovery (`git reset --hard $PRE_SQUASH_REF`) and conflict resolution via `git show $PRE_SQUASH_REF:<file>`.

**All subsequent steps use `$MERGE_BASE` — never hardcode `main`.**

**Shell variable gotcha:** When combining env variable assignments with commands (e.g., `VAR=x git rebase -i $VAR`), the variable may not expand correctly. Always **hardcode the actual commit hash** in the rebase command rather than relying on `$MERGE_BASE` expansion across env assignments.

4. **Has commits to squash** — `git log $MERGE_BASE..HEAD --oneline` must show 2+ commits. If only 1: nothing to squash.

## Step 1: Snapshot the Diff

Save the full diff against the merge-base BEFORE any rebase operation:

```bash
git diff $MERGE_BASE > /tmp/squash-before.diff
```

This is the safety net. It captures the exact state of all changes on this branch relative to its fork point.

**Use `$MERGE_BASE` (not `$BASE_BRANCH`)** — the base branch tip may move, but the merge-base is a fixed commit.

## Step 2: Analyze Commits

Run:
```bash
git log $MERGE_BASE..HEAD --oneline
git log $MERGE_BASE..HEAD --stat
```

Categorize the branch type and present a grouping proposal to the user.

### Feature branches

Goal: logical, atomic commits without fixup noise.

Look for patterns:
- **Core implementation** commits (the actual feature)
- **Fixup/correction** commits ("fix typo", "oops", "address review")
- **Test** commits
- **Migration/schema** commits

Propose grouping like:
1. `feat: add X` (core implementation, folding in fixups)
2. `feat: add tests for X` (if tests are substantial enough to warrant a separate commit)
3. `feat: add migration for X` (if migrations exist)

### Refactor branches

Goal: group by type of change, so each commit is a coherent, reviewable unit.

Look for patterns:
- **Rename/move** commits (same change across many files)
- **Signature/interface** changes
- **Implementation** changes
- **Test updates** that mirror the above

Propose grouping like:
1. `refactor: rename FooService to BarService` (all rename changes)
2. `refactor: extract X into separate module` (structural changes)
3. `refactor: update tests for new structure`

### When unsure

Present the commit list and ask:

> I see N commits on this branch. How would you like them grouped?
>
> Here are the commits:
> [list]
>
> Options:
> 1. **Single commit** — squash everything into one
> 2. **By type** — group renames, implementation, tests, migrations separately
> 3. **Logical units** — I'll propose a grouping based on what I see
> 4. **Custom** — tell me how to group them

**Always wait for user confirmation before proceeding.**

## Step 3: Execute the Squash

### Simple case: squash everything into one commit

Use `git reset --soft` — simpler and less error-prone than interactive rebase for this case:

```bash
git reset --soft $MERGE_BASE
git commit -m "agreed commit message"
```

### Multiple groups: interactive rebase

Use a single `git rebase -i $MERGE_BASE` pass. Construct a todo script that handles reordering, fixup, AND reword all at once.

For each group the user confirmed:
1. `pick` the first commit of the group (or `reword` if the message needs changing)
2. `fixup` all subsequent commits in that group

Reorder the todo lines so that each group's commits are contiguous.

**Execution approach — write a temp script as GIT_SEQUENCE_EDITOR:**

```bash
cat > /tmp/squash-todo.sh << 'SCRIPT'
#!/bin/bash
# Rewrite the todo file: reorder + mark fixups
# $1 is the todo file path provided by git rebase
# ... build sed/awk commands based on confirmed grouping ...
SCRIPT
chmod +x /tmp/squash-todo.sh

GIT_SEQUENCE_EDITOR=/tmp/squash-todo.sh git rebase -i $MERGE_BASE
```

Build the script content based on the confirmed grouping. Each group becomes one resulting commit. Handle `pick`/`reword`/`fixup` in this single pass — **never run a second rebase**.

After the rebase, if any commit messages still need updating, use `git commit --amend -m "new message"` for the tip commit only.

## Step 4: Verify No Changes Lost

```bash
git diff $MERGE_BASE > /tmp/squash-after.diff
diff /tmp/squash-before.diff /tmp/squash-after.diff
```

- **No output** = diffs are identical. All changes preserved.
- **Any output** = something was lost or changed during rebase. **STOP.** Show the difference to the user. Do NOT proceed.

If verification fails, restore immediately:
```bash
git reset --hard $PRE_SQUASH_REF
```

## Step 5: Run Tests

Run the project's test suite to confirm nothing broke. Use whatever test runner the project defines (check CLAUDE.md, Makefile, or package scripts). If a pytest-docker skill or similar is available, use it.

**Verifying results:** With parallel test runners or `-q` mode, the summary line (`X passed`) may not appear. The reliable signal is **absence of `FAILED` or `ERROR`** in the output — grep for those rather than looking for a pass count.

**Exception — skip rerun only when ALL of these hold:**

1. **Simple case was used** (`git reset --soft` + single commit — Step 3's "simple case" path). Multi-group interactive rebase always reruns tests, no exception — reordering/fixup can replay commits into intermediate states that never existed together, so a clean Step 4 diff doesn't rule out an ordering-dependent break introduced mid-rebase.
2. **Step 4's diff check passed clean** (no output). This proves the working tree is byte-identical to the pre-squash tip — `reset --soft` doesn't touch the tree, only history, so this is guaranteed by construction for the simple case, not just likely.
3. **Tests are known green on the pre-squash tip in this session** — either you ran them yourself earlier in this session, or the user explicitly confirms they're currently passing. An assumption ("probably fine") does not count; a stale CI badge does not count. If there's no positive evidence, run the tests.

If all three hold: skip Step 5, and say so explicitly in Step 6's report (e.g. "Tests: skipped — simple squash, clean diff-check, tests confirmed green pre-squash"). Never skip silently — the report must show the reasoning was applied, not just omit the line.

Rationale: a `reset --soft` squash changes history shape only, never tree content. If the tree is provably identical (point 2) and was already proven to pass (point 3), rerunning is testing the same tree twice — the cost is wall-clock time, not risk reduction.

## Step 6: Report

Summarize:
- How many commits were squashed into how many
- The resulting commit messages
- Diff verification: passed/failed
- Test results: passed/failed

## Conflict Resolution

Rebase conflicts are common when reordering commits, because intermediate states are replayed that may never have existed together. Since `PRE_SQUASH_REF` holds the known-good final state:

**Recommended approach for any conflicted file:**
```bash
git show $PRE_SQUASH_REF:<conflicted-file> > <conflicted-file>
git add <conflicted-file>
```

This is safe because the before/after diff check (Step 4) is the real safety net — it will catch any divergence. You don't need to manually reason about conflict markers when the final state is already known.

After resolving all conflicts:
```bash
GIT_EDITOR=/tmp/squash-msg-editor.sh git rebase --continue
```

## Red Flags — STOP

- `diff` output between before/after is non-empty — changes were lost
- Rebase conflict during squash — resolve carefully using `PRE_SQUASH_REF`, re-verify diff
- Tests fail after squash — investigate before proceeding
- User hasn't confirmed grouping — never squash without approval

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Forgetting to snapshot diff before squash | Always do Step 1 first — it's your undo safety net |
| Squashing without asking user | Always present grouping proposal and wait for confirmation |
| Losing changes during reorder | The before/after diff check catches this — never skip it |
| Not running tests after | A squash can silently break things if commits had ordering dependencies — unless the Step 5 exception applies (simple case + clean diff-check + tests already known green) |
| Force-pushing without telling user | After squash, remind user the branch needs force-push and confirm before doing it |
| Assuming base is always `main` | Branch may be stacked on another feature branch — always run Step 0 to detect the real base |
| Diffing against branch tip instead of merge-base | The base branch tip can move — always diff against `$MERGE_BASE` for stable comparison |
| Running two rebases (fixup then reword) | Handle everything in a single rebase pass to halve the risk |
| Using `$MERGE_BASE` in env assignment + command | Shell variable expansion is unreliable across `VAR=x cmd $VAR` — hardcode the hash |
| Manually reasoning about conflict markers | Use `git show $PRE_SQUASH_REF:<file>` to get the known-good final state — the diff check is the real safety net |
| Searching for "X passed" in test output | Parallel runners may omit summary line — grep for absence of `FAILED`/`ERROR` instead |
