"""Tests for scripts/ci/assemble_review_comment.py.

The assembler collects status from every CI sub-workflow into ReviewItems
classified by severity (error / action_required / warning / info), then
renders them into a single PR comment body.

Status data comes from two sources:
  1. --review-statuses-json: JSON array of status objects from workflow_call
     jobs (review-labels, etc.). Each has kind/source/title/summary/how_to_fix.
  2. --needs-json: {job_name: result} from all-checks-pass. Failed jobs not
     claimed by any status become synthesized ❌ Error items.

Layout rules tested here:
  - each item is its own ### section (no group headers)
  - errors + action_required always visible
  - warnings shown only when present
  - info in a collapsible <details> block
  - sections separated by ---
  - how_to_fix rendered at bottom of action_required items
  - empty → clean banner
  - jobs with declared statuses excluded from failed-jobs list
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "assemble_review_comment.py"
_spec = importlib.util.spec_from_file_location("assemble_review_comment", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load assemble_review_comment.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["assemble_review_comment"] = _mod
_spec.loader.exec_module(_mod)

MARKER = _mod.MARKER
ReviewItem = _mod.ReviewItem


# ─── collect_from_statuses ──────────────────────────────────────────


def test_statuses_empty_json():
    items, sources = _mod.collect_from_statuses("")
    assert items == []
    assert sources == set()


def test_statuses_bad_json():
    items, sources = _mod.collect_from_statuses("not json")
    assert items == []
    assert sources == set()


def test_statuses_action_required():
    statuses = json.dumps([{
        "kind": "action_required",
        "source": "review-labels",
        "title": "CI-sensitive file review",
        "summary": "Changes detected.",
        "how_to_fix": "Add the label.",
    }])
    items, sources = _mod.collect_from_statuses(statuses)
    assert len(items) == 1
    assert items[0].severity == "action_required"
    assert items[0].title == "CI-sensitive file review"
    assert items[0].how_to_fix == "Add the label."
    assert sources == {"review-labels"}


def test_statuses_info():
    statuses = json.dumps([{
        "kind": "info",
        "source": "review-labels",
        "title": "CI-sensitive file review",
        "summary": "Label present.",
    }])
    items, sources = _mod.collect_from_statuses(statuses)
    assert len(items) == 1
    assert items[0].severity == "info"
    assert sources == {"review-labels"}


def test_statuses_multiple():
    statuses = json.dumps([
        {"kind": "action_required", "source": "review-labels",
         "title": "CI review", "summary": "Missing label."},
        {"kind": "action_required", "source": "review-labels",
         "title": "MCP review", "summary": "Missing label."},
    ])
    items, sources = _mod.collect_from_statuses(statuses)
    assert len(items) == 2
    assert sources == {"review-labels"}


def test_statuses_unknown_kind_becomes_info():
    statuses = json.dumps([{
        "kind": "bogus",
        "source": "some-job",
        "title": "X",
        "summary": "Y",
    }])
    items, _ = _mod.collect_from_statuses(statuses)
    assert items[0].severity == "info"


def test_statuses_no_source():
    """Status without a source field — still rendered, just not excluded."""
    statuses = json.dumps([{
        "kind": "info",
        "title": "X",
        "summary": "Y",
    }])
    items, sources = _mod.collect_from_statuses(statuses)
    assert len(items) == 1
    assert sources == set()


# ─── collect_failed_jobs ─────────────────────────────────────────────


def test_failed_jobs_empty_needs():
    assert _mod.collect_failed_jobs("", "https://run") == []


def test_failed_jobs_no_failures():
    needs = json.dumps({"tests": "success", "lint": "skipped"})
    assert _mod.collect_failed_jobs(needs, "https://run") == []


def test_failed_jobs_collects_only_failures():
    needs = json.dumps({"tests": "success", "lint": "failure", "js-tests": "failure"})
    items = _mod.collect_failed_jobs(needs, "https://run/123")
    assert len(items) == 2
    assert all(i.severity == "error" for i in items)
    # sorted by name
    names = [i.title for i in items]
    assert names == ["js-tests", "lint"]
    assert all(i.link == "https://run/123" for i in items)


def test_failed_jobs_bad_json():
    assert _mod.collect_failed_jobs("not json", "https://run") == []


def test_failed_jobs_excluded_by_source():
    """Jobs whose name contains a declared source are excluded."""
    needs = json.dumps({
        "Review label gate / Review label gate": "failure",
        "tests": "failure",
    })
    items = _mod.collect_failed_jobs(needs, "https://run", exclude_sources={"review-label-gate"})
    assert len(items) == 1
    assert items[0].title == "tests"


def test_failed_jobs_no_exclusion_without_sources():
    """Without exclude_sources, all failures are shown."""
    needs = json.dumps({"review-labels": "failure", "tests": "failure"})
    items = _mod.collect_failed_jobs(needs, "https://run")
    assert len(items) == 2


def test_failed_jobs_per_job_url():
    """When job_urls is provided, the link points to the specific job."""
    needs = json.dumps({"tests": "failure", "lint": "failure"})
    job_urls = {"tests": "https://run/1/job/2", "lint": "https://run/1/job/3"}
    items = _mod.collect_failed_jobs(needs, "https://fallback", job_urls=job_urls)
    assert len(items) == 2
    links = {i.title: i.link for i in items}
    assert links["tests"] == "https://run/1/job/2"
    assert links["lint"] == "https://run/1/job/3"


def test_failed_jobs_fallback_to_run_url():
    """Jobs not in job_urls fall back to run_url."""
    needs = json.dumps({"tests": "failure", "lint": "failure"})
    job_urls = {"tests": "https://run/1/job/2"}
    items = _mod.collect_failed_jobs(needs, "https://fallback", job_urls=job_urls)
    links = {i.title: i.link for i in items}
    assert links["tests"] == "https://run/1/job/2"
    assert links["lint"] == "https://fallback"


# ─── collect_lockfile ─────────────────────────────────────────────────


def test_lockfile_skipped():
    assert _mod.collect_lockfile(None, Path("/dev/null")) == []


def test_lockfile_no_changes():
    items = _mod.collect_lockfile(False, Path("/dev/null"))
    assert len(items) == 1
    assert items[0].severity == "info"
    assert "No lockfile changes" in items[0].summary


def test_lockfile_changed_with_content():
    diff = Path("/tmp/_test_lf.md")
    diff.write_text("#### `package-lock.json`\n\n| col | | |")
    items = _mod.collect_lockfile(True, diff)
    diff.unlink(missing_ok=True)
    assert len(items) == 1
    assert items[0].severity == "info"
    assert "dependency versions changed" in items[0].summary
    assert "#### `package-lock.json`" in items[0].detail


def test_lockfile_changed_no_content():
    items = _mod.collect_lockfile(True, Path("/nonexistent"))
    assert len(items) == 1
    assert items[0].severity == "action_required"
    assert "diff content was unavailable" in items[0].summary
    assert items[0].how_to_fix  # has a fix instruction


# ─── collect_timings ─────────────────────────────────────────────────


def test_timings_missing_file():
    assert _mod.collect_timings(Path("/nonexistent")) == []


def test_timings_info():
    f = Path("/tmp/_test_timings.json")
    f.write_text(json.dumps({"severity": "info", "summary": "All good.", "detail": "", "report_url": "https://report"}))
    items = _mod.collect_timings(f)
    f.unlink(missing_ok=True)
    assert len(items) == 1
    assert items[0].severity == "info"
    assert items[0].link == "https://report"


def test_timings_warning():
    f = Path("/tmp/_test_timings.json")
    f.write_text(json.dumps({"severity": "warning", "summary": "Slower.", "detail": "- job: +5s", "report_url": ""}))
    items = _mod.collect_timings(f)
    f.unlink(missing_ok=True)
    assert items[0].severity == "warning"
    assert "- job: +5s" in items[0].detail


def test_timings_error_promoted_to_info():
    """Timings is an observability job — never error severity."""
    f = Path("/tmp/_test_timings.json")
    f.write_text(json.dumps({"severity": "error", "summary": "bad", "detail": "", "report_url": ""}))
    items = _mod.collect_timings(f)
    f.unlink(missing_ok=True)
    assert items[0].severity == "info"


# ─── render_comment ───────────────────────────────────────────────────


def test_render_empty_shows_clean_banner():
    """Completely clean — dog kaomoji + 'no issues' banner, no sections."""
    body = _mod.render_comment([])
    assert body.startswith(MARKER)
    assert "૮ >ﻌ< ა" in body
    assert "No issues — all checks passed!" in body
    assert "###" not in body  # no section headers
    assert "✅" not in body  # old banner gone


def test_render_info_only_shows_banner_plus_details():
    """Info items only — 'no issues' banner + collapsible details below."""
    items = [
        ReviewItem(severity="info", title="lockfile", summary="No changes."),
        ReviewItem(severity="info", title="timings", summary="OK."),
    ]
    body = _mod.render_comment(items)
    assert "૮ >ﻌ< ა" in body
    assert "No issues — all checks passed!" in body
    assert "<details>" in body
    assert "</details>" in body
    assert "No changes." in body
    assert "OK." in body
    # No blocking sections
    assert "### ❌" not in body
    assert "### ⚠️" not in body


def test_render_info_only_with_pending_shows_banner_plus_footer():
    items = [ReviewItem(severity="info", title="lockfile", summary="No changes.")]
    body = _mod.render_comment(items, pending_jobs=["ci-timings"])
    assert "No issues — all checks passed!" in body
    assert "<details>" in body
    assert "Still running" in body
    assert "`ci-timings`" in body


def test_render_group_header_for_errors():
    """Errors appear under a '### ❌ Job failures' group header."""
    items = [
        ReviewItem(severity="error", title="tests", summary="Job **tests** failed.", link="https://run"),
        ReviewItem(severity="error", title="lint", summary="Job **lint** failed.", link="https://run"),
    ]
    body = _mod.render_comment(items)
    assert "### ❌ Job failures" in body
    # Each item is a #### sub-section
    assert "#### tests" in body
    assert "#### lint" in body
    # Items should be under the group header
    assert body.index("### ❌ Job failures") < body.index("#### tests")


def test_render_group_header_for_action_required():
    """Action required items appear under a '### ⚠️ Action required' group header."""
    items = [
        ReviewItem(severity="action_required", title="CI review", summary="Need label."),
    ]
    body = _mod.render_comment(items)
    assert "### ⚠️ Action required" in body
    assert "#### CI review" in body


def test_render_group_header_for_warnings():
    items = [
        ReviewItem(severity="warning", title="CI timings", summary="Slower."),
    ]
    body = _mod.render_comment(items)
    assert "### ⚠️ Warnings" in body
    assert "#### CI timings" in body
    assert "<details>" not in body  # no info items

    items2 = [ReviewItem(severity="info", title="x", summary="y")]
    body2 = _mod.render_comment(items2)
    assert "### ⚠️ Warnings" not in body2


def test_render_no_duplicated_severity_in_item_body():
    """Items don't repeat the severity label — the group header carries it."""
    items = [ReviewItem(severity="error", title="tests", summary="Job failed.", link="https://run")]
    body = _mod.render_comment(items)
    assert "#### tests" in body
    assert "Job failed." in body
    # The severity label should NOT appear in the item body
    assert "**❌ Error**" not in body


