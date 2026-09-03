---
name: process-mr-feedback
description: "Use when processing review feedback on a GitLab merge request you have checked out — working through the unresolved discussion threads a reviewer (a human, or an automated/LLM diff-note) left on the MR. Triggers on phrases like 'process the review feedback', 'address the MR comments', 'work through the review threads', 'handle the review comments'. GitLab-only (requires `glab`). Refuses if the MR's source branch isn't currently checked out, because it needs a working tree to verify findings and apply fixes."
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
- **Check remote divergence before spending any verification.** Fetch and compare `HEAD` to `@{u}` in
  Stage 1. If the branch is behind or diverged, stop there — the Stage 5 push would fail after all
  the work is already done.
- **Single open MR.** If the branch resolves to multiple MRs, auto-pick the one with
  `state == "opened"`; ask only if 0 or ≥2 are open.
- **Threads sharing an anchor or a fix are one unit of verification.** Cluster them in Stage 1c;
  verify and fix once per cluster, but reply and resolve per `discussion_id`.
- **Verify before implementing.** Never apply a reviewer's request blind — re-read the cited code
  and judge whether the finding is real first. (This is the `superpowers:receiving-code-review`
  stance: evaluate, don't perform agreement.)
- **A cheap tier can never dismiss a finding.** `haiku`/`sonnet` verifiers return
  `valid | needs-clarification | escalate` only. Every `invalid` verdict — and therefore every
  Dismiss and Push back — must come from the session model. Security-flavoured findings never route
  below the session model at all.
- **Model routing applies to verification only.** Stage 4 implements fixes and edits the repo; it
  always runs on the session model.
- **Never use `AskUserQuestion`.** Every choice this skill puts to the user in the terminal — the outward
  batch confirmation, any clarification, and curation on the fallback path — is **plain text with numbered options**, then wait for a
  reply. The tool runs a countdown and assumes a default when it expires; a review disposition and an
  irreversible post are exactly the decisions that must not be answered by a timer.
- **Curation happens in the plannotator gate.** Stage 3a writes the write-up to a file and opens
  it with `plannotator annotate "$FILE" --gate --json`, which blocks until the user approves,
  annotates, or closes. `approved` applies every block's `**Default:**`; `dismissed` aborts and
  touches nothing; `annotated` overrides per thread. The blocking call *is* the read gate.
- **The terminal prompts are the fallback, not the primary path.** When `command -v plannotator`
  fails or the gate produces no payload, fall back to: print path + counts + table, **end the turn**,
  then two sequential numbered text prompts in a later turn — Fix candidates first, wait, then
  Push back / Dismiss / Defer. Never one combined list, and never `AskUserQuestion`.
- **Write-ups always go to a file, never the terminal**, whatever the thread count (Stage 3a).
  The document carries the counts, the overview table and every thread write-up; the terminal
  carries the path and the counts only.
- **Lint/format/tests are a hard pre-commit gate.** If any fail, STOP before the outward batch —
  nothing is pushed, replied, or resolved on top of a red tree.
- **Stage by explicit path, never `git add -A`.** A dirty tree is tolerated at Stage 1, so a
  catch-all `add` would sweep the user's unrelated work into a review-fix commit that Stage 5 pushes.
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

Capture `iid`, `source_branch`, `target_branch`, `web_url`, and the project id (`project_id`). Hold
the iid and project id in shell vars — **`:iid` is not a `glab api` placeholder** and is sent to the
server literally (HTTP 400 `noteable_id is invalid`), so every endpoint path needs the real value
interpolated:

```bash
eval "$(glab mr view --output json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("PID=%s\nIID=%s" % (d["project_id"], d["iid"]))')"
echo "MR !$IID in project $PID"
```

- **Multiple MRs on the branch:** if `glab mr view` errors with "merge request ID number required"
  + several matches, call `glab mr view <iid> --output json` on each and **auto-pick the single one
  with `state == "opened"`**. Stop and ask only if 0 or ≥2 are open.
- **Branch mismatch:** confirm the current branch equals `source_branch`. If not, stop and surface
  the mismatch — switching branches is the user's call, not yours.
- **Dirty working tree:** if `git status --porcelain` is non-empty, surface it before making any
  edits and let the user decide whether to proceed.
- **Remote divergence:** check this *before* verifying anything.

  ```bash
  git fetch --quiet
  if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    git rev-list --left-right --count '@{u}...HEAD'   # output: <behind>	<ahead>
  else
    echo "no upstream — Stage 5 needs: git push -u <remote> <branch>"
  fi
  ```

  A non-zero **behind** count means the remote branch has commits you don't — the Stage 5 `git push`
  will be rejected. **Stop and surface it now**, because everything between here and the push
  (verification, curation, edits, the test gate) would be thrown away. Rebasing or pulling is the
  user's call, not yours. Guard the `@{u}` read as shown: on a never-pushed branch it is a fatal
  error, not an empty result.

#### 1b. Fetch and filter the threads

Per [references/glab-discussions.md](references/glab-discussions.md): keep only **open, resolvable**
threads (skip system notes and already-resolved threads). Build an in-memory list, one entry per
thread:

```
{ discussion_id, author, path, line, body, has_prior_replies, tier, cluster }
```

`tier` is filled in below (Stage 2a routes on the body you have already read here); `cluster` comes
from Stage 1c. For the anchor, fall back to `old_path`/`old_line` when `new_line` is null — a note on
a **deleted** line is still anchored to a file, and treating it as anchorless throws that away. Only a
wholly null `position` (a general MR comment) counts as "no anchor".

**If zero threads qualify**, report "No open review threads to process." and stop — there is nothing
to do.

For any thread with `has_prior_replies == true`, flag it ("has prior discussion") so you don't talk
over an exchange that's already in progress.

#### 1c. Cluster threads that share work

Reviewers routinely leave several threads on one location or one recurring pattern. Group before
verifying:

- **Same anchor** — identical `path`:`line` (two reviewers, or one reviewer commenting twice).
- **Same fix** — different anchors that one edit resolves (the same convention broken in three spots
  in one file).

Verify **once per cluster** and implement **once**, then reply to **every** `discussion_id` in the
cluster and apply each thread's own resolve flag. Without this, two Stage 4 fixes race on one
location — the second fix's premise is stale the moment the first lands — and the replies can
contradict each other.

**If more than ~15 threads qualify**, say so and offer to scope the run (by file, by reviewer, or by
the blocking subset) before verification starts. Nothing breaks at higher counts; it just spends a lot
of verification and produces a long curation sequence, so the user should get the choice.

### 2. Verify each finding

This is the core of the skill: **reviewers — human or LLM — flag things that aren't real, or whose
suggested fix wouldn't work.** Do not skip to implementing.

For each cluster (a single thread, or several that share a fix — see Stage 1c):

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

**Mechanism / scale.** The unit here is the **cluster** from Stage 1c, not the raw thread — a cluster
of three threads on one line gets one verification, and its verdict applies to all three.

For **≤3 clusters**, verify inline on the session model — dispatch overhead outweighs any saving at
that size, so no routing applies. For **4+**, dispatch one read-only verification sub-agent per
cluster **in parallel** (a single message with many tool calls) and aggregate. **Cap each wave at ~8
sub-agents**; with more clusters than that, send consecutive waves rather than one enormous message.

#### 2a. Model routing (the 4+ path only)

Assign a tier **while building the thread list in Stage 1** — you have already read every body there,
so no separate triage hop is warranted. A cluster takes the **highest** tier any of its threads earns:
one member with a security flavour or prior replies pulls the whole cluster to the session model. Pass
the tier as the `model` param on the `Agent` call; it takes precedence over any agent-definition
frontmatter. Omit it to inherit the session model.

Route on **scope of evidence needed**, not on how hard the wording sounds. A one-sentence comment can
be a single-line grep or a six-file read; the read cost is the signal.

| Tier | Route when | Typical findings |
|---|---|---|
| `haiku` | single anchor, textual/mechanical, no cross-file reasoning, **and you can name the oracle** (see below) | typo, import order, naming convention, hard-coded color instead of a semantic token, ticket ref in a docstring, missing type hint, stale comment |
| `sonnet` | one file, local logic, at most one grep to confirm | off-by-one, wrong exception tier, missing `await`, a whole route body wrapped in `try/except Exception`, unescaped template interpolation *(unless it is a security finding — see overrides)* |
| _omit (session model)_ | multi-file, semantic, or high-stakes | layering / aggregate-boundary questions, temporal `as_of` threading, cascade-closure correctness, N+1 across repositories, exploitability, or a reviewer disputing a design decision |

**Overrides — these force the session model regardless of the table:**

- `has_prior_replies == true`. You are entering a live exchange, not judging a fresh claim.
- No `path`/`line` anchor. A general comment has no bounded scope to check cheaply.
- **Security-flavoured findings** — authn/authz, injection, XSS or an unencoded sink, CSRF, secret
  handling, path traversal, SSRF, deserialization. These often *look* mechanical (a one-line grep),
  which is exactly why the table would misroute them. A cheap tier may waste your curation time; it
  may not clear a security finding.

**The oracle requirement (`haiku` only).** A convention finding — "use a semantic token", "this
naming is wrong", "no ticket refs in docstrings" — is only mechanical *if the rule is written down
somewhere the verifier can read*. A `haiku` verifier told nothing but the comment judges from its
priors, and a confident prior-based `valid` is the verdict this skill trusts most (it flows straight
to Fix with no re-check). So: **name the oracle in the brief or don't route it to `haiku`.** Fill the
brief's `Oracle:` line with the concrete rule source — a project rule file (`CLAUDE.md`, a design-book
doc, a conventions table), a lint/hook command the verifier can run, or a sibling file that
demonstrates the convention. No oracle available → route to `sonnet` and tell it to locate the rule
itself before judging.

