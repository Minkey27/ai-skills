---
name: process-mr-feedback
description: "Use when processing review feedback on a GitLab merge request you have checked out — working through the unresolved discussion threads a reviewer (a human, or an automated/LLM diff-note) left on the MR. Triggers on phrases like 'process the review feedback', 'address the MR comments', 'work through the review threads', 'handle the review comments'. GitLab-only (requires `glab`). Refuses if the MR's source branch isn't currently checked out, because it needs a working tree to verify findings and apply fixes."
---

# process-mr-feedback

Work through the open review threads on the current branch's GitLab MR: fetch the threads, **verify each against the actual code**, let the user curate a disposition per thread, implement the accepted fixes, then — behind one confirmation — push, reply, and resolve.

The **unit of work is a discussion thread**; its author (human or automated diff-note) is metadata, not a branch in the logic.

## Config

Optional `AI_SKILLS_*` env vars; recommended setup is one line in `~/.zshenv`:

```sh
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
```

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_MR_TOOL` | `gh` | Must be `glab`. Unset or `gh` → stop with a "GitLab-only" message. |
| `AI_SKILLS_LINT_CMD` | _(empty)_ | Lint before committing. Empty → skip with a note. |
| `AI_SKILLS_FORMAT_CMD` | _(empty)_ | Format before committing. Empty → skip with a note. |
| `AI_SKILLS_TEST_CMD` | _(empty)_ | Tests before committing, scoped to touched areas where possible. Empty → skip with a warning. |
| `AI_SKILLS_COMMIT_TRAILER` | _(empty)_ | Trailer appended to commit messages. |

If the session exposes a project test-runner skill (e.g. `pytest-docker`), **invoke it** instead of `AI_SKILLS_TEST_CMD` — it knows the project's tiers and flags.

## Hard rules

- **GitLab only.** `${AI_SKILLS_MR_TOOL:-gh}` must be `glab`; otherwise stop.
- **Branch must be checked out and equal the MR's `source_branch`.** No working tree → stop.
- **Check remote divergence before any verification** (Stage 1). Behind or diverged → stop; the Stage 5 push would fail after all the work.
- **Single open MR.** Several MRs on the branch → auto-pick `state == "opened"`; ask only at 0 or ≥2.
- **Threads sharing an anchor or a fix are one unit** (Stage 1c cluster): verify and fix once, reply and resolve per `discussion_id`.
- **Verification is grouped by file — one sub-agent per file, `model: sonnet`, full verdict space** (Stage 2). Stage 4 edits the repo on the session model.
- **Verify before implementing.** Never apply a request blind — re-read the cited code first. (The `superpowers:receiving-code-review` stance: evaluate, don't perform agreement.)
- **An `invalid` verdict carries its evidence into the write-up.** Dismiss and Push back post to a permanent thread; the user approves each at the Stage 3 gate and needs to see what was read, not a bare "verified invalid".
- **Never use `AskUserQuestion`.** Every question — outward-batch confirmation, clarifications, fallback curation — is plain text with numbered options, then wait. Its countdown assumes a default; a review disposition and an irreversible post must not be answered by a timer.
- **Curation happens in the plannotator gate** (Stage 3a): `plannotator annotate "$FILE" --gate --json` blocks until the user approves, annotates, or closes. `approved` applies every `**Default:**`; `dismissed` aborts; `annotated` overrides per thread.
- **Terminal prompts are the fallback.** Only when `command -v plannotator` fails or the gate returns no payload: print path + counts + table, **end the turn**, then two sequential numbered prompts in a later turn — Fix candidates first, wait, then Push back / Dismiss / Defer.
- **Write-ups go to a file, never the terminal.** Terminal gets the path and counts only.
- **Lint/format/tests are a hard pre-commit gate.** Any failure → STOP before the outward batch.
- **Stage by explicit path, never `git add -A`.** A dirty tree is tolerated at Stage 1; a catch-all add would push the user's unrelated work.
- **Outward actions need one explicit confirmation.** Push + replies + resolves go as one batch after the user sees the exact reply text and resolve flags.
- **Push before reply.** A reply citing `<sha>` waits until the commit is visible. Push fails → abort the batch.
- **Never auto-resolve Push back or Defer.** Only Fix and Dismiss resolve.
- **No performative agreement in replies** — no "thanks" / "good catch" / "you're absolutely right".
- **`Content-Type` header is mandatory** on `glab api ... --input -`; without it GitLab returns HTTP 415. See [references/glab-discussions.md](references/glab-discussions.md).
- **Honor `--dry-run`** (`--dry-run`, or "dry run" in the message): build everything, write **nothing** — no edits, commit, push, or posts. Print planned fixes and reply/resolve payloads as a receipt.

## Workflow

### 1. Detect the MR and fetch the threads

```bash
if [ "${AI_SKILLS_MR_TOOL:-gh}" != "glab" ]; then
  echo "STOP: process-mr-feedback is GitLab-only. Set AI_SKILLS_MR_TOOL=glab to use it."
  exit 1
