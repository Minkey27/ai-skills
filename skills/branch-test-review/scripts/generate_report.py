#!/usr/bin/env python3
"""Generate an HTML report of pytest tests added or modified on the current branch.

The report compares HEAD against the merge-base with a base branch (default `main`),
finds test functions whose lines overlap diff hunks, and renders them grouped by
file -> class -> method with collapsible source code.

Usage:
    python generate_report.py [--base main] [--output branch-tests-review.html]
                              [--repo .] [--include-all]
"""
from __future__ import annotations

import argparse
import ast
import html
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ----------------------------- git plumbing -----------------------------


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command; raise on non-zero exit."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _git_or_none(args: list[str], cwd: Path) -> str | None:
    try:
        return _git(args, cwd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def find_merge_base(repo: Path, base_branch: str) -> tuple[str, str]:
    """Resolve a merge-base sha. Returns (sha, ref_used).

    Tries `<base>`, then `origin/<base>`, then falls back to the other common
    default name (main <-> master).
    """
    candidates: list[str] = [base_branch, f"origin/{base_branch}"]
    fallback = "master" if base_branch == "main" else "main" if base_branch == "master" else None
    if fallback:
        candidates += [fallback, f"origin/{fallback}"]

    tried = []
    for ref in candidates:
        if _git_or_none(["rev-parse", "--verify", "--quiet", ref], cwd=repo) is None:
            continue
        sha = _git_or_none(["merge-base", "HEAD", ref], cwd=repo)
        if sha:
            return sha.strip(), ref
        tried.append(ref)

    raise SystemExit(
        f"Could not resolve a merge-base. Tried refs: {candidates}. "
        f"Pass --base <branch> to point at the correct base branch."
    )


def changed_files(repo: Path, merge_base: str) -> list[tuple[str, str]]:
    """Files changed between merge_base and HEAD as (status, path)."""
    out = _git(["diff", "--name-status", merge_base, "HEAD"], cwd=repo)
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][:1]  # A, M, R, D, C, T
        path = parts[-1]
        rows.append((status, path))
    return rows


def file_in_base(repo: Path, merge_base: str, path: str) -> bool:
    return _git_or_none(["cat-file", "-e", f"{merge_base}:{path}"], cwd=repo) is not None


def changed_line_ranges(repo: Path, merge_base: str, path: str) -> list[tuple[int, int]]:
    """Return (start, end) line ranges in HEAD that differ from merge_base for `path`."""
    diff = _git_or_none(
        ["diff", "--unified=0", merge_base, "HEAD", "--", path], cwd=repo
    )
    if not diff:
        return []
    ranges: list[tuple[int, int]] = []
    for line in diff.splitlines():
        if not line.startswith("@@"):
            continue
        # Header looks like: @@ -a,b +c,d @@ optional context
        try:
            after = line.split("+", 1)[1]
            spec = after.split(" ", 1)[0]
            if "," in spec:
                start_s, count_s = spec.split(",", 1)
                start = int(start_s)
                count = int(count_s)
            else:
                start = int(spec)
                count = 1
            if count <= 0:
                # Pure deletion, no new lines on this side
                continue
            ranges.append((start, start + count - 1))
        except (IndexError, ValueError):
            continue
    return ranges


@dataclass
class DiffHunk:
    """A single @@ hunk parsed from `git diff` output.

    `lines` is a list of (marker, text) pairs where marker is one of
    ' ' (context), '+' (added), '-' (removed), or '\\' (no-newline marker).
    `new_start`/`new_count` are inclusive of any leading/trailing context the
    `--unified=<n>` setting added — so the new-side range is suitable for
    overlap checks against test line ranges.
    """

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)

    @property
    def new_range(self) -> tuple[int, int]:
        if self.new_count <= 0:
            # Pure deletion: anchor at new_start so it can still attach to a
            # test whose body straddles the deleted region.
            return (self.new_start, self.new_start)
        return (self.new_start, self.new_start + self.new_count - 1)

    @property
    def header(self) -> str:
        return (
            f"@@ -{self.old_start},{self.old_count} "
            f"+{self.new_start},{self.new_count} @@"
        )


def parse_diff_hunks(
    repo: Path, merge_base: str, path: str, context: int = 3
) -> list[DiffHunk]:
    """Parse `git diff --unified=<context>` for `path` into structured hunks."""
    diff = _git_or_none(
        ["diff", f"--unified={context}", merge_base, "HEAD", "--", path],
        cwd=repo,
    )
    if not diff:
        return []

    def _parse_spec(spec: str) -> tuple[int, int]:
        if "," in spec:
            a, b = spec.split(",", 1)
            return int(a), int(b)
        return int(spec), 1

    hunks: list[DiffHunk] = []
    current: DiffHunk | None = None
    in_body = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            # @@ -a,b +c,d @@ optional context
            try:
                _, rest = line.split("-", 1)
                old_spec, after = rest.split(" +", 1)
                new_spec = after.split(" @@", 1)[0]
                old_start, old_count = _parse_spec(old_spec.strip())
                new_start, new_count = _parse_spec(new_spec.strip())
            except (IndexError, ValueError):
                in_body = False
                continue
            if current is not None:
                hunks.append(current)
            current = DiffHunk(old_start, old_count, new_start, new_count, [])
            in_body = True
            continue
        if not in_body or current is None:
            continue
        if not line:
            # Blank line inside a hunk represents an empty context line.
            current.lines.append((" ", ""))
            continue
        marker = line[0]
        if marker in (" ", "+", "-", "\\"):
            current.lines.append((marker, line[1:]))
    if current is not None:
        hunks.append(current)
    return hunks


# ----------------------------- test discovery -----------------------------


