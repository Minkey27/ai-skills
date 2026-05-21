# Posting GitLab diff notes via `glab api`

Endpoint: `POST /projects/:id/merge_requests/:iid/discussions`

## The content-type gotcha

`glab api --input -` reads JSON from stdin but does **not** set `Content-Type: application/json`. Without the header GitLab parses the body as form-encoded and rejects nested `position` with HTTP 415:

```
{"error":"The provided content-type '' is not supported."}
```

**Always pass the header explicitly:**

```bash
python3 -c '...print json...' | glab api projects/<id>/merge_requests/<iid>/discussions \
  --method POST \
  --header "Content-Type: application/json" \
  --input -
```

## Required `diff_refs` SHAs

Fetch via `glab mr view --output json` (or `glab api projects/<id>/merge_requests/<iid>`) and use:

```json
{
  "position": {
    "position_type": "text",
    "base_sha":  "...",
    "head_sha":  "...",
    "start_sha": "..."
  }
}
```

- `base_sha` — merge-base of the MR
- `head_sha` — tip of the MR's source branch
- `start_sha` — GitLab's per-MR start sha (returned in `diff_refs`)

## Line-number rules

| Line type in diff | Required position fields |
|---|---|
| Added line (`+`)     | `new_line` only (`old_line: null`) |
| Removed line (`-`)   | `old_line` only (`new_line: null`) |
| Unchanged context    | **both** `new_line` and `old_line` |

For unchanged context lines, compute `old_line` from preceding hunks:

```
old_line = new_line + (deletions before) − (additions before)
```

Get hunks with `git diff --unified=0 BASE..HEAD -- <file>`.

`new_path` and `old_path` are usually the same — they differ only on file renames.

## Multi-line notes

For a range spanning multiple lines, add `line_range`:

```json
"line_range": {
  "start": {"new_line": <s>, "old_line": <s_old_or_null>, "type": "new|old|expanded"},
  "end":   {"new_line": <e>, "old_line": <e_old_or_null>, "type": "new|old|expanded"}
}
```

For single-line notes (`line_start == line_end`), omit `line_range` entirely.
