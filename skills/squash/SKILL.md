---
name: squash
description: Use when the current branch has messy or fixup commits that need to be reorganized into clean logical commits before merging or creating a PR. Pass `yolo` to take the recommended grouping without waiting for confirmation.
---

# Squash Branch Commits

## Overview

Interactive-rebase the current branch's commits on its merge-base, grouping them into clean, logical commits. Verify no changes are lost by comparing the squashed tree with the pre-squash tip.

**Core principle:** Ask first, squash second, verify always.

## Arguments

One optional argument: `yolo`. It removes exactly one prompt — the Step 2 grouping confirmation, which becomes the recommendation. Step 0's base-branch question and Step 4's diff-verification are unchanged.

## Pre-flight Checks

1. **Clean working tree** — `git status` must show no uncommitted changes. If dirty: stop, tell user to commit or stash.
2. **Not on the default branch.** Step 0's block refuses it once the name is known.
3. **No rebase in progress.** Neither `"$(git rev-parse --git-path rebase-merge)"` nor `"$(git rev-parse --git-path rebase-apply)"` exists. Never test `.git/rebase-merge` literally: in a linked worktree `.git` is a file, so that check never fires.

## Step 0: Determine the Base Branch

The branch may be based off the default branch or off another feature branch. Using the wrong base will destroy commits that don't belong to this branch.

**Detection:** score every local and `origin/` ref by how many commits HEAD has made since diverging from it. The fewest wins. This is the same loop as `finalize-branch` Step 4a.

```bash
DEFAULT=${AI_SKILLS_TARGET_BRANCH:-$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')}
DEFAULT=${DEFAULT:-main}
BRANCH=$(git branch --show-current)
[ "$BRANCH" = "$DEFAULT" ] && echo "STOP: on $DEFAULT" && exit 1
# Fetch the default branch only. A full `git fetch origin` also refreshes this branch's
# own tracking ref, which finalize-branch holds for its force-with-lease.
git fetch origin "$DEFAULT" --quiet

BEST=""; BEST_N=""
for REF in $(git for-each-ref --format='%(refname:short)' refs/heads refs/remotes/origin \
             | grep -v -E "^origin/HEAD$|^origin$|^${BRANCH}$|^origin/${BRANCH}$"); do
  MB=$(git merge-base "$REF" HEAD 2>/dev/null) || continue
  # A force-pushed parent no longer contains the tip HEAD was built on; score from that tip.
  case "$REF" in origin/*)
    FP=$(git merge-base --fork-point "$REF" HEAD 2>/dev/null) && ! git merge-base --is-ancestor "$FP" "$REF" && MB=$FP ;;
  esac
  # Skip refs that already contain HEAD: their merge-base IS HEAD, scoring a false 0.
  [ "$MB" = "$(git rev-parse HEAD)" ] && continue
  N=$(git rev-list --count "$MB..HEAD")
  # On a tie, prefer the default branch over an arbitrary sibling.
  if [ -z "$BEST_N" ] || [ "$N" -lt "$BEST_N" ] \
     || { [ "$N" -eq "$BEST_N" ] && { [ "$REF" = "$DEFAULT" ] || [ "$REF" = "origin/$DEFAULT" ]; }; }; then
    BEST_N=$N; BEST=$REF
  fi
done
echo "default: $DEFAULT"
echo "nearest by divergence: $BEST ($BEST_N commits since its merge-base)"
```

The `--fork-point` override covers a parent that was force-pushed after this branch forked, such as an epic re-cut onto main. Its old commits stay in HEAD under SHAs the new parent no longer has, so the plain merge-base falls back to main and the parent's work gets counted as this branch's own. When every parent commit was rewritten, that is a tie, and the tie silently goes to the default. The remote ref's reflog still holds the old tip. Only `origin/` refs get the override: a local branch's reflog starts with the commit it was created from, which would score every sibling cut from the same tip as a parent.

Never score by containment instead (every branch whose tip is an ancestor of HEAD). Every branch already merged into the default passes that test, so a long-lived repository lists hundreds of candidates, the "closest" one only measures how recently it was merged, and the default itself drops out as soon as its tip moves past HEAD's fork point. A merged branch's tip sits at or behind the default's merge-base, so divergence scoring ranks it no better than the default, and the tie goes to the default.

