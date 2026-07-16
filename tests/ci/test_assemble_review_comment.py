"""Tests for scripts/ci/assemble_review_comment.py.

The assembler merges outputs from several CI review sub-workflows into a
single PR comment body. Each section must follow a consistent format:

    ### {emoji} {Title}

    **{Action required | Information}** — {summary}. {action note}

    {data}

Sections are omitted when their lane was skipped (``None``), and the
``Action required`` / ``Information`` tag must accurately reflect whether
a human needs to do something.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "assemble_review_comment.py"
_spec = importlib.util.spec_from_file_location("assemble_review_comment", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load assemble_review_comment.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

MARKER = _mod.MARKER


# ─── lockfile section ────────────────────────────────────────────────


def test_lockfile_skipped_omits_section():
    assert _mod.section_lockfile(None, Path("/dev/null")) == ""


def test_lockfile_no_changes_is_information():
    s = _mod.section_lockfile(False, Path("/dev/null"))
    assert "📦" in s
    assert "**Information**" in s
    assert "Action required" not in s


def test_lockfile_changed_with_content_includes_diff():
    diff = Path("/tmp/_test_lf.md")
    diff.write_text("#### `package-lock.json`\n\n| col | | |")
    s = _mod.section_lockfile(True, diff)
    assert "**Information**" in s
    assert "dependency versions changed" in s
    assert "#### `package-lock.json`" in s
    diff.unlink(missing_ok=True)


def test_lockfile_changed_no_content_is_action_required():
    s = _mod.section_lockfile(True, Path("/nonexistent"))
    assert "**Action required**" in s
    assert "diff content was unavailable" in s


# ─── ci-review section ───────────────────────────────────────────────


def test_ci_review_skipped_omits_section():
    assert _mod.section_ci_review(None) == ""


def test_ci_review_label_present_is_information():
    s = _mod.section_ci_review(True)
    assert "🔒" in s
    assert "**Information**" in s
    assert "ci-reviewed" in s


def test_ci_review_label_missing_is_action_required():
    s = _mod.section_ci_review(False)
    assert "**Action required**" in s
    assert "Add the `ci-reviewed` label" in s
    assert "eslint" in s


# ─── mcp-catalog section ──────────────────────────────────────────────


def test_mcp_review_skipped_omits_section():
    assert _mod.section_mcp_review(None) == ""


def test_mcp_review_label_present_is_information():
    s = _mod.section_mcp_review(True)
    assert "🔧" in s
    assert "**Information**" in s


def test_mcp_review_label_missing_is_action_required():
    s = _mod.section_mcp_review(False)
    assert "**Action required**" in s
    assert "Add the `ci-reviewed` label" in s
    assert "MCP catalog" in s


# ─── full assembly ───────────────────────────────────────────────────


def test_assemble_all_skipped_shows_clean_banner():
    body = _mod.assemble(None, Path("/dev/null"), None, None)
    assert body.startswith(MARKER)
    assert "✅" in body
    assert "**Information**" in body
    # No section headers when all lanes skipped.
    assert "###" not in body


def test_assemble_sections_joined_with_divider():
    diff = Path("/tmp/_test_lf2.md")
    diff.write_text("#### `package-lock.json`\n\n| col | | |")
    body = _mod.assemble(True, diff, False, False)
    diff.unlink(missing_ok=True)
    assert body.startswith(MARKER)
    assert "૮ >ﻌ< ა" in body
    # All three sections present, separated by horizontal rules.
    assert body.count("---") >= 2
    assert "📦" in body
    assert "🔒" in body
    assert "🔧" in body


def test_assemble_only_ci_review_lane_ran():
    """MCP section omitted when only CI-sensitive files changed."""
    body = _mod.assemble(None, Path("/dev/null"), False, None)
    assert "🔒" in body
    assert "🔧" not in body
    assert "📦" not in body


def test_assemble_only_mcp_catalog_lane_ran():
    """CI-review section omitted when only MCP catalog files changed."""
    body = _mod.assemble(None, Path("/dev/null"), None, False)
    assert "🔧" in body
    assert "🔒" not in body
    assert "📦" not in body


def test_assemble_ci_and_mcp_independent_gating():
    """Both sections can appear independently with their own label status."""
    body = _mod.assemble(None, Path("/dev/null"), True, False)
    # CI-review section: label present → Information
    assert "🔒" in body
    # MCP section: label missing → Action required
    assert "🔧" in body
    # The lockfile section should not be present.
    assert "📦" not in body


def test_assemble_action_required_appears_before_information_ordering():
    """When both action-required and information sections are present,
    each has its own tag — order doesn't matter but tags must be correct."""
    body = _mod.assemble(None, Path("/dev/null"), True, False)
    assert "**Information**" in body  # ci-review: label present
    assert "**Action required**" in body  # mcp: label missing
