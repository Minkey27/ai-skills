---
name: write-mr-description
description: "Writes the GitLab merge request description for the current branch, then creates or updates the MR with `glab`. Use for every MR description — a new MR, a rewrite of an existing one, or any request to 'write up', 'summarise' or 'describe' the changes on a branch for review. Runs forked so it cannot see the implementation session. GitLab-only (requires `glab`); `finalize-branch` delegates its MR step here."
allowed-tools: Bash, Read, Grep, Glob, mcp__claude_ai_ClickUp__clickup_get_task, mcp__claude_ai_ClickUp__clickup_search
context: fork
---

# Write MR description

A reviewer opens the MR to answer one question: *should this be merged?* Anything that does
not help them answer it costs them time. The diff already shows what changed, line by line
— your job is only to supply what the diff cannot.

## Why this runs forked

You cannot see the implementation session, and that is deliberate. A description written
from session memory narrates the journey — the approach tried first, the refactor along the
way, the edge case found at 4pm — and none of that belongs in front of a reviewer. Derive
everything from the diff and the ticket. Wanting context you do not have is a signal the
detail was not needed.

For the same reason **you** run `glab` yourself and never hand the body back for another
session to post. A session that knows the journey will be tempted to improve the text on
the way through, which puts the journey straight back in.

## Config

Same `AI_SKILLS_*` variables as `finalize-branch`:

| Variable | Default | Use |
|---|---|---|
| `AI_SKILLS_TARGET_BRANCH` | `main` | Starting point for target-branch detection |
| `AI_SKILLS_REVIEWERS` | _(empty)_ | Comma-separated reviewers; empty → omit `--reviewer` |
| `AI_SKILLS_TICKET_PREFIX` | _(empty)_ | Ticket prefix; empty → match any uppercase slug |

## The body

Four parts, in this order. No others — no Summary, no Overview, no Notes, no Changelog, no
Test plan, no Follow-ups.

```markdown
Closes BPZ-1004

## Why

[1–3 sentences of prose. Not bullets.]

## What

- [The change a reviewer must check, and the reason they cannot see.]

## Caveats

[Omitted entirely unless there is something to flag.]
```

**`Closes BPZ-###` is the first line** — keyword, space, ticket id, nothing else on the
line. Downstream automation (Zapier → ClickUp) parses it to transition the ticket, so a
reworded or reformatted line means the ticket silently never advances. If no ticket
reference exists, omit the line completely: never invent one, never leave a placeholder.

**`## Why` is prose on purpose.** A bullet list invites one more bullet; a paragraph forces
you to decide what the reason actually is.

**`## What` carries only what the diff cannot show** — a rationale, a blast radius, a
compatibility answer, a dependency bump. Not a tour of the files.

**`## Caveats` exists only for what would otherwise mislead a reviewer:** an untested path
that matters, a CI job red for a pre-existing reason, a check only doable by hand,
deliberate scope creep. Most MRs have no Caveats section at all. It is never a test report
— "the suite passes" is worth zero and belongs nowhere.

## Budgets

Limits, not targets. Coming in under is good.

| Part | Limit |
|---|---|
| Title | 72 chars |
| Why | 3 sentences, prose |
| What | 5 bullets, 25 words each |
| Caveats | omitted unless it flags something |
| **Whole body** | **200 words — hard, no exceptions** |

The 200-word cap has no escape hatch. A subtle correctness or security argument compresses
to about three sentences; find the load-bearing one rather than asking for more room.

## Title

- Conventional-commit prefix for infra, chores and bugfixes: `fix(projecten):`, `perf(ci):`,
  `chore(docker):`.
- Plain imperative sentence for features: `Filter the project overview by assigned colleague`.
- **Never** a ticket reference — `Closes` already carries it, and the title is where it rots.
- Never prefix `Draft:` or `WIP:`; use the `--draft` flag.

## Never

❌ `## Test plan`, or any step-by-step for the reviewer to run, verify or click through.
   They read the diff and CI; they did not ask for a QA script. If something genuinely
   cannot be trusted without a manual check, that is one line in **Caveats**.