def is_test_file(path: str) -> bool:
    p = Path(path)
    if p.suffix != ".py":
        return False
    if p.name == "conftest.py":
        return False
    if p.name.startswith("test_") or p.name.endswith("_test.py"):
        return True
    return any(part in ("tests", "test") for part in p.parts)


@dataclass
class TestEntry:
    cls: str | None
    name: str
    docstring: str
    start_line: int
    end_line: int
    source: str


def extract_tests(file_path: Path) -> list[TestEntry]:
    """Parse a Python test file and return its test functions/methods."""
    try:
        source = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    source_lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    tests: list[TestEntry] = []

    def _is_test(node: ast.AST) -> bool:
        return (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        )

    def _entry(node, cls: str | None) -> TestEntry:
        start = node.lineno
        if node.decorator_list:
            start = min(start, min(d.lineno for d in node.decorator_list))
        end = node.end_lineno or start
        doc = ast.get_docstring(node, clean=True) or ""
        body = "\n".join(source_lines[start - 1 : end])
        return TestEntry(
            cls=cls,
            name=node.name,
            docstring=doc,
            start_line=start,
            end_line=end,
            source=body,
        )

    for node in tree.body:
        if _is_test(node):
            tests.append(_entry(node, None))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if _is_test(sub):
                    tests.append(_entry(sub, node.name))

    return tests


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] < b[0] or b[1] < a[0])


@dataclass
class FileBucket:
    file: str
    is_new: bool
    tests: list[TestEntry] = field(default_factory=list)
    hunks: list[DiffHunk] = field(default_factory=list)


def collect(
    repo: Path, merge_base: str, include_all_in_changed_files: bool
) -> list[FileBucket]:
    buckets: list[FileBucket] = []
    for status, path in changed_files(repo, merge_base):
        if status == "D":
            continue
        if not is_test_file(path):
            continue
        abs_path = repo / path
        if not abs_path.is_file():
            continue
        all_tests = extract_tests(abs_path)
        if not all_tests:
            continue

        is_new = not file_in_base(repo, merge_base, path)
        # Hunks are only useful for modified files; for new files the full
        # source already represents the change.
        file_hunks = (
            [] if is_new else parse_diff_hunks(repo, merge_base, path, context=3)
        )
        if is_new or include_all_in_changed_files:
            chosen = all_tests
        else:
            hunk_ranges = changed_line_ranges(repo, merge_base, path)
            if not hunk_ranges:
                continue
            chosen = [
                t
                for t in all_tests
                if any(overlaps((t.start_line, t.end_line), h) for h in hunk_ranges)
            ]
        if chosen:
            buckets.append(
                FileBucket(file=path, is_new=is_new, tests=chosen, hunks=file_hunks)
            )

    buckets.sort(key=lambda b: b.file)
    return buckets


# ----------------------------- HTML rendering -----------------------------


