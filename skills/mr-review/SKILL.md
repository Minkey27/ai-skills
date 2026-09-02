---
name: mr-review
description: "MANUAL INVOCATION ONLY. Trigger exclusively when the user types the literal slash command `/mr-review` — never on natural-language phrases like 'review the MR' or 'review this branch'. Reviews the GitLab MR of the currently checked-out branch and posts user-approved findings back as line-anchored diff notes. Works for the user's own MR or a teammate's. GitLab-only (requires `glab`). Refuses if the MR's source branch isn't checked out. Supports `--dry-run`."
---

# /mr-review

End-to-end review of the open MR on the current branch. The skill orchestrates four jobs that are easy to do badly when done by hand:

1. Gather intent (ticket + MR description) so review findings can be judged against the *goal*, not just the diff.
2. Run `superpowers:requesting-code-review` to get an initial set of findings.
3. Verify each finding by re-reading the actual code, because reviewers (human or LLM) routinely flag things that aren't really problems or whose recommendations don't actually work.
4. Let the user curate which findings get posted, then post them to GitLab as line-anchored diff notes.

## When to use

**This skill is manual-only.** Trigger exclusively on the literal slash command `/mr-review`. If the user says "review the MR" or any natural-language variation without typing the slash form, do **not** invoke this skill — handle the request without it, or ask whether they want to run `/mr-review`.

Once invoked, the skill applies to **any MR the user currently has checked out**, whether they authored it or pulled down a teammate's branch:

- **Pre-flight self-review** — user finished their own work and wants a critical pass before requesting human review.
- **Teammate review** — user checked out a colleague's branch (`glab mr checkout <iid>` or `git checkout <branch>`) and wants to leave structured feedback on the MR.

Do **not** use this skill for:
- An MR whose source branch is **not currently checked out**. The verification step needs a working tree; without one, sub-agents can only see files via `git show`, which kills their ability to grep neighbors or understand surrounding code. Tell the user to check out the branch first (`glab mr checkout <iid>`).
- Posting ad-hoc comments unrelated to a review pass — just use `glab mr note` directly.
- Reviewing a GitHub PR. This skill is GitLab-only (see Config below). For GitHub, use a separate `pr-review` skill or run `superpowers:requesting-code-review` by itself.

**Authorship doesn't matter** — the skill works the same for the user's own MR and a teammate's. The only difference is tone: when reviewing someone else's work, the discrepancy report and findings will be sent to the author via diff notes, so be precise and neutral. When self-reviewing, the same notes are essentially the user talking to themselves; that's fine too.

## Config

This skill reads optional config via the `AI_SKILLS_*` env vars. Recommended setup is one line in `~/.zshenv`:

```sh
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
```

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_MR_TOOL` | _(auto-detect)_ | Must resolve to `glab`. If unset, detect from `git remote get-url origin` (URL contains "gitlab" → `glab`). If it resolves to anything else, the skill stops with a "GitLab-only" message. |
| `AI_SKILLS_TICKET_PREFIX` | _(empty)_ | Ticket prefix (e.g. `PROJ`). Empty → match any uppercase slug like `FOO-123`. |

Ticket lookup additionally depends on which tracker MCP is available in the session — see Step 2. The skill works without any tracker MCP; it just skips the intent-from-ticket step.

## Hard rules

- **GitLab only.** Resolve the tool early: `AI_SKILLS_MR_TOOL` if set, else auto-detect from the origin remote URL. If it doesn't resolve to `glab`, stop and tell the user this skill targets GitLab; for GitHub, suggest running `superpowers:requesting-code-review` directly.
- **Branch must match.** Confirm the current branch is the MR's source branch via `glab mr view --output json`. If not, stop and tell the user — switching branches mid-review is the user's call, not yours.
- **Never post without confirmation.** Even if every finding looks great, present the checklist and wait for the user to pick. Posting to GitLab is irreversible (notifications fire, threads exist forever).
- **Never use `AskUserQuestion`.** Curation, clarifications, the post confirmation — all of it is
  **plain text with numbered options**, then wait for a reply. The tool runs a countdown and assumes a
  default when it expires, and this skill's decision is "which findings get posted to a shared MR".
  A timer must not be able to answer that.
- **Presentation and curation prompts never share a turn.** Step 7a (discrepancy report + finding write-ups + overview table) must end the assistant turn; the first curation prompt goes in a *later* turn, after the user has replied. A same-turn prompt preempts the analysis — the user picks findings without having read the verification results. Text order within a turn does not count as "presenting before prompting".
- **Curation is two sequential prompts** — Recommended first, wait for the answer, then Optional (see Step 7). Never one combined list.
- **More than 5 findings: write-ups go to a file, not the terminal.** Only the overview table, the file path and the severity counts stay on screen (Step 7a). The write-up *format* is unchanged — it moves medium, it does not get shortened.
- **Content-Type header is mandatory** when calling `glab api ... --input -` to create a discussion. Without it GitLab returns HTTP 415. Full position-payload rules and a worked example live in [references/glab-diff-notes.md](references/glab-diff-notes.md). Don't re-derive them.
- **Sub-agents that verify findings must read the actual files**, not summaries. The whole point is to catch hallucinated or out-of-date findings — that only works if they look at current code at the MR's tip.
- **Honor `--dry-run`.** If the user invokes `/mr-review --dry-run` (or types "dry run" in the same message), build the payloads and print them as the receipt instead of POSTing. Posting to GitLab is irreversible; dry-run is how the user can sanity-check the anchor lines and body text before committing to the notifications.

## Workflow

### 1. Detect the MR and load the diff

First gate on the tool:

```bash
TOOL="${AI_SKILLS_MR_TOOL:-}"
[ -z "$TOOL" ] && git remote get-url origin 2>/dev/null | grep -qi gitlab && TOOL=glab
if [ "$TOOL" != "glab" ]; then
  echo "STOP: /mr-review is GitLab-only. Set AI_SKILLS_MR_TOOL=glab or use a GitLab remote."
  exit 1
fi
```

Then fetch the MR:

```bash
glab mr view --output json
```

Capture: MR `iid`, `source_branch`, `target_branch`, `title`, `description`, `web_url`, `diff_refs` (`base_sha`, `head_sha`, `start_sha`). The SHAs are needed later when posting diff notes.

**If `glab mr view` errors with "merge request ID number required" + multiple matches**, this means several MRs share the current source branch (typically one open + one or more closed/merged from previous iterations). Disambiguate as follows:

1. Pull the iids from the error message and call `glab mr view <iid> --output json` on each.
2. **Auto-pick the single MR where `state == "opened"`.** That is the only candidate that matters for review.
3. If two or more MRs are open on the same branch (very rare), stop and ask the user which one to target.
4. If zero MRs are open (all candidates are closed/merged), stop — there's nothing to review.

Confirm current branch matches `source_branch`. If not, stop and surface the mismatch.

Get the unified diff so later steps can identify added/removed/context lines for accurate position payloads. Reuse the values captured from the `glab mr view` JSON above — do **not** re-invoke bare `glab mr view` here (it errors again in the multi-MR case the disambiguation just handled):

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

> **Note:** review range, line math, and the position payload all use GitLab's `diff_refs` SHAs. The guard above makes the working tree safe for verification sub-agents — after it passes, `HEAD` and `diff_refs.head_sha` are the same commit.

### 2. Find the ticket (optional)

This step only runs if a project tracker MCP is available in the current session. Check the tool list for one of:

- A ClickUp MCP (`mcp__*clickup*` or similar)
- A Jira MCP (`mcp__*jira*`)
- A Linear MCP (`mcp__*linear*`)

If none are present, skip this step and continue from Step 4 with "ticket unavailable" noted in the discrepancy report.

**Build the ticket pattern from config:**

```bash
PATTERN="${AI_SKILLS_TICKET_PREFIX:-[A-Z]+}-[0-9]+"
```

Try these sources in order; stop at the first hit:

1. **Branch name** — regex matches anywhere in the branch (e.g. `feat/PROJ-456-add-thing`, `PROJ-123`, `andrew/PROJ-789-fix`).
2. **MR title** — same regex, plus bracketed forms `[PROJ-123]` or `(PROJ-123)`.
3. **MR description** — same regex, *and* any tracker URL that the available MCP would understand (e.g. `app.clickup.com/t/<id>`, `<org>.atlassian.net/browse/<id>`, `linear.app/<org>/issue/<id>`). Treat the URL's id segment as a direct task id.
4. **Ask the user** — if nothing matches, ask once: "I couldn't find a ticket reference. Want to provide one, or proceed without?"

Fetch via whichever MCP is available. For ClickUp:

```
1. mcp__<clickup-server>__clickup_get_task(taskId="<TICKET>")
   # Many ClickUp setups accept custom ids directly here.

