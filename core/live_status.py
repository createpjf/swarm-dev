"""
core/live_status.py
Claude Code-style live status display for agent orchestration.
Shows per-task status rows, elapsed time, and summary.
Supports i18n via core.i18n module.
"""

from __future__ import annotations
import time
from typing import Optional

from core.i18n import t

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

try:
    from core.theme import theme as _theme
except ImportError:
    class _FallbackTheme:
        success = "green"; error = "red"; warning = "yellow"
        muted = "dim"; heading = "bold"; info = "cyan"
        accent = "bold magenta"; accent_light = "magenta"
    _theme = _FallbackTheme()


# ── Status icons ─────────────────────────────────────────────────────────────

ICON_WORKING   = f"[bold {_theme.info}]●[/bold {_theme.info}]"
ICON_DONE      = f"[bold {_theme.success}]✓[/bold {_theme.success}]"
ICON_IDLE      = f"[{_theme.muted}]○[/{_theme.muted}]"
ICON_FAIL      = f"[bold {_theme.error}]✗[/bold {_theme.error}]"
ICON_REVIEW    = f"[{_theme.accent}]◆[/{_theme.accent}]"
ICON_CANCELLED = f"[{_theme.muted} {_theme.warning}]⊘[/{_theme.muted} {_theme.warning}]"
ICON_PAUSED    = f"[bold {_theme.warning}]⏸[/bold {_theme.warning}]"


# ── Task row ─────────────────────────────────────────────────────────────────

class TaskRow:
    """One row in the status display, representing a single task."""
    __slots__ = ("task_id", "agent_id", "status", "description",
                 "elapsed", "error_msg", "review_score", "review_verdict",
                 "partial_preview")

    def __init__(self, task_id: str):
        self.task_id     = task_id
        self.agent_id    = ""
        self.status      = "pending"   # pending/working/review/done/failed
        self.description = ""
        self.elapsed:      Optional[float] = None
        self.error_msg   = ""
        self.review_score: Optional[int] = None
        self.review_verdict: str = ""       # V0.02: LGTM / NEEDS_WORK
        self.partial_preview: str = ""


# ── Live Status Display ──────────────────────────────────────────────────────