fi
```

```bash
glab mr view --output json
```

Capture `iid`, `source_branch`, `target_branch`, `web_url`, `project_id`. **`:iid` is not a `glab api` placeholder** — it is sent literally (HTTP 400 `noteable_id is invalid`), so interpolate the real values:

```bash
eval "$(glab mr view --output json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("PID=%s\nIID=%s" % (d["project_id"], d["iid"]))')"
echo "MR !$IID in project $PID"
```

- **Multiple MRs** ("merge request ID number required" + several matches): `glab mr view <iid> --output json` on each; auto-pick the single `state == "opened"`; ask only at 0 or ≥2.
- **Branch mismatch:** current branch ≠ `source_branch` → stop and surface it.
- **Dirty tree:** `git status --porcelain` non-empty → surface before editing; the user decides.
- **Remote divergence** — check before verifying anything:

  ```bash
  git fetch --quiet
  if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    git rev-list --left-right --count '@{u}...HEAD'   # output: <behind>	<ahead>
  else
    echo "no upstream — Stage 5 needs: git push -u <remote> <branch>"
  fi
  ```

  Non-zero **behind** → the Stage 5 push will be rejected. **Stop now**; everything between here and the push would be thrown away. Rebasing or pulling is the user's call. Guard the `@{u}` read as shown — on a never-pushed branch it is a fatal error.

#### 1b. Fetch and filter the threads

Per [references/glab-discussions.md](references/glab-discussions.md): keep only **open, resolvable** threads (skip system notes and resolved threads). One entry per thread:

```
{ discussion_id, author, path, line, body, has_prior_replies, cluster }
```

`cluster` comes from 1c. Anchor: fall back to `old_path`/`old_line` when `new_line` is null — a note on a deleted line is still file-anchored. Only a wholly null `position` (a general MR comment) is "no anchor".

Zero threads → "No open review threads to process." and stop.

`has_prior_replies == true` → flag "has prior discussion" so you don't talk over an exchange in progress.

#### 1c. Cluster threads that share work

- **Same anchor** — identical `path`:`line`.
- **Same fix** — different anchors one edit resolves (one convention broken in three spots).

Verify **once per cluster**, implement **once**, reply to **every** `discussion_id` with its own resolve flag. Otherwise two Stage 4 fixes race on one location and the replies contradict.

**More than ~15 threads** → say so and offer to scope the run (by file, reviewer, or blocking subset) before verifying.

### 2. Verify each finding

**Reviewers — human or LLM — flag things that aren't real, or whose suggested fix wouldn't work.** Don't skip to implementing.

Per cluster:

1. Read the code at `path`:`line` in its **current** state plus enough context to judge. Shifted line → find the equivalent location.
2. Classify:
   - **`valid`** — real and actionable as described.
   - **`invalid`** — a misread, already handled, or not applicable here.
   - **`needs-clarification`** — you can't tell what change is requested.
3. For `valid`, draft a **concrete fix** — the actual change.
4. Default disposition:

   | Validity | Default disposition | Why |
   |---|---|---|
   | `valid` | **Fix** | real + actionable → implement |
   | `invalid` (reviewer is wrong) | **Push back** | reply with reasoning, leave open |
   | `invalid` (already done / N-A) | **Dismiss** | reply noting it's handled, resolve |
   | `needs-clarification` | **Defer** | reply with a question, leave open |

**Mechanism.** Two units stack: a **cluster** is one unit of *verdict*; a **file** is one unit of *dispatch*. Group clusters by `path` and send one read-only sub-agent per file, `model: sonnet`, in parallel (single message, many tool calls).

Per file, not per cluster: it collapses redundant reads, and — more important — **a suggested fix is often only wrong next to another thread on the same file.** One comment being a false positive can be exactly what makes a second comment's cheap remedy wrong. The brief carries every cluster on the file and asks for that interaction (task 4).

Clusters with no `path` get one brief of their own. Cap a wave at ~8 sub-agents; more files → consecutive waves.

No model routing, no cheap tier: every verifier is `sonnet` with the full verdict space, `invalid` included. The Stage 3 gate guards a wrong dismissal — the user approves every Dismiss and Push back before anything posts (see [Why this shape](#why-this-shape)). A cluster that needs other files → the sub-agent reads them; escalating costs more than the read.

#### 2c. Sub-agent brief

One per file, carrying every cluster anchored to it:

```
Verify these MR review comments against the actual code on the current branch.