CSS = r"""
/* ============================================================
   Theme tokens — light is the default, dark overrides via
   [data-theme="dark"] on <html>. prefers-color-scheme is
   honoured on first load via the inline boot script in <head>.
   ============================================================ */
:root,
:root[data-theme="light"] {
  --bg: #f4f3ee;
  --surface: #ffffff;
  --surface-alt: #fbf9f3;
  --surface-hover: #f1efe7;
  --border: #e4e2d8;
  --border-strong: #cfcbbc;
  --text: #1c1c1a;
  --text-muted: #6a6862;
  --text-faint: #9a9789;
  --accent: #2a55c9;
  --accent-soft: #e2ecff;
  --accent-fg: #ffffff;
  --badge-new-bg: #e1f3e7;
  --badge-new-fg: #1a6a36;
  --badge-mod-bg: #fef0dc;
  --badge-mod-fg: #8a5500;
  --badge-nodoc-bg: #fbe6e6;
  --badge-nodoc-fg: #8a1c1c;
  --code-bg: #faf8f1;
  --code-border: #e8e5d8;
  --code-gutter-bg: #f3f0e6;
  --code-gutter-fg: #b3ae9b;
  --code-gutter-border: #e4e2d8;
  --code-fg: #1c1c1a;
  --code-shadow: 0 1px 0 rgba(0,0,0,0.02);
  --shadow-card: 0 1px 2px rgba(20,20,15,0.04), 0 1px 1px rgba(20,20,15,0.03);
}

:root[data-theme="dark"] {
  --bg: #131210;
  --surface: #1c1b17;
  --surface-alt: #24221d;
  --surface-hover: #2a2722;
  --border: #2f2c25;
  --border-strong: #423e34;
  --text: #ebe7da;
  --text-muted: #a39e8d;
  --text-faint: #6f6a5d;
  --accent: #7aa7ff;
  --accent-soft: #1d2a4a;
  --accent-fg: #0c1326;
  --badge-new-bg: #163a25;
  --badge-new-fg: #8de0a8;
  --badge-mod-bg: #3a2a10;
  --badge-mod-fg: #f3c374;
  --badge-nodoc-bg: #3a1818;
  --badge-nodoc-fg: #f0a0a0;
  --code-bg: #16140f;
  --code-border: #2a2820;
  --code-gutter-bg: #1a1814;
  --code-gutter-fg: #5a574c;
  --code-gutter-border: #2a2820;
  --code-fg: #ebe7da;
  --code-shadow: 0 1px 0 rgba(0,0,0,0.4) inset;
  --shadow-card: 0 1px 2px rgba(0,0,0,0.4);
}

/* ============================================================
   Base
   ============================================================ */
* { box-sizing: border-box; }
html { background: var(--bg); color-scheme: light; }
html[data-theme="dark"] { color-scheme: dark; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
               Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 14px;
  line-height: 1.55;
  color: var(--text);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

/* Smooth (but quick) theme transitions on the bits that swap colour */
body,
header.page,
.toolbar,
details.file,
details.test > summary,
.test-body,
.codeblock,
.codeblock-head,
pre,
.test-doc-full,
.badge,
.btn,
.input,
.kbd {
  transition: background-color 140ms ease, border-color 140ms ease,
              color 140ms ease, box-shadow 140ms ease;
}

/* ============================================================
   Header
   ============================================================ */
header.page {
  padding: 28px 32px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
header.page h1 {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.012em;
}
header.page .meta {
  color: var(--text-muted);
  font-size: 13px;
}
header.page code {
  background: var(--surface-alt);
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  font-size: 12px;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
}
.stats {
  margin-top: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 14px 24px;
  font-size: 13px;
  color: var(--text-muted);
}
.stats b {
  color: var(--text);
  font-weight: 600;
  margin-right: 4px;
  font-variant-numeric: tabular-nums;
}

/* ============================================================
   Toolbar
   ============================================================ */
.toolbar {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 12px 32px;
  background: color-mix(in srgb, var(--surface) 92%, transparent);
  backdrop-filter: saturate(140%) blur(8px);
  -webkit-backdrop-filter: saturate(140%) blur(8px);
  border-bottom: 1px solid var(--border);
}
.input {
  flex: 1 1 280px;
  min-width: 200px;
  padding: 8px 12px 8px 34px;
  font-size: 13px;
  border: 1px solid var(--border-strong);
  border-radius: 7px;
  background: var(--surface-alt);
  color: var(--text);
  font-family: inherit;
  background-image: var(--search-icon);
  background-repeat: no-repeat;
  background-position: 10px center;
  background-size: 14px 14px;
}
.input::placeholder { color: var(--text-faint); }
.input:focus {
  outline: 3px solid var(--accent-soft);
  border-color: var(--accent);
}
.toolbar label.check {
  font-size: 12px;
  color: var(--text-muted);
  display: inline-flex;
  align-items: center;
  gap: 7px;
  user-select: none;
  cursor: pointer;
  padding: 6px 10px;
  border-radius: 7px;
  border: 1px solid transparent;
}
.toolbar label.check:hover { background: var(--surface-hover); }
.toolbar label.check input { accent-color: var(--accent); }
.btn {
  font-size: 12px;
  font-family: inherit;
  background: var(--surface-alt);
  border: 1px solid var(--border-strong);
  color: var(--text);
  padding: 6px 11px;
  border-radius: 7px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  line-height: 1;
}
.btn:hover { background: var(--surface-hover); border-color: var(--text-muted); }
.btn:active { transform: translateY(1px); }
.btn.icon { padding: 6px 8px; }
.btn svg { width: 14px; height: 14px; }
.kbd {
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
  padding: 1px 5px;
  background: var(--surface-alt);
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  color: var(--text-muted);
}

/* ============================================================
   Main
   ============================================================ */
main {
  padding: 18px 32px 56px;
  max-width: 1180px;
  margin: 0 auto;
}
.empty {
  margin: 96px auto;
  text-align: center;
  color: var(--text-muted);
}
.empty h2 {
  margin: 0 0 6px;
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}

/* ============================================================
   File / class / test cards
   ============================================================ */
details.file {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 9px;
  margin-bottom: 12px;
  overflow: hidden;
  box-shadow: var(--shadow-card);
}
details.file > summary {
  list-style: none;
  cursor: pointer;
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  background: var(--surface);
}
details.file > summary::-webkit-details-marker { display: none; }
details.file[open] > summary {
  border-bottom: 1px solid var(--border);
  background: var(--surface-alt);
}
.caret {
  width: 9px;
  height: 9px;
  display: inline-block;
  border-right: 1.5px solid var(--text-muted);
  border-bottom: 1.5px solid var(--text-muted);
  transform: rotate(-45deg);
  transition: transform 0.18s ease;
  flex: 0 0 auto;
  margin-right: 2px;
}
details[open] > summary .caret { transform: rotate(45deg); }
.file-path {
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 12.5px;
  flex: 1 1 auto;
  word-break: break-all;
  color: var(--text);
}
.file-path .dim { color: var(--text-faint); }
.badge {
  display: inline-flex;
  align-items: center;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  padding: 2px 8px;
  border-radius: 11px;
  flex: 0 0 auto;
  line-height: 1.4;
}
.badge.new   { background: var(--badge-new-bg);   color: var(--badge-new-fg); }
.badge.mod   { background: var(--badge-mod-bg);   color: var(--badge-mod-fg); }
.count {
  font-size: 11.5px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
  flex: 0 0 auto;
}
.file-body { padding: 0 0 4px 0; }

details.cls > summary {
  list-style: none;
  cursor: pointer;
  padding: 9px 16px 9px 30px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
}
details.cls > summary::-webkit-details-marker { display: none; }
details.cls > summary:hover { background: var(--surface-hover); }
.class-name {
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-weight: 600;
  color: var(--text);
  font-size: 12.5px;
}
.module-label {
  font-style: italic;
  color: var(--text-faint);
  font-size: 12px;
}
.tests-body { padding: 0; }

details.test { border-top: 1px solid var(--border); }
details.test > summary {
  list-style: none;
  cursor: pointer;
  padding: 0 16px 0 30px;
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 13px;
  height: 32px;
  line-height: 32px;
  box-sizing: border-box;
}
details.test > summary::-webkit-details-marker { display: none; }
details.test > summary:hover { background: var(--surface-hover); }
details.test[open] > summary { background: var(--surface-alt); }
details.test > summary .caret {
  width: 7px;
  height: 7px;
  border-right: 1.5px solid var(--text-faint);
  border-bottom: 1.5px solid var(--text-faint);
}
.test-name {
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-weight: 500;
  color: var(--text);
  flex: 0 1 auto;
  max-width: 52%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1;
}
.test-desc {
  color: var(--text-muted);
  font-size: 12.5px;
  flex: 1 1 0;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1;
}
.test-desc.empty {
  color: var(--text-faint);
  font-style: italic;
}
.lineref {
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--text-faint);
  margin-left: auto;
  flex: 0 0 auto;
  line-height: 1;
}

/* ============================================================
   Test body — docstring + code block
   ============================================================ */
.test-body {
  padding: 12px 18px 16px 48px;
  background: var(--surface-alt);
  border-top: 1px solid var(--border);
}
.test-doc-full {
  margin: 0 0 12px;
  padding: 9px 14px;
  background: var(--surface);
  border-left: 3px solid var(--accent);
  color: var(--text);
  font-size: 13px;
  white-space: pre-wrap;
  border-radius: 0 6px 6px 0;
  line-height: 1.55;
}
.test-doc-full strong {
  display: block;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-faint);
  margin-bottom: 4px;
}

.codeblock {
  background: var(--code-bg);
  border: 1px solid var(--code-border);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: var(--code-shadow);
}
.codeblock-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 12px;
  border-bottom: 1px solid var(--code-border);
  background: var(--code-gutter-bg);
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--text-faint);
}
.codeblock-head .dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--text-faint); opacity: 0.45;
  flex: 0 0 auto;
}
.codeblock-head .lang {
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
  color: var(--text-muted);
}
.codeblock-head .spacer { flex: 1 1 auto; }
.codeblock-head .copy-btn {
  font-family: inherit;
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 5px;
  border: 1px solid var(--border-strong);
  background: var(--surface);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.codeblock-head .copy-btn:hover { color: var(--text); border-color: var(--text-muted); }
.codeblock-head .copy-btn.ok { color: var(--badge-new-fg); border-color: var(--badge-new-fg); }
.codeblock-head .copy-btn svg { width: 11px; height: 11px; }

.codeblock pre {
  margin: 0;
  background: var(--code-bg);
  color: var(--code-fg);
  padding: 12px 0;
  overflow-x: auto;
  font-size: 12.5px;
  line-height: 1.6;
}
.codeblock code {
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", "SF Mono", Menlo, Consolas, monospace;
  font-variant-ligatures: none;
  white-space: pre;
  display: block;
  padding: 0 16px 0 0;
}

/* Custom line-numbers (independent of any Prism plugin) */
pre.with-lineno {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0;
}
pre.with-lineno .ln {
  user-select: none;
  text-align: right;
  padding: 0 12px 0 14px;
  color: var(--code-gutter-fg);
  background: var(--code-gutter-bg);
  border-right: 1px solid var(--code-gutter-border);
  font-variant-numeric: tabular-nums;
  white-space: pre;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.6;
}
pre.with-lineno > code {
  padding-left: 14px;
}

/* ============================================================
   highlight.js integration
   The github / github-dark stylesheets are loaded from cdnjs and
   bring their own token colors. We only need to make sure their
   .hljs block doesn't paint over our code-block surface.
   ============================================================ */
.codeblock pre code.hljs,
.codeblock pre code {
  background: transparent !important;
  color: var(--code-fg);
  padding: 0 16px 0 14px;
}
/* Keep code blocks readable even before highlight.js attaches */
.codeblock pre code:not(.hljs) { color: var(--code-fg); }

/* ============================================================
   Diff block (collapsible)
   ============================================================ */
details.diff-toggle {
  margin-bottom: 12px;
  border: 1px solid var(--code-border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--code-bg);
  box-shadow: var(--code-shadow);
}
details.diff-toggle > summary.diff-summary {
  list-style: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 12px;
  background: var(--code-gutter-bg);
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 11.5px;
  color: var(--text-muted);
}
details.diff-toggle[open] > summary.diff-summary {
  border-bottom: 1px solid var(--code-border);
}
details.diff-toggle > summary.diff-summary::-webkit-details-marker { display: none; }
details.diff-toggle > summary.diff-summary:hover { background: var(--surface-hover); }
.diff-summary-label {
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
  color: var(--text-muted);
}
.diff-summary-meta { color: var(--text-faint); }
.diff-summary-meta .diff-stat-add {
  color: var(--badge-new-fg);
  font-weight: 600;
}
.diff-summary-meta .diff-stat-del {
  color: var(--badge-nodoc-fg);
  font-weight: 600;
}
.diff-block { background: var(--code-bg); }
.diff-hunk + .diff-hunk { border-top: 1px solid var(--code-border); }
.diff-hunk-header {
  background: var(--code-gutter-bg);
  color: var(--text-muted);
  padding: 4px 14px;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 11.5px;
  border-bottom: 1px solid var(--code-border);
  user-select: none;
}
.diff-line {
  display: grid;
  grid-template-columns: 18px 1fr;
  padding: 0;
  font-family: ui-monospace, "JetBrains Mono", "Fira Code", "SF Mono", Menlo, Consolas, monospace;
  font-variant-ligatures: none;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre;
}
.diff-line .diff-mark {
  text-align: center;
  user-select: none;
  color: var(--text-faint);
  border-right: 1px solid var(--code-gutter-border);
  background: var(--code-gutter-bg);
}
.diff-line .diff-text { padding: 0 12px; white-space: pre; }

.diff-line.diff-add { background: rgba(46, 160, 67, 0.10); }
.diff-line.diff-add .diff-mark {
  color: var(--badge-new-fg);
  background: color-mix(in srgb, var(--badge-new-bg) 80%, transparent);
}
.diff-line.diff-add .diff-text { color: var(--badge-new-fg); }

.diff-line.diff-del { background: rgba(248, 81, 73, 0.10); }
.diff-line.diff-del .diff-mark {
  color: var(--badge-nodoc-fg);
  background: color-mix(in srgb, var(--badge-nodoc-bg) 80%, transparent);
}
.diff-line.diff-del .diff-text { color: var(--badge-nodoc-fg); }

.diff-line.diff-meta {
  color: var(--text-faint);
  font-style: italic;
}

:root[data-theme="dark"] .diff-line.diff-add { background: rgba(46, 160, 67, 0.18); }
:root[data-theme="dark"] .diff-line.diff-del { background: rgba(248, 81, 73, 0.18); }

.diff-notice {
  margin-bottom: 12px;
  padding: 9px 14px;
  background: var(--surface);
  border: 1px dashed var(--border-strong);
  border-radius: 6px;
  color: var(--text-muted);
  font-size: 12.5px;
}
.diff-notice strong {
  display: block;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-faint);
  margin-bottom: 4px;
}

/* ============================================================
   Misc
   ============================================================ */
.hidden { display: none !important; }
.toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--text);
  color: var(--bg);
  font-size: 12.5px;
  padding: 8px 14px;
  border-radius: 6px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 180ms ease;
  z-index: 50;
}
.toast.show { opacity: 0.94; }

@media (max-width: 700px) {
  header.page, .toolbar, main { padding-left: 16px; padding-right: 16px; }
  details.test > summary { padding-left: 18px; }
  .test-name { max-width: 60%; }
  .test-body { padding-left: 18px; }
}
"""