2. If that errors / returns nothing:
   mcp__<clickup-server>__clickup_search(query="<TICKET>")
   # Then take the first result whose custom_id matches exactly.
```

For Jira / Linear, use the analogous `get_issue` / `search` tools the MCP exposes.

If a URL form was the source, the embedded id is already the raw task id — skip the custom-id dance and call the MCP's get-task tool with it directly.

### 3. Score ticket confidence (decide whether to use it)

Tickets vary wildly in clarity. Before letting the ticket shape the review, judge confidence on three dimensions:

- **Goal clarity** — does the ticket state a concrete outcome ("Add X so users can Y")?
- **Acceptance criteria** — explicit, even informal, list of what "done" looks like?
- **Match to diff** — does the work in the MR plausibly correspond to the ticket?

Confidence levels:

| Level | Heuristic | What to do |
|---|---|---|
| High | Clear goal + criteria + diff matches | Use ticket as primary source of truth for intent. |
| Medium | Goal is clear but criteria are vague | Use the goal; don't lean on missing criteria. |
| Low | Body is empty, title-only, or unrelated to diff | **Ignore the ticket entirely** for this review. Note this in the discrepancy report so the user knows. |

Be honest about low confidence — a misread ticket produces worse findings than no ticket. Don't invent criteria to fill gaps.

### 4. Build an intent summary

In a short scratch note (kept in this conversation, not written to a file), write:

- **Goal (from ticket, if confidence ≥ Medium)**: one sentence.
- **MR description summary**: 2–3 bullets of what the MR claims to do.
- **What the diff actually does**: 2–3 bullets, derived from reading the diff, not the description.

Compare them. Flag any of:

- MR claims a behavior the diff doesn't deliver.
- Diff includes substantial work the MR description doesn't mention.
- Ticket goal and MR description disagree (and ticket confidence is high enough to trust).
- Diff touches a domain the ticket says is out of scope.

Save discrepancies for the final report — *do not* let them become "findings" themselves. They are upstream of code review.

### 5. Run the code review

Invoke the finder skill:

```
Skill: superpowers:requesting-code-review
```

Pass it the `BASE_SHA` and `HEAD_SHA` computed in Step 1 and fill its reviewer
template: `DESCRIPTION` = what the MR does (use the intent summary from step 4),
`PLAN_OR_REQUIREMENTS` = the ticket goal when ticket confidence is ≥ Medium,
otherwise state that no requirements were available rather than inventing them.

The reviewer returns prose, not a schema. Convert its `Issues` sections into the
structured list below, mapping severity headings onto this skill's scale:
`Critical` → `critical` (or `high` when it is a bug without data-loss / security
impact), `Important` → `medium` (or `high` when it breaks a user-visible path),
`Minor` → `low`, pure style remarks → `nit`. Ignore `Strengths`,
`Recommendations`, and `Assessment` — only findings feed the rest of this
workflow, and the reviewer's merge verdict is never posted to the MR.

Capture the findings as:

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

If `line_start` / `line_end` aren't given, do not invent them — leave null and treat the finding as file-level (not line-anchored). Many reviewer outputs are vague about line numbers; guessing produces wrong anchors and confusing diff notes.

### 6. Fan out to verify findings

For every finding, dispatch a sub-agent **in parallel** (single message, many tool calls) using the `general-purpose` Agent type with `model: sonnet` — verification is a bounded read-and-judge task that runs cheaper/faster on Sonnet 5, while the review pass stays on the session model for recall. Each sub-agent gets a self-contained brief:

```
Verify this code-review finding against the actual code on the current branch.