File: <path>
Oracle: <rule file / lint command / example file that settles a convention question — omit if N/A>

Threads:
  [<n>] Line: <line>   (or "no code anchor" for a general comment)
        Comment: <body>
        Also raised by: <other comments in this cluster, verbatim — omit if the cluster is one thread>
  [<n>] ... (repeat per cluster anchored to this file)

Tasks:
  1. Read the file and the surrounding context for every cited location at the current branch tip
     before judging any of them. If a line has shifted, find the equivalent location.
  2. For each thread, decide whether the issue the comment describes is actually present at (or
     near) the cited location. If an Oracle is given, consult it before judging a convention
     claim — the rule as written wins over your priors about what the convention should be.
  3. If the issue is real, draft the concrete fix. If the comment suggests a fix, judge whether it
     would actually work without introducing a new problem. Where a cluster holds several
     comments, draft ONE fix that satisfies all of them.
  4. Check the threads against each other. Say so explicitly if one comment being wrong, or one
     fix being applied, changes whether another is real or makes another's fix wrong — that
     interaction is invisible to anyone judging a single thread in isolation.

Report per thread, under 150 words each:
  - validity: valid | invalid | needs-clarification — one-sentence reason
  - invalid_kind (only if invalid): reviewer-misread | already-handled | not-applicable
  - evidence (required if invalid): what you read that shows the comment is wrong
  - suggested_fix: the concrete change (only if valid)
  - corrected_anchor: <right path:line if the cited one was wrong>
  - notes: anything else worth knowing, including any interaction found in task 4

Read the actual files at the branch tip — do not parrot the comments back. If judging a thread
needs files beyond this one, read them.
```

`evidence` is what makes an `invalid` verdict curatable at the gate.

#### 2d. Aggregate

One table keyed by `discussion_id`; a cluster's verdict applies to every thread in it. Continue to Stage 3.

### 3. Present the findings, then curate dispositions

The write-up goes to a file; the file opens in plannotator with an Approve button; the call blocks until the user approves, annotates, or closes. Each thread block carries its proposed disposition as a `**Default:**` line, so approving is a deliberate act on a document the user has opened.

Without plannotator, plain-text numbered prompts with a **hard turn boundary**: present, end the turn, ask in a *later* turn. "Before" is a turn boundary, not text order. **`AskUserQuestion` is forbidden on both paths.**

**3a. Analysis turn.**

**Location.** Always a file. `GITDIR="$(git rev-parse --absolute-git-dir)"`, `SLUG="$(git rev-parse --abbrev-ref HEAD | tr '/' '-')"`, write to `$GITDIR/mr-feedback-$SLUG.md` — never committed, invisible to `git status`, isolated per worktree.

The file stands alone. In order:

1. **Meta block** — MR title and number, thread count, any thread with prior replies.
2. **One-line count by verdict** (`valid` / `invalid` / `needs-clarification`).
3. **Overview table** — the scan layer and index. `#` links to the thread's anchor.

   ```
   | # | file:line | author | verdict | proposed disposition | one-line fix summary |
   |---|-----------|--------|---------|----------------------|----------------------|
   | [1](#1--dropdown-click-rewrites-user_roles-on-every-request) | service.py:62 | <user> | valid | Fix | resolve via repo, not transient |
   | [2](#2--x-is-already-async) | routes.py:107 | <user> | invalid | Push back | reviewer misread; X is already async |
   | [3](#3--which-format-is-meant) | (no anchor) | <user> | needs-clarification | Defer | ask which format is meant |
   | [4](#4--semantic-token) | macros.html:14 | <user> | valid | Fix | use semantic token, not text-gray-500 |
   | [5](#5--same-edit-as-4) | macros.html:14 | <other> | valid ↳ #4 | Fix | same edit as #4 |
   ```

   A thread riding another's cluster verdict carries `↳ #<n>` after its verdict. Anchors assume GitHub-style slugs (lowercase, em-dash → double hyphen, spaces → hyphens); if plannotator slugifies differently the links just don't jump.