# highlight.js — loaded from cdnjs. The bundled `highlight.min.js` ships
# with the popular languages (including Python) so we don't need a separate
# language script. Token colors come from the matching github stylesheet,
# which is swapped between light/dark by the JS below.
HLJS_VERSION = "11.10.0"
HLJS_JS = f"https://cdnjs.cloudflare.com/ajax/libs/highlight.js/{HLJS_VERSION}/highlight.min.js"
HLJS_CSS_LIGHT = (
    f"https://cdnjs.cloudflare.com/ajax/libs/highlight.js/{HLJS_VERSION}/styles/github.min.css"
)
HLJS_CSS_DARK = (
    f"https://cdnjs.cloudflare.com/ajax/libs/highlight.js/{HLJS_VERSION}/styles/github-dark.min.css"
)

# Inline boot script: runs before <body> paints, sets the right theme AND
# inserts the matching highlight.js stylesheet so the page never flashes the
# wrong colours on dark-mode reloads.
THEME_BOOT_JS = (
    r"""
(function(){
  var LIGHT_CSS = '"""
    + HLJS_CSS_LIGHT
    + r"""';
  var DARK_CSS = '"""
    + HLJS_CSS_DARK
    + r"""';
  try {
    var stored = localStorage.getItem('btr-theme');
    var mql = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
    var theme = stored || (mql && mql.matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
    var link = document.createElement('link');
    link.id = 'hljs-css';
    link.rel = 'stylesheet';
    link.href = theme === 'dark' ? DARK_CSS : LIGHT_CSS;
    link.dataset.lightHref = LIGHT_CSS;
    link.dataset.darkHref = DARK_CSS;
    document.head.appendChild(link);
  } catch(e) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
"""
)