#### 2b. Verdict permissions — cheap tiers cannot dismiss

**A `haiku` or `sonnet` verifier may return only `valid`, `needs-clarification`, or `escalate`. It
may NOT return `invalid`.** A cheap tier that leans invalid returns `escalate` with its reasoning;
you then re-verify that thread on the **session model**, and only that verdict may become a
**Dismiss** or **Push back**.

The asymmetry is the point. A wrong `valid` costs you curation time — you read the proposed fix in
Stage 3 and drop it. A wrong `invalid` maps to Dismiss, which **auto-resolves the thread** in Stage 5
and closes a real finding with a confident reply on it. Restricting the weak tier's *verdict space*
gives a floor on the damage without needing to predict which threads it will get right.

#### 2c. Sub-agent brief

Two substitutions per tier — do **both**, or the brief contradicts itself:

| Placeholder | `haiku` / `sonnet` | session model |
|---|---|---|
| `<VERDICT_SPACE>` | `valid \| needs-clarification \| escalate` | `valid \| invalid \| needs-clarification` |
| `<KIND_LINE>` | `escalate_kind (only if escalate): looks-wrong \| looks-already-handled \| looks-not-applicable \| needs-wider-context \| unsure` | `invalid_kind (only if invalid): reviewer-misread \| already-handled \| not-applicable` |

