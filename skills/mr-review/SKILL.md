---
name: mr-review
description: "MANUAL INVOCATION ONLY. Trigger exclusively when the user types the literal slash command `/mr-review` — never on natural-language phrases like 'review the MR' or 'review this branch'. Reviews the GitLab MR of the currently checked-out branch and posts user-approved findings back as line-anchored diff notes. Works for the user's own MR or a teammate's. GitLab-only (requires `glab`). Refuses if the MR's source branch isn't checked out. Supports `--dry-run`."
---

# /mr-review

Review the open MR on the current branch end to end: gather intent (ticket + MR description), run `superpowers:requesting-code-review`, verify every finding against the real code, let the user curate, post the approved findings as line-anchored diff notes.

## When to use

**Manual-only.** Trigger on the literal `/mr-review`. For "review the MR" in natural language, don't invoke this — handle it without the skill or ask whether they want `/mr-review`.

Works on any MR whose source branch is checked out — the user's own (pre-flight self-review) or a teammate's (`glab mr checkout <iid>`). Authorship only changes tone: notes on a teammate's MR go to the author, so be precise and neutral.

Not for:
- An MR whose branch is **not checked out**. Verification needs a working tree; `git show` can't grep neighbours. Tell the user to `glab mr checkout <iid>` first.
- Ad-hoc comments — use `glab mr note` directly.
- GitHub PRs. GitLab-only; for GitHub run `superpowers:requesting-code-review` by itself.

## Config

Optional `AI_SKILLS_*` env vars; recommended setup is one line in `~/.zshenv`:

```sh
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
```

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_MR_TOOL` | _(auto-detect)_ | Must resolve to `glab`. Unset → detect from `git remote get-url origin` (contains "gitlab" → `glab`). Anything else stops the skill with a "GitLab-only" message. |
| `AI_SKILLS_TICKET_PREFIX` | _(empty)_ | Ticket prefix (e.g. `PROJ`). Empty → match any `FOO-123`-shaped slug. |

Ticket lookup also needs a tracker MCP in the session (Step 2); without one the skill skips intent-from-ticket.

## Hard rules

- **GitLab only.** Resolve the tool early (`AI_SKILLS_MR_TOOL`, else origin URL). Not `glab` → stop; for GitHub suggest `superpowers:requesting-code-review` directly.
- **Branch must match** the MR's `source_branch` (`glab mr view --output json`). Mismatch → stop; switching branches is the user's call.
- **Never post without confirmation.** Posting is irreversible — notifications fire, threads persist. The write-up goes through the gate every time.
- **Never use `AskUserQuestion`.** Every question — clarifications, post confirmation, fallback curation — is plain text with numbered options, then wait. The tool's countdown assumes a default on expiry; "which findings get posted to a shared MR" must not be answered by a timer.
- **Curation happens in the plannotator gate** (Step 7a): `plannotator annotate "$FILE" --gate --json` blocks until the user approves, annotates, or closes. `approved` applies every block's `**Default:**`; `dismissed` aborts; `annotated` overrides per finding. The block *is* the read gate.
- **Terminal prompts are the fallback.** Only when `command -v plannotator` fails or the gate returns no payload: print path + counts + discrepancy report + table, **end the turn**, then two sequential numbered prompts in a later turn — Recommended first, wait, then Optional. Never one combined list.
- **Write-ups go to a file, never the terminal**, whatever the count. File: meta block, counts, discrepancy report, overview table, every finding block. Terminal: path and counts only.
- **`Content-Type` header is mandatory** on `glab api ... --input -`; without it GitLab returns HTTP 415. Position-payload rules and a worked example: [references/glab-diff-notes.md](references/glab-diff-notes.md). Don't re-derive them.
- **Verification sub-agents read the actual files** at the MR tip, not summaries — that is the only way to catch hallucinated or stale findings.
- **Honor `--dry-run`** (`/mr-review --dry-run`, or "dry run" in the message): build the payloads, print them as the receipt, POST nothing.

## Workflow

### 1. Detect the MR and load the diff

Gate on the tool:

```bash
TOOL="${AI_SKILLS_MR_TOOL:-}"
[ -z "$TOOL" ] && git remote get-url origin 2>/dev/null | grep -qi gitlab && TOOL=glab
if [ "$TOOL" != "glab" ]; then
  echo "STOP: /mr-review is GitLab-only. Set AI_SKILLS_MR_TOOL=glab or use a GitLab remote."
  exit 1