4. **Cluster sections and thread blocks**:

```markdown
### #1 — dropdown click rewrites user_roles on every request
`valid` · `Fix` · `service.py:62` · `<user>` · verification **anchor drifted to :71**

**Problem.** <What the reviewer flagged and whether it is actually present. Mechanism
only, capped at about 6 lines. Name the exact symbols and cite file:line inline as you
narrate. Weave the verification in as prose — "the anchor had drifted; the call now sits
at :71", "confirmed against the design-book token table", "the reviewer read `close()` as
a hard delete; it soft-closes".>

**Why it bites.** <1–2 sentences: the user-visible consequence, and what fails to catch
it. For an `invalid` verdict this instead says what the reviewer's reading would have cost
if it were true — that is what makes a push-back legible.>

**Fix.** <The concrete change, one bullet per edit if there are several, real code inline.
For Push back / Defer this carries the technical reasoning or the question instead.>

**My read.** <One sentence: fix it / push back / defer, and why — only when the call isn't
already obvious from the block.>

**Default:** fix — annotate this block with `push back`, `dismiss` or `defer` to change it.

---
```

**Rules:**

- **Heading: `### #<n> — <headline>`.** ID plus headline only — outline entry, table link target, annotation anchor.
- **One metadata line under the heading**, `·`-separated: verdict, proposed disposition, anchor as a code span, author, verification delta flag. No line → `(no anchor)`, matching the table.
- **Delta flag: 2–4 words** as `verification **<flag>**` — label plain, flag bold. Typical: verified as claimed, anchor drifted to :<line>, inverted the diagnosis, reviewer misread the call, could not verify; coin one when none fits.
- **`**Problem.**` — mechanism only, ~6 lines**; **`**Why it bites.**` required and separate.** Run-on bold lead-ins, never colon-labelled one-liners, no `**Verification:**` badge line — verification is woven into the prose; the flag only indexes it. `inverted the diagnosis` carries both claim and correction, so ~8 lines; never meet the cap by dropping the correction. No runtime consequence → `**Why it bites.**` names who is misled and when.
- **`**Default:**` is the last line** before the separator: the proposed disposition and the words that override it. The gate reads it. One disposition, three forms — don't swap them:
  - **annotation input** — lowercase text the user types: `fix`, `push back`, `dismiss`, `defer`. Match case-insensitively.
  - **display** — metadata line and table, Title Case with a space: `Fix`, `Push back`, `Dismiss`, `Defer`.
  - **frozen-map key** — single token: `Fix`, `PushBack`, `Dismiss`, `Defer`.

  Only `Push back` vs `PushBack` differs beyond case.
- **Cluster headings** when threads share a mechanism: `## Cluster A — <the mechanism>` plus one line on how they interact. Every thread keeps its own block, metadata line, `**Default:**` and `---`.
- **Flag prior replies** in the block as well as the meta block.

**Terminal gets** the absolute path on its own line and the verdict count. Nothing else.

**Hand it over.** A separate `bash` block doesn't inherit `$GITDIR`/`$SLUG`, so inline the substitutions:

```bash
command -v plannotator >/dev/null &&
plannotator annotate "$(git rev-parse --absolute-git-dir)/mr-feedback-$(git rev-parse --abbrev-ref HEAD | tr '/' '-').md" --gate --json
```