The `<KIND_LINE>` substitution is what keeps the cheap tier from smuggling a dismissal back in through
a field name — an `invalid_kind` prompt asks for exactly the conclusion Stage 2b forbids, and its
`escalate_kind` form is what the session-model re-verify actually needs to pick up the thread.

```
Verify this MR review comment against the actual code on the current branch.

Thread:
  File: <path>
  Line: <line>   (or "no code anchor" for a general comment)
  Comment: <body>
  Also raised by: <other comments in this cluster, verbatim — omit the line if the cluster is one thread>
  Oracle: <rule file / lint command / example file that settles a convention question — omit if N/A>

Tasks:
  1. Read the cited file and surrounding context at the current branch tip. Decide whether the
     issue the comment describes is actually present at (or near) the cited location. If the line
     has shifted, find the equivalent location.
  2. If an Oracle is given, consult it before judging a convention claim — the rule as written wins
     over your priors about what the convention should be.
  3. If the issue is real, draft the concrete fix. If the comment suggests a fix, judge whether it
     would actually work without introducing a new problem. Where the cluster holds several comments,
     draft ONE fix that satisfies all of them.

Report (under 150 words):
  - validity: <VERDICT_SPACE> — one-sentence reason
  - <KIND_LINE>
  - suggested_fix: the concrete change (only if valid)
  - corrected_anchor: <right path:line if the cited one was wrong>
  - notes: anything else worth knowing

Read the actual files at the branch tip — do not parrot the comment back.
```

Append this to a `haiku` or `sonnet` brief:

```
You may NOT return "invalid". If you believe the comment is wrong, already handled, or not
applicable, return validity: escalate and explain your reasoning — a stronger model will make that
call. Return escalate also if judging this needs files beyond the one cited, or if you are unsure.
```

#### 2d. Aggregate

Re-verify every `escalate` on the session model, then fold those verdicts in. Aggregate into a
single table keyed by `discussion_id`, carrying the tier that produced each verdict (and `escalated`
where it applies), then continue to Stage 3.

### 3. Present the findings, then curate dispositions

**Curation runs through the plannotator gate.** The write-up goes to a file, the file opens in
the annotation UI with an Approve button, and the call blocks until the user approves,
annotates, or closes it. Each thread block carries its own proposed disposition as a
`**Default:**` line, so approving is a deliberate act on a document the user has necessarily
opened — not a reply to a prompt they may not have read.

When plannotator is unavailable the skill falls back to plain-text numbered prompts, and there
the **hard turn boundary** still applies: you present, your turn ends, and only in a *later*
turn do you ask. Analysis and question in one turn means the question gets answered before the
analysis is read. "Before" means a turn boundary, not text order. **`AskUserQuestion` is
forbidden on both paths** — its countdown assumes a default when it expires, which is the
wrong failure mode for "which of these reviewer findings do I act on".

**3a. Analysis turn.**

**Where it goes.** Always a file, never the terminal — regardless of thread count. Resolve
`GITDIR="$(git rev-parse --absolute-git-dir)"` and
`SLUG="$(git rev-parse --abbrev-ref HEAD | tr '/' '-')"`, then write the whole write-up to
`$GITDIR/mr-feedback-$SLUG.md`. Inside the git dir the file is never committed, never
appears in `git status`, and is isolated per worktree — no `.gitignore` edit needed, in any
repo.

The file must stand alone. Content, in this order:

1. **The meta block** — MR title and number, thread count, and a note naming any thread
   that already carries prior replies.
2. **A one-line count by verdict** (`valid` / `invalid` / `needs-clarification`).
3. **The overview table** — the scan layer, and the file's index. It comes before the
   detail it indexes because a file is read from the top. The `#` cell links to the
   thread's anchor.

   ```
   | # | file:line | author | tier | verdict | proposed disposition | one-line fix summary |
   |---|-----------|--------|------|---------|----------------------|----------------------|
   | [1](#1--dropdown-click-rewrites-user_roles-on-every-request) | service.py:62 | <user> | sonnet | valid | Fix | resolve via repo, not transient |
   | [2](#2--x-is-already-async) | routes.py:107 | <user> | haiku→session | invalid | Push back | reviewer misread; X is already async |
   | [3](#3--which-format-is-meant) | (no anchor) | <user> | session | needs-clarification | Defer | ask which format is meant |
   | [4](#4--semantic-token) | macros.html:14 | <user> | haiku | valid | Fix | use semantic token, not text-gray-500 |
   | [5](#5--same-edit-as-4) | macros.html:14 | <other> | ↳ #4 | valid | Fix | same edit as #4 |
   ```

   The `tier` column exists so you can see what judged what — an `invalid` verdict must
   always show `session` or `<cheap>→session`, never a bare cheap tier. Write `inline` for
   the ≤3-cluster path, and `↳ #<n>` for a thread that rides another thread's cluster
   verdict.

   The `#` anchors assume GitHub-style heading slugs (lowercase, em-dash dropped leaving a
   double hyphen, spaces → hyphens). If plannotator's renderer slugifies differently the
   links just don't jump — navigation only, never content — and the block sits right under
   the table regardless.

4. **Cluster sections and thread blocks** — the detail layer, in the shape below.

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

- **The heading is `### #<n> — <headline>`.** ID plus headline, nothing else. It has to work
  as an editor outline entry, as a link target for the overview table, and as the block a
  decision annotation attaches to.
- **One metadata line directly under the heading**, `·`-separated: verdict, proposed
  disposition, anchor as a code span, thread author, verification delta flag. The
  disposition belongs here because the reader needs to know what is proposed while reading
  the thread, not only when they reach the table. A thread with no line to point at
  carries `(no anchor)`, matching what the overview table's `file:line` column already
  does.