JS = r"""
(function () {
  const root = document.documentElement;
  const search = document.getElementById('search');
  const onlyNoDoc = document.getElementById('only-no-doc');
  const expandAll = document.getElementById('expand-all');
  const collapseAll = document.getElementById('collapse-all');
  const toggleDiffsBtn = document.getElementById('toggle-diffs');
  const themeBtn = document.getElementById('theme-toggle');
  const themeLabel = document.getElementById('theme-label');
  const toast = document.getElementById('toast');
  const tests = Array.from(document.querySelectorAll('details.test'));
  const classes = Array.from(document.querySelectorAll('details.cls'));
  const files = Array.from(document.querySelectorAll('details.file'));
  const empty = document.getElementById('empty-state');

  // ---- Theme ---------------------------------------------------------
  function syncHljsStylesheet(theme) {
    const link = document.getElementById('hljs-css');
    if (!link) return;
    const target = theme === 'dark' ? link.dataset.darkHref : link.dataset.lightHref;
    if (target && link.href !== target) link.href = target;
  }
  function updateThemeLabel() {
    if (!themeLabel) return;
    const t = root.getAttribute('data-theme');
    themeLabel.textContent = t === 'dark' ? 'Light' : 'Dark';
  }
  updateThemeLabel();
  syncHljsStylesheet(root.getAttribute('data-theme'));

  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('btr-theme', next); } catch(_) {}
      updateThemeLabel();
      syncHljsStylesheet(next);
    });
  }

  // ---- Syntax highlighting ------------------------------------------
  if (window.hljs && typeof window.hljs.highlightAll === 'function') {
    window.hljs.highlightAll();
  } else {
    // Defensive: in the (rare) case the CDN script hasn't parsed yet
    // when this script runs, try again on load.
    window.addEventListener('load', () => {
      if (window.hljs && typeof window.hljs.highlightAll === 'function') {
        window.hljs.highlightAll();
      }
    });
  }

  // ---- Filters -------------------------------------------------------
  function applyFilters() {
    const q = (search.value || '').trim().toLowerCase();
    const noDocOnly = onlyNoDoc.checked;
    let visibleTests = 0;

    for (const t of tests) {
      const hay = t.dataset.search || '';
      const matchesQuery = !q || hay.includes(q);
      const matchesDoc = !noDocOnly || t.dataset.hasdoc === '0';
      const show = matchesQuery && matchesDoc;
      t.classList.toggle('hidden', !show);
      if (show) visibleTests++;
    }

    for (const c of classes) {
      const visible = c.querySelectorAll('details.test:not(.hidden)').length;
      c.classList.toggle('hidden', visible === 0);
    }
    for (const f of files) {
      const visible = f.querySelectorAll('details.test:not(.hidden)').length;
      f.classList.toggle('hidden', visible === 0);
      const countSpan = f.querySelector('summary .count.visible-count');
      if (countSpan) {
        countSpan.textContent = visible + ' shown';
      }
    }
    if (empty) {
      empty.classList.toggle('hidden', visibleTests > 0);
    }
  }

  function setAll(open) {
    // "Expand all" deliberately skips diff toggles — diffs are opt-in and
    // would otherwise dominate the view. "Collapse all" still closes them so
    // a single button can fully reset the page.
    const selector = open ? 'details:not(.diff-toggle)' : 'details';
    for (const d of document.querySelectorAll(selector)) {
      d.open = open;
    }
    if (!open) updateDiffsLabel();
  }

  function setAllDiffs(open) {
    for (const d of document.querySelectorAll('details.diff-toggle')) {
      d.open = open;
    }
    updateDiffsLabel();
  }

  function updateDiffsLabel() {
    if (!toggleDiffsBtn) return;
    const diffs = document.querySelectorAll('details.diff-toggle');
    if (!diffs.length) {
      toggleDiffsBtn.disabled = true;
      toggleDiffsBtn.textContent = 'No diffs';
      return;
    }
    const anyOpen = Array.from(diffs).some((d) => d.open);
    toggleDiffsBtn.textContent = anyOpen ? 'Hide diffs' : 'Show diffs';
  }

  search.addEventListener('input', applyFilters);
  onlyNoDoc.addEventListener('change', applyFilters);
  expandAll.addEventListener('click', () => setAll(true));
  collapseAll.addEventListener('click', () => setAll(false));
  if (toggleDiffsBtn) {
    toggleDiffsBtn.addEventListener('click', () => {
      const diffs = document.querySelectorAll('details.diff-toggle');
      const anyClosed = Array.from(diffs).some((d) => !d.open);
      setAllDiffs(anyClosed); // if any are closed, open all; otherwise close all
    });
    // Keep the label in sync when users open diffs individually.
    document.addEventListener('toggle', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('diff-toggle')) {
        updateDiffsLabel();
      }
    }, true);
    updateDiffsLabel();
  }

  // ---- Copy buttons --------------------------------------------------
  function showToast(msg) {
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.classList.remove('show'), 1400);
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.copy-btn');
    if (!btn) return;
    const pre = btn.closest('.codeblock').querySelector('code');
    if (!pre) return;
    const text = pre.innerText;
    const done = () => {
      const original = btn.querySelector('.copy-label');
      if (original) {
        const prev = original.textContent;
        original.textContent = 'Copied';
        btn.classList.add('ok');
        setTimeout(() => { original.textContent = prev; btn.classList.remove('ok'); }, 1100);
      }
      showToast('Copied to clipboard');
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
    } else {
      fallbackCopy(text, done);
    }
  });

  function fallbackCopy(text, done) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch(_) {}
    document.body.removeChild(ta);
  }

  // ---- Keyboard shortcuts -------------------------------------------
  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea')) return;
    if (e.key === '/') {
      e.preventDefault();
      search.focus();
      search.select();
    } else if (e.key.toLowerCase() === 't' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (themeBtn) themeBtn.click();
    } else if (e.key.toLowerCase() === 'e') {
      setAll(true);
    } else if (e.key.toLowerCase() === 'c') {
      setAll(false);
    } else if (e.key.toLowerCase() === 'd') {
      if (toggleDiffsBtn) toggleDiffsBtn.click();
    }
  });

  applyFilters();
})();
"""


