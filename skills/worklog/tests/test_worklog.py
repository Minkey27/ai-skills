import importlib.util
import json as _json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Load the script as a module by path (it lives under scripts/, not a package).
_SPEC = importlib.util.spec_from_file_location(
    "worklog", Path(__file__).resolve().parents[1] / "scripts" / "worklog.py"
)
worklog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worklog)


def _dt(minute):
    # Offset from a fixed base so minute values >= 60 roll into later hours
    # (e.g. _dt(90) -> 10:30); values 0..59 keep their literal minute.
    return datetime(2026, 6, 16, 9, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)


# --- Task 1 -----------------------------------------------------------------
def test_module_exposes_idle_cap():
    assert worklog.IDLE_CAP_MINUTES == 15


# --- Task 2: active_minutes -------------------------------------------------
def test_active_minutes_empty_and_single():
    assert worklog.active_minutes([]) == 0.0
    assert worklog.active_minutes([_dt(0)]) == 0.0


def test_active_minutes_sums_small_gaps():
    assert worklog.active_minutes([_dt(0), _dt(5), _dt(8)]) == 8.0


def test_active_minutes_caps_large_gap():
    assert worklog.active_minutes([_dt(0), _dt(50)], cap_minutes=15) == 15.0


def test_active_minutes_sorts_input():
    assert worklog.active_minutes([_dt(8), _dt(0), _dt(5)]) == 8.0


# --- Task 3: parse_window ---------------------------------------------------
def test_parse_window_default_is_today_utc():
    start, end = worklog.parse_window(None, date(2026, 6, 16), timezone.utc)
    assert start == datetime(2026, 6, 16, 0, 0, 0, tzinfo=timezone.utc)
    assert end.hour == 23 and end.minute == 59 and end.tzinfo == timezone.utc


def test_parse_window_single_date():
    start, end = worklog.parse_window("2026-06-10", date(2026, 6, 16), timezone.utc)
    assert start.day == 10 and end.day == 10


def test_parse_window_range_inclusive():
    start, end = worklog.parse_window("2026-06-09..2026-06-13", date(2026, 6, 16), timezone.utc)
    assert start.day == 9 and end.day == 13
    assert start < end


# --- Task 4: branch-set parsers ---------------------------------------------
def test_parse_merge_branches_extracts_quoted_name():
    subjects = [
        "Merge branch 'bpz-744-consolidate-ijkpunt-dates' into 'main'",
        "Merge branch 'add-afdeling' into 'main'",
        "fix(projecten): unrelated non-merge subject",
    ]
    assert worklog.parse_merge_branches(subjects) == {
        "bpz-744-consolidate-ijkpunt-dates",
        "add-afdeling",
    }


def test_normalize_refs_strips_origin_and_drops_head():
    lines = ["main", "origin/bpz-756-floorplan", "origin/HEAD", "", "boyscout-tree"]
    assert worklog.normalize_refs(lines) == {"main", "bpz-756-floorplan", "boyscout-tree"}


# --- Task 5: derive_subject + is_ticket_key ---------------------------------
def test_derive_subject_ticket_branch_with_prefix():
    key, label = worklog.derive_subject(
        "bpz-756-floorplan-overlay-download-issue", None, "BPZ", "sess1234"
    )
    assert key == "BPZ-756"
    assert label == "BPZ-756 floorplan-overlay-download-issue"


def test_derive_subject_non_ticket_feature_branch():
    key, label = worklog.derive_subject("add-afdeling", None, "BPZ", "sess1234")
    assert key == "add-afdeling"
    assert label == "add afdeling"


def test_derive_subject_main_falls_back_to_ai_title():
    key, label = worklog.derive_subject("main", "Fix floorplan overlay", "BPZ", "sess1234")
    assert label == "Fix floorplan overlay"
    assert key == "title:Fix floorplan overlay"


def test_derive_subject_main_untitled_uses_session_id():
    key, label = worklog.derive_subject("main", None, "BPZ", "abcdefgh1234")
    assert key == "untitled:abcdefgh"
    assert label == "main (untitled abcdefgh)"