def test_render_how_to_fix_at_bottom():
    items = [
        ReviewItem(severity="action_required", title="CI review", summary="Need label.",
                   how_to_fix="Add the `ci-reviewed` label."),
    ]
    body = _mod.render_comment(items)
    assert "**How to fix:**" in body
    assert "Add the `ci-reviewed` label." in body
    assert body.index("Need label.") < body.index("How to fix")


def test_render_sections_separated_by_hr():
    items = [
        ReviewItem(severity="error", title="tests", summary="failed."),
        ReviewItem(severity="action_required", title="CI review", summary="need label."),
    ]
    body = _mod.render_comment(items)
    assert "\n\n---\n\n" in body


def test_render_errors_always_visible():
    items = [
        ReviewItem(severity="error", title="tests", summary="Job **tests** failed.", link="https://run"),
        ReviewItem(severity="info", title="lockfile", summary="No changes."),
    ]
    body = _mod.render_comment(items)
    assert "### ❌ Job failures" in body
    assert "#### tests" in body
    assert "Job **tests** failed." in body
    assert "[View logs](https://run)" in body
    assert "<details>" in body
    assert "No changes." in body


def test_render_info_in_collapsible_details():
    items = [
        ReviewItem(severity="info", title="lockfile", summary="No changes."),
        ReviewItem(severity="info", title="timings", summary="OK."),
    ]
    body = _mod.render_comment(items)
    assert "<details>" in body
    assert "</details>" in body
    assert "Details (2 items)" in body
    assert "No changes." in body
    assert "OK." in body