def _e(s: str) -> str:
    return html.escape(s, quote=True)


def _short_sha(sha: str) -> str:
    return sha[:10] if len(sha) >= 10 else sha


COPY_ICON_SVG = (
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="5" y="5" width="8" height="9" rx="1.5"/>'
    '<path d="M3 11V3.5A1.5 1.5 0 0 1 4.5 2H11"/>'
    "</svg>"
)


def _render_code_block(source: str, start_line: int, end_line: int) -> str:
    """Render a styled code block with line-number gutter + copy button.

    The gutter is a sibling element inside a CSS-grid <pre>, so Prism's
    syntax highlighter never has to know about it.
    """
    lines = source.splitlines() or [""]
    gutter = "\n".join(str(start_line + i) for i in range(len(lines)))
    loc = f"L{start_line}" if start_line == end_line else f"L{start_line}–{end_line}"
    return f"""<div class="codeblock">
              <div class="codeblock-head">
                <span class="dot"></span>
                <span class="lang">Python</span>
                <span class="spacer"></span>
                <span class="loc">{loc}</span>
                <button type="button" class="copy-btn" aria-label="Copy code">
                  {COPY_ICON_SVG}<span class="copy-label">Copy</span>
                </button>
              </div>
              <pre class="with-lineno"><span class="ln">{_e(gutter)}</span><code class="language-python">{_e(source)}</code></pre>
            </div>"""


_DIFF_MARKER_CLASSES = {
    "+": "diff-add",
    "-": "diff-del",
    " ": "diff-ctx",
    "\\": "diff-meta",
}