def test_derive_subject_generic_prefix_when_unset():
    key, label = worklog.derive_subject("foo-123-bar", None, "", "sess1234")
    assert key == "FOO-123"
    assert label == "FOO-123 bar"


def test_is_ticket_key():
    assert worklog.is_ticket_key("BPZ-756") is True
    assert worklog.is_ticket_key("add-afdeling") is False
    assert worklog.is_ticket_key("title:Foo") is False


# --- Task 6: in_scope + under_any -------------------------------------------
REPO = "/Users/yyung/Projects/deurdoor"
LIVE = {"/Users/yyung/Projects/worktrees/BPZ-742-x"}
KNOWN = {"add-afdeling", "bpz-744-consolidate-ijkpunt-dates"}


def test_in_scope_primary_repo_any_branch():
    assert worklog.in_scope(REPO, "anything", REPO, LIVE, KNOWN) is True


def test_in_scope_live_worktree_any_branch():
    assert worklog.in_scope(
        "/Users/yyung/Projects/worktrees/BPZ-742-x", "BPZ-742-x", REPO, LIVE, KNOWN
    ) is True


def test_in_scope_deleted_worktree_known_branch():
    assert worklog.in_scope(
        "/Users/yyung/Projects/worktrees/add-afdeling", "add-afdeling", REPO, LIVE, KNOWN
    ) is True


def test_in_scope_unknown_branch_rejected():
    assert worklog.in_scope(
        "/Users/yyung/Projects/worktrees/chore", "chore", REPO, LIVE, KNOWN
    ) is False


def test_under_any():
    parents = {"/Users/yyung/Projects/worktrees"}
    assert worklog.under_any("/Users/yyung/Projects/worktrees/chore", parents) is True
    assert worklog.under_any("/Users/yyung/Projects/other/x", parents) is False


# --- Task 7: parse_session --------------------------------------------------
def test_parse_session_extracts_title_events_prompts():
    lines = [
        _json.dumps({"type": "ai-title", "aiTitle": "Fix overlay", "sessionId": "s1"}),
        _json.dumps({"type": "attachment", "timestamp": "2026-06-16T09:00:00.000Z",
                     "gitBranch": "bpz-756-x", "cwd": "/repo/wt", "sessionId": "s1"}),
        _json.dumps({"type": "user", "timestamp": "2026-06-16T09:05:00.000Z",
                     "gitBranch": "bpz-756-x", "cwd": "/repo/wt", "sessionId": "s1",
                     "message": {"content": "Fix the download please"}}),
        "not json — should be skipped",
        _json.dumps({"type": "user", "timestamp": "2026-06-16T09:06:00.000Z",
                     "gitBranch": "bpz-756-x", "cwd": "/repo/wt", "sessionId": "s1",
                     "message": {"content": [{"type": "text", "text": "Base directory for this skill: /x"}]}}),
    ]
    s = worklog.parse_session(lines)
    assert s["session_id"] == "s1"
    assert s["ai_title"] == "Fix overlay"
    assert s["cwd"] == "/repo/wt"
    assert len(s["events"]) == 3
    assert s["events"][0]["branch"] == "bpz-756-x"
    assert isinstance(s["events"][0]["ts"], datetime)
    assert s["prompts"] == ["Fix the download please"]


# --- Task 8: build_report ---------------------------------------------------
def _sess(session_id, ai_title, cwd, evts, prompts=None):
    return {
        "session_id": session_id,
        "ai_title": ai_title,
        "cwd": cwd,
        "events": [{"ts": _dt(m), "branch": b, "cwd": cwd} for (m, b) in evts],
        "prompts": prompts or [],
    }


def _scope():
    return dict(
        window=(_dt(0), _dt(59)),
        repo_root="/repo",
        live_worktrees={"/repo/wt-756"},
        known_branches={"add-afdeling"},
        worktree_parents={"/wts"},
        ticket_prefix="BPZ",
    )