Finding:
  File: <file>
  Lines: <line_start>-<line_end>  (or "file-level")
  Issue: <issue text>
  Recommendation: <recommendation text>

Tasks:
  1. Read the cited file and surrounding context. Confirm whether the described issue
     is actually present at the cited location on the current branch. If the lines
     have shifted, find the equivalent location.
  2. Independently judge whether the recommendation, if applied, would actually
     resolve the issue without introducing a new problem.

Report:
  - issue_real: yes / no / partial — with one-sentence reason
  - fix_sound:  yes / no / risky   — with one-sentence reason
  - corrected_lines: <if the line numbers were wrong, give the right ones>
  - notes: anything else worth knowing

Be specific. Do not parrot the finding back — actually look at the code. Under 150 words.
```

Aggregate the results into a single table keyed by finding id.

### 7. Present findings, then the curation prompts

**7a. Pre-prompt presentation.**

**Where it goes.** Route by finding count:

- **5 or fewer** — print everything below into the terminal, as before.
- **More than 5** — the detail layer moves to a file and only the scan layer stays on screen:
  1. Resolve `GITDIR="$(git rev-parse --git-dir)"` and `SLUG="$(git rev-parse --abbrev-ref HEAD | tr '/' '-')"`, then write every finding write-up to `$GITDIR/mr-review-$SLUG.md`. Inside the git dir the file is never committed, never appears in `git status`, and is isolated per worktree — no `.gitignore` edit needed, in any repo.
  2. The file holds **exactly** the content below, same order and same format, and must stand alone — the user reads it in an editor, where folding, search and jump-to-`file:line` work.
  3. Print to the terminal **only**: the absolute file path on its own line, a one-line count by severity, the discrepancy report (item 1 — it is the verdict on the MR as a whole, not per-finding detail, and the user needs it to weigh the findings), the overview table, and the `excluded, and why` lines. Nothing else — no write-ups, no excerpts, no "highlights".

Either way the content is, in this order:

1. **The discrepancy report from step 4** in plain text. Not selectable; it's context the user needs to decide what to post.
2. **A prose write-up of each finding** — one block per finding, in the shape below. This is the *detail layer*; the numbered options in 7c stay minimal because the detail already lives here. See [Finding write-up format](#finding-write-up-format) for the full rules — it is prose with run-on bold lead-ins, **not** colon-labelled one-liners, and it carries **no verification badge line**.

   ```markdown
   ### F1 — [medium] service.py:62 — every dropdown click rewrites user_roles

   **Problem.** <mechanism, 2–5 sentences. Name the exact symbols and cite file:line
   inline as you go. Explain the sequence that produces the bad state and what the
   user-visible consequence is.>

   **Fix.** <the concrete change. One bullet per edit if there is more than one;
   inline the actual code line where it clarifies.>

   **My read.** <one sentence: take it / skip it / fold into F<n>, and why.>

   ---
   ```
3. **An overview table at the end** — the *scan layer* the user reads right before choosing:

   ```
   | ID | Sev | Anchor | Real? | Fix sound? | Bucket |
   |----|-----|--------|-------|------------|--------|
   | F1 | medium | service.py:62 | ✓ yes | ⚠ risky | Recommended |
   | F2 | medium | test_routes.py:107 | ✓ yes | ✓ yes | Recommended |
   | F3 | low | (file-level) | ✓ yes | ✓ yes | Optional |
   ```

**Then END YOUR TURN.** The presentation must be a complete assistant message that **asks nothing**. Putting the report "before" the prompt *within one turn* does **not** satisfy this step — "before" means a turn boundary, not text order. Wait for the user's reply (an acknowledgment like "go", a question about a finding, or a re-classification request) and only then send the first curation prompt from 7c. This reply beat is also where the user can interrogate a finding or move it between buckets *before* the prompt frames the decision.

Do not skip straight from verification results to the prompt — the write-ups and table are what let the user answer without scrolling back through the session.

**7b. Classify every finding into exactly one bucket:**

| Bucket | Rule | Prompt it goes into |
|---|---|---|
| **Recommended** | `issue_real ∈ {yes, partial}` AND `fix_sound != no` AND (severity ∈ {`critical`, `high`, `medium`} OR the corrected diagnosis is materially useful even at `low`) | Prompt 1 — "Confirm to post" |
| **Optional** | `issue_real ∈ {yes, partial}` but severity is `low`/`nit`, OR `fix_sound == risky` (real but suggestion has caveats) | Prompt 2 — "Optional additions" |
| **Excluded** | `issue_real == no` (verified false positive), OR sub-agent recommends declining | Not shown as a selectable option. Listed in the discrepancy report instead. |

> **Precedence:** the rules overlap for a `medium`+ finding with `fix_sound == risky` — the risky clause wins and the finding goes to **Optional**, regardless of severity. A real issue whose suggested fix has caveats should not be posted on the skill's recommendation; the user opts in with the caveat visible in the badge.

> **Why `partial` belongs in Recommended for Critical findings.** A `partial` verdict often means the *bug* is real but the reviewer's diagnosis of *how* it triggers was wrong. The sub-agent provides a corrected diagnosis; that corrected version is the one that gets posted. Down-rating it to Optional would defeat the verification step's whole purpose.

**7c. Two sequential text prompts.** Plain text with numbered options — **never `AskUserQuestion`**. Wait for a reply to each before sending the next.

- **Prompt 1 — Recommended.** List **only** the Recommended bucket, one numbered line each. Say plainly that every line is one the skill recommends posting, state the default ("all of them unless you say otherwise"), and how to subtract ("reply with the numbers to drop, or 'go' / 'none'"). **Wait for the answer before sending prompt 2.**
- **Prompt 2 — Optional additions.** A second, separate prompt with **only** the Optional bucket. Nothing here is recommended, so the default is the opposite: **none go out unless the user names numbers.** Skip the prompt entirely when the bucket is empty.

Do **not** merge the buckets into one list — the recommended picks deserve the user's full attention before the optional ones compete for it, and the two have opposite defaults, which one list cannot express.

Numbered text has no 4-option ceiling, so a long bucket stays **one** prompt — don't fragment it. Order by severity inside each prompt so heavy hitters come first.

**Keep each line minimal** — the detail already appeared in 7a. Line shape: `[F3 medium] auth/repositories.py:128 — every dropdown click rewrites user_roles` (ID + severity + anchor + headline), plus the verification badge in parentheses where one applies (`✓ verified, fix sound`, `⚠ corrected: triggers on sort/filter, not first load`). No summary sentences — the write-ups are a scroll away and stay on screen precisely because nothing covers them.

```markdown
**Recommended — 4 findings.** Default is all of them. Reply with numbers to drop, or "go".