- Nearest is the default, local or `origin/`, or there is no candidate at all: use the default. Say nothing.
- Nearest differs: **ask the user**, quoting both candidates and their counts. `yolo` does not skip this question.

> What branch is this based off? (default: `<DEFAULT>`, nearest: `<BEST>`)

## Step 1: Record the Recovery Point

```bash
BASE_BRANCH=<confirmed base>
FP=$(git merge-base --fork-point "$BASE_BRANCH" HEAD 2>/dev/null) && ! git merge-base --is-ancestor "$FP" "$BASE_BRANCH" \
  && echo "STOP: $BASE_BRANCH was force-pushed after this branch forked from it"
echo "MERGE_BASE=$(git merge-base HEAD "$BASE_BRANCH")"
echo "PRE_SQUASH_REF=$(git rev-parse HEAD)"
```

**On `STOP`, squash nothing.** The branch still carries the base's old commits, and the merge-base sits below them, so a squash folds the base's work into this branch's commits. Tell the user to restack first with the `rebase-on-epic` skill, then run squash again. Called from `finalize-branch`, stop the whole run.

**`PRE_SQUASH_REF` is the single most important safety mechanism.** It enables instant recovery (`git reset --hard <PRE_SQUASH_REF>`), conflict resolution via `git show <PRE_SQUASH_REF>:<file>`, and it is the tree Step 4 compares against.

**Write both hashes literally into every later command.** Each Bash call is a fresh shell, so the variables do not survive, and an empty one fails silently: with `$PRE_SQUASH_REF` unset, `git diff --quiet $PRE_SQUASH_REF HEAD` becomes `git diff --quiet HEAD`, which passes on any clean tree. Hardcoding also sidesteps the `VAR=x cmd $VAR` expansion trap in the rebase command.

Every later step uses the merge-base, never the base branch's tip. The tip may move, but the merge-base is a fixed commit.

4. **Has commits to squash.** `git log <MERGE_BASE>..HEAD --oneline` must show 2+ commits. If only 1: nothing to squash.

## Step 2: Analyze Commits

Run:
```bash
git log <MERGE_BASE>..HEAD --oneline
git log <MERGE_BASE>..HEAD --stat
```

**The recommended grouping is the one a reviewer reads fastest.** History is rewritten here for the reader of the MR, not the author — a commit is a unit of review, not a unit of work.

### Build the recommendation

1. **Sort every change as mechanical or surgical.** Mechanical: a tool or a single rule produced it identically across files — formatter runs, renames, import-path sweeps, codemods, generated files, lockfiles. Surgical: a person decided each line — logic, behaviour-asserting tests, migrations, config that encodes a judgement. The stat is the tell: mechanical touches many files with a near-equal insert/delete count; surgical is uneven and small.
2. **When the branch holds both, mechanical changes get their own commit, shared with nothing surgical.** Name the tool and command in the message (`style: reformat templates with djlint --reformat`) so the reviewer re-runs it instead of reading 500 files. A surgical fix the mechanical change forced (rendering broke under the new layout) is still surgical — its own commit, placed after the mechanical one it repairs.
3. **Split the surgical set the way the reviewer reads it.** Pick one axis and hold it: by layer (domain → persistence → presentation → tests), by dependency (the commit each later one needs comes first), or by concern (one behaviour per commit). Each commit makes sense read alone, and its message says what to check.
4. **Size: 2–5 commits is the working range.** One commit when the branch is a single surgical change readable in one pass. Above 7, the split has followed files instead of reviewable units — merge until it doesn't. Review-round fixups fold into the commit they correct; the reviewer never sees the version that was wrong.

### Present it

Show the commit list, then the recommendation with the reviewer's reading path in one line per commit, then 1–2 alternatives — coarser or finer, and single-commit whenever it is not the recommendation. Options numbered, recommendation first.

The shape to match — a djlint rollout, one mechanical commit fenced off from the surgical ones around it:

```
build(lint): make the djlint hook and CI step check files          4 files
style(templates): reformat all templates with djlint             536 files  +39085/-38835   mechanical — re-run, don't read
fix(templates): preserve rendering under djlint's layout          38 files                  surgical — repairs the reformat
test(templates): assert on rendered markup, not line breaking     23 files                  surgical — tests, own commit
fix(lint): stop djlint evaluating Jinja expressions               66 files                  surgical — config judgement
```