- **The verification delta flag is 2–4 words**, rendered as `verification **<flag>**` —
  the label `verification` in plain text and the flag itself in bold, exactly as the
  block above shows. The flag is typically one of: verified as claimed, anchor drifted to
  :<line>, inverted the diagnosis, reviewer misread the call, could not verify — write a
  2–4 word flag of your own when none fits.
- **`**Problem.**` is mechanism only, capped at about 6 lines**; `**Why it bites.**` is
  required and separate. Run-on bold lead-ins, never colon-labelled one-liners, and no
  `**Verification:**` badge line — verification is woven into the prose, and the metadata
  flag is only an index into it. Two consequences of that: a thread flagged `inverted the
  diagnosis` carries both the reviewer's claim and the correction, so about 8 lines is its
  honest ceiling — never buy the cap by dropping the correction, which the no-badge rule
  gives no other home; and when a thread has no runtime consequence, `**Why it bites.**`
  names who is misled and when rather than inventing a failure mode.
- **`**Default:**` is the last line of every block**, before the separator, and states the
  proposed disposition plus the words that override it. This is what the curation gate
  reads. The same disposition takes three distinct forms, each with one job — do not swap
  them:
  - **annotation input** — the literal lowercase text a user types onto a block: `fix`,
    `push back`, `dismiss`, `defer`. Match it case-insensitively.
  - **display** — the metadata line and overview table, Title Case with a space:
    `Fix`, `Push back`, `Dismiss`, `Defer`.
  - **frozen-map key** — Stage 3's disposition map, a single token, no space:
    `Fix`, `PushBack`, `Dismiss`, `Defer`.

  Only `Push back` (display) vs `PushBack` (key) differ between the forms; the other three
  disposition words are spelled identically apart from case.
- **Cluster headings** when several threads share one mechanism: `## Cluster A — <the
  mechanism>` plus a one-line note on how they interact. Every thread inside still gets its
  own block, its own metadata line, its own `**Default:**` and its own `---` — a cluster
  heading is not licence to merge them into one paragraph.
- **Flag any thread with prior replies** in its block as well as in the meta block.

**What the terminal gets.** The absolute write-up path on its own line, and a one-line count by
verdict. Nothing else — no write-ups, no excerpts, no table.

**Hand it over.** Open the write-up in the annotation UI and block on it. The command
re-derives the path itself — a separate `bash` block does not inherit `$GITDIR`/`$SLUG`
from the resolve above (only files cross blocks), so inline the substitutions:

```bash
command -v plannotator >/dev/null &&
plannotator annotate "$(git rev-parse --absolute-git-dir)/mr-feedback-$(git rev-parse --abbrev-ref HEAD | tr '/' '-').md" --gate --json
```

`--gate` adds the Approve button; `--json` emits the decision on stdout. The call blocks until
the user approves, annotates, or closes the window, which is what makes it a read gate.

**Run it in the background.** The gate can block far longer than the Bash tool's 10-minute
foreground cap — reviewing async, the user may not get to it for hours — and a foreground call
is killed at that cap. Launch this block with `run_in_background: true`, then poll its output
and read the decision JSON once it exits. Never run it inline.

**Approve discards annotations.** Clicking Approve emits a bare `approved` payload; if the
user annotated blocks first, plannotator drops those annotations before the skill sees them.
A user who has marked up any block must submit via the annotation flow, not Approve — Approve
means "every default stands, untouched." Say this when you hand the write-up over.

**3b. Read the gate's decision.**

| Decision | Meaning | What you do |
|---|---|---|
| `approved` | Approve was clicked | Every `**Default:**` stands. That is the disposition map. |
| `dismissed` | The window was closed without approving | **Abort.** No edits, no commit, no push, no replies, no resolves. Say so and stop. |
| `annotated` | Annotations came back | Each annotation overrides the `**Default:**` of the thread whose block it anchors to. Unannotated threads keep theirs. |
| anything else | An unrecognised or unparseable payload | **Abort**, exactly as `dismissed`. Print what came back and stop. Never fall through to freezing the defaults into a disposition map — an unreadable answer is not an affirmative one. |

Mapping rules for `annotated`:

- Annotations anchor per block — plannotator's annotatable nodes are paragraphs, headings and
  list items — so the thread's `###` heading is the intended target. Map an annotation to a
  thread by the `#<n>` token in its anchor text or its body.
