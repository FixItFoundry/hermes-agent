#!/usr/bin/env python3
"""Assemble the unified CI review comment for a pull request.

Collects status from every CI sub-workflow into a single PR comment body
that the ``comment-live`` job upserts via the
``<!-- hermes-ci-review-bot -->`` marker.

Every piece of information is classified into one of four severities:

``error``
    A CI job failed. Always shown, with a link to the job logs.

``action_required``
    A human must do something before merge (add a label, verify a finding).
    Always shown.

``warning``
    Something noteworthy but not blocking (e.g. CI timing regression).
    Shown only when present.

``info``
    Purely informational (e.g. lockfile diff, label present, timings OK).
    Shown in a collapsible ``<details>`` section so it doesn't clutter
    the comment. Kept as short as possible.

Layout (top to bottom):

    ## ૮ >ﻌ< ა CI review

    ### ❌ Job failures              (only if errors exist)
    ### ⚠️ Action required          (only if action_required exist)
    ### ⚠️ Warnings                 (only if warnings exist)
    <details><summary>ℹ️ Details</summary> ... </details>
    <sub>⏳ Still running: ...</sub>   (only if jobs are pending)

When there are no errors/action_required/warnings, a "no issues!" banner
is shown instead of the blocking sections, and info items (if any) still
appear in the collapsible ``<details>`` block.

Status data comes from two sources:

1. ``--review-statuses-json`` — a JSON array of status objects declared
   by each workflow_call job (see ``emit_review_status.py``). Each object:
   ``{kind, source, title, summary, how_to_fix?, detail?, link?}``.
   ``source`` is the workflow name that declared the status; it's used to
   exclude the corresponding job from the failed-jobs error list (the job
   already has its own action_required section).

2. ``--needs-json`` — a ``{job_name: result}`` dict from
   ``all-checks-pass``. Jobs that failed and weren't claimed by any
   status object become synthesized ❌ Error items.

Exits 0 always — comment posting is best-effort (fork PRs are read-only).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Hidden marker the comment system uses to find-and-edit its
# previous comment instead of stacking new ones on each run.
MARKER = "<!-- hermes-ci-review-bot -->"

# Severity ordering for display.
_SEVERITY_ORDER = ["error", "action_required", "warning", "info"]

# Severities that trigger the "blocking issues" layout (vs. the
# "no issues!" banner).
_BLOCKING_SEVERITIES = ("error", "action_required", "warning")

_SEVERITY_GROUP_HEADER = {
    "error": "### ❌ Job failures",
    "action_required": "### ⚠️ Action required",
    "warning": "### ⚠️ Warnings",
    "info": "### ℹ️ Details",
}


@dataclass
class ReviewItem:
    """A single piece of review information with a severity tag."""

    severity: str  # "error" | "action_required" | "warning" | "info"
    title: str  # short section title, e.g. "package-lock.json"
    summary: str  # one-line summary
    detail: str = ""  # optional markdown detail (tables, bullet lists, etc.)
    link: str = ""  # optional URL (e.g. job logs, report)
    link_label: str = "View logs"  # label for the link
    how_to_fix: str = ""  # optional markdown checklist for action_required items
    source: str = ""  # workflow that declared this status (for dedup)


def _bool(val: str | None) -> bool | None:
    """Coerce a workflow_call output string to a tri-state bool.

    Returns ``True`` / ``False`` for ``"true"`` / ``"false"``, and ``None``
    when the value is empty (the job was skipped, so there's no status to
    report).
    """
    if not val:
        return None
    return val.strip().lower() == "true"


# ---------------------------------------------------------------------------
# Collectors — each returns a list of ReviewItems (possibly empty)
# ---------------------------------------------------------------------------


def collect_from_statuses(review_statuses_json: str) -> tuple[list[ReviewItem], set[str]]:
    """Parse a JSON array of status objects into ReviewItems.

    Each status object can have:
        kind:        "error" | "action_required" | "warning" | "info"
        source:      workflow name (e.g. "review-label-gate")
        title:       section heading
        summary:     one-line description
        how_to_fix:  markdown checklist (optional)
        detail:      markdown detail (optional)
        link:        URL (optional)

    Returns ``(items, sources)`` where ``sources`` is the set of
    ``source`` values — used by :func:`collect_failed_jobs` to exclude
    jobs that already declared their own status.
    """
    if not review_statuses_json:
        return [], set()
    try:
        statuses = json.loads(review_statuses_json)
    except (json.JSONDecodeError, TypeError):
        return [], set()
    if not isinstance(statuses, list):
        return [], set()

    items: list[ReviewItem] = []
    sources: set[str] = set()

    for s in statuses:
        if not isinstance(s, dict):
            continue
        kind = s.get("kind", "info")
        if kind not in _SEVERITY_ORDER:
            kind = "info"
        source = s.get("source", "")
        if source:
            sources.add(source)
        items.append(ReviewItem(
            severity=kind,
            title=s.get("title", "Unknown"),
            summary=s.get("summary", ""),
            detail=s.get("detail", ""),
            link=s.get("link", ""),
            link_label=s.get("link_label", "View logs"),
            how_to_fix=s.get("how_to_fix", ""),
            source=source,
        ))

    return items, sources


def collect_failed_jobs(
    needs_json: str,
    run_url: str,
    exclude_sources: set[str] | None = None,
    job_urls: dict[str, str] | None = None,
) -> list[ReviewItem]:
    """Build error items for failed CI jobs from the ``needs`` context.

    ``needs_json`` is the JSON string emitted by ``all-checks-pass`` — a
    ``{job_name: result}`` dict where result is ``success`` / ``failure``
    / ``skipped``. Only ``failure`` entries become error items.

    ``exclude_sources`` is a set of ``source`` values from status objects
    declared by workflow_call jobs (see :func:`collect_from_statuses`).
    Job names containing any of these source strings are excluded — their
    failure is already covered by their own action_required section.

    ``job_urls`` is an optional ``{job_name: html_url}`` dict from the
    live poller. When a job's name is in this dict, the ❌ Error link
    points directly to that job's logs page instead of the whole run.
    Falls back to ``run_url`` when no per-job URL is available.
    """
    if not needs_json:
        return []
    try:
        needs = json.loads(needs_json)
    except (json.JSONDecodeError, TypeError):
        return []

    # Pre-normalize exclude sources once: lowercase + hyphens→spaces, so
    # "review-label-gate" matches "Review label gate / Review label gate".
    norm_sources = {
        src.lower().replace("-", " ") for src in (exclude_sources or set())
    }

    items: list[ReviewItem] = []
    for name, result in sorted(needs.items()):
        if result != "failure":
            continue
        if norm_sources:
            norm = name.lower().replace("-", " ")
            if any(src in norm for src in norm_sources):
                continue
        link = (job_urls or {}).get(name, run_url)
        items.append(ReviewItem(
            severity="error",
            title=name,
            summary=f"Job **{name}** failed.",
            link=link,
        ))
    return items


def collect_lockfile(changed: bool | None, diff_path: Path) -> list[ReviewItem]:
    """Collect review items for the package-lock.json diff section."""
    if changed is None:
        return []
    if not changed:
        return [ReviewItem(
            severity="info",
            title="package-lock.json",
            summary="No lockfile changes — locked versions match the target branch.",
        )]
    content = diff_path.read_text(encoding="utf-8").strip() if diff_path.exists() else ""
    if not content:
        return [ReviewItem(
            severity="action_required",
            title="package-lock.json",
            summary="Lockfile changes detected but the diff content was unavailable (artifact expired or download failed).",
            how_to_fix="Inspect `package-lock.json` directly in the PR diff.",
        )]
    return [ReviewItem(
        severity="info",
        title="package-lock.json",
        summary="Locked npm dependency versions changed.",
        detail=content,
    )]


def collect_timings(status_path: Path) -> list[ReviewItem]:
    """Collect a review item from the CI timings review-status JSON."""
    if not status_path.exists():
        return []
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    severity = data.get("severity", "info")
    # Timings is never "error" severity — it's an observability job.
    if severity not in ("info", "warning"):
        severity = "info"
    report_url = data.get("report_url", "")

    return [ReviewItem(
        severity=severity,
        title="CI timings",
        summary=data.get("summary", "CI timings available."),
        detail=data.get("detail", ""),
        link=report_url,
        link_label="View report" if report_url else "",
    )]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_item(item: ReviewItem) -> str:
    """Render a single ReviewItem as a markdown ``####`` sub-section.

    The group header (``### ❌ Job failures`` etc.) carries the severity
    emoji, so items don't repeat it. Layout per item::

        #### {title}

        {summary}

        {detail}

        [{link_label}]({link})

        **How to fix:**

        {how_to_fix}
    """
    parts = [f"#### {item.title}", "", item.summary]

    if item.detail:
        parts += ["", item.detail]
    if item.link:
        parts += ["", f"[{item.link_label}]({item.link})"]
    if item.how_to_fix:
        parts += ["", "**How to fix:**", "", item.how_to_fix]

    return "\n".join(parts)


def _render_group(header: str, items: list[ReviewItem]) -> str:
    """Render a severity group: ``###`` header + items separated by ``---``."""
    blocks = [_render_item(i) for i in items]
    return f"{header}\n\n" + "\n\n---\n\n".join(blocks)


def _render_info_details(items: list[ReviewItem]) -> str:
    """Render info items as a collapsible ``<details>`` block."""
    blocks = [_render_item(i) for i in items]
    detail_md = "\n\n---\n\n".join(blocks)
    count = len(items)
    label = f"ℹ️ Details ({count} item{'s' if count != 1 else ''})"
    return f"<details>\n<summary>{label}</summary>\n\n{detail_md}\n\n</details>"


def _render_pending_footer(pending_jobs: list[str]) -> str:
    """Render the dimmed ``<sub>`` footer for jobs still running."""
    job_list = ", ".join(f"`{j}`" for j in sorted(pending_jobs))
    return f"\n\n---\n\n<sub>⏳ Still running: {job_list}</sub>\n"


def render_comment(items: list[ReviewItem], pending_jobs: list[str] | None = None) -> str:
    """Render the full comment body from a list of review items.

    Items are grouped by severity under ``###`` group headers, separated
    by ``---``. Errors and action_required items are always visible.
    Warnings are shown only when present. Info items are in a collapsible
    ``<details>`` block. If ``pending_jobs`` is non-empty, a dimmed
    ``<sub>`` footer is appended listing jobs still running.

    When there are no errors, action_required, or warnings (only info
    items, or nothing at all), a "no issues!" banner with a dog kaomoji
    is shown at the top, and info items (if any) follow in a collapsible
    ``<details>`` block.
    """
    pending = pending_jobs or []

    # Group by severity
    by_severity: dict[str, list[ReviewItem]] = {s: [] for s in _SEVERITY_ORDER}
    for item in items:
        by_severity.setdefault(item.severity, []).append(item)

    info = by_severity.get("info", [])
    has_blocking = any(by_severity.get(s) for s in _BLOCKING_SEVERITIES)

    # ── No results yet ──────────────────────────────────────────────
    if not items and not pending:
        return f"{MARKER}\n## ૮ >ﻌ< ა CI review\n\nNo issues — all checks passed!\n"

    if not items and pending:
        job_list = ", ".join(f"`{j}`" for j in sorted(pending))
        return f"{MARKER}\n## ⏳ CI review\n\nCI checks are running. Waiting on: {job_list}.\n"

    # ── Build sections ──────────────────────────────────────────────
    sections: list[str] = []

    if not has_blocking:
        # No blocking issues — "no issues!" banner.
        header = "## ૮ >ﻌ< ა CI review\n\nNo issues — all checks passed!"
    else:
        # Blocking issues — normal grouped layout.
        header = "## ૮ >ﻌ< ა CI review"
        for sev in _BLOCKING_SEVERITIES:
            group = by_severity.get(sev, [])
            if group:
                sections.append(_render_group(_SEVERITY_GROUP_HEADER[sev], group))

    # Info: collapsible <details> (same in both layouts)
    if info:
        sections.append(_render_info_details(info))

    body = f"{MARKER}\n{header}"
    if sections:
        body += "\n\n---\n\n" + "\n\n---\n\n".join(sections)

    # Pending footer (same in both layouts)
    if pending:
        body += _render_pending_footer(pending)

    return body


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble(
    needs_json: str = "",
    run_url: str = "",
    job_urls: dict[str, str] | None = None,
    review_statuses_json: str = "",
    lockfile_changed: bool | None = None,
    lockfile_diff: Path = Path("/dev/null"),
    timings_status: Path = Path("/dev/null"),
    pending_jobs: list[str] | None = None,
) -> str:
    """Assemble the full comment body from all available inputs."""
    items: list[ReviewItem] = []

    # 1. Structured statuses from workflow_call jobs (review-labels, etc.)
    status_items, sources = collect_from_statuses(review_statuses_json)
    items.extend(status_items)

    # 2. Synthesized error items for failed jobs not covered by statuses
    items.extend(collect_failed_jobs(needs_json, run_url, exclude_sources=sources, job_urls=job_urls))

    # 3. Lockfile diff
    items.extend(collect_lockfile(lockfile_changed, lockfile_diff))

    # 4. CI timings
    items.extend(collect_timings(timings_status))

    return render_comment(items, pending_jobs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--needs-json",
        default="",
        help="JSON string of {job_name: result} from the all-checks-pass job.",
    )
    parser.add_argument(
        "--run-url",
        default="",
        help="URL to the CI run summary page (for failed job links).",
    )
    parser.add_argument(
        "--review-statuses-json",
        default="",
        help="JSON array of status objects declared by workflow_call jobs.",
    )
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
        "--timings-status",
        type=Path,
        default=Path("/dev/null"),
        help="Path to the CI timings review-status JSON file.",
    )
    parser.add_argument(
        "--pending-jobs",
        default="",
        help="Comma-separated list of job names still running (shown in a dimmed footer).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output file for the assembled comment body.",
    )
    args = parser.parse_args()

    pending = [j.strip() for j in args.pending_jobs.split(",") if j.strip()] if args.pending_jobs else None

    body = assemble(
        needs_json=args.needs_json,
        run_url=args.run_url,
        review_statuses_json=args.review_statuses_json,
        lockfile_changed=_bool(args.lockfile_changed),
        lockfile_diff=args.lockfile_diff,
        timings_status=args.timings_status,
        pending_jobs=pending,
    )

    args.output.write_text(body)
    print(f"Wrote {len(body)} chars to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
