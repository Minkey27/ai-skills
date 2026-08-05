# Examples

One real change, described twice. Same branch, same diff — 653 words, then 164.

The bad half is verbatim from GitLab. It is not a strawman: it has a correct `Closes` line,
accurate technical content and real reasoning. That is the point — this is what a competent
description looks like when nothing is deleted.

## Bad — 653 words

```markdown
Closes BPZ-1004

## Summary

Adds a **"Toegewezen aan"** filter to the project overview sidebar: pick a colleague and the list narrows to projects where they hold an internal-contact seat. The logged-in user is pinned first as *"Mijn projecten"*.

**Data layer**
- `search_paginated` gains an `assigned_user_id` filter, implemented as a semi-join (`id IN (SELECT project_id ...)`) rather than a JOIN — a user can hold several seats on one project, which a JOIN would return once per seat, duplicating the row and inflating the count query.
- The seat lookup goes through `apply_temporal_filter`, so a historical `as_of` sees the seat holder as they were then.
- Adds a partial index on `project_internal_contacts (user_id) WHERE effective_to IS NULL`. No existing index led with `user_id`, and the filter runs the lookup twice per request (count query + paged select).

**Shared searchable-dropdown component** (used by 5 other templates)
- Dispatches a bubbling `change` event on selection, so a caller can wire `hx-trigger="change"` — assigning `.value` programmatically never fires one. Only fires when the value actually differs, so re-picking the selected option costs no request.
- Accepts an `hx_attrs` dict rendered onto the hidden input, so the caller attaches its own HTMX wiring to the element the event fires on.
- Falls back to the placeholder when `selected_value` matches no entry in `choices`. It previously rendered a blank label — reachable via any URL naming a since-deactivated colleague.
- The trigger now wears the `select` component classes instead of `btn` plus hand-rolled borders, so it matches a native `<select>` beside it. Design book updated to match, including the focus-state rule (`select` ships its own outline).

**URL threading**
- `projects_filter_url` now takes the filter set as one dict. It previously took nine positional args repeated across eight call sites, where a mistyped position silently swapped two filters.
- Every value is urlencoded, not just `q`. Inside an `href` the browser decodes `&amp;` back to a real separator, so a raw `&` in `klant_id` or `assigned_user_id` — both unvalidated query params — injected extra filter params into every chip and pagination link on the page.
- The chip container is gated on the chip-backed keys only; gating on `active_filters` as a whole rendered an empty container (and a stray margin above the table) whenever only a sidebar-only filter was set.

**Also fixed in the touched handler**
- 🔐 `edit_project_form` had no `@require_authentication()` — the only handler in the router without it, and nothing gated it globally. An anonymous `GET /projecten/{id}/edit` returned the full project plus every active user's display name. Pre-existing, but this MR refactors the helper that renders that list. Verified: it now `302`s to login.
- Skips the klant, year and colleague queries on the HTMX path. That branch renders the content partial, which never reads them, and it is the hot path (the search box fires on `keyup`).

## Test plan

- `docker compose exec backend uv run poe migrate` — applies the new index migration.
- Automated (all green):
  - `pytest tests/integration/infra/db/projects/ tests/integration/presentation/projecten/ -n 0` — includes single-seat match, multi-seat de-duplication, exclusion after unassign, composition with the status filter, historical `as_of`, inactive-user pinning, the rendered HTMX wiring, and the unknown-id placeholder fallback.
  - `pytest tests/integration/infra/db/test_migration_drift.py -n 0` — model/migration parity for the new index.
  - `pytest tests/integration/presentation/hang_sluitwerk/ tests/integration/presentation/test_get_routes_200.py tests/integration/presentation/test_authentication_redirects.py -n 0` — the other dropdown consumers and route auth.
  - `npx vitest run tests/js/searchable-dropdown.test.js` — 7 tests, incl. the no-op dispatch guard.
- Manual:
  1. Open `/projecten/`, pick a colleague in **Toegewezen aan** → list narrows, URL gains `assigned_user_id`, back/forward works.
  2. Combine it with a status checkbox and a klant → all filters compose; remove the status chip → the assigned filter survives.
  3. Page to 2 → the filter survives.
  4. Re-pick the same colleague → no new request in the network tab.
  5. Visit `/projecten/?assigned_user_id=garbage` → trigger shows "Alle collega's", not a blank button.
  6. Log out, open `/projecten/<id>/edit` → redirected to login.
```

## Good — 164 words

```markdown
Closes BPZ-1004

## Why

The project overview could not be narrowed to a single colleague, so finding your own
projects meant scanning every row. Internal-contact seats were already stored; nothing
exposed them as a filter.

## What

- Semi-join, not JOIN: a user can hold several seats on one project, so a JOIN duplicates
  the row and inflates the count.
- New partial index on `project_internal_contacts (user_id)` — no index led with it, and
  the filter runs the lookup twice per request.
- The shared searchable dropdown now dispatches a bubbling `change`, so callers can wire
  `hx-trigger`. Five other templates use it.
- `projects_filter_url` takes one dict instead of nine positional args, and urlencodes
  every value — a raw `&` previously injected filters into every link.
- `edit_project_form` lacked `@require_authentication()`; an anonymous GET returned the
  project plus every active user's name.

## Caveats

The missing-auth fix sits outside BPZ-1004's scope — it is here because this MR refactors
the user list that was leaking.
```

## What survived, and why

| Kept | Because |
|---|---|
| The semi-join rationale | A reviewer seeing `id IN (SELECT …)` would otherwise ask why it is not a JOIN. |
| The index justification | It defends a migration, which is the costliest thing in the diff to get wrong. |
| The dropdown's blast radius | Five other templates consume that component — invisible from this diff. |
| The urlencode fix | An unvalidated `&` injecting filters into every link is a bug a reviewer must confirm. |
| The missing `@require_authentication()` | An anonymous data leak, and the single most important line in the MR. |

What went, and why:

| Cut | Because |
|---|---|
| `poe migrate` | The migration file is in the diff; running it is not the reviewer's job. |
| Four `pytest` invocations | CI runs them. Naming them proves nothing a green pipeline does not. |
| A six-step manual click-through | The reviewer did not ask for a QA script. |
| The design-book focus-state rule | Visible in the diff, and it changes nothing about the merge decision. |
| The chip-container gating | Real, but a reviewer reading the template sees it. |
| The HTMX-path query skipping | Same — an optimisation legible from the code. |

Note what the `## Caveats` line does. It is not about test coverage at all: it flags scope
creep the reviewer would otherwise query. That is the section's most common real use.