❌ A bullet per changed file — the diff already shows the file list, in a better format.
❌ A bullet per commit, or any reproduction of `git log` — the Commits tab exists.
❌ A bullet per test added, or any list of `test_*` names.
❌ "The suite passes", "all green", "lint + format clean" — CI says this already.
❌ The `🤖 Generated with Claude Code` footer.
❌ Narrating the implementation ("initially used a dict, then switched to a dataclass").
❌ Restating the ticket description instead of summarising the outcome.
❌ Explaining what obvious code does.
❌ Sections for future work, possible improvements or things out of scope.
❌ Emoji headings, bold-word soup, nested bullets.
❌ Filler: comprehensive, robust, seamlessly, significantly, carefully, properly, ensures,
   leverages.
❌ Hedged non-statements ("this should improve performance in most cases").
❌ `--fill` (`glab mr create --fill` / `gh pr create --fill`) — it rebuilds the description
   from commit messages, which is exactly the noise this skill removes.

## Example

A real 653-word description and the same change in 164 words:
[references/examples.md](references/examples.md). Read it before drafting.

## Steps

Each fenced `bash` block below is a **separate Bash invocation**. Shell state — variables,
`set` options, anything not on disk — does not persist between them; only the working
directory does. So a value decided in one step and needed in a later one is persisted to a
state file, and every later block re-sources that file before using it:

```bash
STATE="${MR_STATE:-/tmp/mr-state.sh}"
```

`$STATE` is not itself carried state — it's a constant expression, re-derived identically in
every block. Step 1 starts by truncating the file (`: > "$STATE"`), so a file left behind by
an earlier run can never leak into this one. Every later block instead does `. "$STATE"`.

Persisted across steps, via `printf '%q'` (so spaces and shell metacharacters survive the
round trip) appended to `$STATE`: `$BRANCH`, `$NEEDS_PUSH` (step 1), `$TARGET` (step 2),
`$BASE_SHA` (step 3), `$TICKET` (step 4), `$BODY`, `$TITLE` (step 5), `$COUNT`, `$IID`
(step 8). `$TARGET_DEFAULT`, `$BEST`, `$BEST_N`, `$WORDS`, `$OPEN`, `$REVIEWER_FLAG` are used
and discarded within the single block that computes them — they never need to survive to a
later invocation.

### 1. State check

```bash
STATE="${MR_STATE:-/tmp/mr-state.sh}"
: > "$STATE"

BRANCH=$(git branch --show-current)
git status --porcelain | grep -q . && echo "STOP: uncommitted changes" && exit 1

if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
  UNPUSHED=$(git rev-list --count "@{u}..HEAD")
  [ "$UNPUSHED" -gt 0 ] && NEEDS_PUSH=1 || NEEDS_PUSH=0
else
  NEEDS_PUSH=1
fi

printf 'BRANCH=%q\n' "$BRANCH" >> "$STATE"
printf 'NEEDS_PUSH=%q\n' "$NEEDS_PUSH" >> "$STATE"
```