def test_build_report_groups_ticket_and_dedupes_across_sessions():
    sessions = [
        _sess("s1", "T", "/repo/wt-756", [(0, "bpz-756-x"), (5, "bpz-756-x")]),
        _sess("s2", "T", "/repo/wt-756", [(10, "bpz-756-x")]),
    ]
    report = worklog.build_report(sessions, **_scope())
    rows = {r["subject"]: r for r in report["subjects"]}
    assert "BPZ-756 x" in rows
    assert rows["BPZ-756 x"]["session_count"] == 2
    assert rows["BPZ-756 x"]["active_min"] == 10.0
    assert rows["BPZ-756 x"]["wallclock_min"] == 10.0
    # meta counts only sessions that contributed in-scope events
    assert report["meta"]["sessions_counted"] == 2


def test_build_report_includes_non_ticket_known_branch():
    sessions = [_sess("s3", None, "/wts/add-afdeling", [(0, "add-afdeling"), (5, "add-afdeling")])]
    report = worklog.build_report(sessions, **_scope())
    assert any(r["subject"] == "add afdeling" for r in report["subjects"])


def test_build_report_routes_unknown_branch_to_unattributed():
    sessions = [_sess("s4", None, "/wts/chore", [(0, "chore"), (5, "chore")])]
    report = worklog.build_report(sessions, **_scope())
    assert report["subjects"] == []
    assert report["meta"]["sessions_counted"] == 0
    assert len(report["meta"]["unattributed"]) == 1
    assert report["meta"]["unattributed"][0]["branch"] == "chore"


def test_build_report_filters_outside_window():
    sessions = [_sess("s5", "T", "/repo", [(0, "bpz-756-x")]),
                _sess("s6", "T", "/repo", [(90, "bpz-756-x")])]
    win = _scope()
    win["window"] = (_dt(0), _dt(59))
    report = worklog.build_report(sessions, **win)
    rows = {r["subject"]: r for r in report["subjects"]}
    assert rows["BPZ-756 x"]["session_count"] == 1


def test_build_report_marks_started_vs_continued_ticket():
    sessions = [
        # touched long before the window, and again today -> continued, not started
        _sess("s1", "T", "/repo", [(-1000, "bpz-700-old"), (0, "bpz-700-old")]),
        # first ever touch falls inside the window -> started today
        _sess("s2", "T", "/repo", [(5, "bpz-701-new")]),
    ]
    report = worklog.build_report(sessions, **_scope())
    rows = {r["subject"]: r for r in report["subjects"]}
    assert rows["BPZ-700 old"]["started_in_window"] is False
    assert rows["BPZ-701 new"]["started_in_window"] is True


def test_build_report_routes_unowned_branch_to_reviews():
    # bpz-756-x is a known branch, but the user authored nothing on it -> review
    sessions = [_sess("s1", "T", "/repo/wt-756", [(0, "bpz-756-x"), (5, "bpz-756-x")])]
    scope = _scope()
    scope["owned_branches"] = set()  # nothing owned
    report = worklog.build_report(sessions, **scope)
    assert report["subjects"] == []
    assert report["meta"]["totals"]["active_min"] == 0.0
    reviews = report["meta"]["reviews"]
    assert len(reviews) == 1 and reviews[0]["subject"] == "BPZ-756 x"


def test_build_report_owned_branch_stays_as_work():
    sessions = [_sess("s1", "T", "/repo/wt-756", [(0, "bpz-756-x"), (5, "bpz-756-x")])]
    scope = _scope()
    scope["owned_branches"] = {"bpz-756-x"}
    report = worklog.build_report(sessions, **scope)
    assert any(r["subject"] == "BPZ-756 x" for r in report["subjects"])
    assert report["meta"]["reviews"] == []


def test_build_report_title_row_never_a_review():
    # main/title rows aren't branch-derived, so an empty owned set can't demote them
    sessions = [_sess("s1", "T", "/repo", [(0, "main"), (5, "main")])]
    scope = _scope()
    scope["owned_branches"] = set()
    report = worklog.build_report(sessions, **scope)
    assert any(r["subject"] == "T" for r in report["subjects"])
    assert report["meta"]["reviews"] == []