fi
```

Fetch the MR:

```bash
glab mr view --output json
```

Capture `iid`, `source_branch`, `target_branch`, `title`, `description`, `web_url`, `diff_refs` (`base_sha`, `head_sha`, `start_sha`). The SHAs anchor the diff notes later.

**"merge request ID number required" + multiple matches** means several MRs share the branch (one open, others closed/merged). Call `glab mr view <iid> --output json` on each iid from the error; **auto-pick the single `state == "opened"`**. Two open → ask which. Zero open → stop, nothing to review.

Confirm the current branch equals `source_branch`; otherwise stop and surface the mismatch.

Get the unified diff from the captured JSON — don't re-invoke bare `glab mr view` (it errors again in the multi-MR case):

```bash
git fetch origin "$TARGET_BRANCH" --quiet   # target_branch from the captured JSON

# Guard: the local tip must be exactly the MR's head. Unpushed local commits (or
# an unpulled remote) make line math and verification diverge from what GitLab shows.
[ "$(git rev-parse HEAD)" = "$DIFF_HEAD_SHA" ] || {
  echo "STOP: local HEAD != diff_refs.head_sha — push or pull so the working tree matches the MR tip."
  exit 1
}

BASE_SHA="$DIFF_BASE_SHA"    # diff_refs.base_sha from the captured JSON
HEAD_SHA="$DIFF_HEAD_SHA"    # diff_refs.head_sha
git diff --unified=0 "$BASE_SHA".."$HEAD_SHA"
```

Review range, line math and position payloads all use the `diff_refs` SHAs. Once the guard passes, `HEAD` is the MR tip and the working tree is safe for verification.

### 2. Find the ticket (optional)

Runs only if a tracker MCP is in the session — ClickUp (`mcp__*clickup*`), Jira (`mcp__*jira*`), or Linear (`mcp__*linear*`). None → skip to Step 4 with "ticket unavailable" noted for the discrepancy report.

```bash
PATTERN="${AI_SKILLS_TICKET_PREFIX:-[A-Z]+}-[0-9]+"
```

Sources in order, stop at the first hit:

1. **Branch name** — regex anywhere (`feat/PROJ-456-add-thing`, `andrew/PROJ-789-fix`).
2. **MR title** — same regex, plus `[PROJ-123]` / `(PROJ-123)`.
3. **MR description** — same regex, plus any tracker URL the MCP understands (`app.clickup.com/t/<id>`, `<org>.atlassian.net/browse/<id>`, `linear.app/<org>/issue/<id>`). The URL's id segment is a raw task id — pass it straight to the get-task tool.
4. **Ask once**: "I couldn't find a ticket reference. Want to provide one, or proceed without?"

ClickUp fetch:

```
1. mcp__<clickup-server>__clickup_get_task(taskId="<TICKET>")
   # Many ClickUp setups accept custom ids directly here.

2. If that errors / returns nothing:
   mcp__<clickup-server>__clickup_search(query="<TICKET>")
   # Then take the first result whose custom_id matches exactly.