def _render_diff_hunks(hunks: list[DiffHunk]) -> str:
    """Render a list of diff hunks into a collapsible styled diff block.

    Wrapped in a `<details class="diff-toggle">` so the diff is collapsed by
    default and `Expand all` can skip it (see JS in `JS`). Empty input returns
    an empty string so callers can decide whether to emit a placeholder.
    """
    if not hunks:
        return ""
    adds = sum(1 for h in hunks for m, _ in h.lines if m == "+")
    dels = sum(1 for h in hunks for m, _ in h.lines if m == "-")
    n_hunks = len(hunks)
    summary_meta = (
        f'{n_hunks} hunk{"" if n_hunks == 1 else "s"} · '
        f'<span class="diff-stat-add">+{adds}</span> '
        f'<span class="diff-stat-del">−{dels}</span>'
    )

    hunks_html_parts: list[str] = []
    for h in hunks:
        line_parts: list[str] = []
        for marker, text in h.lines:
            cls = _DIFF_MARKER_CLASSES.get(marker, "diff-ctx")
            shown = marker if marker != " " else " "
            line_parts.append(
                f'<div class="diff-line {cls}">'
                f'<span class="diff-mark">{_e(shown)}</span>'
                f'<span class="diff-text">{_e(text) if text else "&nbsp;"}</span>'
                "</div>"
            )
        hunks_html_parts.append(
            f'<div class="diff-hunk">'
            f'<div class="diff-hunk-header">{_e(h.header)}</div>'
            f'{"".join(line_parts)}'
            "</div>"
        )

    return f"""<details class="diff-toggle">
              <summary class="diff-summary">
                <span class="caret"></span>
                <span class="diff-summary-label">Diff</span>
                <span class="diff-summary-meta">{summary_meta}</span>
              </summary>
              <div class="diff-block">
                {"".join(hunks_html_parts)}
              </div>
            </details>"""


def render_test(
    t: TestEntry,
    file_hunks: list[DiffHunk] | None = None,
    is_new_file: bool = False,
) -> str:
    summary_doc = ""
    desc_class = "test-desc empty"
    desc_text = "— no docstring —"
    if t.docstring:
        first_line = t.docstring.strip().splitlines()[0]
        desc_class = "test-desc"
        desc_text = first_line
        summary_doc = t.docstring
    has_doc = "1" if t.docstring else "0"
    search_hay = " ".join(
        filter(None, [t.cls or "", t.name, t.docstring, t.source])
    ).lower()
    if summary_doc:
        doc_block = (
            f'<div class="test-doc-full"><strong>Docstring</strong>{_e(summary_doc)}</div>'
        )
    else:
        doc_block = ""
    lineref = (
        f"L{t.start_line}"
        if t.end_line == t.start_line
        else f"L{t.start_line}–{t.end_line}"
    )

    # Pick the hunks whose new-file range overlaps with this test's body.
    if is_new_file:
        diff_block = (
            '<div class="diff-notice"><strong>Diff</strong>'
            "This test lives in a newly added file — the full source below "
            "is the change.</div>"
        )
    else:
        relevant_hunks = [
            h
            for h in (file_hunks or [])
            if overlaps(h.new_range, (t.start_line, t.end_line))
        ]
        if relevant_hunks:
            diff_block = _render_diff_hunks(relevant_hunks)
        else:
            diff_block = (
                '<div class="diff-notice"><strong>Diff</strong>'
                "No diff hunks overlap this test (it was included via "
                "<code>--include-all</code>).</div>"
            )

    return f"""
        <details class="test" data-name="{_e(t.name)}" data-hasdoc="{has_doc}" data-search="{_e(search_hay)}">
          <summary>
            <span class="caret"></span>
            <span class="test-name">{_e(t.name)}</span>
            <span class="{desc_class}">{_e(desc_text)}</span>
            <span class="lineref">{lineref}</span>
          </summary>
          <div class="test-body">
            {doc_block}
            {diff_block}
            {_render_code_block(t.source, t.start_line, t.end_line)}
          </div>
        </details>
    """


def render_class_group(
    cls: str | None,
    entries: list[TestEntry],
    file_hunks: list[DiffHunk] | None = None,
    is_new_file: bool = False,
) -> str:
    tests_html = "".join(
        render_test(t, file_hunks=file_hunks, is_new_file=is_new_file)
        for t in entries
    )
    if cls is None:
        header = '<span class="module-label">module-level tests</span>'
    else:
        header = f'<span class="class-name">{_e(cls)}</span>'
    return f"""
        <details class="cls" open>
          <summary>
            <span class="caret"></span>
            {header}
            <span class="count">{len(entries)} {"test" if len(entries) == 1 else "tests"}</span>
          </summary>
          <div class="tests-body">
            {tests_html}
          </div>
        </details>
    """


def render_file(bucket: FileBucket) -> str:
    # Group tests by class, preserving discovery order; class=None goes last
    groups: dict[str | None, list[TestEntry]] = {}
    for t in bucket.tests:
        groups.setdefault(t.cls, []).append(t)
    ordered_keys = sorted(groups.keys(), key=lambda k: (k is None, (k or "").lower()))
    groups_html = "".join(
        render_class_group(
            k,
            groups[k],
            file_hunks=bucket.hunks,
            is_new_file=bucket.is_new,
        )
        for k in ordered_keys
    )
    badge = (
        '<span class="badge new">new file</span>'
        if bucket.is_new
        else '<span class="badge mod">modified</span>'
    )
    n = len(bucket.tests)
    return f"""
        <details class="file" open>
          <summary>
            <span class="caret"></span>
            <span class="file-path">{_e(bucket.file)}</span>
            {badge}
            <span class="count">{n} {"test" if n == 1 else "tests"}</span>
            <span class="count visible-count">{n} shown</span>
          </summary>
          <div class="file-body">
            {groups_html}
          </div>
        </details>
    """