1. [F1 high] repositories.py:128 — dropdown click rewrites user_roles  (✓ verified)
2. [F3 medium] services.py:120 — duplicate 'Afdeling' enum  (⚠ lines shifted to 125–128)
3. [F4 medium] floorplan-editor.js:1543 — derefs state nulled mid-POST  (✓ verified)
4. [F6 medium] handlers.py:2455 — as_of not threaded  (✓ verified)
```

**Silence is not an answer.** If no reply comes, stop — do not fall back to the default and post.

For findings in the **Excluded** bucket, list them in the discrepancy report with a one-line "why excluded" so the user knows the skill considered them and what verification found. Don't silently drop findings.

### 8. Post the selected findings

**If `--dry-run` was requested**, skip the POSTs. Instead, print each constructed payload to the terminal as the receipt, formatted so the user can verify the anchor lines and body text. Continue to the receipt section below as if posts had succeeded; the receipt should make it obvious nothing was actually sent (e.g. `[DRY-RUN] Would post 8 notes to MR !<iid>`).

Otherwise: for each selected finding, build the discussion payload and POST it. GitLab's API is one discussion per request — there is no batch endpoint. Prefer a single Python helper script that loops over the payloads and captures `discussion_id` + `note_id` from each response, rather than firing many parallel `Bash` calls; sequential posting from one script is easier to debug if a payload is rejected, and the throughput cost (a few hundred ms per POST) is negligible compared to the time you already spent verifying.

Payload skeleton (full rules in [references/glab-diff-notes.md](references/glab-diff-notes.md)):

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

Line-number rules (added vs removed vs context) and the `Content-Type` header gotcha are in [references/glab-diff-notes.md](references/glab-diff-notes.md). Read it before constructing the payload — getting the position wrong silently anchors notes to the wrong file or rejects them with HTTP 415.

For findings with `line_start == line_end`, omit `line_range` (single-line note). For ranges, include both endpoints.

**For file-level findings without line numbers** (e.g. "add this missing test"), post as a general MR note via `glab mr note create` (not `--message` — that flag is deprecated):

```bash
glab mr note create <iid> --message "<body>"
```

`glab mr note create` writes the URL of the posted note to stdout; capture it for the receipt.

**For diff-note URLs**: the `POST .../discussions` response returns a JSON body with `id` (discussion id) and `notes[0].id` (the actual note id). Build the clickable URL as `{mr_web_url}#note_{notes[0].id}` — that anchors the browser to the specific note, which is much more useful than the bare discussion id.