- The vocabulary is `fix`, `push back`, `dismiss`, `defer`, matched **case-insensitively** —
  those are the lowercase words a user types, while the disposition map you freeze records
  `Fix | PushBack | Dismiss | Defer`. Any of the four may override any other, in either
  direction — a proposed `Fix` can become a `Defer`, a proposed `Push back` can become a
  `Fix`.
- An annotation whose text is **not** in that vocabulary — a question, "explain more", "the
  anchor is wrong" — applies **nothing** for that thread. Answer it, then re-open the write-up.
- If an annotation cannot be mapped to exactly one thread, **ask**. Never guess, and never
  quietly fall back to the default.
- Cluster members that ride another thread's verdict (`↳ #<n>` in the table) follow the
  annotation on their cluster lead unless they carry one of their own.

**Print an applied receipt** naming every thread and its final disposition before Stage 4 does
any work.

**Silence is never consent.** The only thing that freezes a disposition map is an affirmative
payload — `approved`, or `annotated`, where each annotation overrides its own block and the
rest keep their defaults. Every other answer aborts: `dismissed` (which plannotator signals as
`{"decision": "dismissed"}`, or as exit 0 with empty output), and any unrecognised or
unparseable payload. A failure that yields **no answer at all** is the different case — the
binary missing, the browser never opening, a non-zero exit with nothing on stdout, the process dying before it emits
JSON — and only that routes to `3b-fallback` below, where the user answers in the terminal
instead. **Abort when an answer came back and was not an approval; fall back only when no
answer could be obtained.**

**3b-fallback. No plannotator.** Guard the gate with `command -v plannotator`. Use this path when
the binary is absent, or when the invocation produces **no payload** — a launch failure, a missing
browser, a process that dies before emitting JSON. A non-zero exit that still carried a payload is
not a fallback case: read it through the decision table above, where anything unrecognised aborts.
When you take this path, do this instead:

1. Print the absolute file path, the verdict count, the overview table and any note about
   threads carrying prior replies. **Then END YOUR TURN** — ask nothing in that turn.