class LiveStatus:
    """
    Polls .task_board.json and renders a Claude Code-style live panel.

    Shows one row per task (not per agent), so you can see:
      ✓ planner   decomposing tasks                      6.8s
      ● executor  implement a modern responsive HTML…   12s
      ○ reviewer  waiting…                              —
    """

    def __init__(self, console: Console, agents_config: list[dict]):
        self.console    = console
        self.start_time = time.time()
        self.agent_ids  = [a["id"] for a in agents_config]
        self.rows: dict[str, TaskRow] = {}   # task_id → TaskRow
        self._live: Optional[Live] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        self._live = Live(
            self._build_display(),
            console=self.console,
            refresh_per_second=2,
            transient=True,
        )
        self._live.start()

    def stop(self):
        if self._live:
            self._live.stop()
            self._live = None
        # Print final (permanent) display
        self.console.print(self._build_display(final=True))

    # ── Poll task board ───────────────────────────────────────────────────

    def poll(self, board):
        """Read task board and update task rows."""
        now = time.time()

        try:
            data = board._read()
        except Exception:
            return

        for tid, t in data.items():
            # Get or create row
            if tid not in self.rows:
                self.rows[tid] = TaskRow(tid)
            row = self.rows[tid]

            agent_id = t.get("agent_id") or ""
            status   = t.get("status", "pending")
            row.agent_id    = agent_id
            row.description = _truncate(t.get("description", ""), 42)

            if status == "claimed":
                row.status  = "working"
                claimed_at  = t.get("claimed_at")
                row.elapsed = (now - claimed_at) if claimed_at else None
                # Show streaming partial result preview
                partial = t.get("partial_result", "")
                if partial:
                    row.partial_preview = _clean_preview(partial, 50)

            elif status == "review":
                row.status  = "review"
                claimed_at  = t.get("claimed_at")
                row.elapsed = (now - claimed_at) if claimed_at else None

            elif status == "completed":
                row.status = "done"
                started = t.get("claimed_at")
                ended   = t.get("completed_at")
                if started and ended:
                    row.elapsed = ended - started
                # Review score — V0.02 CritiqueSpec > critique > review_scores
                critique_spec_raw = t.get("critique_spec")
                critique = t.get("critique")
                if critique_spec_raw:
                    try:
                        import json as _json
                        cs = _json.loads(critique_spec_raw) if isinstance(
                            critique_spec_raw, str) else critique_spec_raw
                        row.review_score = int(cs.get("composite_score", 0))
                        row.review_verdict = cs.get("verdict", "")
                    except (ValueError, TypeError):
                        pass
                elif critique:
                    row.review_score = critique.get("score")
                    row.review_verdict = "LGTM" if critique.get("passed") else "NEEDS_WORK"
                else:
                    scores = t.get("review_scores", [])
                    if scores:
                        avg = sum(r["score"] for r in scores) / len(scores)
                        row.review_score = int(avg)

            elif status == "failed":
                row.status = "failed"
                started    = t.get("claimed_at")
                row.elapsed = (now - started) if started else None
                # Extract error reason (i18n-aware)
                flags = t.get("evolution_flags", [])
                for f in flags:
                    if f.startswith("failed:"):
                        err = f[7:]
                        # Simplify common errors using i18n
                        from core.i18n import t as _t
                        if "401" in err:
                            row.error_msg = _t("error.api_key")
                        elif "403" in err:
                            row.error_msg = _t("error.forbidden")
                        elif "429" in err:
                            row.error_msg = _t("error.rate_limit")
                        elif "timeout" in err.lower() or "timed out" in err.lower():
                            row.error_msg = _t("error.timeout")
                        elif "connect" in err.lower():
                            row.error_msg = _t("error.connect")
                        else:
                            row.error_msg = _truncate(err, 40)
                        break

            elif status == "cancelled":
                row.status = "cancelled"

            elif status == "paused":
                row.status = "paused"

            elif status == "pending":
                row.status = "pending"

        # Update live display
        if self._live:
            self._live.update(self._build_display())

    # ── Build display ─────────────────────────────────────────────────────

    def _build_display(self, final: bool = False) -> Table:
        """Build the status table showing one row per task."""
        now = time.time()
        total_elapsed = now - self.start_time

        table = Table(
            show_header=False,
            show_edge=False,
            box=None,
            padding=(0, 1),
            expand=False,
        )
        table.add_column("icon", width=2, no_wrap=True)
        table.add_column("agent", width=10, style="bold")
        table.add_column("desc", min_width=30, max_width=50)
        table.add_column("time", width=8, justify="right", style="dim")

        # Sort: working first, then done, then failed, then pending
        order = {"working": 0, "review": 1, "done": 2, "failed": 3,
                 "cancelled": 4, "paused": 5, "pending": 6}
        sorted_rows = sorted(self.rows.values(),
                             key=lambda r: (order.get(r.status, 9),
                                            r.elapsed or 0))

        done_count = 0
        fail_count = 0
        working_count = 0
        cancelled_count = 0

        for row in sorted_rows:
            icon = _icon_for(row.status)

            if row.status == "working":
                if row.partial_preview:
                    desc_text = f"[{_theme.info}]{row.partial_preview}[/{_theme.info}]"
                else:
                    desc_text = f"[{_theme.info}]{row.description}[/{_theme.info}]"
                working_count += 1
            elif row.status == "review":
                desc_text = f"[{_theme.accent_light}]{t('status.review')}[/{_theme.accent_light}]"
                working_count += 1
            elif row.status == "done":
                if row.review_score is not None:
                    verdict_tag = f" [{row.review_verdict}]" if row.review_verdict else ""
                    desc_text = f"[{_theme.success}]{row.description} ({row.review_score}/10{verdict_tag})[/{_theme.success}]"
                else:
                    desc_text = f"[{_theme.success}]{row.description}[/{_theme.success}]"
                done_count += 1
            elif row.status == "failed":
                reason = row.error_msg or t("status.failed")
                desc_text = f"[{_theme.error}]{reason}[/{_theme.error}]"
                fail_count += 1
            elif row.status == "cancelled":
                desc_text = f"[dim yellow]{t('status.cancelled')}[/dim yellow]"
                cancelled_count += 1
            elif row.status == "paused":
                desc_text = f"[{_theme.warning}]{t('status.paused')} — {row.description}[/{_theme.warning}]"
            else:  # pending
                desc_text = f"[{_theme.muted}]{t('status.pending')}[/{_theme.muted}]"

            elapsed_str = _fmt_time(row.elapsed) if row.elapsed else "—"
            table.add_row(icon, row.agent_id, desc_text, elapsed_str)

        # If no rows yet, show agents waiting
        if not self.rows:
            for aid in self.agent_ids:
                table.add_row(ICON_IDLE, aid, f"[{_theme.muted}]{t('status.pending')}[/{_theme.muted}]", "—")

        # Summary row
        table.add_row()
        total_tasks = len(self.rows)

        if final:
            parts = [f"{done_count} {t('summary.done').lower()}"]
            if fail_count:
                parts.append(f"[{_theme.error}]{fail_count} {t('summary.failed')}[/{_theme.error}]")
            if cancelled_count:
                parts.append(f"[{_theme.warning}]{cancelled_count} {t('summary.cancelled')}[/{_theme.warning}]")
            parts.append(_fmt_time(total_elapsed))
            if fail_count or cancelled_count:
                summary = f"[{_theme.warning}]{t('summary.finished')}[/{_theme.warning}] · {' · '.join(parts)}"
            else:
                summary = f"[{_theme.success}]{t('summary.done')}[/{_theme.success}] · {' · '.join(parts)}"
        else:
            parts = []
            if done_count:
                parts.append(f"{done_count} {t('summary.done').lower()}")
            if working_count:
                parts.append(f"{working_count} {t('summary.working')}")
            if fail_count:
                parts.append(f"[{_theme.error}]{fail_count} {t('summary.failed')}[/{_theme.error}]")
            if cancelled_count:
                parts.append(f"[{_theme.warning}]{cancelled_count} {t('summary.cancelled')}[/{_theme.warning}]")
            parts.append(f"{_fmt_time(total_elapsed)} {t('summary.elapsed')}")
            summary = f"[{_theme.muted}]{' · '.join(parts)}[/{_theme.muted}]"

        table.add_row("", "", summary, "")

        return table