`--gate` adds Approve; `--json` emits the decision on stdout. **Run it with `run_in_background: true`** and poll — the user may take hours, and a foreground call dies at the Bash tool's 10-minute cap.

**Approve discards annotations.** Approve emits a bare `approved` and drops any annotations made first. A user who has marked up a block must submit via the annotation flow. Say this when handing over.

**3b. Read the gate's decision.**

| Decision | Meaning | What you do |
|---|---|---|
| `approved` | Approve clicked | Every `**Default:**` stands — that is the disposition map. |
| `dismissed` | Window closed without approving | **Abort.** No edits, commit, push, replies, or resolves. Say so. |
| `annotated` | Annotations returned | Each overrides the `**Default:**` of the block it anchors to; the rest keep theirs. |
| anything else | Unrecognised / unparseable | **Abort** as `dismissed`. Print what came back. Never freeze defaults on an unreadable answer. |

`annotated` mapping:

- Annotations anchor per block (paragraph, heading, list item); the `### #<n>` heading is the intended target. Map by the `#<n>` token in anchor text or body.
- Vocabulary: `fix`, `push back`, `dismiss`, `defer` — case-insensitive; the map records `Fix | PushBack | Dismiss | Defer`. Any may override any other in either direction.
- Text outside the vocabulary (a question, "the anchor is wrong") applies **nothing**. Answer it, re-open the write-up.
- Can't map to exactly one thread → **ask**. Never guess, never fall back to the default.
- Cluster members riding another's verdict (`↳ #<n>`) follow the annotation on their cluster lead unless they carry their own.

**Print an applied receipt** naming every thread and its final disposition before Stage 4 does anything.

**Silence is never consent.** Only an affirmative payload freezes a disposition map: `approved`, or `annotated`. `dismissed` (`{"decision": "dismissed"}`, or exit 0 with empty output) and any unrecognised payload abort. **No answer at all** — binary missing, browser never opened, non-zero exit with empty stdout, process died before emitting JSON — is the only case that routes to the fallback. *Abort when an answer came back and wasn't approval; fall back only when no answer could be obtained.*

**3b-fallback. No plannotator.** Guard with `command -v plannotator`. Take this path on absence or **no payload**; a non-zero exit that still carried a payload goes through the table above.

1. Print the absolute path, verdict count, overview table, and any prior-replies note. **Then END YOUR TURN** — ask nothing.
2. In a *later* turn, two sequential numbered plain-text prompts — **never `AskUserQuestion`**. Prompt 1: **Fix** candidates only — default all, reply with numbers to drop (or 'all' / 'none'); **wait**. Prompt 2: the remaining Push back / Dismiss / Defer threads to confirm or change; skip if none.
3. One line per thread — `#<id> · <verdict> · file:line`, plus a cluster note where one edit covers several. No fix summary; it's in the file.

```markdown
**Fix candidates** — default is all four. Reply with numbers to drop, or "go".

1. #1 · valid · service.py:62
2. #4 · valid · macros.html:14  (with #5 — one edit)
3. #7 · valid · deur_repository.py:686
4. #9 · valid · handlers.py:2455
```

Numbered text has no 4-option ceiling — one prompt per bucket. Never mix Fix and non-Fix; finish Prompt 1 before Prompt 2. No reply → stop.

**Output of this stage:** a frozen disposition map

```
{ discussion_id -> Fix | PushBack | Dismiss | Defer }
```

plus drafted fix text (Fix) and reply text (every disposition), carried into Stages 4 and 5.

### 4. Implement the Fix threads and commit

Only **Fix** threads; the rest produce replies in Stage 5 and no code change.

**Order:** blocking/correctness → simple (typos, imports, renames) → complex/refactor.

**One edit per cluster**, not per thread — editing one location twice makes the second edit's premise stale.

**Before each edit:** re-read the target file (the verification snapshot may be stale) and **grep for callers before changing any signature or return type**, updating call sites in the same change. One concern at a time so a failure is traceable.

**Track touched files** — the staging step needs the explicit list.

**Pre-commit gate** (portable):