### 9. Print the receipt

After all posts (or after building all dry-run previews), print a table that the user can scan and click through:

```
Posted N diff notes + M general notes to MR !<iid>. (<mr_web_url>)

| ID | Severity | Anchor | URL |
|----|----------|--------|-----|
| F1 | critical | doors_table.html:43 | <mr_web_url>#note_<note_id> |
...
```

For dry-runs, replace "Posted" with "[DRY-RUN] Would post" and the URL column with `(dry-run — not sent)`.

Also restate any **Excluded** findings from step 7 with one-line "why excluded" reasons so the user knows what didn't get posted and why. This closes the loop: every finding the reviewer produced ends up either Posted, Optional-but-not-picked, or Excluded-with-reason. Nothing is silently dropped.

## Finding write-up format

This is the shape of the per-finding blocks in step 7a — what the user reads on screen. It is **not** the diff-note body (that's the next section, and it stays short).

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
- **No `**Verification:**` badge line.** Verification results get *woven into* the prose instead, as corrections in the reviewer's own voice — "Important correction to the original recommendation: `target_document_version_id` is the row's string id, while `get_page_sizes` needs the int `version` field", "Verification downgraded this to partial: nothing is locked at that point, so it's transaction duration, not lock contention", "That last part is deliberate, not laziness — `page_geometry.py:38-42` documents that PyMuPDF failures aren't a well-defined exception set". Badges live **only** in the overview table.
- **`**My read.**` is one sentence** — take it / skip it / fold into F*n* — and only when the call isn't obvious from the block.
- **`---` between every finding.** Including two consecutive findings inside the same cluster. The separator is what makes a long list navigable.
- **Clustering is encouraged** when findings share one mechanism: give the cluster a `## Cluster A — <the mechanism>` heading and a one-line note on how the findings interact (e.g. "F1's write-back closes F6; F2's narrowing is what makes it legible"). Each finding inside still gets its own full block and its own `---` — a cluster heading is not a licence to merge findings into one paragraph.
- **The overview table always stays**, last, covering every finding including clustered ones.

## Body formatting for diff notes

Each note body should follow this shape so reviewers see consistent, scannable comments:

```markdown
**<short title>** — severity: <critical|high|medium|low|nit>

<issue paragraph: what's wrong and why it matters>

**Suggested fix:** <recommendation in 1–3 sentences>
```

Don't paste the entire finding object. Don't include verification metadata in the body (that's for you, not the MR audience). Keep it under ~120 words per note — long notes get skimmed.

## Failure modes to watch for

- **Tool doesn't resolve to `glab`** (env var says otherwise, or auto-detect finds no GitLab remote) — stop early with a message pointing at the Config section. Don't attempt the workflow with `gh`; the diff-note API shape is completely different.
- **`glab mr view` returns nothing** — no MR on the branch. Tell the user, suggest `glab mr create --draft` if they want one (omit `--reviewer` unless `$AI_SKILLS_REVIEWERS` is set), and stop.
- **Local tip doesn't match the MR** — `git rev-parse HEAD` ≠ `diff_refs.head_sha`. Either the remote has commits you don't (pull) or you have unpushed commits (push). Stop until they match — anchors and verification would otherwise run against code the MR doesn't have. The Step 1 guard enforces this.
- **The reviewer returns no findings** — perfectly valid. Still produce the discrepancy report from step 4 (if any) and stop without posting.
- **Tracker MCP unavailable** — proceed without the ticket. Note "ticket unavailable" in the discrepancy report.
- **Findings with invented line numbers** — when the sub-agent reports `issue_real: no` because the cited line doesn't contain the cited problem, treat it as a hallucination, not a real finding.

## Why this shape

Code review skills tend to over-trigger findings (false positives) because LLMs pattern-match on diff text without considering surrounding context or whether the recommendation actually fits the codebase's conventions. The verification fan-out exists to catch that *before* the user has to filter manually in a checklist of 30 items. The discrepancy report exists because finding-level review misses the larger question: "is this MR doing what it claims?" — which is often where the biggest issues live.

The **two sequential curation prompts** in Step 7 separate *recommended* from *optional* so the user's job on the first prompt is a scan-and-subtract rather than a careful read of every item to decide what's worth posting. The two buckets also have **opposite defaults** — all of Recommended goes out unless subtracted; none of Optional goes out unless named — which a single list cannot express. Don't collapse them. Each line stays **minimal** (ID + anchor + headline + badge) because the *write-ups-then-table* presentation in 7a already carried the detail: write-ups are the detail layer, the overview table is the scan layer, the numbered prompt is just the decision layer. Repeating finding detail in the prompt duplicates 7a and makes the list unscannable.

**Every question is text, never `AskUserQuestion`.** The tool runs a countdown and assumes a default when it expires, and the decision it would be gating here is "post these findings to a shared MR" — a timer must not be able to answer that. Its labels also truncate, which is a bad container for anything the user has to weigh. Text costs nothing by comparison: the analysis stays on screen and scrollable, and with no 4-option ceiling a 12-finding bucket stays one prompt instead of three batches.

The **turn-break between 7a and 7c** exists because of an observed failure, not theory: a run that emitted the full report and the first prompt in one turn technically satisfied "print before the prompt", but the user was asked to curate findings they had never read. Reading requires a turn the user gets to finish; any wording that lets the presentation and the prompt share a turn re-opens that hole.

The **dry-run** mode exists because the first time you run `/mr-review` on a real MR, you don't yet know whether the line-anchor math is right for this codebase's file layout. Posting eight diff notes to the wrong lines is irreversible and noisy; running the same flow with `--dry-run` first costs one round trip and catches anchor bugs before the team sees them.

The **two-stage open-MR resolution** in Step 1 handles the common case where a branch has accumulated multiple MRs across iterations — typically one open and one or more closed/merged. Auto-picking the single open MR matches user intent virtually every time; only stop if the disambiguation is genuinely ambiguous (two opens, or zero opens).