def render_page(
    buckets: list[FileBucket],
    repo_name: str,
    base_ref: str,
    merge_base: str,
) -> str:
    total_tests = sum(len(b.tests) for b in buckets)
    total_files = len(buckets)
    total_classes = sum(
        len({t.cls for t in b.tests if t.cls is not None}) for b in buckets
    )
    no_doc = sum(1 for b in buckets for t in b.tests if not t.docstring)

    files_html = "".join(render_file(b) for b in buckets)
    if not buckets:
        body = (
            '<div class="empty"><h2 style="margin:0 0 6px;font-weight:600;">No test changes detected</h2>'
            f'<p>No test functions added or modified vs <code>{_e(base_ref)}</code> '
            f"(merge-base <code>{_short_sha(merge_base)}</code>).</p></div>"
        )
    else:
        body = f'<div id="empty-state" class="empty hidden"><p>No tests match the current filters.</p></div>{files_html}'

    # Inline a small SVG search icon so the search input gets a leading glyph
    # without needing an extra HTTP request.
    search_svg = (
        "data:image/svg+xml;utf8,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' "
        "stroke='%239a9789' stroke-width='1.5' stroke-linecap='round' "
        "stroke-linejoin='round'><circle cx='7' cy='7' r='4.5'/>"
        "<path d='m10.5 10.5 3 3'/></svg>"
    )
    sun_svg = (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true">'
        '<circle cx="8" cy="8" r="3"/>'
        '<path d="M8 1.5v1.5M8 13v1.5M2.6 2.6l1.1 1.1M12.3 12.3l1.1 1.1M1.5 8H3M13 8h1.5M2.6 13.4l1.1-1.1M12.3 3.7l1.1-1.1"/>'
        "</svg>"
    )
    moon_svg = (
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true">'
        '<path d="M13 9.5A5.5 5.5 0 1 1 6.5 3a4.5 4.5 0 0 0 6.5 6.5z"/>'
        "</svg>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Branch Tests · {_e(repo_name)}</title>
<script>{THEME_BOOT_JS}</script>
<style>{CSS}</style>
<style>:root {{ --search-icon: url("{search_svg}"); }}</style>
</head>
<body>
<header class="page">
  <h1>Branch test review · {_e(repo_name)}</h1>
  <div class="meta">vs <code>{_e(base_ref)}</code> · merge-base <code>{_short_sha(merge_base)}</code></div>
  <div class="stats">
    <span><b>{total_files}</b> files</span>
    <span><b>{total_classes}</b> classes</span>
    <span><b>{total_tests}</b> tests</span>
    <span><b>{no_doc}</b> without docstring</span>
  </div>
</header>
<div class="toolbar" role="toolbar" aria-label="Test review controls">
  <input type="search" id="search" class="input" autocomplete="off" spellcheck="false"
         placeholder="Search test name, class, file, docstring, or code…">
  <label class="check"><input type="checkbox" id="only-no-doc"> Only without docstring</label>
  <button id="expand-all" class="btn" type="button" title="Expand tests and groups, but leave diffs collapsed (E)">Expand all</button>
  <button id="collapse-all" class="btn" type="button" title="Collapse everything including diffs (C)">Collapse all</button>
  <button id="toggle-diffs" class="btn" type="button" title="Toggle all diffs (D)">Show diffs</button>
  <button id="theme-toggle" class="btn icon" type="button" title="Toggle theme (T)" aria-label="Toggle theme">
    <span class="theme-sun">{sun_svg}</span>
    <span class="theme-moon" style="display:none">{moon_svg}</span>
    <span id="theme-label" class="theme-text">Dark</span>
  </button>
</div>
<main>
{body}
</main>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<script src="{HLJS_JS}"></script>
<script>{JS}</script>
<script>
  // Swap sun/moon icon based on current theme
  (function () {{
    function syncIcon() {{
      var dark = document.documentElement.getAttribute('data-theme') === 'dark';
      var sun = document.querySelector('.theme-sun');
      var moon = document.querySelector('.theme-moon');
      if (sun && moon) {{
        sun.style.display = dark ? 'none' : '';
        moon.style.display = dark ? '' : 'none';
      }}
    }}
    syncIcon();
    new MutationObserver(syncIcon).observe(
      document.documentElement, {{ attributes: true, attributeFilter: ['data-theme'] }}
    );
  }})();
</script>
</body>
</html>
"""


# ----------------------------- entry point -----------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default="main",
        help="Base branch to compare against (default: main; falls back to master).",
    )
    parser.add_argument(
        "--output",
        default="branch-tests-review.html",
        help="Path to write the HTML report (default: branch-tests-review.html).",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Repo root (default: current directory).",
    )
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Include every test in any changed test file, not just tests whose lines changed.",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists() and _git_or_none(["rev-parse", "--git-dir"], cwd=repo) is None:
        print(f"error: {repo} is not a git repository", file=sys.stderr)
        return 2

    merge_base, base_ref = find_merge_base(repo, args.base)
    buckets = collect(repo, merge_base, include_all_in_changed_files=args.include_all)

    repo_name = repo.name
    page = render_page(buckets, repo_name=repo_name, base_ref=base_ref, merge_base=merge_base)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path
    output_path.write_text(page, encoding="utf-8")

    total_tests = sum(len(b.tests) for b in buckets)
    no_doc = sum(1 for b in buckets for t in b.tests if not t.docstring)
    print(
        f"Wrote {output_path} — {total_tests} test(s) across {len(buckets)} file(s) "
        f"(merge-base {_short_sha(merge_base)} of {base_ref}). "
        f"{no_doc} test(s) without a docstring."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