**Wait for user confirmation before proceeding.** In yolo: still print the list and the recommendation so the user can read what happened, then take option 1 in the same turn.

## Step 3: Execute the Squash

### Simple case: squash everything into one commit

Use `git reset --soft` — simpler and less error-prone than interactive rebase for this case:

```bash
git reset --soft <MERGE_BASE>
git commit -m "agreed commit message"
```

### Multiple groups: interactive rebase

Use a single `git rebase -i <MERGE_BASE>` pass. Write the finished todo list and one message file per resulting commit, then hand the list to git through `GIT_SEQUENCE_EDITOR`. It handles reordering, fixup AND the messages all at once.

For each group the user confirmed:
1. `pick` the first commit of the group
2. `fixup` all subsequent commits in that group
3. `exec git commit --amend --quiet -F <msgfile>` to set the group's message

Reorder the todo lines so that each group's commits are contiguous. Order the groups so a commit's files already exist when it replays: a commit that edits a file goes after the commit that creates it, so a cross-cutting cleanup commit belongs in the last group.

```bash
GITDIR="$(git rev-parse --absolute-git-dir)"

cat > "$GITDIR/squash-msg-1.txt" <<'EOF'
feat(scope): first group's message
EOF
cat > "$GITDIR/squash-msg-2.txt" <<'EOF'
test(scope): second group's message
EOF

cat > "$GITDIR/squash-todo.txt" <<'EOF'
pick a1b2c3d first commit of group 1
fixup e4f5a6b later commit of group 1
exec git commit --amend --quiet -F "$(git rev-parse --absolute-git-dir)/squash-msg-1.txt"
pick 0c9d8e7 first commit of group 2
fixup 1f2e3d4 later commit of group 2
exec git commit --amend --quiet -F "$(git rev-parse --absolute-git-dir)/squash-msg-2.txt"
EOF

GIT_SEQUENCE_EDITOR="cp '$GITDIR/squash-todo.txt'" git rebase -i <MERGE_BASE>
```

Keep the heredocs quoted (`<<'EOF'`), or a backtick or `$` in a commit subject runs as a command. Every file lives in the worktree's own git directory, never shared `/tmp`, where a concurrent run in another worktree or session overwrites it. The `exec` lines set each message once its group is complete, so a conflict that re-runs a step cannot desync them the way a counter-based `GIT_EDITOR` does, which leaves `# This is a combination of N commits` as the subject.

Handle `pick`, `fixup` and `exec` in this single pass. **Never run a second rebase.**

## Step 4: Verify No Changes Lost

```bash
git status --porcelain
git diff --quiet <PRE_SQUASH_REF> HEAD && echo "identical" || git diff --stat <PRE_SQUASH_REF> HEAD
```

- **Empty status and `identical`** = the squashed tip has exactly the pre-squash tree. All changes preserved.
- **Any other output** = something was lost or changed during rebase. **STOP.** Show the difference to the user. Do NOT proceed.

Re-run pre-flight check 3 as well: a rebase stopped on a failed `exec` leaves a clean, identical tree while it is still in progress.

If verification fails, restore immediately (run `git rebase --abort` first if a rebase is still in progress):
```bash
git reset --hard <PRE_SQUASH_REF>
```

## Step 5: Run Tests — only after a conflicted squash

Run the project's test suite only when the squash hit merge conflicts that you resolved by hand. Use whatever test runner the project defines (check CLAUDE.md, Makefile, or package scripts). If a pytest-docker skill or similar is available, use it.

A conflict-free squash — simple case or multi-group — needs no test run: Step 4's diff check already proved the working tree is byte-identical to the pre-squash tip, and identical trees test identically. Skip Step 5 and say so explicitly in Step 6's report (e.g. "Tests: skipped — no conflicts, clean diff-check"). Never skip silently — the report must show the reasoning was applied, not just omit the line.

**Verifying results:** With parallel test runners or `-q` mode, the summary line (`X passed`) may not appear. The reliable signal is **absence of `FAILED` or `ERROR`** in the output — grep for those rather than looking for a pass count.

## Step 6: Report

