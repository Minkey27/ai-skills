---
name: posting-mr-notes
description: Use when about to post anything to a GitLab merge request on the user's behalf, such as diff notes, general MR comments, replies to review threads, or resolving threads, and whenever such a post was denied by the auto mode classifier ("External System Writes", "Blocked by classifier").
---

# Posting MR notes

## Overview

**Spell the script path in full, never with `~`.** Below, `<home>` stands for the user's home directory written out, such as `/Users/you`. The allow rule `Bash(python3 <home>/.claude/skills/posting-mr-notes/scripts/post_notes.py:*)` in `~/.claude/settings.json` matches the literal command text, and the permission check does not expand `~` or `$HOME`. Auto mode keeps a narrow rule like this, but a command spelled `python3 ~/.claude/...` misses it and falls through to the classifier, which can deny the post even though the script is allowlisted.

The auto mode classifier judges a post on two things: **did the user just ask for this exact post**, and **can it see what is being posted**. Authorization decides; command shape only tips a borderline case. A post is denied when the latest "user" signal is not an instruction to post: a `y` to a push question, a subagent report, or gate defaults applied on silence (step 3).

## The protocol

1. **Write the batch file with the `Write` tool.** One JSON file in the scratchpad, format in the docstring of `scripts/post_notes.py` (kinds: `diff`, `thread`, `general`, `reply`, with optional `"resolve": true`). A finding with no line goes up as a `thread`, never `general`: a `general` note is a plain comment nobody can resolve, and tools that collect open threads skip it. Bodies live in JSON and never pass through a shell, so backticks and quotes arrive intact.
2. **Dry run it:**
   `python3 <home>/.claude/skills/posting-mr-notes/scripts/post_notes.py --dry-run /abs/batch.json`
   Check anchors and body text in the output.
3. **Check the authorization predicate.** Posting is allowed only when the user's most recent chat message tells you to post this batch ("post it", "go", "post F2 and F4").

   One standing exception: a `/finalize-branch` run is the ask for its own review threads on the user's own MR, since posting them is that skill's last step.

   Otherwise, end the turn with the receipt table and one line: `Reply "post" to send N notes to MR !<iid>.` A `y` to a different question (push, commit), a subagent's hand-back and a gate that timed out or was closed are not authorization.
4. **Post with one bare command, alone in its Bash call:**
   `python3 <home>/.claude/skills/posting-mr-notes/scripts/post_notes.py /abs/batch.json`
   Copy the prefix character for character, with `<home>` written out. No `~`, no `cd`, no variable assignment, no `sed` to patch the batch, no heredoc, no pipe. To change a note, `Edit` the JSON file first, then run the bare command.
5. **Relay the receipt.** Each line carries the note URL, or `FAILED: <glab error>` when the note was not posted, or the URL followed by `RESOLVE FAILED: <glab error>` when the note posted but the thread stayed open. Fix `FAILED` notes in the JSON and post a batch holding only those. Never re-post a `RESOLVE FAILED` note, because it is already on the MR; tell the user which thread still needs resolving.

## When the classifier denies the post

Stop. The denial covers the outcome, so retrying, splitting the batch, switching to `glab api` directly or moving it into a subagent all pursue the same blocked outcome. Instead:

1. Say plainly whether anything was posted (the receipt says which).
2. Show the exact bare command from step 4.
3. Ask the user to reply "post", or to run it themselves as `! python3 ...`.

After their reply, run the bare command once.

## Quick reference

| Symptom | Cause | Fix |
|---|---|---|
| Denied, reason names "without the user asking" | latest user message was about something else | ask in chat, wait for "post" |
| Denied right after a subagent report | the report is the latest message and carries no user authority | ask in chat |
| Denied although `post_notes.py` is allowlisted | the command missed the rule: `~`, `$HOME`, a relative path, or a `cd`, `&&` or pipe around it | ask in chat; after "post", run the exact step 4 command |
| Denied `[Credential Materialization]` | read the token (`glab config get token`, `--show-token`) | never touch the token; `glab api` is already authenticated |
| HTTP 415 | JSON body sent without `Content-Type` | the script sets it; hand-rolled calls need `--header "Content-Type: application/json"` |
| Code spans vanish from the posted note | body passed through `python3 -c "..."` or `echo` in zsh | bodies only in the JSON file |
| 400 `line_code can't be blank` | context line outside a rendered hunk | re-anchor on an added or removed line nearby |

## Common mistakes

- **Treating silence as consent.** A gate that came back empty or closed means post nothing.
- **Shortening the path to `~/.claude/...`.** The shell expands it, the permission check does not, so the allowlist misses.
- **Patching the batch and posting in one call** (`sed -i ... && python3 ...`). A compound command passes only when every part matches a rule, so the allowlist misses and the classifier sees a script it cannot read.
- **Hand-rolling a new post script per session.** The shared script keeps the command identical every time.
