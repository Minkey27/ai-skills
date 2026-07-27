---
name: handoff
description: Compact the current conversation into a handoff document for another agent to pick up.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

# handoff

Write a handoff document summarising the current conversation so a fresh agent can
continue the work. Save to the temporary directory of the user's OS — not the
current workspace.

## Rules

- **Reference, don't duplicate.** Anything already captured in another artifact
  (spec, plan, ADR, issue, MR/PR, commit, diff, test output file) gets a path or
  URL, not a retelling. The next agent can read those itself; what it cannot
  recover is the conversation.
- **Redact secrets.** No API keys, tokens, passwords, connection strings, or
  personally identifiable information. Replace with `[REDACTED: <what it was>]`.
- **Tailor to the argument.** If the user passed arguments, treat them as a
  description of what the next session will focus on: lead with that, keep detail
  that serves it, and compress the rest to one-liners.
- **Write what a cold reader needs.** No "as discussed above", no pronouns
  pointing at this conversation. The reader has zero context.

## Steps

1. **Resolve the focus.** Arguments given → that is the next session's job.
   No arguments → infer from the last thing being worked on, and say so
   explicitly in the doc so the reader can correct course.

2. **Gather ground truth before writing.** Don't trust conversation memory for
   state that has a source:
   ```bash
   git status --short && git log --oneline -10 && git diff --stat
   ```
   Note the branch, whether the tree is dirty, and which work is committed vs.
   still in the working tree.

3. **Pick the output path.** Use the OS temp dir, resolved at write time:
   ```bash
   echo "${TMPDIR:-/tmp}"
   ```
   Filename: `handoff-<slug>-<YYYY-MM-DD>.md`, where `<slug>` is a short
   kebab-case topic (ticket id if there is one, e.g. `handoff-BPZ-892-2026-07-27.md`).
   Get the date from `date +%F` — never guess it.

4. **Write the document** with the template below. Omit sections that would be
   empty; never pad with "N/A" rows.

5. **Report the absolute path** back to the user, plus a one-line note on how to
   use it (`Read <path>` in the new session).

## Template

```markdown
# Handoff: <topic>

**Focus of the next session:** <one or two sentences — from the arguments, or
inferred and flagged as inferred>

**Repo:** <path>  **Branch:** <branch>  **Tree:** clean | dirty (see below)

## State

Where things stand right now. What is done, what is half-done, what is untouched.
Committed work as SHAs; uncommitted work as file paths.

## Artifacts

| What | Where |
|---|---|
| Plan / spec | `path/to/plan.md` |
| MR / PR | <url> |
| Ticket | <url or id> |
| Test output | `path` |

## Context not in any artifact

The reason this document exists. Decisions made and *why*, options rejected and
why, constraints the user stated, dead ends already explored, user preferences
expressed during the session. Nothing here should be recoverable by reading the
diff.

## Open questions / blockers

Numbered, each with what is blocked on it. Mark anything that needs a human
answer before work can continue.

## Next steps

Numbered, ordered, concrete. First item should be actionable without any
clarification.

## Suggested skills

Skills the next agent should invoke, and when:

- `<skill-name>` — <why / at which step>

## Verification

How to check the work is correct — the exact commands, not a description of them.
```

## Suggested-skills section

This section is required. Populate it by matching the next session's focus
against skills that are actually available, and say *when* each one applies
rather than just naming it. Typical picks:

| Situation | Skill |
|---|---|
| Any test run | the project's test-runner skill (e.g. `pytest-docker`) |
| Implementation continuing from a written plan | `superpowers:executing-plans` |
| Feature/design work not yet specced | `superpowers:brainstorming` |
| A reproducing bug | `superpowers:systematic-debugging` |
| Work is finished, needs review + MR | `finalize-branch` |
| Rebasing / squashing before review | `rebase-on-main`, `squash` |
| Review feedback waiting on an MR | `process-mr-feedback` |

If a project rule mandates a skill (e.g. "always invoke X before pytest"), carry
that mandate into the section verbatim — the next agent's `CLAUDE.md` may load it
too, but redundancy is cheap and a missed mandate is not.

## Anti-patterns

- Pasting diffs or file contents into the doc. Link them.
- A blow-by-blow transcript of the conversation. The reader wants state and
  reasons, not history.
- Vague next steps ("continue the implementation"). Name the file and the change.
- Writing into the workspace. It is a scratch artifact, not a project file.
