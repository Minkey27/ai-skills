---
name: process-mr-feedback
description: "Use when processing review feedback on a GitLab merge request you have checked out — working through the open discussion threads a reviewer (a human, or an automated/LLM diff-note) left on the MR. Fetches the unresolved resolvable threads via `glab`, verifies each finding against the current code, presents them with suggested fixes and a proposed disposition (Fix / Push back / Dismiss / Defer) for you to curate, then implements the accepted fixes, runs the project's configured lint/format/tests, commits, and — behind a single confirmation — pushes and posts a reply + resolves each thread. Triggers on phrases like 'process the review feedback', 'address the MR comments', 'work through the review threads', 'handle the review comments'. GitLab-only (requires `glab`). Refuses if the MR's source branch isn't currently checked out, because it needs a working tree to verify findings and apply fixes."
---

# process-mr-feedback

Work through the open review threads on the GitLab MR for the current branch. The skill turns a pile of reviewer comments into a controlled loop: fetch the threads, **verify each finding against the actual code** (reviewers — human or LLM — flag things that aren't real), let you curate a disposition per thread, then implement the accepted fixes and — only after one explicit confirmation — push, reply, and resolve.

The **unit of work is a discussion thread**; who authored it (a human reviewer or an automated diff-note) is metadata, not a branch in the logic.

## Config

This skill reads optional config via `AI_SKILLS_*` env vars. Recommended setup is one line in
`~/.zshenv`:

```sh
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
```

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_MR_TOOL` | `gh` | Must be `glab` for this skill. If unset or `gh`, the skill stops with a "GitLab-only" message. |
| `AI_SKILLS_LINT_CMD` | _(empty)_ | Lint command run before committing fixes. Empty → skip (a note is printed). |
| `AI_SKILLS_FORMAT_CMD` | _(empty)_ | Format command run before committing fixes. Empty → skip (a note is printed). |
| `AI_SKILLS_TEST_CMD` | _(empty)_ | Test command run before committing fixes, scoped to touched areas where possible. Empty → skip (a warning is printed). |
| `AI_SKILLS_COMMIT_TRAILER` | _(empty)_ | Optional trailer line appended to commit messages. Empty → no trailer. |

If the current session exposes a project test-runner skill (e.g. a `pytest-docker`-style skill),
**prefer invoking that skill** over running `AI_SKILLS_TEST_CMD` directly — it knows the project's
test tiers and flags. The env var is the portable fallback when no such skill is present.

## Hard rules

- **GitLab only.** Check `${AI_SKILLS_MR_TOOL:-gh}` early; if not `glab`, stop and tell the user
  this skill targets GitLab.
- **Branch must be checked out and match the MR `source_branch`.** Verification and fixes need a
  live working tree; without one, stop and tell the user to check the branch out first.
- **Single open MR.** If the branch resolves to multiple MRs, auto-pick the one with
  `state == "opened"`; ask only if 0 or ≥2 are open.
- **Verify before implementing.** Never apply a reviewer's request blind — re-read the cited code
  and judge whether the finding is real first. (This is the `superpowers:receiving-code-review`
  stance: evaluate, don't perform agreement.)
- **Presentation and curation never share a turn.** The analysis turn (per-thread summaries +
  overview table) must END before any `AskUserQuestion`. A same-turn dialog seizes the screen and
  buries the analysis — "before" means a turn boundary, not text order.
- **`AskUserQuestion` has no default-checked option.** All checkboxes start empty. Curation is
  therefore **two sequential prompts** — Fix candidates first, then Push back / Dismiss / Defer —
  never one combined dialog.
- **Lint/format/tests are a hard pre-commit gate.** If any fail, STOP before the outward batch —
  nothing is pushed, replied, or resolved on top of a red tree.
- **Outward actions need one explicit confirmation.** Push + replies + resolves go out as a single
  batch only after the user confirms; show the exact reply text and resolve flags first.
- **Push before reply.** A reply citing `<sha>` must not be posted until the commit is pushed and
  visible to reviewers. If the push fails, abort the reply/resolve batch.
- **Never auto-resolve Push back or Defer.** Only Fix and Dismiss resolve their threads; a
  disagreement or a deferral stays open for the reviewer to close.
- **No performative agreement in replies.** State the fix or the reasoning — no "thanks" /
  "good catch" / "you're absolutely right".
- **Content-Type header is mandatory** for `glab api ... --input -` (the reply POST). Without it
  GitLab returns HTTP 415. See [references/glab-discussions.md](references/glab-discussions.md).
- **Honor `--dry-run`.** If invoked with `--dry-run` (or "dry run" in the same message), build
  everything but make **no writes** — no edits, no commit, no push, no posts. Print the planned
  fixes and the reply/resolve payloads as a receipt.

## Workflow

### 1. Detect the MR and fetch the threads

Gate on the tool first:

```bash
if [ "${AI_SKILLS_MR_TOOL:-gh}" != "glab" ]; then
  echo "STOP: process-mr-feedback is GitLab-only. Set AI_SKILLS_MR_TOOL=glab to use it."
  exit 1
fi
```

Then resolve the MR:

```bash
glab mr view --output json
```

Capture `iid`, `source_branch`, `target_branch`, `web_url`, and the project id.

- **Multiple MRs on the branch:** if `glab mr view` errors with "merge request ID number required"
  + several matches, call `glab mr view <iid> --output json` on each and **auto-pick the single one
  with `state == "opened"`**. Stop and ask only if 0 or ≥2 are open.
- **Branch mismatch:** confirm the current branch equals `source_branch`. If not, stop and surface
  the mismatch — switching branches is the user's call, not yours.
- **Dirty working tree:** if `git status --porcelain` is non-empty, surface it before making any
  edits and let the user decide whether to proceed.

Fetch and filter the discussion threads per
[references/glab-discussions.md](references/glab-discussions.md): keep only **open, resolvable**
threads (skip system notes and already-resolved threads). Build an in-memory list, one entry per
thread:

```
{ discussion_id, author, path, line, body, has_prior_replies }
```

**If zero threads qualify**, report "No open review threads to process." and stop — there is nothing
to do.

For any thread with `has_prior_replies == true`, flag it ("has prior discussion") so you don't talk
over an exchange that's already in progress.

### 2. Verify each finding

This is the core of the skill: **reviewers — human or LLM — flag things that aren't real, or whose
suggested fix wouldn't work.** Do not skip to implementing.

For each thread:

1. Read the code at the anchor (`path`:`line`) in its **current** state, plus enough surrounding
   context to judge. If the line has shifted, find the equivalent location.
2. Classify validity:
   - **`valid`** — the issue is real and actionable as described.
   - **`invalid`** — wrong: based on a misread, already handled, or not applicable to this codebase.
   - **`needs-clarification`** — ambiguous; you cannot tell what change is being requested.
3. For `valid` findings, draft a **concrete suggested fix** — the actual change, not a vibe.
4. Propose a **default disposition**:

   | Validity | Default disposition | Why |
   |---|---|---|
   | `valid` | **Fix** | real + actionable → implement |
   | `invalid` (reviewer is wrong) | **Push back** | reply with reasoning, leave open for the reviewer |
   | `invalid` (already done / N-A) | **Dismiss** | reply noting it's handled, resolve |
   | `needs-clarification` | **Defer** | reply with a question, leave open |

**Mechanism / scale.** For **≤3 threads**, verify inline. For **4+**, dispatch one read-only
verification sub-agent per thread **in parallel** (a single message with many tool calls) and
aggregate. Sub-agent brief:

```
Verify this MR review comment against the actual code on the current branch.

Thread:
  File: <path>
  Line: <line>   (or "no code anchor" for a general comment)
  Comment: <body>

Tasks:
  1. Read the cited file and surrounding context at the current branch tip. Decide whether the
     issue the comment describes is actually present at (or near) the cited location. If the line
     has shifted, find the equivalent location.
  2. If the issue is real, draft the concrete fix. If the comment suggests a fix, judge whether it
     would actually work without introducing a new problem.

Report (under 150 words):
  - validity: valid | invalid | needs-clarification — one-sentence reason
  - suggested_fix: the concrete change (only if valid)
  - corrected_anchor: <right path:line if the cited one was wrong>
  - notes: anything else worth knowing

Read the actual files at the branch tip — do not parrot the comment back.
```

Aggregate the sub-agent verdicts into a single table keyed by `discussion_id`, then continue to
Stage 3.

### 3. Present the findings, then curate dispositions

This stage has a **hard turn boundary**: you present, your turn ends, and only in a *later* turn do
you open the curation dialog. A same-turn `AskUserQuestion` seizes the screen and the user decides
without having read the analysis. "Before" means a turn boundary, not text order.

**3a. Analysis turn (ENDS before any dialog).** Print, in this order:

1. **A short summary per thread** — one block each, 2–4 sentences: what the reviewer asked, the
   verdict (`valid` / `invalid` / `needs-clarification`), and the proposed fix or the pushback
   reasoning. Flag any thread that has prior replies.
2. **An overview table** at the end — the scan layer:

   ```
   | # | file:line | author | verdict | proposed disposition | one-line fix summary |
   |---|-----------|--------|---------|----------------------|----------------------|
   | 1 | service.py:62 | <user> | valid | Fix | resolve via repo, not transient |
   | 2 | routes.py:107 | <user> | invalid | Push back | reviewer misread; X is already async |
   | 3 | (no anchor) | <user> | needs-clarification | Defer | ask which format is meant |
   ```

**Then END YOUR TURN.** No `AskUserQuestion` in this turn. Wait for the user's reply (a "go", a
question about a thread, or a re-classification request). This reply beat is where the user can
interrogate a finding or move it between dispositions *before* the dialog frames the decision.

**3b. Curation dialog (later turn) — two sequential `AskUserQuestion` prompts.** `AskUserQuestion`
has no default-checked option, so split by recommendation rather than cram everything into one
dialog:

- **Prompt 1 — Fix candidates (recommended).** A standalone `AskUserQuestion` containing **only**
  the threads proposed as **Fix**. The user ticks to confirm, unticks to drop. **Wait for the
  answer before sending Prompt 2.**
- **Prompt 2 — Push back / Dismiss / Defer (judgment calls).** A second, separate
  `AskUserQuestion` containing the remaining threads, so the user can confirm or change each
  disposition. Skip this prompt entirely if there are none.

Keep option labels **minimal** — the detail already appeared in 3a. Label shape:
`#<id> · <verdict> · file:line`. Do not repeat the fix summary in the label.

If a bucket exceeds 4 options, batch **within** the bucket across consecutive prompts (1a, 1b, …
then 2a, 2b, …) — never mix Fix and non-Fix in one prompt, and finish all Fix prompts before the
first judgment prompt.

The user may override any proposed disposition here (e.g. move a `Fix` to `Defer`, or a
`Push back` to `Fix`).

**Output of this stage:** a frozen disposition map

```
{ discussion_id -> Fix | PushBack | Dismiss | Defer }
```

plus the drafted fix text (for Fix) and reply text (for every disposition), carried into Stages 4
and 5.

### 4. Implement the Fix threads and commit

Work only the threads dispositioned **Fix**. Push back / Dismiss / Defer produce replies in Stage 5
and no code change here.

**Order of implementation** — blocking/correctness first, then cheap, then expensive:

1. Blocking / correctness fixes.
2. Simple fixes (typos, imports, renames).
3. Complex / refactor fixes.

**Before each edit:** re-read the target file (don't trust the verification snapshot — it may be
stale), and **grep for callers before changing any signature or return type**, updating call sites
in the same change. Make **one concern at a time** so a later failure is traceable to a specific
fix.

**Pre-commit verification gate** (portable — no hardcoded toolchain):

```bash
# Lint + format — empty var means skip (print a note).
[ -n "$AI_SKILLS_LINT_CMD" ]   && eval "$AI_SKILLS_LINT_CMD"   || echo "AI_SKILLS_LINT_CMD unset — skipping lint"
[ -n "$AI_SKILLS_FORMAT_CMD" ] && eval "$AI_SKILLS_FORMAT_CMD" || echo "AI_SKILLS_FORMAT_CMD unset — skipping format"
```

For **tests**: if the session exposes a project test-runner skill (e.g. a `pytest-docker`-style
skill), invoke that skill instead of running the raw command. Otherwise:

```bash
[ -n "$AI_SKILLS_TEST_CMD" ] && eval "$AI_SKILLS_TEST_CMD" || echo "WARNING: AI_SKILLS_TEST_CMD unset — tests NOT run"
```

**If lint, format, or tests fail → STOP here.** Surface the failure and do **not** proceed to the
outward batch. Nothing is pushed, replied, or resolved on top of a red tree.

**Commit locally.** One commit, or a few logical commits if the fixes are genuinely independent.
Reference what the threads asked for in the message. Append the trailer only if configured:

```bash
MSG="fix: address review feedback on <area>"
[ -n "$AI_SKILLS_COMMIT_TRAILER" ] && MSG="$MSG

$AI_SKILLS_COMMIT_TRAILER"
git commit -m "$MSG"
```

**Do not push yet** — push is part of the gated outward batch (Stage 5). Capture each resulting
commit SHA; the Fix replies cite it.

**Under `--dry-run`:** make no edits and no commit. Instead describe, per Fix thread, the change you
*would* make and the SHA placeholder the reply would cite.

### 5. Push, reply, resolve (the outward batch)

Everything reviewers can see goes out together, behind **one** confirmation.

**The gate.** In an assistant turn that **ENDS**, show:

- the push target (branch → remote), and
- per thread: disposition, the **verbatim reply text** that will be posted, and whether the thread
  will be resolved.

Then ask for a single confirmation to send the whole batch. Do not split this into per-thread
prompts — the user approves the batch once.

**On confirm, in order:**

1. **Push the branch — only if Stage 4 produced a commit.**

   ```bash
   git push
   ```

   The push must succeed before any reply is posted — a reply citing `<sha>` is useless until the
   commit is visible to reviewers. **If the push fails, abort the reply/resolve batch** and report;
   post nothing.

   If **no thread was dispositioned Fix** (so Stage 4 made no commit), there is nothing to make
   visible — skip the push and go straight to the replies. No reply in this case cites a `<sha>`.

2. **Reply + resolve per thread**, using the recipes in
   [references/glab-discussions.md](references/glab-discussions.md) (reply = `POST
   .../discussions/<id>/notes` with the `Content-Type` header; resolve = `PUT
   .../discussions/<id>?resolved=true`):

   | Disposition | Reply | Resolve? |
   |---|---|---|
   | **Fix** | `Fixed in <sha>: <one line>` | **yes** |
   | **Dismiss** | the reasoning (already handled / not applicable) | **yes** |
   | **Push back** | the technical reasoning for disagreeing | **no — leave open** |
   | **Defer** | acknowledgement + that it's tracked as follow-up | **no — leave open** |

   Prefer a single `python3` helper looping over `(discussion_id, body, resolve?)` tuples; capture
   each reply's note id for the receipt.

3. **Print the outcome table:**

   ```
   | # | discussion_id | disposition | replied? | resolved? | note_url |
   |---|---------------|-------------|----------|-----------|----------|
   ```

**Reply tone:** no performative agreement — state the fix or the reasoning, never "thanks" /
"good catch" / "you're absolutely right".

**Under `--dry-run`:** push nothing and POST/PUT nothing. Print the push target and every
reply/resolve payload as a receipt, made obviously a dry-run, e.g.
`[DRY-RUN] Would push <branch> and reply+resolve N threads on MR !<iid>`.

## Failure modes to watch for

- **`AI_SKILLS_MR_TOOL` is not `glab`** — stop early; this skill is GitLab-only.
- **`glab mr view` returns nothing** — no MR on the branch. Tell the user and stop; there's nothing
  to process.
- **Branch not checked out / current branch ≠ `source_branch`** — stop and surface the mismatch.
  Switching branches is the user's call.
- **Multiple or zero open MRs on the branch** — auto-pick the single `state=="opened"` MR; stop and
  ask only if 0 or ≥2 are open.
- **Zero qualifying threads** — report "nothing to process" and stop.
- **`needs-clarification` finding** — never silently guess what the reviewer meant. Default to
  Defer with a drafted question reply; leave the thread open.
- **Lint / format / tests fail** — STOP before the outward batch. Never push or post on top of a
  red tree.
- **Thread already has prior replies** — flag it ("has prior discussion") and don't talk over an
  exchange in progress; let the user decide whether to add to it.
- **Push fails at the gate** — abort the reply/resolve batch so no reply cites a SHA the reviewer
  can't see.
- **Dirty working tree at start** — surface it before making edits and let the user decide whether
  to proceed.

## Why this shape

**Verification before curation kills false positives.** Code-review output — human or LLM — routinely
flags things that aren't real, or recommends fixes that don't fit the codebase. Verifying each
thread against the current code *before* presenting it means the user curates a list of real,
checked findings instead of filtering noise by hand. This is the `receiving-code-review` stance made
mechanical: evaluate, don't perform agreement.

**The turn break between presentation and the dialog exists because of an observed failure, not
theory.** When the analysis and the first `AskUserQuestion` share a turn, the dialog seizes the
screen and the user is asked to curate findings they never read. Reading requires a turn the user
gets to finish; any wording that lets the presentation and the prompt share a turn re-opens that
hole. "Before" means a turn boundary, not text order.

**Push before reply keeps every `<sha>` link valid.** A reply that says "Fixed in `<sha>`" is
worthless until the commit is visible to reviewers; posting it before the push (or when the push
fails) leaves a dead link in a permanent thread. Sequencing push as step one of the outward batch —
and aborting on push failure — guarantees the citation resolves.

**Resolve only Fix and Dismiss.** Resolving a thread is a social signal that the matter is closed.
For a disagreement (Push back) or a deferral (Defer), it isn't closed — auto-resolving would
silently end a conversation the reviewer never agreed to end. Those stay open for the reviewer to
close.
