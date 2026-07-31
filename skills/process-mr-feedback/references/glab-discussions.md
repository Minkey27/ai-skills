# Working with GitLab MR discussions via `glab api`

This skill reads and writes **discussion threads** on a merge request: list them, reply to one,
and resolve one. (Creating a *new* line-anchored diff note is a different endpoint — that's what a
review-posting skill does. Here we only act on threads that already exist.)

All commands assume `glab` is authenticated and the MR's source branch is checked out.

## Resolve the MR and project id

```bash
glab mr view --output json
```

Capture: `iid`, `source_branch`, `target_branch`, `web_url`. For the project id, use the
`project_id` field from the JSON, or pass the URL-encoded `OWNER/REPO` path — `glab api` accepts
either in the `projects/:id/...` position.

> If `glab mr view` errors with "merge request ID number required" plus several matches, the branch
> has multiple MRs (typically one open + older closed/merged ones). Call
> `glab mr view <iid> --output json` on each, **auto-pick the single one with `state == "opened"`**,
> and only stop to ask if 0 or ≥2 are open.

## Fetch the threads that need processing

```bash
glab api "projects/$PID/merge_requests/$IID/discussions?per_page=100" --paginate
```

> **`:iid` is not a glab placeholder.** `glab api` substitutes only repo-scoped placeholders —
> `:branch`, `:fullpath`, `:group`, `:id`, `:namespace`, `:repo`, `:user`, `:username`. An MR iid has
> no repo to resolve from, so `:iid` is sent to the server literally and GitLab answers
> `HTTP 400 {"error":"noteable_id is invalid"}`. **Always interpolate the real iid** (and, for
> clarity, the real project id) into the path. Capture both first:
>
> ```bash
> PID=$(glab mr view --output json | python3 -c 'import json,sys; print(json.load(sys.stdin)["project_id"])')
> IID=$(glab mr view --output json | python3 -c 'import json,sys; print(json.load(sys.stdin)["iid"])')
> ```

Keep only threads that are **open and resolvable**, and drop system notes:

- The first note has `resolvable == true`.
- At least one note in the thread still has `resolved == false`.
- `system == true` notes are skipped entirely.

Per kept thread, capture:

| Field | Source | Use |
|---|---|---|
| `discussion_id` | `.id` | reply + resolve target |
| `author` | `.notes[0].author.username` | **metadata only** (human vs bot) |
| `path` | `.notes[0].position.new_path // .notes[0].position.old_path` | code anchor |
| `line` | `.notes[0].position.new_line // .notes[0].position.old_line` | code anchor |
| `body` | `.notes[0].body` | the reviewer's comment |
| `has_prior_replies` | `(.notes | length) > 1` | flag "has prior discussion" |

Two fields are **not** from the API and get filled in by the skill: `tier` (the verification model,
assigned in Stage 2a) and `cluster` (threads sharing an anchor or a fix, grouped in Stage 1c).

Example — list unresolved resolvable threads with anchor + body:

```bash
glab api "projects/$PID/merge_requests/$IID/discussions?per_page=100" --paginate \
  | jq -r '.[]
           | select(.notes[0].system != true)
           | select(.notes[0].resolvable == true)
           | select([.notes[] | select(.resolved == false)] | length > 0)
           | {id,
              author: .notes[0].author.username,
              path:   (.notes[0].position.new_path // .notes[0].position.old_path),
              line:   (.notes[0].position.new_line // .notes[0].position.old_line),
              body:   .notes[0].body,
              replies:(.notes | length)}'
```

> Note: `--paginate` merges pages into one JSON stream; `jq '.[]'` iterates it.
>
> **Anchors come in three shapes**, and only the third is genuinely anchorless:
>
> | Shape | `position` | Meaning |
> |---|---|---|
> | `new_line` set | present | normal note on a current line |
> | `new_line` null, `old_line` set | present | note on a line the diff **deleted** — `old_path`/`old_line` still locate it |
> | whole `.position` null | absent | a general MR comment, no code anchor at all |
>
> The `//` fallbacks above keep the deleted-line case anchored to a file instead of degrading it to
> "no anchor". Anchorless threads are still valid to reply to and resolve.

## Reply to a thread (add a note to an existing discussion)

```bash
printf '%s' '{"body": "Fixed in <sha>: <one line>"}' \
  | glab api "projects/$PID/merge_requests/$IID/discussions/<discussion_id>/notes" \
    --method POST \
    --header "Content-Type: application/json" \
    --input -
```

## Resolve a thread

```bash
glab api "projects/$PID/merge_requests/$IID/discussions/<discussion_id>?resolved=true" \
  --method PUT
```

To leave a thread **unresolved** (Push back / Defer), simply skip this PUT — post the reply only.

## The Content-Type gotcha

`glab api --input -` reads JSON from stdin but does **not** set `Content-Type: application/json`.
Without the header GitLab parses the body as form-encoded and rejects it with HTTP 415:

```
{"error":"The provided content-type '' is not supported."}
```

**Always pass `--header "Content-Type: application/json"`** on any `glab api ... --input -` call
(the reply POST above). The resolve PUT carries its flag in the query string, so it needs no body
and no header.

## Batching — there is no batch endpoint

GitLab posts one note and resolves one discussion per request. Loop over the threads. Prefer a
single `python3` helper that iterates a list of `(discussion_id, body, resolve?)` tuples — POSTing
the reply, then (if `resolve`) PUTting `resolved=true`, capturing each response — over many parallel
`Bash` calls. Sequential posting from one script is far easier to debug if a payload is rejected,
and the per-request cost (a few hundred ms) is negligible next to the verification work already
done. Capture each reply's `id` (note id) to build a clickable `{web_url}#note_{id}` for the
receipt.

**Auth: the helper must shell out to `glab api` via `subprocess`** — never extract a token
(`glab auth status --show-token`, `GITLAB_TOKEN`, config files) into the script or an env var to
call the REST API directly with `urllib`/`requests`. A materialized token can leak via transcript,
shell history, or process listing; `glab` keeps the credential inside its own process.

```python
import json, subprocess

def reply(project, iid, discussion_id, body, resolve):
    subprocess.run(
        ["glab", "api", f"projects/{project}/merge_requests/{iid}/discussions/{discussion_id}/notes",
         "--method", "POST", "--header", "Content-Type: application/json", "--input", "-"],
        input=json.dumps({"body": body}), text=True, check=True)
    if resolve:
        subprocess.run(
            ["glab", "api", f"projects/{project}/merge_requests/{iid}/discussions/{discussion_id}?resolved=true",
             "--method", "PUT"], check=True)
```