def test_render_order_errors_then_action_then_warn_then_info():
    items = [
        ReviewItem(severity="info", title="i", summary="info"),
        ReviewItem(severity="warning", title="w", summary="warn"),
        ReviewItem(severity="action_required", title="a", summary="action"),
        ReviewItem(severity="error", title="e", summary="error"),
    ]
    body = _mod.render_comment(items)
    error_pos = body.index("### ❌ Job failures")
    action_pos = body.index("### ⚠️ Action required")
    warn_pos = body.index("### ⚠️ Warnings")
    info_pos = body.index("<details>")
    assert error_pos < action_pos < warn_pos < info_pos


# ─── render_comment (pending jobs) ────────────────────────────────────


def test_render_pending_only_shows_running_banner():
    body = _mod.render_comment([], pending_jobs=["ci-timings"])
    assert body.startswith(MARKER)
    assert "⏳" in body
    assert "`ci-timings`" in body
    assert "###" not in body


def test_render_pending_footer_appended_after_items():
    items = [ReviewItem(severity="info", title="lockfile", summary="No changes.")]
    body = _mod.render_comment(items, pending_jobs=["ci-timings"])
    assert "૮ >ﻌ< ა" in body
    assert body.index("No changes.") < body.index("Still running")
    assert "<sub>⏳ Still running: `ci-timings`</sub>" in body