```

Jira / Linear: the analogous `get_issue` / `search` tools.

### 3. Score ticket confidence

Judge three dimensions — goal clarity, acceptance criteria, match to the diff:

| Level | Heuristic | What to do |
|---|---|---|
| High | Clear goal + criteria + diff matches | Primary source of truth for intent. |
| Medium | Clear goal, vague criteria | Use the goal; don't lean on missing criteria. |
| Low | Empty body, title-only, or unrelated to diff | **Ignore the ticket**; say so in the discrepancy report. |

A misread ticket produces worse findings than none. Don't invent criteria.

### 4. Build an intent summary

In a scratch note (this conversation, not a file):

- **Goal** (ticket, if confidence ≥ Medium) — one sentence.
- **MR description summary** — 2–3 bullets of what it claims.
- **What the diff actually does** — 2–3 bullets from reading the diff.

Flag: claims the diff doesn't deliver; substantial work the description omits; ticket and description disagreeing (when the ticket is trusted); the diff touching something the ticket scopes out. These are the **discrepancy report** — upstream of code review, never findings.

**Verdict:** any flag → `⚠ needs attention`; none → `✓ matches`. Ticket unavailable (Step 2) or ignored (Step 3 Low) → append `(ticket unavailable)` / `(ticket ignored)`; the description-vs-diff comparison still runs and still sets the verdict.

### 5. Run the code review

```
Skill: superpowers:requesting-code-review
```

Pass `BASE_SHA`/`HEAD_SHA` from Step 1. Reviewer template: `DESCRIPTION` = the intent summary; `PLAN_OR_REQUIREMENTS` = the ticket goal when confidence ≥ Medium, otherwise state that none were available.

The reviewer returns prose. Convert its `Issues` into the list below, mapping severity: `Critical` → `critical` (or `high` when no data-loss/security impact); `Important` → `medium` (or `high` when it breaks a user-visible path); `Minor` → `low`; style → `nit`. Ignore `Strengths`, `Recommendations`, `Assessment` — the merge verdict is never posted.

```
[
  {
    "id": "F1",
    "severity": "critical|high|medium|low|nit",
    "file": "path/to/file.py",
    "line_start": 42,
    "line_end": 42,
    "title": "short headline",
    "issue": "what the reviewer says is wrong",
    "recommendation": "what the reviewer suggests"
  },
  ...
]
```

No line numbers given → leave null and treat as file-level. Never invent them; guessed anchors produce wrong diff notes.

### 6. Fan out to verify findings

**Group findings by file; one sub-agent per file**, in parallel (single message, many tool calls), `general-purpose` type, `model: sonnet` — a bounded read-and-judge task; the review pass stays on the session model for recall.

Per file, not per finding: it collapses redundant reads, and — more important — **a recommendation is often only wrong next to another finding on the same file.** One false positive can be exactly what makes a second finding's cheap remedy wrong. A per-finding agent can't see that; task 3 asks for it.

Findings with no `file` get one brief of their own. Cap a wave at ~8 sub-agents; more files → consecutive waves.

```
Verify these code-review findings against the actual code on the current branch.

File: <file>

Findings:
  [<id>] Lines: <line_start>-<line_end>  (or "file-level")
         Issue: <issue text>
         Recommendation: <recommendation text>
  [<id>] ... (repeat per finding on this file)

Tasks:
  1. Read the file and the surrounding context for every cited location before judging
     any of them. If the lines have shifted, find the equivalent location.
  2. For each finding: confirm whether the described issue is actually present at the
     cited location on the current branch, and independently judge whether the
     recommendation, if applied, would resolve it without introducing a new problem.
  3. Check the findings against each other. Say so explicitly if one being wrong, or
     one being applied, changes whether another is real or makes another's
     recommendation wrong — that interaction is invisible to anyone reading a single
     finding in isolation.

Report per finding id, under 150 words each:
  - issue_real: yes / no / partial — with one-sentence reason
  - fix_sound:  yes / no / risky   — with one-sentence reason
  - corrected_lines: <if the line numbers were wrong, give the right ones>
  - notes: anything else worth knowing, including any interaction found in task 3

