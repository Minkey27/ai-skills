---
name: requesting-code-review
description: Use when finalize-branch or mr-review asks for it, or when the user wants a one-pass review of a git commit range (e.g. a GitHub PR) — dispatches one read-only reviewer subagent (plan fit and code quality, plus a correctness bug hunt) and returns a prose review (Strengths / Critical / Important / Minor / Assessment).
---

# Requesting Code Review

Dispatch one reviewer subagent over a git range. Give it only the context below — never your session history — so it judges the work product, not your reasoning.

## 1. Get the SHAs

```bash
BASE_SHA=$(git merge-base origin/main HEAD)   # or the caller's base
HEAD_SHA=$(git rev-parse HEAD)
```

## 2. Dispatch the reviewer

Dispatch a `general-purpose` subagent with the prompt in [code-reviewer.md](code-reviewer.md), filling its placeholders:

- `[DESCRIPTION]` — what was built
- `[PLAN_OR_REQUIREMENTS]` — what it should do (ticket goal, plan path, or "none available")
- `[BASE_SHA]`, `[HEAD_SHA]` — from step 1

## 3. Hand back the review

The reviewer returns Strengths, Issues (Critical / Important / Minor, each with file:line), Recommendations, and an Assessment. When a calling skill invoked this one, return the review to it unchanged; the caller converts and verifies the issues. Run standalone, fix Critical and Important issues, and push back with evidence where the reviewer is wrong.

Adapted from [superpowers](https://github.com/obra/superpowers) (MIT, see [LICENSE](LICENSE)).