def test_render_pending_multiple_jobs_sorted():
    body = _mod.render_comment([], pending_jobs=["docker", "ci-timings"])
    assert "`ci-timings`" in body
    assert "`docker`" in body
    assert body.index("`ci-timings`") < body.index("`docker`")


def test_render_no_pending_no_footer():
    items = [ReviewItem(severity="info", title="x", summary="y")]
    body = _mod.render_comment(items)
    assert "Still running" not in body


# ─── assemble (integration) ──────────────────────────────────────────


def test_assemble_all_skipped_clean_banner():
    body = _mod.assemble()
    assert body.startswith(MARKER)
    assert "૮ >ﻌ< ა" in body
    assert "No issues — all checks passed!" in body
    assert "###" not in body


def test_assemble_failed_job_shown():
    needs = json.dumps({"tests": "failure", "lint": "success"})
    body = _mod.assemble(needs_json=needs, run_url="https://run/1")
    assert "### ❌ Job failures" in body
    assert "#### tests" in body
    assert "https://run/1" in body


def test_assemble_with_review_statuses():
    """Statuses from review-labels render directly + exclude gate from errors."""
    statuses = json.dumps([{
        "kind": "action_required",
        "source": "review-label-gate",
        "title": "CI-sensitive file review",
        "summary": "Changes detected.",
        "how_to_fix": "Add the label.",
    }])
    needs = json.dumps({
        "Review label gate / Review label gate": "failure",
        "tests": "success",
    })
    body = _mod.assemble(
        needs_json=needs,
        run_url="https://run",
        review_statuses_json=statuses,
    )
    # The action_required section is rendered from the status object
    assert "### ⚠️ Action required" in body
    assert "#### CI-sensitive file review" in body
    assert "Add the label." in body
    # The review-labels job failure is NOT shown as ❌ Error
    assert "### ❌ Job failures" not in body


def test_assemble_pending_jobs():
    body = _mod.assemble(pending_jobs=["ci-timings"])
    assert "⏳" in body
    assert "`ci-timings`" in body


def test_assemble_with_items_and_pending():
    needs = json.dumps({"tests": "failure"})
    body = _mod.assemble(needs_json=needs, run_url="https://run", pending_jobs=["ci-timings"])
    assert "### ❌ Job failures" in body
    assert "#### tests" in body
    assert "Still running" in body
    assert "`ci-timings`" in body


def test_assemble_lockfile_info():
    body = _mod.assemble(lockfile_changed=False)
    assert "<details>" in body
    assert "No lockfile changes" in body