# --- Task 9: discover_session_files -----------------------------------------
def test_discover_session_files(tmp_path):
    proj = tmp_path / "-Users-x-Projects-deurdoor"
    proj.mkdir()
    (proj / "a.jsonl").write_text("{}")
    (proj / "b.jsonl").write_text("{}")
    (proj / "notes.txt").write_text("ignore me")
    found = worklog.discover_session_files(tmp_path)
    assert sorted(p.name for p in found) == ["a.jsonl", "b.jsonl"]


# --- session branch inheritance ------------------------------------------------
def test_inherit_session_branch_fills_generic_events():
    events = [{"branch": "bpz-756-x"}, {"branch": None}, {"branch": "HEAD"},
              {"branch": "main"}, {"branch": "bpz-756-x"}]
    out = worklog.inherit_session_branch(events)
    assert [e["branch"] for e in out] == ["bpz-756-x"] * 5


def test_inherit_session_branch_keeps_other_feature_branches():
    events = [{"branch": "bpz-756-x"}, {"branch": "bpz-756-x"}, {"branch": "bpz-757-y"}, {"branch": None}]
    out = worklog.inherit_session_branch(events)
    assert [e["branch"] for e in out] == ["bpz-756-x", "bpz-756-x", "bpz-757-y", "bpz-756-x"]


def test_inherit_session_branch_leaves_pure_main_session_alone():
    events = [{"branch": "main"}, {"branch": None}]
    assert [e["branch"] for e in worklog.inherit_session_branch(events)] == ["main", None]


def test_in_scope_subdirectory_of_repo_root_and_worktree():
    assert worklog.in_scope("/repo/backend/src", "main", "/repo", set(), set())
    assert worklog.in_scope("/repo/wt-756/backend", None, "/repo", {"/repo/wt-756"}, set())
    assert not worklog.in_scope("/repository", "main", "/repo", set(), set())


def test_parse_session_drops_command_and_handback_noise():
    lines = [
        _json.dumps({"type": "user", "timestamp": "2026-06-16T09:00:00.000Z", "sessionId": "s1",
                     "message": {"content": "<command-message>worklog</command-message>"}}),
        _json.dumps({"type": "user", "timestamp": "2026-06-16T09:01:00.000Z", "sessionId": "s1",
                     "message": {"content": "<local-command-caveat>Caveat: ...</local-command-caveat>"}}),
        _json.dumps({"type": "user", "timestamp": "2026-06-16T09:02:00.000Z", "sessionId": "s1",
                     "message": {"content": "Another Claude session sent a message:\n<agent-message>x"}}),
        _json.dumps({"type": "user", "timestamp": "2026-06-16T09:03:00.000Z", "sessionId": "s1",
                     "message": {"content": "go per your rec"}}),
    ]
    assert worklog.parse_session(lines)["prompts"] == ["go per your rec"]


# --- union estimate + untitled flag ----------------------------------------------
def test_build_report_union_active_merges_parallel_sessions():
    sessions = [
        _sess("s1", "T", "/repo/wt-756", [(0, "bpz-756-x"), (10, "bpz-756-x")]),
        _sess("s2", None, "/wts/add-afdeling", [(0, "add-afdeling"), (10, "add-afdeling")]),
    ]
    report = worklog.build_report(sessions, **_scope())
    assert report["meta"]["totals"]["active_min"] == 20.0
    assert report["meta"]["totals"]["union_active_min"] == 10.0
    assert all(row["untitled"] is False for row in report["subjects"])


def test_build_report_subtotals_split_ticketed_non_ticketed_untitled():
    sessions = [
        _sess("s1", "T", "/repo/wt-756", [(0, "bpz-756-x"), (10, "bpz-756-x")]),
        _sess("s2", "Tooling", "/repo", [(0, "main"), (5, "main")]),
        _sess("abcdef12", None, "/repo", [(0, "main"), (2, "main")]),
    ]
    sub = worklog.build_report(sessions, **_scope())["meta"]["subtotals"]
    assert sub["ticketed"] == {"wallclock_min": 10.0, "active_min": 10.0}
    assert sub["non_ticketed"] == {"wallclock_min": 7.0, "active_min": 7.0}
    assert sub["untitled"] == {"count": 1, "wallclock_min": 2.0, "active_min": 2.0}