Refuse on a dirty tree — the diff you describe must be the diff that gets pushed. `$NEEDS_PUSH`
tests for **unpushed commits**, not merely for a configured upstream — an upstream that
already exists but is behind (e.g. after `finalize-branch`'s squash step rewrites history)
still needs a push. If the branch has no upstream, push it in step 8; do not push before the
target branch is settled. A post-squash push may be rejected as non-fast-forward — step 8
retries with `--force-with-lease` when that happens.

### 2. Target branch

Never assume `main`. Branches are frequently stacked on an epic branch, and retargeting a
stacked branch at `main` proposes merging unreviewed upstream work.

```bash
STATE="${MR_STATE:-/tmp/mr-state.sh}"
. "$STATE"

TARGET_DEFAULT="${AI_SKILLS_TARGET_BRANCH:-main}"
git fetch origin --quiet

BEST=""; BEST_N=""
for REF in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin \
             | grep -v -E "^origin/HEAD$|^origin$|^origin/${BRANCH}$"); do
  MB=$(git merge-base "$REF" HEAD 2>/dev/null) || continue
  # Skip refs that already contain HEAD — their merge-base IS HEAD, scoring a false 0.
  [ "$MB" = "$(git rev-parse HEAD)" ] && continue
  N=$(git rev-list --count "$MB..HEAD")
  # On a tie, prefer the configured default over an arbitrary sibling branch.
  if [ -z "$BEST_N" ] || [ "$N" -lt "$BEST_N" ] \
     || { [ "$N" -eq "$BEST_N" ] && [ "$REF" = "origin/$TARGET_DEFAULT" ]; }; then
    BEST_N=$N; BEST=$REF
  fi
done
echo "configured default: origin/$TARGET_DEFAULT"
echo "nearest by divergence: $BEST ($BEST_N commits since its merge-base)"

# $TARGET is the branch name WITHOUT the origin/ prefix — it is consumed as origin/$TARGET.
if [ "$BEST" = "origin/$TARGET_DEFAULT" ] || [ -z "$BEST" ]; then
  TARGET="$TARGET_DEFAULT"
else
  TARGET="${BEST#origin/}"
fi
echo "target: $TARGET"

printf 'TARGET=%q\n' "$TARGET" >> "$STATE"
```

This iterates every ref under `refs/remotes/origin`, so it costs one `merge-base` call per
remote branch. "Nearest" is the candidate with the **fewest** commits HEAD has accumulated
*since diverging from it* — not the fewest commits contained by it: `--merged HEAD`
containment breaks the moment the default branch's tip stops being an ancestor of HEAD (it
advances past the fork point on almost every branch that isn't freshly forked), silently
handing the win to whatever merged-in sibling branch happens to still be an ancestor.

- Nearest equals the configured default → use it, say nothing.
- Nearest differs → **stop here.** Do not proceed to step 3, do not push, do not create or
  update anything. This skill runs forked with no way to prompt a human directly — end your
  report with the disagreement (both candidate branches and their commit counts) so the
  caller can put the question to the user.

### 3. Read the scope

The diff is what you write from. Read all three, in order:

```bash
STATE="${MR_STATE:-/tmp/mr-state.sh}"
. "$STATE"

BASE_SHA=$(git merge-base "origin/$TARGET" HEAD)
git log --oneline "$BASE_SHA"..HEAD
git diff --stat "$BASE_SHA"..HEAD
git diff "$BASE_SHA"..HEAD

printf 'BASE_SHA=%q\n' "$BASE_SHA" >> "$STATE"
```

Merge-base, never `origin/$TARGET` directly — the remote can be ahead of the fork point,
which pulls unrelated upstream commits into the diff you are describing.

### 4. Ticket

```bash
STATE="${MR_STATE:-/tmp/mr-state.sh}"
. "$STATE"

PATTERN="${AI_SKILLS_TICKET_PREFIX:-[A-Z]+}-[0-9]+"
TICKET=$(printf '%s\n' "$BRANCH" | grep -oE "$PATTERN" | head -1)
[ -z "$TICKET" ] && TICKET=$(git log --format='%s%n%b' "$BASE_SHA"..HEAD | grep -oE "$PATTERN" | head -1)
echo "Ticket: ${TICKET:-<none>}"

printf 'TICKET=%q\n' "${TICKET:-}" >> "$STATE"
```

Then fetch it with the ClickUp tools (`clickup_get_task`, or `clickup_search` on the id)
and read it **for intent only** — what problem was being solved. That is the one thing the
diff cannot tell you, and it is safe to read because it predates the implementation.

Do not copy the ticket's wording into `## Why`; summarise the outcome. If the lookup fails,
derive `Why` from the diff and commit messages, and say so in your final report.

### 5. Draft to `$BODY`

```bash
STATE="${MR_STATE:-/tmp/mr-state.sh}"
. "$STATE"

BODY="${MR_BODY_FILE:-/tmp/mr-body.md}"
cat > "$BODY" <<'EOF'
Closes <ticket from step 4, or drop this line>

## Why

...
EOF

TITLE="<your title>"   # <= 72 chars, per ## Title

printf 'BODY=%q\n' "$BODY" >> "$STATE"
printf 'TITLE=%q\n' "$TITLE" >> "$STATE"
```

Draft `$TITLE` alongside the body, following the `## Title` section's rules, and check it
against the 72-char budget there. If the environment mandates a scratchpad directory instead
of `/tmp`, set `MR_BODY_FILE` (or assign `BODY` directly) to a path inside it — every later
step re-sources `$BODY` from `$STATE` rather than hardcoding the path, so the step that writes
the file and the steps that read it cannot drift apart.

Check for `.gitlab/merge_request_templates/`. If a template exists, note it in your final
report but do not follow it — this format wins.

### 6. The deletion pass

Before running the word gate, delete your weakest `## What` bullet. Then reread. If the
description is still complete, that bullet was noise — and there is probably another like
it. Repeat until removing anything would leave a real gap. If a real gap appears, restore
the bullet you just cut and stop deleting.

Then check every surviving bullet against the diff: **if a reviewer would already know it
from the file list or the code itself, cut it.**

### 7. Word gate

```bash
STATE="${MR_STATE:-/tmp/mr-state.sh}"
. "$STATE"

WORDS=$(wc -w < "$BODY" | tr -d ' ')
echo "body: $WORDS words"
[ "$WORDS" -gt 200 ] && echo "OVER BUDGET — cut, do not create" && exit 1
```

Over budget means cut. It does not mean create it anyway and mention the overrun.

### 8. Create or update

```bash
STATE="${MR_STATE:-/tmp/mr-state.sh}"
. "$STATE"

if [ "${NEEDS_PUSH:-0}" = "1" ]; then
  git push -u origin "$BRANCH" || git push --force-with-lease -u origin "$BRANCH"
fi

OPEN=$(glab api "projects/:id/merge_requests?source_branch=$BRANCH&state=opened" \
  | python3 -c "import json,sys; print(' '.join(str(m['iid']) for m in json.load(sys.stdin)))")
COUNT=$(printf '%s\n' $OPEN | grep -c . || true)
IID=$(printf '%s\n' $OPEN | head -1)
echo "open MRs on $BRANCH: ${COUNT:-0} (${OPEN:-none})"

printf 'COUNT=%q\n' "${COUNT:-0}" >> "$STATE"
printf 'IID=%q\n' "${IID:-}" >> "$STATE"
```

Several MRs can share one source branch, and `glab mr view` errors when it is ambiguous. The
plain `git push` above can be rejected as non-fast-forward when an earlier step (e.g.
`finalize-branch`'s squash) already pushed this branch and then rewrote its history — the
`--force-with-lease` retry handles that case without clobbering anyone else's work.

- Exactly one open MR → update it in place, including the target branch, so a stale MR never
  points at the wrong one:

  ```bash
  STATE="${MR_STATE:-/tmp/mr-state.sh}"
  . "$STATE"

  glab mr update "$IID" --description "$(cat "$BODY")" --title "$TITLE" --target-branch "$TARGET"
  ```

- None → create:

  ```bash
  STATE="${MR_STATE:-/tmp/mr-state.sh}"
  . "$STATE"

  REVIEWER_FLAG=""
  [ -n "${AI_SKILLS_REVIEWERS:-}" ] && REVIEWER_FLAG="--reviewer $AI_SKILLS_REVIEWERS"

  glab mr create \
    --title "$TITLE" \
    --description "$(cat "$BODY")" \
    --target-branch "$TARGET" \
    --draft \
    --assignee @me \
    $REVIEWER_FLAG \
    --yes
  ```

  `glab` has no `--description-file`, hence the command substitution.

- Two or more open MRs → **stop here.** Update nothing. End your report with the question
  (list the IIDs found) so the caller can put it to the user.

### 9. Report

Print the word count, the target branch, the MR URL, and any of these that applied: the
ticket lookup failed, a project MR template was ignored.

If you stopped short of creating or updating anything instead — target-branch ambiguity
(step 2), the word-budget gate (step 7), or two or more open MRs (step 8) — report that in
place of a URL: say which one stopped you, and include the question and the candidate list
(branch names or MR IIDs) so the caller can put it to the user.