Summarize:
- How many commits were squashed into how many
- The resulting commit messages
- Diff verification: passed/failed
- Test results: passed/failed
- Grouping drift after a conflicted squash (see Conflict Resolution), or none

## Conflict Resolution

Rebase conflicts are common when reordering commits, because intermediate states are replayed that may never have existed together. Since `PRE_SQUASH_REF` holds the known-good final state:

**Recommended approach for any conflicted file:**
```bash
git show <PRE_SQUASH_REF>:<conflicted-file> > <conflicted-file>   # or git rm <conflicted-file>, when PRE_SQUASH_REF has no such file
git add <conflicted-file>
```

This is safe because Step 4's tree check is the real safety net: it will catch any divergence. You don't need to manually reason about conflict markers when the final state is already known.

After resolving all conflicts:
```bash
GIT_EDITOR=true git rebase --continue
```

`GIT_EDITOR=true` accepts git's prefilled message, and the group's `exec` line sets the real one. When the resolution leaves nothing staged (`git diff --cached --quiet` succeeds), the step became empty: run `git rebase --skip` instead of `--continue`.

**Check the grouping after a conflicted squash.** A file taken from `PRE_SQUASH_REF` carries its final content into whichever commit is replaying, so an earlier group can absorb a later group's change while Step 4 still passes. Compare `git log --stat <MERGE_BASE>..HEAD` against the agreed grouping (commit count, messages, files per commit) and report any drift in Step 6. A skipped `pick` that leads a group is the worst case: that group's fixups fold into the previous group's commit, and its `exec` overwrites the previous group's message.

## Red Flags — STOP

- Step 4 shows anything but an empty status plus `identical`: changes were lost
- Rebase conflict during squash — resolve carefully using `PRE_SQUASH_REF`, re-verify diff
- Tests fail after squash — investigate before proceeding
- User hasn't confirmed grouping — never squash without approval (in yolo, the argument is the approval, and only for the recommended grouping)

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Forgetting to record `PRE_SQUASH_REF` | Always do Step 1 first: it's your undo and Step 4's reference |
| Squashing without asking user | Always present the grouping proposal; wait for confirmation unless `yolo`, which takes the recommendation only |
| Recommending a single commit because `reset --soft` carries no conflict risk | Safety is Step 4's diff check, not the grouping. Recommend the split a reviewer reads fastest; the mechanics follow from the grouping, never the reverse |
| Losing changes during reorder | Step 4's tree check catches this, so never skip it |
| Skipping tests after a conflicted squash | Hand-resolved conflicts are the one place a squash can change behaviour — Step 5 runs the suite then, and only then; a conflict-free squash skips it on the strength of Step 4's clean diff-check |
| Force-pushing without telling user | Run standalone: remind the user the branch needs a force-push and confirm before pushing. Called from `finalize-branch`: don't push at all, its Step 4e pushes with a pinned lease |
| Assuming base is always the default branch | Branch may be stacked on another feature branch, so always run Step 0 to detect the real base |
| Scoring base candidates by containment | Every branch merged into the default is an ancestor of HEAD. Score by commits since divergence (Step 0) |
| Squashing on a parent that was force-pushed after the fork | Its old commits are still in the branch. Step 1 stops; restack with `rebase-on-epic`, then squash |
| Using the base branch tip instead of the merge-base | The tip can move, so Steps 2 and 3 always take `<MERGE_BASE>` |
| Running two rebases (fixup then reword) | One pass: `pick`, `fixup` and `exec git commit --amend -F` lines in a single todo list |
| Setting messages with a counter-based `GIT_EDITOR` | A conflict re-runs the step and desyncs the counter, leaving `# This is a combination of N commits` as a subject. Use the `exec` lines |
| Writing snapshots, todo lists or message files to `/tmp` | `/tmp` is shared across worktrees and sessions, so a concurrent run overwrites them. Use `git rev-parse --absolute-git-dir` |
| Relying on `$MERGE_BASE` or `$PRE_SQUASH_REF` in a later command | Each Bash call is a fresh shell and an empty variable fails silently. Hardcode the hash |
| Manually reasoning about conflict markers | Use `git show <PRE_SQUASH_REF>:<file>` to get the known-good final state, then check the grouping for drift |
| Searching for "X passed" in test output | Parallel runners may omit summary line — grep for absence of `FAILED`/`ERROR` instead |