Be specific. Do not parrot the findings back — actually look at the code.
```

Aggregate into one table keyed by finding id.

### 7. Present findings, then the curation gate

**7a. Presentation.**

**Classify first** (7b) — the bucket sets each block's `**Default:**` and the table's Bucket column.

**Location.** Always a file. `GITDIR="$(git rev-parse --absolute-git-dir)"`, `SLUG="$(git rev-parse --abbrev-ref HEAD | tr '/' '-')"`, write to `$GITDIR/mr-review-$SLUG.md` — never committed, invisible to `git status`, isolated per worktree, no `.gitignore` edit.

The file stands alone. In order:

1. **Meta block** — MR title and number, commit range, file/line counts, ticket + confidence.
2. **One-line count by severity**, then `excluded, and why` lines for every **Excluded** finding — not selectable, listed so nothing is silently dropped.
3. **Discrepancy report** (Step 4) — the verdict on the MR as a whole; it calibrates trust in the table, so it precedes it. **Heading: `## Discrepancy report — <verdict>`**, the verdict from Step 4 — `## Discrepancy report — ⚠ needs attention`, `## Discrepancy report — ✓ matches (ticket unavailable)`. The heading is what a reader scanning the file sees; a mismatch buried in prose under a neutral heading gets skipped. Flags as bullets under it; `✓ matches` gets one line naming what was compared.
4. **Overview table** — the scan layer and index. `ID` links to the finding's anchor.

   ```
   | ID | Sev | Anchor | Real? | Fix sound? | Bucket |
   |----|-----|--------|-------|------------|--------|
   | [F1](#f1rec---every-dropdown-click-rewrites-user_roles) | medium | service.py:62 | ✓ yes | ⚠ risky | Recommended |
   | [F2](#f2rec---route-test-asserts-nothing) | medium | test_routes.py:107 | ✓ yes | ✓ yes | Recommended |
   | [F3](#f3skip---stale-docstring) | low | (file-level) | ✓ yes | ✓ yes | Optional |
   ```

   Anchors assume GitHub-style slugs (lowercase, brackets dropped, the ` - ` separator collapsing to three hyphens, spaces → hyphens). If plannotator slugifies differently the links just don't jump — navigation only.