2. In a *later* turn, send two sequential plain-text prompts with numbered options — **never
   `AskUserQuestion`**. Prompt 1 is the **Fix** candidates only: state the default plainly
   ("all of them unless you say otherwise") and how to subtract ("reply with the numbers to
   drop, or 'all' / 'none'"); **wait for the answer**. Prompt 2 is the remaining Push back /
   Dismiss / Defer threads so the user can confirm or change each disposition; skip it if
   there are none.
3. Keep each line minimal — `#<id> · <verdict> · file:line`, plus a cluster note where one
   edit covers several threads. Do not repeat the fix summary; it is in the file.

```markdown
**Fix candidates** — default is all four. Reply with numbers to drop, or "go".

1. #1 · valid · service.py:62
2. #4 · valid · macros.html:14  (with #5 — one edit)
3. #7 · valid · deur_repository.py:686
4. #9 · valid · handlers.py:2455
```

Numbered text has no 4-option ceiling, so a long bucket stays one prompt — do not fragment it.
Never mix Fix and non-Fix in one prompt, and finish Prompt 1 before Prompt 2.
**Silence is not an answer** — if the reply doesn't come, stop; do not assume the default
and proceed.

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

**One edit per cluster, not per thread.** Threads grouped in Stage 1c share a fix; implement it once.
Editing the same location twice makes the second edit's premise stale and can revert the first.

**Before each edit:** re-read the target file (don't trust the verification snapshot — it may be
stale), and **grep for callers before changing any signature or return type**, updating call sites
in the same change. Make **one concern at a time** so a later failure is traceable to a specific
fix.

**Track the files you touch** as you go — the staging step below needs the explicit path list.

**Pre-commit verification gate** (portable — no hardcoded toolchain):

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

For **tests**: if the session exposes a project test-runner skill (e.g. a `pytest-docker`-style
skill), invoke that skill instead of running the raw command. Otherwise:

```bash
if [ -n "$AI_SKILLS_TEST_CMD" ]; then
  eval "$AI_SKILLS_TEST_CMD"     # non-zero exit = gate failure — STOP
else
  echo "WARNING: AI_SKILLS_TEST_CMD unset — tests NOT run"
fi
```

**If lint, format, or tests fail → STOP here.** Surface the failure and do **not** proceed to the
outward batch. Nothing is pushed, replied, or resolved on top of a red tree.

**Stage explicitly, then commit locally.** Edits made with the file tools are **not** staged, so a
bare `git commit` exits 1 with "no changes added to commit". Stage the fix files **by path** — never
`git add -A`/`-u`, which would sweep any pre-existing dirty work (tolerated at Stage 1) into a commit
Stage 5 pushes:

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

One commit, or a few logical commits if the fixes are genuinely independent. Reference what the
threads asked for in the message.

**Do not push yet** — push is part of the gated outward batch (Stage 5). After each commit, record
`discussion_id -> sha` for every thread that commit satisfies:

```bash
git rev-parse --short HEAD
```

With several commits, one sha does **not** cover every thread — a Fix reply must cite the commit that
actually contains its change, so build the map as you commit rather than reaching for `HEAD` at
Stage 5.

**Under `--dry-run`:** make no edits and no commit. Instead describe, per Fix thread, the change you
*would* make and the SHA placeholder the reply would cite.

### 5. Push, reply, resolve (the outward batch)

Everything reviewers can see goes out together, behind **one** confirmation.

**The gate.** In an assistant turn that **ENDS**, show:

- the push target (branch → remote), and
- per thread: disposition, the **verbatim reply text** that will be posted, and whether the thread
  will be resolved.

Then ask, **in plain text**, for a single confirmation to send the whole batch, and end the turn. Do
not split this into per-thread prompts — the user approves the batch once. This confirmation gates
posts and resolves on someone else's MR: it must never be answered by a countdown expiring, which is
why it is text and not `AskUserQuestion`. **No reply means no batch** — never treat silence, or a
message about something else, as consent.

**On confirm, in order:**

1. **Push the branch — only if Stage 4 produced a commit.**

   ```bash
   git push                              # or: git push -u <remote> <branch> if there is no upstream
   ```

   The push must succeed before any reply is posted — a reply citing `<sha>` is useless until the
   commit is visible to reviewers. **If the push fails, abort the reply/resolve batch** and report;
   post nothing. A rejection here means the remote moved after the Stage 1 divergence check — report
   it as such; do not force-push to get past it.

   If **no thread was dispositioned Fix** (so Stage 4 made no commit), there is nothing to make
   visible — skip the push and go straight to the replies. No reply in this case cites a `<sha>`.

2. **Reply + resolve per thread**, using the recipes in
   [references/glab-discussions.md](references/glab-discussions.md) (reply = `POST
   .../discussions/<id>/notes` with the `Content-Type` header; resolve = `PUT
   .../discussions/<id>?resolved=true`):

   | Disposition | Reply | Resolve? |
   |---|---|---|
   | **Fix** | `Fixed in <sha>: <one line>` — `<sha>` from the Stage 4 `discussion_id -> sha` map, not a blanket `HEAD` | **yes** |
   | **Dismiss** | the reasoning (already handled / not applicable) | **yes** |
   | **Push back** | the technical reasoning for disagreeing | **no — leave open** |
   | **Defer** | acknowledgement + that it's tracked as follow-up | **no — leave open** |

   **Clustered threads each get their own reply**, citing the same sha and naming the shared edit —
   one reply on the cluster's "primary" thread leaves the others silently resolved with no
   explanation.

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
- **`HTTP 400 {"error":"noteable_id is invalid"}`** — you left `:iid` in the endpoint path. `glab`
  substitutes only repo-scoped placeholders; interpolate the real iid.
- **Branch is behind the remote** — stop at Stage 1. Verification, edits and the test gate would all
  be discarded when the Stage 5 push is rejected.
- **Zero qualifying threads** — report "nothing to process" and stop.
- **Several threads on one anchor** — cluster them (Stage 1c). Verifying and fixing each separately
  burns tokens, makes the second fix's premise stale, and can produce contradicting replies.
- **`git commit` says "no changes added to commit"** — the fixes were never staged. Stage by explicit
  path; `git add -A` would also drag in unrelated dirty work.
- **A convention finding routed to `haiku` with no oracle** — it judged from priors, not from the
  rule. Re-route to `sonnet` or supply the rule source in the brief.
- **`needs-clarification` finding** — never silently guess what the reviewer meant. Default to
  Defer with a drafted question reply; leave the thread open.
- **A cheap tier returned `invalid`** — it was not allowed to. Treat the verdict as `escalate` and
  re-verify on the session model; do not let it reach the disposition map.
- **Everything escalated** — the routing was too aggressive for this MR (a diff whose findings are all
  semantic). Cost is the only casualty; note it and carry on with the session-model verdicts.
- **Lint / format / tests fail** — STOP before the outward batch. Never push or post on top of a
  red tree.
- **Thread already has prior replies** — flag it ("has prior discussion") and don't talk over an
  exchange in progress; let the user decide whether to add to it.
- **Push fails at the gate** — abort the reply/resolve batch so no reply cites a SHA the reviewer
  can't see.
- **No answer from the curation gate, or no reply to a fallback prompt or the outward-batch
  confirmation** — stop and leave the state as it is. There is no default disposition and no
  implied consent to post; the run resumes whenever the user answers. A `dismissed` gate and an
  unrecognised payload both count as "no answer" here; a gate that produced no payload at all
  routes to the fallback prompts instead of stopping.
- **Dirty working tree at start** — surface it before making edits and let the user decide whether
  to proceed.

## Why this shape

**Verification before curation kills false positives.** Code-review output — human or LLM — routinely
flags things that aren't real, or recommends fixes that don't fit the codebase. Verifying each
thread against the current code *before* presenting it means the user curates a list of real,
checked findings instead of filtering noise by hand. This is the `receiving-code-review` stance made
mechanical: evaluate, don't perform agreement.

**Tiered verification is safe because the verdict space is restricted, not because the routing is
accurate.** Predicting which threads a weak model will judge correctly is guesswork; bounding what a
weak model is allowed to *conclude* is not. Since `valid` errors surface at curation and `invalid`
errors auto-resolve real findings, letting cheap tiers propose but never dismiss keeps the failure
mode recoverable at every tier. Routing on evidence-scope (how many files must be read) rather than
apparent difficulty is what makes most threads land cheap; the security override is a second axis —
blast radius — that overrides scope wherever a shallow fix would look correct.

**The verdict restriction is scoped to this skill on purpose.** Sibling skills (`finalize-branch`,
`mr-review`) run `model: sonnet` verifiers with the full verdict space, including `invalid`. That is
not an oversight to be "fixed" by copying this rule across: there, a wrongly-dismissed finding is
dropped from a list the user is about to curate, and the user still sees the discrepancy report. Here
a wrong `invalid` **posts a confident reply on a permanent thread and resolves it** — an outward,
social, hard-to-retract action. The restriction tracks that asymmetry in consequence, not a general
distrust of Sonnet.

**The gate exists because the turn break it replaces was only a proxy.** That break came from an
observed failure, not theory: when the analysis and the question shared a turn, the user was
asked to curate findings they never read. But a stopped turn only proves the assistant stopped
talking. A blocking `plannotator annotate --gate` call cannot return until the user has been in
the document, so it enforces the same intent directly, and each thread's proposed disposition
sits inside its own block instead of in a list three screens away. On the fallback path, where
there is no document, the turn break still carries the whole guarantee — so it stays there,
worded exactly as before: "before" means a turn boundary, not text order.

**Every question here is text, never `AskUserQuestion`.** Two reasons, and the second is the one that
matters. First, labels truncate, so a dialog is a bad container for anything the user must weigh.
Second, the tool runs a countdown and assumes a default when it expires — and the decisions in this
skill are "close a reviewer's finding as invalid" and "post replies and resolves on a shared MR". A
timer must not be able to answer those. The write-up stays in a file the user can re-read,
which is what lets the 3b-fallback's numbered lines stay one line long. The same rule covers a long
bucket: `AskUserQuestion`'s 4-option ceiling forced batching, numbered text has no ceiling, so a
12-thread Fix list stays a single prompt instead of three.

**Push before reply keeps every `<sha>` link valid.** A reply that says "Fixed in `<sha>`" is
worthless until the commit is visible to reviewers; posting it before the push (or when the push
fails) leaves a dead link in a permanent thread. Sequencing push as step one of the outward batch —
and aborting on push failure — guarantees the citation resolves.

**Resolve only Fix and Dismiss.** Resolving a thread is a social signal that the matter is closed.
For a disagreement (Push back) or a deferral (Defer), it isn't closed — auto-resolving would
silently end a conversation the reviewer never agreed to end. Those stay open for the reviewer to
close.