```bash
# Lint + format — empty var means skip (print a note). Use if/else, NOT `[ -n ] && eval || echo`:
# the && … || form routes a FAILING command into the "skipped" branch and exits 0, defeating the gate.
if [ -n "$AI_SKILLS_LINT_CMD" ]; then
  eval "$AI_SKILLS_LINT_CMD"     # non-zero exit = gate failure — STOP
else
  echo "AI_SKILLS_LINT_CMD unset — skipping lint"
fi
if [ -n "$AI_SKILLS_FORMAT_CMD" ]; then
  eval "$AI_SKILLS_FORMAT_CMD"   # non-zero exit = gate failure — STOP
else
  echo "AI_SKILLS_FORMAT_CMD unset — skipping format"
fi
```

**Tests**: invoke the project test-runner skill if the session has one (e.g. `pytest-docker`); otherwise:

```bash
if [ -n "$AI_SKILLS_TEST_CMD" ]; then
  eval "$AI_SKILLS_TEST_CMD"     # non-zero exit = gate failure — STOP
else
  echo "WARNING: AI_SKILLS_TEST_CMD unset — tests NOT run"
fi
```

**Any failure → STOP.** Nothing is pushed, replied, or resolved on a red tree.

**Stage explicitly, commit locally.** File-tool edits are not staged — a bare `git commit` exits 1 with "no changes added to commit". Never `git add -A`/`-u`: dirty work tolerated at Stage 1 would land in a commit Stage 5 pushes.

```bash
git add <file1> <file2>          # explicit paths only — the files your fixes touched
git status --short               # confirm nothing unrelated is staged

MSG="fix: address review feedback on <area>"
if [ -n "$AI_SKILLS_COMMIT_TRAILER" ]; then
  MSG="$MSG

$AI_SKILLS_COMMIT_TRAILER"
fi
git commit -m "$MSG"
```

One commit, or a few logical ones if the fixes are independent; the message references what the threads asked for.

**Don't push yet** — that's the Stage 5 batch. After each commit record `discussion_id -> sha` for every thread it satisfies:

```bash
git rev-parse --short HEAD
```

With several commits, a Fix reply must cite the commit that actually contains its change — build the map as you commit, don't reach for `HEAD` at Stage 5.

**`--dry-run`:** no edits, no commit. Describe per Fix thread the change you *would* make and the SHA placeholder the reply would cite.

### 5. Push, reply, resolve (the outward batch)

Everything reviewers can see goes out together behind **one** confirmation.

**The gate.** In a turn that **ends**, show the push target (branch → remote) and, per thread, the disposition, the **verbatim reply text**, and whether it will be resolved. Ask in plain text for one confirmation of the whole batch — not per thread. This gates posts and resolves on someone else's MR, which is why it is text, not `AskUserQuestion`. **No reply means no batch**; a message about something else is not consent.

**On confirm, in order:**

1. **Push — only if Stage 4 produced a commit.**

   ```bash
   git push                              # or: git push -u <remote> <branch> if there is no upstream
   ```

   Must succeed before any reply — a reply citing `<sha>` is useless until the commit is visible. **Push fails → abort the batch**, post nothing, report it as the remote having moved since Stage 1; never force-push past it.

   No Fix threads (no commit) → skip the push, go to replies; none cites a `<sha>`.

2. **Reply + resolve per thread**, per [references/glab-discussions.md](references/glab-discussions.md) (reply = `POST .../discussions/<id>/notes` with the `Content-Type` header; resolve = `PUT .../discussions/<id>?resolved=true`):

   | Disposition | Reply | Resolve? |
   |---|---|---|
   | **Fix** | `Fixed in <sha>: <one line>` — `<sha>` from the Stage 4 map, not a blanket `HEAD` | **yes** |
   | **Dismiss** | the reasoning (already handled / not applicable) | **yes** |
   | **Push back** | the technical reasoning for disagreeing | **no — leave open** |
   | **Defer** | acknowledgement + that it's tracked as follow-up | **no — leave open** |

   **Every clustered thread gets its own reply**, citing the same sha and naming the shared edit — one reply on the "primary" leaves the others resolved without explanation.

   Prefer one `python3` helper looping over `(discussion_id, body, resolve?)`; capture each note id for the receipt.

