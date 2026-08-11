---
name: finalize-branch
description: Use when implementation is complete and you want to review, simplify, squash, and create an MR — the full post-implementation finalization workflow before sending work for review. Pass `yolo` as an argument to skip the simplify/squash confirmation gates while still letting the user curate which code-review findings to fix.
---

# Finalize Branch

## Overview

Workflow that chains four post-implementation steps: code review, simplify, squash, and MR creation.

**Core principle:** Review it, clean it, squash it, ship it.

**Announce at start:** "I'm using the finalize-branch skill to review, simplify, squash, and create an MR for this branch." If yolo mode is active, add: "Running in **yolo** mode — auto-applying simplify and squash without confirmation gates. Code-review findings will still be curated by you."

## Arguments

The skill accepts one optional argument that comes through as the literal text after `/finalize-branch`.

| Arg | Effect |
|---|---|
| _(none)_ | **Gated mode** (default). User confirms between Steps 1→2, 2→3, 3→4. |
| `yolo` (also accepts `--yolo`, `auto`, `-y`) | **Yolo mode.** Gates between Steps 2→3→4 are removed. Step 1 keeps the curation checklist (that's selection, not gating). |

Detect by matching the args case-insensitively against `^(yolo|--yolo|auto|-y)$`. If anything else is passed, ask the user what they meant rather than guessing.

**What yolo does NOT change:**
- Pre-flight checks still run and still stop the workflow on failure.
- Step 1's presentation + curation prompts (verification fan-out, then Recommended/Optional prompts) still run — the user picks which findings to address.
- The `squash` sub-skill has its own internal confirmation; we don't override that, surface it to the user as-is.
- The squash skill's diff-verification (no changes lost) still runs.

## Config

This skill reads optional config via the `AI_SKILLS_*` env vars. Recommended setup is one line in `~/.zshenv`:

```sh
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
```

That makes the variables available to every shell Claude spawns. Commands below use `${VAR:-default}` syntax inline.

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_MR_TOOL` | `gh` | `gh` (GitHub) or `glab` (GitLab) |
| `AI_SKILLS_REVIEWERS` | _(empty)_ | Comma-separated reviewer handles. Empty → no `--reviewer` flag |
| `AI_SKILLS_TARGET_BRANCH` | `main` | Target branch for the MR/PR |
| `AI_SKILLS_TICKET_PREFIX` | _(empty)_ | Ticket prefix (e.g. `PROJ`). Empty → match any uppercase slug |

## Pre-flight Checks

Before starting, verify all three conditions. Stop if any fails.

```bash
TARGET="${AI_SKILLS_TARGET_BRANCH:-main}"

# 1. Not on the target branch
[ "$(git branch --show-current)" = "$TARGET" ] && echo "STOP: on $TARGET" && exit 1

# 2. Clean working tree
git status --porcelain | grep -q . && echo "STOP: uncommitted changes" && exit 1

# 3. Has commits ahead of the target branch
git fetch origin "$TARGET" --quiet
MERGE_BASE=$(git merge-base "origin/$TARGET" HEAD)
[ "$(git rev-parse HEAD)" = "$MERGE_BASE" ] && echo "STOP: no commits ahead of $TARGET" && exit 1
```

## The Process

```dot
digraph finalize {
    rankdir=TB;
    node [shape=box];

    preflight [label="Pre-flight checks"];
    review [label="Step 1: superpowers:requesting-code-review"];
    verify [label="Fan out sub-agents\nto verify each finding"];
    present [label="Present summaries + table\nEND TURN, wait for reply"];
    curate [label="Sequential curation prompts\n(Recommended, then Optional)" shape=diamond];
    fix [label="Fix selected findings\n+ commit"];
    gate1 [label="User confirms\n(skipped in yolo)" shape=diamond];
    simplify [label="Step 2: Simplify\n(auto-applies in yolo)"];
    gate2 [label="User confirms\n(skipped in yolo)" shape=diamond];
    squash [label="Step 3: Squash\n(squash skill has its own gates)"];
    gate3 [label="User confirms\n(skipped in yolo)" shape=diamond];
    mr [label="Step 4: Create MR"];
    done [label="Done" shape=doublecircle];

    preflight -> review -> verify -> present -> curate -> fix -> gate1;
    gate1 -> simplify [label="proceed"];
    simplify -> gate2 -> squash [label="proceed"];
    squash -> gate3 -> mr [label="proceed"];
    mr -> done;
}
```

### Step 1: Code Review (with verification + curation)

This step is identical in both gated and yolo modes — the curation prompts *are* the gate.

**REQUIRED SUB-SKILL:** Use `superpowers:requesting-code-review` to generate findings — it dispatches one reviewer sub-agent over the git range and returns a prose review (Strengths / Critical / Important / Minor / Assessment) that step 1a converts into the structured findings list.

Compute SHAs using the merge-base against the target branch (never the remote branch directly):

```bash
git fetch origin "${AI_SKILLS_TARGET_BRANCH:-main}" --quiet
BASE_SHA=$(git merge-base "origin/${AI_SKILLS_TARGET_BRANCH:-main}" HEAD)
HEAD_SHA=$(git rev-parse HEAD)
```

Invoke `superpowers:requesting-code-review` with these SHAs, filling its reviewer
template: `DESCRIPTION` = what this branch built (derive it from `git log` over the
range), `PLAN_OR_REQUIREMENTS` = the plan file or ticket if one exists, otherwise
say so explicitly rather than inventing requirements.

**1a. Structure the findings.** The reviewer returns prose, not a schema — convert
its `Issues` sections into the list below. Map its severity headings onto this
skill's scale: `Critical` → `critical` (or `high` when it is a bug without data
loss / security impact), `Important` → `medium` (or `high` when it breaks a
user-visible path), `Minor` → `low`, and pure style remarks → `nit`. Ignore
`Strengths`, `Recommendations`, and `Assessment` — this step consumes findings
only; the assessment verdict is not a gate.

Capture each finding as:

```
{
  "id": "F1",
  "severity": "critical|high|medium|low|nit",
  "file": "path/to/file.py",
  "line_start": 42,
  "line_end": 42,
  "title": "short headline",
  "issue": "what the reviewer says is wrong",
  "recommendation": "what the reviewer suggests"
}
```

If line numbers aren't provided, leave them null and treat the finding as file-level. Do not invent line numbers — wrong anchors mislead the user later.

**1b. Fan out to verify each finding in parallel.** Single message, one sub-agent per finding (use the `general-purpose` Agent type, `model: sonnet` — verification is a bounded read-and-judge task that runs cheaper/faster on Sonnet 5; the reviewer stays on the session model for recall). Each sub-agent gets:

```
Verify this code-review finding against the actual code on the current branch.

Finding:
  File: <file>
  Lines: <line_start>-<line_end>  (or "file-level")
  Issue: <issue text>
  Recommendation: <recommendation text>

Tasks:
  1. Read the cited file and surrounding context. Confirm whether the described
     issue is actually present at the cited location on the current branch. If
     the lines have shifted, find the equivalent location.
  2. Independently judge whether the recommendation, if applied, would actually
     resolve the issue without introducing a new problem.

Report:
  - issue_real: yes / no / partial — with one-sentence reason
  - fix_sound:  yes / no / risky   — with one-sentence reason
  - corrected_lines: <if the line numbers were wrong, give the right ones>
  - notes: anything else worth knowing

Be specific. Do not parrot the finding back — actually look at the code. Under 150 words.
```

Aggregate the results into a table keyed by finding id.

**1c. Present the findings — then END YOUR TURN.** Before any curation prompt, the user must be able to read what each finding *is*, what the *suggested fix* is, and what verification concluded. Print, in this order:

1. **A prose write-up of each finding** — one block per finding. This is the *detail layer*; the numbered options in 1d stay minimal because the detail already lives here. Full rules in [Finding write-up format](#finding-write-up-format) — prose with run-on bold lead-ins, **not** colon-labelled one-liners, and **no verification badge line**.

   ```markdown
   ### F1 — [medium] services.py:120 — duplicate enum 'Afdeling'

   **Problem.** <mechanism, 2–5 sentences. Name the exact symbols and cite file:line
   inline as you go. Explain the sequence that produces the bad state and the
   user-visible consequence.>

   **Fix.** <the concrete change. One bullet per edit if there is more than one;
   inline the actual code line where it clarifies.>

   **My read.** <one sentence: take it / skip it / fold into F<n>, and why.>

   ---
   ```

2. **An overview table at the end** — the *scan layer* the user reads right before deciding:

   ```
   | ID | Sev | Anchor | Real? | Fix sound? | Bucket |
   |----|-----|--------|-------|------------|--------|
   | F1 | medium | services.py:120 | ✓ yes | ✓ yes | Recommended |
   | F2 | low | (file-level) | ✓ yes | ⚠ risky | Optional |
   ```

**Then END YOUR TURN.** The presentation must be a complete assistant message that **asks nothing** — a same-turn prompt means the user picks findings they never read. Putting the report "before" the prompt *within one turn* does **not** satisfy this. Wait for the user's reply (a "go", a question about a finding, or a re-classification) and only then send the curation prompts in 1d.

**Excluded findings** (`issue_real == no` — verified false positives) are **not** shown as options. List them in a brief "dropped, and why" line inside the presentation so the user knows they were considered.

**1d. Curation prompts** (in the turn *after* the user replies to 1c). Classify each shown finding into one bucket, then send **sequential plain-text prompts with numbered options** — **never `AskUserQuestion`**, never one combined list:

| Bucket | Rule | Prompt |
|---|---|---|
| **Recommended** | `issue_real ∈ {yes, partial}` AND `fix_sound != no` AND severity ∈ {`critical`, `high`, `medium`} | Prompt 1 |
| **Optional** | shown but not recommended: `low`/`nit` severity, or `fix_sound == risky` (real but the fix has caveats) | Prompt 2 |

> **Precedence:** the rules overlap for a `medium`+ finding with `fix_sound == risky` — the risky clause wins and the finding goes to **Optional**, regardless of severity. A fix with caveats should not be auto-recommended; the user opts in with the caveat visible in the badge.

> **Why `partial` counts as real.** A `partial` verdict usually means the *bug* is real but the reviewer's diagnosis of *how* it triggers was wrong. Fix the corrected version from the verification report, not the original claim.

- **Prompt 1** — only the Recommended bucket, one numbered line each. Say plainly that every line is skill-recommended and that the default is **all of them**; the user replies with numbers to drop (or "go" / "none"). **Wait for the answer before Prompt 2.**
- **Prompt 2** — only the Optional bucket. Nothing here is recommended, so the default is the opposite: **none are fixed unless the user names numbers.** Skip this prompt if the bucket is empty.

**Keep each line minimal** — the detail already appeared in 1c. Line shape: `[F3 medium] services.py:120 — duplicate enum 'Afdeling'` (ID + severity + anchor + headline), plus the verification badge in parentheses where one applies (`✓ verified, fix sound`, `⚠ lines shifted to 125–128`). No summary sentences.

```markdown
**Recommended — 3 findings.** Default is all of them. Reply with numbers to drop, or "go".

1. [F1 high] downloads.py:64 — IDOR on document download  (✓ verified)
2. [F3 medium] services.py:120 — duplicate 'Afdeling' enum  (⚠ lines shifted to 125–128)
3. [F5 medium] handlers.py:2455 — as_of not threaded  (✓ verified)
```

Numbered text has no 4-option ceiling, so a long bucket stays **one** prompt — don't fragment it. Order by severity inside each prompt so heavy hitters come first. Never mix buckets; finish Prompt 1 before Prompt 2. **Silence is not an answer** — if no reply comes, stop rather than applying the default.

**1e. Fix the selected findings, commit.** Skip any the user did not select. Commit with a message that names what was fixed (e.g. `fix: close idor on document download, dedupe afdeling enum`) — never session-local finding ids (`F1`, `F3`), which mean nothing in git history.

**1f. Gate transition:**
- **Gated mode:** Ask "Proceed to Step 2 (simplify)?" before continuing.
- **Yolo mode:** Skip the confirmation — proceed directly to Step 2 in the same turn.

### Step 2: Simplify

**REQUIRED SUB-SKILL:** Use `simplify` (code-simplifier agent) if available.

The code-simplifier analyzes recently modified code on the branch and proposes clarity/consistency improvements.

**Gated mode** (default):
1. Present the proposed changes to the user
2. User approves or rejects individual changes
3. Commit approved changes
4. **GATE:** Ask the user to confirm before proceeding to Step 3

**Yolo mode:**
1. Apply all proposed changes the code-simplifier returns. Trust the sub-skill's judgment.
2. Print a short summary of what was applied (file + one-line description per change) so the user can see what happened.
3. Commit with `refactor: simplify per code-simplifier`.
4. Proceed directly to Step 3 in the same turn — no confirmation.

### Step 3: Squash

**REQUIRED SUB-SKILL:** Use `squash`

The squash skill has its own internal gates that this skill does NOT override (its grouping confirmation and diff-verification are safety, not approval). Yolo mode does not silence them — if the user wants them silenced, that's a change to the `squash` skill itself.

**Gated mode** (default): after squash completes, **GATE:** Ask the user to confirm before proceeding to Step 4.

**Yolo mode:** after squash completes (including its own internal confirmation), proceed directly to Step 4 in the same turn — no extra confirmation.

### Step 4: Create MR

The same tool path serves GitHub and GitLab — only the final create/update command differs
on `${AI_SKILLS_MR_TOOL:-gh}`.

**State across blocks.** Each fenced `bash` block below is a **separate Bash invocation** —
shell variables do not survive between them, only files do. Two values cannot be re-derived
later (`$UPSTREAM_SHA` must be read *before* 4a's fetch; `$TARGET` may have been settled by
the user), so 4a writes them to a state file and every later block re-sources it:

```bash
STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
```

`$STATE` is not itself carried state — it is a constant expression, re-derived identically in
every block, landing under the current worktree's git directory (`git rev-parse --git-path`
resolves to `<main-repo>/.git/worktrees/<name>/mr-state.sh` in a linked worktree). Never
shared `/tmp`: two concurrent worktrees would collide on one file. 4a truncates it (`: >`) so
a file left behind by an earlier run can never leak into this one. Values are appended with
`printf '%q'` so spaces and shell metacharacters survive the round trip.

**4a. Target branch.** Never assume `main`. Branches are frequently stacked on an epic
branch, and retargeting a stacked branch at `main` proposes merging unreviewed upstream work.

```bash
STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
: > "$STATE"

BRANCH=$(git branch --show-current)

# Captured BEFORE the fetch below — this is the value the force-push lease in 4e pins
# against. Empty when the branch has no upstream yet.
UPSTREAM_SHA=$(git rev-parse "@{u}" 2>/dev/null || true)

printf 'BRANCH=%q\n' "$BRANCH" >> "$STATE"
printf 'UPSTREAM_SHA=%q\n' "$UPSTREAM_SHA" >> "$STATE"

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
```

"Nearest" is the candidate with the **fewest** commits HEAD has accumulated *since diverging
from it* — not the fewest commits contained by it: `--merged HEAD` containment breaks the
moment the default branch's tip stops being an ancestor of HEAD (it advances past the fork
point on almost every branch that isn't freshly forked), silently handing the win to whatever
merged-in sibling branch happens to still be an ancestor.

- Nearest equals the configured default (or the loop found no candidate) → use the default,
  say nothing.
- Nearest differs → **stop and ask the user** which branch to target, quoting both candidates
  and their commit counts. Do not push and do not create anything until they answer. This is
  a gate in yolo mode too — yolo removes the confirmations *between* steps, not a genuine
  ambiguity about where the work gets merged.

Then record the decision. `$TARGET` is the branch name **without** the `origin/` prefix; it is
consumed as `origin/$TARGET`:

```bash
STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"

# The settled branch name: the default when it won, the user's answer when they were asked.
TARGET="<settled branch name, no origin/ prefix>"
printf 'TARGET=%q\n' "$TARGET" >> "$STATE"
```

**4b. Ticket** — before drafting, since the ticket (if any) is the body's first line:

```bash
STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
. "$STATE"

BASE_SHA=$(git merge-base "origin/$TARGET" HEAD)
PATTERN="${AI_SKILLS_TICKET_PREFIX:-[A-Z]+}-[0-9]+"
TICKET=$(printf '%s\n' "$BRANCH" | grep -oE "$PATTERN" | head -1)
if [ -z "$TICKET" ]; then
  TICKET=$(git log --format='%s%n%b' $BASE_SHA..HEAD | grep -oE "$PATTERN" | head -1)
fi
echo "Ticket: ${TICKET:-<none>}"

printf 'BASE_SHA=%q\n' "$BASE_SHA" >> "$STATE"
printf 'TICKET=%q\n' "${TICKET:-}" >> "$STATE"
```

Merge-base, never `origin/$TARGET` directly — the remote can be ahead of the fork point,
which pulls unrelated upstream commits into the diff you are describing.

If a tracker MCP is available (ClickUp/Jira/Linear), fetch the ticket and read it **for
intent only** — what problem was being solved. Do not copy its wording into `## Why`;
summarise the outcome. If the lookup fails, derive `Why` from the diff and commit messages,
and say so in your final report.

`Closes <TICKET>` goes first — keyword, space, ticket id, nothing else on the line.
Downstream automation parses it to transition the ticket, so a reworded or reformatted line
silently strands it. Omit the line entirely when no ticket reference exists.

**4c. Draft.** Re-read the diff you are describing (`git diff $BASE_SHA..HEAD`) and write
from it, not from what you remember implementing — a description written from session
memory narrates the journey, which no reviewer asked for. Write the title and body to files under the current
worktree's git directory, not shared `/tmp`: `git rev-parse --git-path` is worktree-aware, so
two concurrent worktrees never collide on the same path, and the location sits outside the
working tree so it never shows up in `git status --porcelain`.

```bash
TITLE_FILE="${MR_TITLE_FILE:-$(git rev-parse --git-path mr-title.txt)}"
BODY_FILE="${MR_BODY_FILE:-$(git rev-parse --git-path mr-body.md)}"
```

Both are constant expressions like `$STATE` — re-derive them in each block that needs them
rather than persisting the paths, so the step that writes a file and the steps that read it
cannot drift apart.

The body starts with the `Closes $TICKET` line when `$TICKET` is non-empty. Announce the
chosen title and the drafted body in your response before creating the MR, so the user sees
what was decided. If the environment mandates a scratchpad directory instead, set
`MR_TITLE_FILE` / `MR_BODY_FILE` (or assign the variables directly) to a path inside it.

Check for `.gitlab/merge_request_templates/` (or `.github/pull_request_template.md`) and
follow it when one exists.

**4d. The deletion pass.** Check every bullet against the diff: **if a reviewer would already
know it from the file list or the code itself, cut it.**

**4e. Push, then create or update.**

```bash
STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
. "$STATE"

if [ -z "${UPSTREAM_SHA:-}" ]; then
  # No upstream existed at 4a — nothing to protect, a first push cannot clobber.
  git push -u origin "$BRANCH"
else
  git push -u origin "$BRANCH" \
    || git push --force-with-lease="refs/heads/$BRANCH:$UPSTREAM_SHA" -u origin "$BRANCH"
fi
```

The plain `git push` gets rejected as non-fast-forward whenever Step 3's squash rewrote
history that was already pushed. The retry pins `--force-with-lease` to `$UPSTREAM_SHA` — the
upstream tip captured in **4a, before the fetch**. An unpinned `--force-with-lease` re-reads
the remote-tracking ref at push time, and 4a's `git fetch origin` already refreshed that ref
to match whatever is on the remote right now — including a teammate's commit pushed in
between — so the lease would authorise the exact overwrite it exists to prevent. Pinning to
the pre-fetch SHA is what makes the lease reject a push when the remote moved out from under
this branch.

Then, on **`gh`**:

```bash
STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
. "$STATE"
TITLE_FILE="${MR_TITLE_FILE:-$(git rev-parse --git-path mr-title.txt)}"
BODY_FILE="${MR_BODY_FILE:-$(git rev-parse --git-path mr-body.md)}"

REVIEWER_FLAG=()
[ -n "${AI_SKILLS_REVIEWERS:-}" ] && REVIEWER_FLAG=(--reviewer "$AI_SKILLS_REVIEWERS")

gh pr create \
  --title "$(cat "$TITLE_FILE")" \
  --body "$(cat "$BODY_FILE")" \
  --base "$TARGET" \
  --draft \
  --assignee @me \
  "${REVIEWER_FLAG[@]}"
```

The reviewer flag must be an **array**, expanded as `"${REVIEWER_FLAG[@]}"`. A plain string
expanded unquoted (`$REVIEWER_FLAG`) works only under bash: zsh does not word-split parameter
expansions, so the whole thing arrives as a single argv element and the tool reports an unknown
flag named `--reviewer handle1,handle2`. An empty array expands to zero arguments in both
shells, which is exactly what the no-reviewers case needs.

On **`glab`**, an MR may already exist on this source branch — and several can share one, in
which case `glab mr view` errors on the ambiguity. Resolve it explicitly:

```bash
STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
. "$STATE"

OPEN=$(glab api "projects/:id/merge_requests?source_branch=$BRANCH&state=opened" \
  | python3 -c "import json,sys; print(' '.join(str(m['iid']) for m in json.load(sys.stdin)))")
echo "open MRs on $BRANCH: ${OPEN:-none}"
```

- **None** → create:

  ```bash
  STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
  . "$STATE"
  TITLE_FILE="${MR_TITLE_FILE:-$(git rev-parse --git-path mr-title.txt)}"
  BODY_FILE="${MR_BODY_FILE:-$(git rev-parse --git-path mr-body.md)}"

  REVIEWER_FLAG=()
  [ -n "${AI_SKILLS_REVIEWERS:-}" ] && REVIEWER_FLAG=(--reviewer "$AI_SKILLS_REVIEWERS")

  glab mr create \
    --title "$(cat "$TITLE_FILE")" \
    --description "$(cat "$BODY_FILE")" \
    --target-branch "$TARGET" \
    --draft \
    --assignee @me \
    "${REVIEWER_FLAG[@]}" \
    --yes
  ```

  `glab` has no `--description-file`, hence the command substitution.

- **Exactly one** → update it in place, including the target branch, so a stale MR never
  points at the wrong one. Pass the IID you just read from the `$OPEN` output above as a
  literal — it does not survive into this block:

  ```bash
  STATE="${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
  . "$STATE"
  TITLE_FILE="${MR_TITLE_FILE:-$(git rev-parse --git-path mr-title.txt)}"
  BODY_FILE="${MR_BODY_FILE:-$(git rev-parse --git-path mr-body.md)}"

  glab mr update <IID> \
    --description "$(cat "$BODY_FILE")" \
    --title "$(cat "$TITLE_FILE")" \
    --target-branch "$TARGET"
  ```

- **Two or more** → **stop.** Update nothing; ask the user which IID to target, listing the
  ones found.

**Clean up** once the URL is in hand — the state file has served its purpose and a stale one
is only a hazard for the next run:

```bash
rm -f "${MR_STATE:-$(git rev-parse --git-path mr-state.sh)}"
```

Return the MR/PR URL when done.

## Red Flags

**Never:**
- Skip the Step 1 curation prompts — even in yolo mode, the user selects which findings to fix. Yolo only skips gates *between* steps, not within Step 1.
- Use `AskUserQuestion` for anything — curation and the between-step gates are plain text with numbered options. The tool runs a countdown and assumes a default when it expires; "which findings do I fix" and "may I force-push" must not be answerable by a timer.
- Ask in the **same turn** as the 1c presentation — the report must end its own turn and the user must reply before the first curation prompt.
- Repeat finding detail (issue text, suggested fix) in the prompt lines — the detail belongs in the 1c write-ups, which stay on screen. Lines stay minimal (ID + severity + anchor + headline + verification badge).
- Combine Recommended and Optional into one list — they are sequential prompts (Recommended first, wait, then Optional) with opposite defaults.
- Treat silence as an answer — no reply means stop, not "apply the default".
- Skip a gate between Steps 1–3 **in gated mode** — every one needs user confirmation in default mode
- Treat anything other than the documented yolo aliases (`yolo`, `--yolo`, `auto`, `-y`) as yolo — ask the user instead of guessing
- Skip the verification fan-out — bucket classification is only trustworthy if sub-agents have confirmed each one
- Apply unverified code-simplifier suggestions in yolo without printing a summary the user can scan
- Force-push without the squash skill's verification passing
- Prefix the MR/PR title with `Draft:` or `WIP:` — use the `--draft` flag
- Drop `--draft` or `--assignee @me` from the create command — both go on every invocation
- Invent or hallucinate a ticket number — only include `Closes <TICKET>` if the reference actually appears in the branch name or commits
- Leave a literal `<TICKET>` placeholder in the description
- Narrate the implementation you just did — the body is derived from the diff, the commits and the ticket, never from session memory
- Add a dedicated test-plan/QA-steps section (or any reviewer QA script) to an MR body — the reviewer reads the diff and CI, and did not ask for a QA script
- Retarget a stacked branch at `main` because the divergence check was skipped — 4a's ambiguity gate holds in yolo mode too
- Force-push with an unpinned `--force-with-lease` — 4a's fetch makes it authorise the very overwrite it exists to prevent
- Build the reviewer flag as a string and expand it unquoted (`$REVIEWER_FLAG`) — that is a bash-only idiom; zsh passes it as one argv element and the create fails on an unknown flag. Use an array plus `"${REVIEWER_FLAG[@]}"`

**Always:**
- Detect the yolo argument before starting — announce it explicitly so the user can interrupt if they didn't mean it
- Compute BASE_SHA as merge-base, never use the remote target branch directly
- Dispatch finding-verification sub-agents in parallel (single message, many tool calls)
- Present per-finding prose write-ups (Problem. / Fix. / My read., `---` separated) + an overview table in a turn that **ends**, before any curation prompt — see [Finding write-up format](#finding-write-up-format)
- Classify findings into Recommended (`issue_real ∈ {yes, partial}` AND `fix_sound != no` AND severity ∈ {critical, high, medium}) vs Optional (everything else shown); `fix_sound == risky` goes to Optional regardless of severity; run them as two sequential prompts
- Drop verified false positives (`issue_real == no`) from the prompts and list them briefly in the 1c presentation
- Commit fixes from each step before proceeding to the next
- Use the tool from `$AI_SKILLS_MR_TOOL` (default `gh`) for MR/PR creation
- Run lint and format before any commits (project-specific; if your project has them, run them)
- Detect the target branch by divergence in Step 4a — never assume `${AI_SKILLS_TARGET_BRANCH:-main}`
- Pass `--draft` and `--assignee @me` on every invocation
- Only add `--reviewer` when `$AI_SKILLS_REVIEWERS` is non-empty
- Extract a ticket reference before drafting; if one exists, prepend a `Closes <TICKET>` first line

## Finding write-up format

The shape of the per-finding blocks in Step 1c — what the user reads on screen when curating.

```markdown
### F4 — [medium] floorplan-editor.js:1543 — applyAdjustFrame derefs state that can be nulled mid-POST

**Problem.** `applyAdjustFrame` awaits the POST at line 1508. `adjustMode` stays `true`
for that whole await, so anything that calls `cancelAdjust()` during it — Escape (2901),
`setDrawMode('pan')` (531), the page-change branch (1256) — runs `_exitAdjust()` and nulls
`adjustHandles`. Phase 2 then hits 1543 `adjustHandles.a.slice()` and throws. Pre-branch that
deref sat inside `if (frame)`; this branch hoisted it out. The throw lands *after* the server
persisted, so `loadDoors()` never runs — canvas shows pre-alignment geometry for saved data.

**Fix.** Two edits in `floorplan-editor.js`:
- Snapshot both objects into locals before the Phase-1 await and use the snapshots in Phase 2:
  `const sentA = adjustHandles.a.slice(), sentB = adjustHandles.b.slice();`
- Reset `applyBtn.disabled = false` where the adjust toolbar is re-shown, so a session
  cancelled mid-flight doesn't leave the button dead.

**My read.** Take it — small, and it's a regression this branch introduced.

---
```

**Rules:**

- **`**Problem.**` / `**Fix.**` are run-on bold lead-ins**, terminated with a period, with the prose continuing on the same line. Not `**Issue:** <one line>`. The write-up is prose that explains a *mechanism*: the trigger, the sequence, the resulting state, the user-visible consequence. Cite `file:line` inline as you narrate rather than only in the header.
- **`**Fix.**` must be actionable, not a restatement of the problem.** Bullet per edit when there is more than one; inline the real code line, the real helper name, the real fixture that already exists. Say when it's a pure test addition with no production change.
- **No `**Verification:**` badge line.** Verification results get *woven into* the prose instead, as corrections in your own voice — "Important correction to the original recommendation: `target_document_version_id` is the row's string id, while `get_page_sizes` needs the int `version` field", "Verification downgraded this to partial: nothing is locked at that point, so it's transaction duration, not lock contention". Badges live **only** in the overview table.
- **`**My read.**` is one sentence** — take it / skip it / fold into F*n* — and only when the call isn't obvious from the block.
- **`---` between every finding.** Including two consecutive findings inside the same cluster. The separator is what makes a long list navigable.
- **Clustering is encouraged** when findings share one mechanism: give the cluster a `## Cluster A — <the mechanism>` heading and a one-line note on how the findings interact (e.g. "F1's write-back closes F6; F2's narrowing is what makes it legible"). Each finding inside still gets its own full block and its own `---` — a cluster heading is not a licence to merge findings into one paragraph.
- **The overview table always stays**, last, covering every finding including clustered ones.

## Integration

**Pairs with:**
- **superpowers:executing-plans** — invoke this skill after plan execution completes
- **superpowers:requesting-code-review** — Step 1 finder (dispatches the reviewer sub-agent)
- **simplify** (code-simplifier) — Step 2 sub-skill
- **squash** — Step 3 sub-skill
