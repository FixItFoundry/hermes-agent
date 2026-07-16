#!/usr/bin/env python3
"""Assemble the unified CI review comment for a pull request.

Several CI review jobs (package-lock diff, CI-sensitive file review, MCP
catalog review) used to post independent PR comments — one each, each with
its own marker. This script merges them into a **single** comment body that
the ``comment-results`` job in ``ci.yml`` upserts via the
``<!-- hermes-ci-review-bot -->`` marker.

Inputs arrive as CLI args:

* ``--lockfile-changed`` — ``true`` if ``lockfile-diff.yml`` reported changes.
* ``--lockfile-diff`` — path to the lockfile diff markdown (downloaded from
  the ``lockfile-diff`` artifact). Only meaningful when ``--lockfile-changed``
  is ``true``.
* ``--ci-review`` — ``true`` / ``false`` / empty (the lane didn't run →
  no CI-sensitive changes, so the section is omitted).
* ``--mcp-catalog`` — ``true`` / ``false`` / empty (same tri-state).

Each section is only included when the corresponding lane actually ran, so a
PR that doesn't touch lockfiles or the MCP catalog gets a shorter comment.
Exits 0 always — comment posting is best-effort (fork PRs are read-only).

Section format (consistent across all sections)::

    ### {emoji} {Title}

    **{Action required | Information}** — {one-line summary}. {action note}

    {data — tables, bullet lists, etc.}

``Action required`` means a human must do something before merge (add a
label, verify a finding). ``Information`` means the section is purely
informational — no action needed, the data is there for reference.

Usage::

    python scripts/ci/assemble_review_comment.py \\
        --lockfile-changed "$LOCKFILE_CHANGED" \\
        --lockfile-diff /tmp/lockfile-diff.md \\
        --ci-review "$CI_REVIEW" \\
        --mcp-catalog "$MCP_CATALOG" \\
        --output /tmp/comment-body.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Hidden marker the ``comment-results`` job uses to find-and-edit its
# previous comment instead of stacking new ones on each run.
MARKER = "<!-- hermes-ci-review-bot -->"


def _bool(val: str | None) -> bool | None:
    """Coerce a workflow_call output string to a tri-state bool.

    Returns ``True`` / ``False`` for ``"true"`` / ``"false"``, and ``None``
    when the value is empty (the job was skipped, so there's no status to
    report).
    """
    if not val:
        return None
    return val.strip().lower() == "true"


def section_lockfile(changed: bool | None, diff_path: Path) -> str:
    """Build the package-lock.json diff section.

    ``changed=None`` means the lockfile-diff job was skipped (no
    ``package-lock.json`` changes) — return an empty string so the section is
    omitted entirely.
    """
    if changed is None:
        return ""
    if not changed:
        return (
            "### 📦 package-lock.json\n\n"
            "**Information** — No lockfile changes. "
            "Locked versions match the target branch.\n"
        )
    content = diff_path.read_text(encoding='utf-8').strip() if diff_path.exists() else ""
    if not content:
        return (
            "### 📦 package-lock.json\n\n"
            "**Action required** — Lockfile changes detected but the diff "
            "content was unavailable (artifact expired or download failed). "
            "Inspect `package-lock.json` directly.\n"
        )
    return (
        "### 📦 package-lock.json\n\n"
        "**Information** — Locked npm dependency versions changed. "
        "Review the version deltas below.\n\n"
        f"{content}\n"
    )


def section_ci_review(reviewed: bool | None) -> str:
    """Build the CI-sensitive file review section."""
    if reviewed is None:
        # Lane didn't run — no CI-sensitive files changed.
        return ""
    if reviewed:
        return (
            "### 🔒 CI-sensitive file review\n\n"
            "**Information** — `ci-reviewed` label is present. "
            "No action needed.\n"
        )
    return (
        "### 🔒 CI-sensitive file review\n\n"
        "**Action required** — This PR changes CI-sensitive files "
        "(eslint config, workflow YAMLs, or composite actions). "
        "These influence what the js-autofix job executes and pushes to main. "
        "Add the `ci-reviewed` label after verifying:\n"
        "- no new eslint rules with custom `fix` functions that write outside linted paths,\n"
        "- no workflow changes that widen permissions or remove guards,\n"
        "- no composite action changes that alter what gets executed.\n"
    )


def section_mcp_review(reviewed: bool | None) -> str:
    """Build the MCP catalog security review section."""
    if reviewed is None:
        # Lane didn't run — no MCP catalog changes.
        return ""
    if reviewed:
        return (
            "### 🔧 MCP catalog security review\n\n"
            "**Information** — `ci-reviewed` label is present. "
            "No action needed.\n"
        )
    return (
        "### 🔧 MCP catalog security review\n\n"
        "**Action required** — This PR changes the bundled MCP catalog or "
        "MCP catalog installer code. MCP entries can define local commands "
        "that users later install into `mcp_servers`, so this needs explicit "
        "maintainer review before merge. "
        "Add the `ci-reviewed` label after verifying:\n"
        "- any new/changed `optional-mcps/**/manifest.yaml` command and args are expected,\n"
        "- stdio transports do not use shell+egress/exfiltration payloads,\n"
        "- git install refs are pinned and bootstrap commands are minimal,\n"
        "- requested env vars/secrets match the upstream MCP's documented needs.\n"
    )


def assemble(
    lockfile_changed: bool | None,
    lockfile_diff: Path,
    ci_review: bool | None,
    mcp_catalog: bool | None,
) -> str:
    """Assemble the full comment body from individual job outputs.

    Each section builder receives ``None`` when its lane was skipped (so
    the section is omitted entirely), or a ``bool`` when the lane ran
    (``True`` = label present / changes detected, ``False`` = missing /
    no changes).
    """
    sections: list[str] = []

    lf = section_lockfile(lockfile_changed, lockfile_diff)
    if lf:
        sections.append(lf)

    cr = section_ci_review(ci_review)
    if cr:
        sections.append(cr)

    mr = section_mcp_review(mcp_catalog)
    if mr:
        sections.append(mr)

    if not sections:
        # All lanes were skipped or reported no issues — still post a
        # comment so the pending → done transition is visible.
        body = (
            f"{MARKER}\n"
            "## ✅ CI review\n\n"
            "**Information** — All review checks passed. No issues to report.\n"
        )
    else:
        body = f"{MARKER}\n## ૮ >ﻌ< ა CI review\n\n" + "---\n".join(sections)

    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lockfile-changed",
        default="false",
        help="Whether lockfile-diff reported changes ('true', 'false', or empty when skipped).",
    )
    parser.add_argument(
        "--lockfile-diff",
        type=Path,
        default=Path("/dev/null"),
        help="Path to the lockfile diff markdown file.",
    )
    parser.add_argument(
        "--ci-review",
        default="",
        help="ci-reviewed label status: 'true', 'false', or empty (lane skipped).",
    )
    parser.add_argument(
        "--mcp-catalog",
        default="",
        help="MCP catalog review status: 'true', 'false', or empty (lane skipped).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output file for the assembled comment body.",
    )
    args = parser.parse_args()

    body = assemble(
        lockfile_changed=_bool(args.lockfile_changed),
        lockfile_diff=args.lockfile_diff,
        ci_review=_bool(args.ci_review),
        mcp_catalog=_bool(args.mcp_catalog),
    )

    args.output.write_text(body)
    print(f"Wrote {len(body)} chars to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