def test_build_report_flags_untitled_rows():
    sessions = [_sess("abcdef12", None, "/repo", [(0, "main"), (5, "main")])]
    report = worklog.build_report(sessions, **_scope())
    assert report["subjects"][0]["untitled"] is True


# --- hour suggestion --------------------------------------------------------------
def test_count_workdays():
    assert worklog.count_workdays(date(2026, 9, 21), date(2026, 9, 27)) == 5  # Mon..Sun
    assert worklog.count_workdays(date(2026, 9, 26), date(2026, 9, 26)) == 0  # Saturday
    assert worklog.count_workdays(date(2026, 9, 21), date(2026, 9, 21)) == 1


def test_suggest_hours_splits_budget_in_steps_summing_exactly():
    rows = [
        {"subject": "BPZ-1 a", "ticket": "BPZ-1", "active_min": 90.0, "untitled": False},
        {"subject": "BPZ-2 b", "ticket": "BPZ-2", "active_min": 30.0, "untitled": False},
        {"subject": "Tooling", "ticket": None, "active_min": 30.0, "untitled": False},
        {"subject": "main (untitled x)", "ticket": None, "active_min": 60.0, "untitled": True},
    ]
    out = worklog.suggest_hours(rows, 8.0)
    by_label = {e["label"]: e for e in out}
    assert sum(e["hours"] for e in out) == 8.0
    assert all(e["hours"] % 0.25 == 0 for e in out)
    assert by_label["BPZ-1"]["hours"] == 4.75         # 19.2 units -> 19
    assert by_label["BPZ-2"]["hours"] == 1.75         # 6.4 -> 6, takes the leftover unit
    assert by_label["non-ticketed"]["hours"] == 1.5
    # laid out back to back from 09:00, largest first
    assert [(e["label"], e["start"]) for e in out] == [
        ("BPZ-1", "09:00"), ("BPZ-2", "13:45"), ("non-ticketed", "15:30")]
    assert by_label["non-ticketed"]["ticket"] is None
    assert by_label["non-ticketed"]["subjects"] == ["Tooling"]
    assert "main (untitled x)" not in {s for e in out for s in e["subjects"]}


def test_suggest_hours_empty_when_no_budget_or_no_work():
    rows = [{"subject": "BPZ-1 a", "ticket": "BPZ-1", "active_min": 10.0, "untitled": False}]
    assert worklog.suggest_hours(rows, 0) == []
    assert worklog.suggest_hours([], 8.0) == []


def test_parse_session_drops_skill_expansions_and_one_word_answers():
    def user(ts, text):
        return _json.dumps({"type": "user", "timestamp": ts, "sessionId": "s1",
                            "message": {"content": text}})
    lines = [
        user("2026-06-16T09:00:00.000Z", "`/simplify → 4 cleanup agents`\n\nYou are ..."),
        user("2026-06-16T09:01:00.000Z", "1"),
        user("2026-06-16T09:02:00.000Z", "go"),
        user("2026-06-16T09:03:00.000Z", "push"),
    ]
    assert worklog.parse_session(lines)["prompts"] == ["push"]


# --- commit enrichment ------------------------------------------------------------
def test_enrich_commits_skips_generic_branches(monkeypatch):
    log = [
        "Merge branch 'bpz-756-x' into 'main'\x00HEAD -> main, origin/main",
        "feat: thing\x00bpz-756-x",
    ]
    monkeypatch.setattr(worklog, "_git", lambda repo_root, args: log)
    report = {"subjects": [
        {"branches": ["main"], "commits": [], "merged_commits": []},
        {"branches": ["bpz-756-x"], "commits": [], "merged_commits": []},
    ]}
    worklog._enrich_commits(report, "/repo", _dt(0), _dt(59), "me@example.com")
    assert report["subjects"][0]["commits"] == []
    assert report["subjects"][0]["merged_commits"] == []
    assert report["subjects"][1]["merged_commits"] == ["Merge branch 'bpz-756-x' into 'main'"]
    assert "feat: thing" in report["subjects"][1]["commits"]
