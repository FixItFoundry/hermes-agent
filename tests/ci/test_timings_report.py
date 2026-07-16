"""Tests for scripts/ci/timings_report.py — generate_review_status().

The review status is a compact JSON consumed by the unified PR comment
assembler. It classifies the CI timings result as info/warning (never error
— timings is an observability job, not a gate) and provides a one-line
summary plus optional per-job delta detail.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "timings_report.py"
_spec = importlib.util.spec_from_file_location("timings_report", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load timings_report.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_T0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _ts(seconds: float) -> str:
    """ISO timestamp `seconds` after T0."""
    dt = _T0.timestamp() + seconds
    return datetime.fromtimestamp(dt, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _job(name: str, dur_s: float, start_s: float = 0.0, conclusion: str = "success") -> dict:
    """Build a normalized job dict with realistic timestamps for wall-time math."""
    return {
        "name": name,
        "duration_s": dur_s,
        "conclusion": conclusion,
        "started_at": _ts(start_s),
        "completed_at": _ts(start_s + dur_s),
        "wait_s": 0.0,
    }


def _timings(jobs: list[dict]) -> dict:
    return {"run_id": "123", "head_sha": "abc", "created_at": "", "jobs": jobs}


def test_no_baseline_is_info():
    t = _timings([_job("tests", 60.0)])
    status = _mod.generate_review_status(t, None)
    assert status["severity"] == "info"
    assert "no baseline" in status["summary"].lower()
    assert status["report_url"] == ""


def test_no_regression_is_info():
    cur = _timings([_job("tests", 60.0)])
    bl = _timings([_job("tests", 60.0)])
    status = _mod.generate_review_status(cur, bl)
    assert status["severity"] == "info"
    assert "+0.0%" in status["summary"]


def test_small_regression_is_info():
    cur = _timings([_job("tests", 65.0)])
    bl = _timings([_job("tests", 60.0)])
    status = _mod.generate_review_status(cur, bl)
    # +8.3% — well under the 25% warning threshold
    assert status["severity"] == "info"


def test_large_regression_is_warning():
    cur = _timings([_job("tests", 80.0)])
    bl = _timings([_job("tests", 60.0)])
    status = _mod.generate_review_status(cur, bl)
    # +33% — above the 25% threshold
    assert status["severity"] == "warning"
    assert "+33" in status["summary"]


def test_improvement_is_info():
    cur = _timings([_job("tests", 40.0)])
    bl = _timings([_job("tests", 60.0)])
    status = _mod.generate_review_status(cur, bl)
    assert status["severity"] == "info"
    assert "-33" in status["summary"]


def test_detail_shows_top_deltas():
    cur = _timings([_job("slow-job", 120.0), _job("fast-job", 30.0, start_s=120.0)])
    bl = _timings([_job("slow-job", 60.0), _job("fast-job", 60.0, start_s=60.0)])
    status = _mod.generate_review_status(cur, bl)
    assert "slow-job" in status["detail"]
    assert "fast-job" in status["detail"]
    # Sorted by abs delta — slow-job (+60) before fast-job (-30)
    assert status["detail"].index("slow-job") < status["detail"].index("fast-job")


def test_skipped_jobs_excluded_from_detail():
    cur = _timings([_job("skipped-job", 0.0, conclusion="skipped"), _job("tests", 60.0)])
    bl = _timings([_job("skipped-job", 0.0, conclusion="skipped"), _job("tests", 60.0)])
    status = _mod.generate_review_status(cur, bl)
    assert "skipped-job" not in status["detail"]


def test_report_url_passed_through():
    t = _timings([_job("tests", 60.0)])
    status = _mod.generate_review_status(t, None, report_url="https://artifact/123")
    assert status["report_url"] == "https://artifact/123"


def test_never_error_severity():
    """Timings is observability — even huge regressions are warnings, not errors."""
    cur = _timings([_job("tests", 600.0)])
    bl = _timings([_job("tests", 60.0)])
    status = _mod.generate_review_status(cur, bl)
    assert status["severity"] == "warning"
    assert status["severity"] != "error"