3. **Outcome table:**

   ```
   | # | discussion_id | disposition | replied? | resolved? | note_url |
   |---|---------------|-------------|----------|-----------|----------|
   ```

**Reply tone:** state the fix or the reasoning — no "thanks" / "good catch" / "you're absolutely right".

**`--dry-run`:** push nothing, POST/PUT nothing. Print the push target and every payload as a receipt, e.g. `[DRY-RUN] Would push <branch> and reply+resolve N threads on MR !<iid>`.

## Failure modes

- **`AI_SKILLS_MR_TOOL` is not `glab`** — stop; GitLab-only.
- **`glab mr view` returns nothing** — no MR; stop.
- **Current branch ≠ `source_branch`** — stop and surface it; switching is the user's call.
- **Multiple or zero open MRs** — auto-pick the single `state=="opened"`; ask only at 0 or ≥2.
- **`HTTP 400 {"error":"noteable_id is invalid"}`** — `:iid` left in the path; `glab` substitutes only repo-scoped placeholders. Interpolate the real iid.
- **Branch behind the remote** — stop at Stage 1; everything up to the push would be discarded.
- **Zero qualifying threads** — "nothing to process"; stop.
- **Several threads on one anchor** — cluster (1c); separate fixes race and replies contradict.
- **`git commit` says "no changes added to commit"** — never staged. Stage by explicit path.
- **A verifier judged a convention claim from its priors** — supply the `Oracle:` line (rule file, lint command, example file) and re-run that file's brief.
- **`needs-clarification`** — never guess. Defer with a drafted question; leave open.
- **An `invalid` verdict with no `evidence`** — not curatable. Re-verify yourself before it reaches the disposition map; a Dismiss posts and resolves.
- **Lint / format / tests fail** — STOP before the outward batch.
- **Thread has prior replies** — flag "has prior discussion"; let the user decide whether to add to it.
- **Push fails at the gate** — abort the batch so no reply cites an invisible SHA.
- **No answer from the gate, a fallback prompt, or the batch confirmation** — stop, leave state as is; the run resumes when the user answers. `dismissed` and an unrecognised payload count as "no answer" here; a gate with no payload at all routes to the fallback prompts instead.
- **Dirty tree at start** — surface it before editing; the user decides.

## Why this shape

**Verification is for the remedies, not the verdicts.** Reviewers rarely flag things that aren't real, but often propose fixes that don't fit — a precedent that doesn't exist, a fix that works at the anchor and breaks a caller. Across this project's processed MRs `invalid` fires on a small minority of human threads; corrected remedies run several times higher. Stage 2 hands the user a checked fix, and the read it takes is the one Stage 4 needs anyway.

**One verifier per file.** Per-thread dispatch re-read each file N times and hid the interaction that matters most: a false positive on one comment is often what makes a neighbour's suggested fix wrong. Grouping by file is cheaper and more accurate, so there is no per-thread path.

**Flat `sonnet`, full verdict space — the gate is the guard.** An earlier version routed across `haiku`/`sonnet`/session and forbade cheap tiers from returning `invalid`, since a wrong dismissal auto-resolves a real finding. True, but redundant: no `invalid` reaches GitLab without the user approving it at the Stage 3 gate, where silence is never consent. What that machinery protected survives as one required field — `evidence` — so the person approving a Dismiss can judge it.

**The gate replaces a turn break that was only a proxy.** When analysis and question shared a turn, the user curated findings they'd never read. A stopped turn proves the assistant stopped talking; a blocking `plannotator annotate --gate` can't return until they've been in the document, and each disposition sits in its own block instead of a list three screens away. On the fallback path the turn break still carries the whole guarantee.

**Text, never `AskUserQuestion`.** Its countdown assumes a default on expiry, and the decisions here are "close a reviewer's finding" and "post on a shared MR". Its labels also truncate; numbered text has no 4-option ceiling, so a 12-thread Fix list is one prompt.

**Push before reply** keeps every `Fixed in <sha>` link valid; aborting on push failure guarantees the citation resolves.

**Resolve only Fix and Dismiss.** Resolving signals the matter is closed. A disagreement or deferral isn't; auto-resolving would end a conversation the reviewer never agreed to end.
