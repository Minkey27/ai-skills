---
name: mr-review
description: "MANUAL INVOCATION ONLY. Trigger this skill exclusively when the user types the literal slash command `/mr-review`. Do NOT trigger on natural-language phrases like 'review the MR', 'review this branch', 'let's review', or any other variation — those must be handled without this skill unless the user explicitly types the slash form. When invoked, runs a full GitLab MR review on the currently checked-out branch: optionally fetches a linked ticket (when a tracker MCP is available), reads the MR description, runs `parallel-code-review`, verifies each finding with parallel sub-agents, surfaces intent discrepancies between ticket/description/diff, and posts the findings the user approves back to the MR as line-anchored diff notes. Works for any MR the user has checked out — their own pre-flight self-review or a teammate's branch. GitLab-only (requires `glab`). Refuses if the MR's source branch isn't currently checked out, because without a working tree the verification sub-agents can't read files at the MR's tip or grep neighbors."
---

# /mr-review

End-to-end review of the open MR on the current branch. The skill orchestrates four jobs that are easy to do badly when done by hand:

1. Gather intent (ticket + MR description) so review findings can be judged against the *goal*, not just the diff.
2. Run `parallel-code-review` to get an initial set of findings.
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
| `AI_SKILLS_MR_TOOL` | `gh` | Must be `glab` for this skill. If unset or `gh`, the skill stops with a "GitLab-only" message. |
| `AI_SKILLS_TICKET_PREFIX` | _(empty)_ | Ticket prefix (e.g. `PROJ`). Empty → match any uppercase slug like `FOO-123`. |

Ticket lookup additionally depends on which tracker MCP is available in the session — see Step 2. The skill works without any tracker MCP; it just skips the intent-from-ticket step.

## Hard rules

- **GitLab only.** Check `${AI_SKILLS_MR_TOOL:-gh}` early. If not `glab`, stop and tell the user this skill targets GitLab; for GitHub, suggest running `superpowers:requesting-code-review` directly.
- **Branch must match.** Confirm the current branch is the MR's source branch via `glab mr view --output json`. If not, stop and tell the user — switching branches mid-review is the user's call, not yours.
- **Never post without confirmation.** Even if every finding looks great, present the checklist and wait for the user to pick. Posting to GitLab is irreversible (notifications fire, threads exist forever).
- **Presentation and curation prompts never share a turn.** Step 7a (discrepancy report + finding summaries + overview table) must end the assistant turn; the first `AskUserQuestion` goes in a *later* turn, after the user has replied. A same-turn prompt visually preempts the analysis — the dialog takes focus and the user picks findings without having read the verification results. Text order within a turn does not count as "presenting before prompting".
- **`AskUserQuestion` has no default-checked option.** All checkboxes always start empty. Do not write "PRE-CHECKED" in option labels and expect them to be selected — they will not be. The skill works around this by splitting curation into two sequential prompts — Recommended first, then Optional (see Step 7).
- **Content-Type header is mandatory** when calling `glab api ... --input -` to create a discussion. Without it GitLab returns HTTP 415. Full position-payload rules and a worked example live in [references/glab-diff-notes.md](references/glab-diff-notes.md). Don't re-derive them.
- **Sub-agents that verify findings must read the actual files**, not summaries. The whole point is to catch hallucinated or out-of-date findings — that only works if they look at current code at the MR's tip.
- **Honor `--dry-run`.** If the user invokes `/mr-review --dry-run` (or types "dry run" in the same message), build the payloads and print them as the receipt instead of POSTing. Posting to GitLab is irreversible; dry-run is how the user can sanity-check the anchor lines and body text before committing to the notifications.

## Workflow

### 1. Detect the MR and load the diff

First gate on the tool:

```bash
if [ "${AI_SKILLS_MR_TOOL:-gh}" != "glab" ]; then
  echo "STOP: /mr-review is GitLab-only. Set AI_SKILLS_MR_TOOL=glab to use it."
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

Get the unified diff so later steps can identify added/removed/context lines for accurate position payloads:

```bash
git fetch origin "$(glab mr view -F json | jq -r .target_branch)" --quiet
BASE_SHA=$(git merge-base "origin/$(glab mr view -F json | jq -r .target_branch)" HEAD)
HEAD_SHA=$(git rev-parse HEAD)
git diff --unified=0 "$BASE_SHA"..HEAD
```

> **Note:** `BASE_SHA` here is the local merge-base for line-math during posting. The `diff_refs.base_sha` from GitLab is what goes in the position payload — keep them distinct.

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

### 5. Run parallel-code-review

Invoke the finder skill:

```
Skill: parallel-code-review
```

Pass it the `BASE_SHA` and `HEAD_SHA` computed in Step 1. It fans out 5
dimension-specialist reviewers and returns the deduped findings list. (Fall back to
`superpowers:requesting-code-review` only if `parallel-code-review` is unavailable.)
When it produces findings, capture them in a structured list:

```
[
  {
    "id": "F1",
    "severity": "high|medium|low|nit",
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

For every finding, dispatch a sub-agent **in parallel** (single message, many tool calls). Each sub-agent gets a self-contained brief:

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

**7a. Pre-prompt presentation.** Print — in this order:

1. **The discrepancy report from step 4** in plain text. Not selectable; it's context the user needs to decide what to post.
2. **A short summary of each finding** — one block per finding. This is the *detail layer*; the checkbox options later stay minimal because the detail already lives here. Each block carries three labelled lines so the user reads the issue and the fix together:

   ```
   ### F1 — [medium] service.py:62 — every dropdown click rewrites user_roles
   **Issue:** <2–4 sentences: what's wrong and why it matters>
   **Suggested fix:** <the recommendation, made concrete — the actual change to post>
   **Verification:** ✓ issue real, fix sound   (or ⚠ risky: <caveat> / ⚠ corrected diagnosis: <…>)
   ```
3. **An overview table at the end** — the *scan layer* the user reads right before ticking:

   ```
   | ID | Sev | Anchor | Real? | Fix sound? | Bucket |
   |----|-----|--------|-------|------------|--------|
   | F1 | medium | service.py:62 | ✓ yes | ⚠ risky | Recommended |
   | F2 | medium | test_routes.py:107 | ✓ yes | ✓ yes | Recommended |
   | F3 | low | (file-level) | ✓ yes | ✓ yes | Optional |
   ```

**Then END YOUR TURN.** The presentation must be a complete assistant message with **no `AskUserQuestion` in the same turn**. The question dialog takes over the screen the moment it fires, so a same-turn prompt buries the analysis above an active dialog and the user decides unread. Putting the report "before" the prompt *within one turn* does **not** satisfy this step — "before" means a turn boundary, not text order. Wait for the user's reply (an acknowledgment like "go", a question about a finding, or a re-classification request) and only then send the first curation prompt from 7c. This reply beat is also where the user can interrogate a finding or move it between buckets *before* the checkbox dialog frames the decision.

Do not skip straight from verification results to the prompt — the summaries and table are what let the user answer the checkboxes without scrolling back through the session.

**7b. Classify every finding into exactly one bucket:**

| Bucket | Rule | Prompt it goes into |
|---|---|---|
| **Recommended** | `issue_real ∈ {yes, partial}` AND `fix_sound != no` AND (severity ∈ {`critical`, `high`, `medium`} OR the corrected diagnosis is materially useful even at `low`) | Prompt 1 — "Confirm to post" |
| **Optional** | `issue_real ∈ {yes, partial}` but severity is `low`/`nit`, OR `fix_sound == risky` (real but suggestion has caveats) | Prompt 2 — "Optional additions" |
| **Excluded** | `issue_real == no` (verified false positive), OR sub-agent recommends declining | Not shown as a selectable option. Listed in the discrepancy report instead. |

> **Precedence:** the rules overlap for a `medium`+ finding with `fix_sound == risky` — the risky clause wins and the finding goes to **Optional**, regardless of severity. A real issue whose suggested fix has caveats should not be posted on the skill's recommendation; the user opts in with the caveat visible in the badge.

> **Why `partial` belongs in Recommended for Critical findings.** A `partial` verdict often means the *bug* is real but the reviewer's diagnosis of *how* it triggers was wrong. The sub-agent provides a corrected diagnosis; that corrected version is the one that gets posted. Down-rating it to Optional would defeat the verification step's whole purpose.

**7c. Two sequential prompts** (works around `AskUserQuestion`'s no-default-checked limitation):

- **Prompt 1: "These N findings are recommended for posting. Tick all you want to send."** A standalone `AskUserQuestion` call containing **only** the Recommended bucket. Make the question text explicit: every option in this list is one the skill recommends posting. The user ticks to confirm, unticks to drop. **Wait for the answer before sending prompt 2.**
- **Prompt 2: "Optional additions — none recommended, but you may still want to post some."** A second, separate `AskUserQuestion` call containing **only** the Optional bucket. Empty selection is the expected default; user ticks to opt in.

Do **not** combine both buckets into a single `AskUserQuestion` call with two questions — the recommended picks deserve the user's full attention before the optional list competes for it. Skip a prompt entirely when its bucket is empty.

If a bucket exceeds 4 options, batch within the bucket across consecutive prompts (1a, 1b, ... then 2a, 2b, ...) — never mix buckets in one prompt, and finish all Recommended prompts before the first Optional one. Group by severity inside each batch so heavy hitters come first.

**Keep options minimal** — the detail already appeared in 7a. Label: `[F3 medium] auth/repositories.py:128 — every dropdown click rewrites user_roles` (ID + severity + anchor + headline). The `description` field carries **only** the verification badge — `✓ verified, fix sound`, `⚠ corrected: triggers on sort/filter, not first load`, `⚠ fix requires repo refactor — bigger than one line` — no summary sentences.

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

## Body formatting for diff notes

Each note body should follow this shape so reviewers see consistent, scannable comments:

```markdown
**<short title>** — severity: <high|medium|low|nit>

<issue paragraph: what's wrong and why it matters>

**Suggested fix:** <recommendation in 1–3 sentences>
```

Don't paste the entire finding object. Don't include verification metadata in the body (that's for you, not the MR audience). Keep it under ~120 words per note — long notes get skimmed.

## Failure modes to watch for

- **`AI_SKILLS_MR_TOOL` is not `glab`** — stop early with a message pointing at the Config section. Don't attempt the workflow with `gh`; the diff-note API shape is completely different.
- **`glab mr view` returns nothing** — no MR on the branch. Tell the user, suggest `glab mr create --draft` if they want one (omit `--reviewer` unless `$AI_SKILLS_REVIEWERS` is set), and stop.
- **Stale local branch** — if `git fetch` shows the remote has commits you don't, the review will be against old code. Surface this and ask whether to pull first.
- **`parallel-code-review` returns no findings** — perfectly valid. Still produce the discrepancy report from step 4 (if any) and stop without posting.
- **Tracker MCP unavailable** — proceed without the ticket. Note "ticket unavailable" in the discrepancy report.
- **Findings with invented line numbers** — when the sub-agent reports `issue_real: no` because the cited line doesn't contain the cited problem, treat it as a hallucination, not a real finding.

## Why this shape

Code review skills tend to over-trigger findings (false positives) because LLMs pattern-match on diff text without considering surrounding context or whether the recommendation actually fits the codebase's conventions. The verification fan-out exists to catch that *before* the user has to filter manually in a checklist of 30 items. The discrepancy report exists because finding-level review misses the larger question: "is this MR doing what it claims?" — which is often where the biggest issues live.

The **two sequential curation prompts** in Step 7 are a workaround for `AskUserQuestion`'s lack of a default-checked field — but the split has a secondary benefit: separating *recommended* from *optional* makes the user's job a one-handed scan-and-tick on the recommended prompt rather than a careful read of every item to decide what's worth posting. Don't collapse the two prompts back into one "everything goes here" list, and don't merge them into a single dialog call with two questions side by side — both lose the recommendation signal's priority. The prompts stay **minimal** (ID + anchor + headline + badge) because the *summaries-then-table* presentation in 7a already carried the detail: summaries are the detail layer, the overview table is the scan layer, and the checkboxes are just the decision layer. Cramming finding detail into option descriptions duplicates 7a and makes the dialog unscannable.

The **turn-break between 7a and 7c** exists because of an observed failure, not theory: a run that emitted the full report and the first `AskUserQuestion` in one turn technically satisfied "print before the prompt", but the dialog seized the screen and the user was asked to curate findings they had never seen. Reading requires a turn the user gets to finish; any wording that lets the presentation and the prompt share a turn re-opens that hole.

The **dry-run** mode exists because the first time you run `/mr-review` on a real MR, you don't yet know whether the line-anchor math is right for this codebase's file layout. Posting eight diff notes to the wrong lines is irreversible and noisy; running the same flow with `--dry-run` first costs one round trip and catches anchor bugs before the team sees them.

The **two-stage open-MR resolution** in Step 1 handles the common case where a branch has accumulated multiple MRs across iterations — typically one open and one or more closed/merged. Auto-picking the single open MR matches user intent virtually every time; only stop if the disambiguation is genuinely ambiguous (two opens, or zero opens).