# ── Helpers ──────────────────────────────────────────────────────────────────

def _icon_for(status: str) -> str:
    return {
        "working":   ICON_WORKING,
        "done":      ICON_DONE,
        "pending":   ICON_IDLE,
        "failed":    ICON_FAIL,
        "review":    ICON_REVIEW,
        "cancelled": ICON_CANCELLED,
        "paused":    ICON_PAUSED,
    }.get(status, ICON_IDLE)


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _truncate(text: str, maxlen: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) > maxlen:
        return text[:maxlen - 1] + "…"
    return text


import re as _re

_THINK_RE = _re.compile(r"<think>.*?</think>", _re.DOTALL)
_TOOL_CODE_RE = _re.compile(r"<tool_code>.*?</tool_code>", _re.DOTALL)


def _clean_preview(text: str, maxlen: int = 50) -> str:
    """Clean LLM output for preview: strip think/tool tags, normalize whitespace."""
    text = _THINK_RE.sub("", text)
    text = _TOOL_CODE_RE.sub("", text)
    text = text.replace("\n", " ").strip()
    # Collapse multiple spaces
    text = _re.sub(r"\s+", " ", text)
    if not text:
        return "thinking…"
    if len(text) > maxlen:
        return text[:maxlen - 1] + "…"
    return text


def strip_think_tags(text: str) -> str:
    """Strip <think>...</think> and <tool_code>...</tool_code> blocks from final output.

    Used to clean LLM output before displaying to user.
    """
    text = _THINK_RE.sub("", text)
    text = _TOOL_CODE_RE.sub("", text)
    # Clean up excessive blank lines left after stripping
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