5. **Cluster sections and finding blocks** — see [Finding write-up format](#finding-write-up-format).

**Terminal gets** the absolute path on its own line and the severity count. Nothing else; the report and table print to the terminal only on the fallback path.

**Hand it over.** A separate `bash` block doesn't inherit `$GITDIR`/`$SLUG`, so inline the substitutions:

```bash
command -v plannotator >/dev/null &&
plannotator annotate "$(git rev-parse --absolute-git-dir)/mr-review-$(git rev-parse --abbrev-ref HEAD | tr '/' '-').md" --gate --json
```

`--gate` adds Approve; `--json` emits the decision on stdout. **Run it with `run_in_background: true`** and poll — the user may take hours, and a foreground call dies at the Bash tool's 10-minute cap.

**Approve discards annotations.** Approve emits a bare `approved` and drops any annotations made first. A user who has marked up a block must submit via the annotation flow. Say this when handing over.

**7b. Buckets** — exactly one per finding:

| Bucket | Rule | Heading tag · default it sets (fallback prompt) |
|---|---|---|
| **Recommended** | `issue_real ∈ {yes, partial}` AND `fix_sound != no` AND (severity ∈ {`critical`, `high`, `medium`} OR `**My read.**` is take at `low`/`nit` — e.g. the corrected diagnosis is materially useful) | `[Rec]` · `take` (fallback Prompt 1, "Confirm to post") |
| **Optional** | every shown finding not Recommended — `low`/`nit` with a `skip` read, `fix_sound == risky`, or `fix_sound == no` on a real finding | `[Skip]` · `skip` (fallback Prompt 2, "Optional additions") |
| **Excluded** | `issue_real == no` (verified false positive), OR the sub-agent recommends declining | Not selectable. Listed in the `excluded, and why` lines. |

> **Precedence:** `medium`+ with `fix_sound == risky` → **Optional**. A real issue with a caveated fix is not posted on the skill's recommendation; the user opts in with the caveat visible in `Fix sound?`.

> **`partial` stays Recommended.** It usually means the bug is real but the reviewer's trigger diagnosis was wrong; the corrected diagnosis is what gets posted. Down-rating it would waste the verification.

Recommended → heading tag `[Rec]` and `**Default:** take`; Optional → heading tag `[Skip]` and `**Default:** skip`; Excluded → no block, so no tag and no `**Default:**` line, not part of the decision surface.

**7c. Read the gate's decision.**

| Decision | Meaning | What you do |
|---|---|---|
| `approved` | Approve clicked | Every `**Default:**` stands. Post the `take` findings. |
| `dismissed` | Window closed without approving | **Abort.** Post nothing. Say so. |
| `annotated` | Annotations returned | Each overrides the `**Default:**` of the block it anchors to; the rest keep theirs. |
| anything else | Unrecognised / unparseable | **Abort** as `dismissed`. Print what came back. Never post defaults on an unreadable answer. |

`annotated` mapping:

- Annotations anchor per block (paragraph, heading, list item); the `### F<n>[Rec|Skip]` heading is the intended target. Map by the `F<n>` token in anchor text or body — the tag is not part of the ID, and an annotation may contradict it.
- Vocabulary: `take`, `skip`, `fold into F<n>` — case-insensitive. `fold into F<n>` = covered by F*n*; don't post separately, record as folded.
- Text outside the vocabulary (a question, "wrong line range", a request for a new finding) applies **nothing**. Answer it, then re-gate on a **delta file** — `$GITDIR/mr-review-$SLUG-round<n>.md` holding only the new or changed blocks in full (metadata line, `**Default:**`, `---`) plus one line naming the untouched findings with their standing defaults. Never re-present the full write-up: the reader cannot tell what changed. Approve on the delta applies the standing defaults and the delta's own.
- Can't map to exactly one finding → **ask**. Never guess, never fall back to the default.

**Print an applied/skipped/folded receipt** naming every finding before posting — the last thing the user sees before notifications fire.

**Silence is never consent.** Only an affirmative payload posts: `approved`, or `annotated`. `dismissed` (`{"decision": "dismissed"}`, or exit 0 with empty output) and any unrecognised payload abort. **No answer at all** — binary missing, browser never opened, non-zero exit with empty stdout, process died before emitting JSON — is the only case that routes to the fallback. *Abort when an answer came back and wasn't approval; fall back only when no answer could be obtained.*

**7c-fallback. No plannotator.** Guard with `command -v plannotator`. Take this path on absence or **no payload**; a non-zero exit that still carried a payload goes through the table above.

1. Print the absolute path, severity count, discrepancy report, overview table, `excluded, and why` lines. **Then END YOUR TURN** — a message that asks nothing. "Before" is a turn boundary, not text order.
2. In a *later* turn, two sequential numbered plain-text prompts. Prompt 1: Recommended only, default all, reply with numbers to drop; **wait**. Prompt 2: Optional only, default none unless named; skip when empty.
3. One line per finding: `[F3 medium] auth/repositories.py:128 — every dropdown click rewrites user_roles` plus a verification flag in parentheses where one applies.

```markdown
**Recommended — 4 findings.** Default is all of them. Reply with numbers to drop, or "go".

1. [F1 high] repositories.py:128 — dropdown click rewrites user_roles  (✓ verified)
2. [F3 medium] services.py:120 — duplicate 'Afdeling' enum  (⚠ lines shifted to 125–128)
3. [F4 medium] floorplan-editor.js:1543 — derefs state nulled mid-POST  (✓ verified)
4. [F6 medium] handlers.py:2455 — as_of not threaded  (✓ verified)
```

Order by severity. Never mix buckets. No reply → stop; don't post.

### 8. Post the selected findings

**`--dry-run`**: skip the POSTs; print each payload as the receipt so the user can check anchors and body text, prefixed `[DRY-RUN] Would post 8 notes to MR !<iid>`.

Otherwise POST one discussion per finding — GitLab has no batch endpoint. Prefer one Python helper that loops and captures `discussion_id` + `note_id` per response over many parallel `Bash` calls; sequential is easier to debug when a payload is rejected, and the latency is negligible.

Payload skeleton (full rules in [references/glab-diff-notes.md](references/glab-diff-notes.md) — read it first; a wrong position silently anchors to the wrong file or 415s):

```python
{
  "body": "<markdown finding body>",
  "position": {
    "position_type": "text",
    "base_sha":  "<diff_refs.base_sha>",
    "head_sha":  "<diff_refs.head_sha>",
    "start_sha": "<diff_refs.start_sha>",
    "new_path":  "<file>",
    "old_path":  "<file>",            # same unless rename
    "new_line":  <int or null>,
    "old_line":  <int or null>,
    # multiline only:
    "line_range": {
      "start": {"new_line": <s>, "old_line": <s_old_or_null>, "type": "new|old|expanded"},
      "end":   {"new_line": <e>, "old_line": <e_old_or_null>, "type": "new|old|expanded"}
    }
  }
}
```

`line_start == line_end` → omit `line_range`. Ranges → both endpoints.

**File-level findings** post as a general MR note:

```bash
glab mr note create <iid> --message "<body>"
```

It prints the note URL; capture it for the receipt.

**Diff-note URLs**: the `POST .../discussions` response carries `id` (discussion) and `notes[0].id` (note). Build `{mr_web_url}#note_{notes[0].id}` — that anchors the browser to the note.

### 9. Print the receipt

```
Posted N diff notes + M general notes to MR !<iid>. (<mr_web_url>)

| ID | Severity | Anchor | URL |
|----|----------|--------|-----|
| F1 | critical | doors_table.html:43 | <mr_web_url>#note_<note_id> |
...
```

Dry-run: "[DRY-RUN] Would post", URL column `(dry-run — not sent)`.

Restate the **Excluded** findings with their one-line reasons. Every finding ends as Posted, Optional-not-picked, or Excluded-with-reason.

## Finding write-up format

The per-finding blocks in the write-up file (Step 7a). Not the diff-note body — that's the next section.

```markdown
### F4[Rec] - applyAdjustFrame derefs state that can be nulled mid-POST
`medium` · `floorplan-editor.js:1543` · verification **verified as claimed**

**Problem.** `applyAdjustFrame` awaits the POST at line 1508. `adjustMode` stays `true`
for that whole await, so anything that calls `cancelAdjust()` during it — Escape (2901),
`setDrawMode('pan')` (531), the page-change branch (1256) — runs `_exitAdjust()` and nulls
`adjustHandles`. Phase 2 then hits 1543 `adjustHandles.a.slice()` and throws. Pre-branch
that deref sat inside `if (frame)`; this branch hoisted it out.

**Why it bites.** The throw lands *after* the server persisted, so `loadDoors()` never
runs and the canvas shows pre-alignment geometry for data that is already saved. Nothing
catches it — the POST succeeded, so there is no failed request to notice.

**Fix.** Two edits in `floorplan-editor.js`:
- Snapshot both objects into locals before the Phase-1 await and use the snapshots in
  Phase 2: `const sentA = adjustHandles.a.slice(), sentB = adjustHandles.b.slice();`
- Reset `applyBtn.disabled = false` where the adjust toolbar is re-shown, so a session
  cancelled mid-flight doesn't leave the button dead.

**My read.** Take it — small, and it's a regression this branch introduced.

**Default:** take — annotate this block with `skip` to drop it, or `fold into F<n>`.

---
```

**Rules:**

- **Heading: `### F<n>[Rec|Skip] - <headline>`.** ID, recommendation tag, headline — nothing else. The tag is the bucket from 7b: `[Rec]` for Recommended, `[Skip]` for Optional, so the call is readable from the outline without opening the block. The heading is also the table's link target and the annotation anchor; a 120-character headline fails all three jobs.
- **One metadata line under the heading**, `·`-separated, in order: severity, anchor(s) as code spans, verification delta flag.
  - Bucket does not repeat here — the heading tag carries it.
  - Anchors are code spans (`` `services.py:120` ``); chain with `→` when the fix spans two places. No line → `(file-level)` or the path it concerns, matching the table's Anchor column.
  - Delta flag: 2–4 words as `verification **<flag>**` — label plain, flag bold. Typical: verified as claimed, corrected the remedy, inverted the diagnosis, widened the line range, downgraded to partial; coin one when none fits.
- **`**Problem.**` — mechanism only, ~6 lines**: trigger, sequence, resulting state, `file:line` cited inline. Quotes and code go to `**Fix.**`; provenance ("this branch hoisted it out") goes to `**My read.**`. `inverted the diagnosis` / `corrected the remedy` carry two mechanisms, so ~8 lines; never meet the cap by dropping the correction.
- **`**Why it bites.**` — required, separate**, 1–2 sentences: the user-visible consequence and what fails to catch it. No runtime consequence → name who is misled and when. Never invent a failure mode.
- **Run-on bold lead-ins** ending in a period, prose on the same line. Never `**Issue:** <one line>`.
- **`**Fix.**` is actionable** — bullet per edit when more than one; real code line, real helper, real fixture. Say when it's a pure test addition.
- **No `**Verification:**` badge line.** Corrections are woven into the prose in your own voice ("Important correction to the original recommendation: …", "Verification downgraded this to partial: …"). The delta flag indexes that prose; it carries no reasoning. Full verdicts live in the table.
- **`**My read.**` — one sentence**: take / skip / fold into F*n*, only when not obvious from the block. A second sentence only when it changes *handling* — outside the diff's hunks, or wider than this branch.
- **`**Default:**` is the last line** before the separator: the disposition that applies on silence and the words that override it. Recommended → `take`; Optional → `skip`. The gate reads this.
- **Heading tag, `**My read.**` and `**Default:**` agree** — `[Rec]` ⇔ take ⇔ Recommended, `[Skip]` ⇔ skip ⇔ Optional. A `take` read at `low`/`nit` is the Recommended criterion for that severity (7b), so bucket it Recommended and tag it `[Rec]`. A `take` read on a `risky` fix is the read being wrong — precedence keeps the bucket Optional, so the read becomes `skip` with the caveat named. When the three disagree at write time, one of them is wrong; fix it before the next block.
- **`---` between every finding**, including within a cluster.
- **Cluster when findings share a mechanism**: `## Cluster A — <the mechanism>` plus one line on how they interact ("F1's write-back closes F6"). Every finding inside keeps its full block, metadata line, `**Default:**` and `---`.
- **Overview table first** — after the counts and excluded lines, before the clusters; it covers every finding.

## Body formatting for diff notes

```markdown
**<short title>** — severity: <critical|high|medium|low|nit>

<issue paragraph: what's wrong and why it matters>

**Suggested fix:** <recommendation in 1–3 sentences>
```

No finding object dump, no verification metadata (that's for you, not the MR audience). Under ~120 words — long notes get skimmed.

## Failure modes

- **Tool doesn't resolve to `glab`** — stop, point at Config. Never attempt with `gh`; the diff-note API differs entirely.
- **`glab mr view` returns nothing** — no MR. Suggest `glab mr create --draft` (omit `--reviewer` unless `$AI_SKILLS_REVIEWERS` is set) and stop.
- **Local tip ≠ `diff_refs.head_sha`** — pull or push until they match; anchors and verification would otherwise target code the MR doesn't have. The Step 1 guard enforces this.
- **Reviewer returns no findings** — valid. Still produce the discrepancy report; post nothing.
- **Tracker MCP unavailable** — proceed; note "ticket unavailable" in the discrepancy report.
- **Invented line numbers** — `issue_real: no` because the cited line doesn't contain the cited problem is a hallucination, not a finding.

## Why this shape

**Verification fan-out.** LLM reviewers pattern-match on diff text and over-trigger; verification catches that before the user filters 30 items by hand. **Discrepancy report.** Finding-level review misses "is this MR doing what it claims?" — often where the biggest issue is. The verdict lives in the heading because a report without one read as background prose and got skipped even when it flagged a mismatch.

**Curation gate.** The turn break it replaced was a proxy: a run once emitted the report and the first prompt in one turn, and the user was asked to curate findings they'd never read. A stopped turn proves the assistant stopped talking, not that anyone read anything; a blocking `plannotator annotate --gate` can't return until they have.

**Per-finding `**Default:**`.** Recommended and Optional have opposite defaults (all out unless subtracted vs none out unless named); one flat list can't express that. Stating the default in each block does, next to the prose that justifies it. The two sequential prompts survive as the fallback, keeping the split and the turn break.

**Text, never `AskUserQuestion`.** Its countdown assumes a default on expiry, and the decision here is "post to a shared MR". Its labels also truncate; numbered text has no 4-option ceiling.

**`--dry-run`.** First run on a real MR, you don't know the anchor math is right for this layout. Eight notes on wrong lines are irreversible; a dry run costs one round trip.

**Open-MR resolution.** Branches accumulate one open MR plus closed/merged ones. Auto-picking the single open one matches intent nearly always; stop only at two opens or zero.
