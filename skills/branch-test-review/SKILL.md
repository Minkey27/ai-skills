---
name: branch-test-review
description: Analyze the pytest tests added or modified on the current branch vs. the merge-base with the base branch (default `main`), then generate a styled, searchable single-file HTML report grouping the tests by file → class → method with collapsible source code and docstring descriptions. Use this skill whenever the user asks to review the tests on a branch, see what tests were added, get an overview of new or changed tests, prepare a "what tests did I write" summary for code review or PR description, or audit a branch's test coverage — even if they don't explicitly say "skill".
---

# Branch Test Review

Generate a self-contained HTML report of the pytest tests added or modified on the current branch, so a reviewer can scan what was tested without flipping between files in the diff.

## When this skill applies

Trigger it when the user wants to:
- Review the tests written on a branch before opening or merging a PR
- Get a structured overview of new/changed tests grouped by file → class → method
- Audit a branch's test coverage at a glance
- Share a "what tests were added" summary with a teammate

## Core idea

Compare the working tree to the **merge-base** of `HEAD` with the base branch (default `main`, fallback `master`). Only test functions whose source overlaps a hunk in that diff are included. This deliberately excludes commits that landed on the base branch but haven't been rebased into this branch yet — the user explicitly does **not** want those in the report.

For every included test, the report shows:
- File path → optional test class → test method name
- The test's docstring (left blank and visibly muted if missing — undocumented tests are worth surfacing)
- The **per-test git diff** — coloured +/- lines from `git diff <merge-base> HEAD` for the hunks that overlap that test, so a reviewer can see exactly what changed without leaving the page. Diffs are **collapsed by default** with a one-line summary (`Diff · N hunks · +X −Y`) and have a dedicated "Show diffs" toolbar button (shortcut `D`). "Expand all" deliberately skips diff blocks to keep the view readable; "Collapse all" still closes them so a single button resets the page.
- The full source of the test, collapsed by default and expandable on click, with Python syntax highlighting
- Per-file and overall counts

For tests in a newly added file, the diff section is replaced with a short "new file" notice (the full source already represents the change). For tests included via `--include-all` that have no overlapping diff hunks, the diff section says so explicitly.

The HTML supports live search, a "only tests without docstring" filter, expand-all / collapse-all, and remembers state purely with `<details>` elements (it works without JavaScript for basic viewing).

## How to run it

From the repo root, run the bundled script:

```bash
python scripts/generate_report.py
```

The script's path relative to this skill is `scripts/generate_report.py`. Invoke it with an absolute path from wherever Claude finds the skill on disk.

### Options

- `--base <branch>` — Base branch to compare against. Defaults to `main`, with `master` as a fallback if `main` doesn't exist.
- `--output <path>` — HTML file to write. Defaults to `branch-tests-review.html` in the current directory.
- `--repo <path>` — Repo root. Defaults to the current directory.
- `--include-all` — Include every test in any changed test file, not just tests whose lines changed. Useful when the user wants surrounding context.

### Workflow Claude should follow

1. Confirm with the user which base branch to compare against if it isn't obvious. `main` is the safe default.
2. Run the script from the repo root.
3. Surface the path to the HTML file. Don't paste large test source blocks into chat — the HTML is the deliverable.
4. Briefly summarise the result: total tests, files touched, and call out any test missing a docstring (since "blank description" is the main signal worth flagging by eye).
5. If the script reports "no test changes detected", say so plainly and confirm the base branch was right — usually that means the user is sitting on the base branch, or has rebased away all their work.

## What counts as a "test"

The script treats any function whose name starts with `test` as a test, whether it's at module level or inside a top-level class. It uses Python's `ast` module, so there's no dependency on pytest plugins or import side effects — the file just has to parse.

A test is considered "changed" if any of its lines (including decorators) overlap a hunk in `git diff <merge-base> HEAD` for that file. Files added on this branch are treated as fully new — every test in them is included.

`conftest.py` is intentionally excluded. Fixtures aren't tests.

## Constraints

- Python 3.10+, `git` on PATH, no third-party Python packages required.
- The HTML loads Prism from a CDN for syntax highlighting. If the user is offline, the page still renders — code blocks just won't be coloured.
- Works on any Python repo using pytest-style tests, not just one project.
