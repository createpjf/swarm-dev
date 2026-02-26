"""
cli/memo_cmd.py — ``cleo memo`` command group.

Actions:
    status    — show Memo integration status + tracking stats
    export    — batch export memories to Memo format (JSON)
    search    — search the Memo platform
    skills    — sync purchased skills from Memo
    tracking  — show export tracking records
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── theme (reuse memory_cmd's pattern) ────────────────────────────────────

try:
    from cli.memory_cmd import _theme
except ImportError:
    class _theme:  # type: ignore
        heading = "bold cyan"
        success = "bold green"
        warning = "bold yellow"
        error = "bold red"
        muted = "dim"


def _get_console():
    try:
        from rich.console import Console
        return Console()
    except ImportError:
        return None


def _load_config() -> dict:
    """Load agents.yaml config."""
    for path in ("config/agents.yaml", "agents.yaml"):
        if os.path.exists(path):
            try:
                import yaml
                with open(path) as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
    return {}


def _get_memo_config():
    """Get MemoConfig from agents.yaml."""
    from adapters.memo.config import MemoConfig
    return MemoConfig.from_yaml(_load_config())


# ══════════════════════════════════════════════════════════════════════════════
#  Main dispatcher
# ══════════════════════════════════════════════════════════════════════════════

def cmd_memo(
    action: str = "status",
    query: Optional[str] = None,
    agent: Optional[str] = None,
    memo_type: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    min_quality: float = 0.6,
    min_score: int = 7,
    output: Optional[str] = None,
    upload: bool = False,
    dry_run: bool = False,
):
    """Dispatch ``cleo memo <action>``."""
    console = _get_console()

    if action == "status":
        _memo_status(console)
    elif action == "export":
        _memo_export(console, agent=agent, memo_type=memo_type,
                     since=since, until=until,
                     min_quality=min_quality, min_score=min_score,
                     output=output, upload=upload, dry_run=dry_run)
    elif action == "search":
        _memo_search(console, query=query, memo_type=memo_type,
                     min_quality=min_quality)
    elif action == "skills":
        _memo_skills(console)
    elif action == "tracking":
        _memo_tracking(console)
    else:
        if console:
            console.print(f"[{_theme.error}]Unknown action: {action}[/{_theme.error}]")
        else:
            print(f"Unknown action: {action}")


# ══════════════════════════════════════════════════════════════════════════════
#  Actions
# ══════════════════════════════════════════════════════════════════════════════

def _memo_status(console):
    """Show Memo integration status."""
    config = _get_memo_config()

    if console:
        console.print(f"\n[{_theme.heading}]🧠 Memo Protocol Integration[/{_theme.heading}]\n")
        console.print(f"  Enabled:       {'✓ Yes' if config.enabled else '✗ No'}")
        console.print(f"  API Base:      {config.api_base_url}")
        console.print(f"  Agent ID:      {config.erc8004_agent_id or '(not set)'}")
        console.print(f"  Wallet:        {config.wallet_address[:10] + '...' if config.wallet_address else '(not set)'}")
        console.print(f"  Auto Upload:   {'✓' if config.auto_upload_enabled else '✗'}")
        console.print(f"  Skill Sync:    {'✓' if config.skill_sync_enabled else '✗'}")
        console.print(f"  LLM Deident:   {'✓' if config.deidentification_use_llm else '✗'}")
        console.print(f"  Domain:        {config.default_domain}")
        console.print(f"  Language:      {config.default_language}")

        # Tracking stats
        try:
            from adapters.memo.tracking import ExportTracker
            tracker = ExportTracker()
            stats = tracker.stats()
            console.print(f"\n  [{_theme.heading}]Export Tracking[/{_theme.heading}]")
            console.print(f"  Total Exported: {stats['total']}")
            if stats["by_type"]:
                for t, c in stats["by_type"].items():
                    console.print(f"    {t}: {c}")
        except Exception:
            pass

        # Local skills
        try:
            from adapters.memo.importer import MemoImporter
            skills_dir = os.path.join("skills", "memo")
            if os.path.isdir(skills_dir):
                count = len([f for f in os.listdir(skills_dir)
                             if f.endswith(".md")])
                console.print(f"\n  [{_theme.heading}]Local Memo Skills[/{_theme.heading}]")
                console.print(f"  Files: {count}")
        except Exception:
            pass
    else:
        print(f"Memo enabled: {config.enabled}")
        print(f"API: {config.api_base_url}")


def _memo_export(console, *, agent, memo_type, since, until,
                 min_quality, min_score, output, upload, dry_run):
    """Export memories to Memo format."""
    config = _get_memo_config()

    from adapters.memo.exporter import MemoExporter, ExportFilter

    filt = ExportFilter(
        agents=[agent] if agent else [],
        types=[memo_type] if memo_type else [],
        date_from=since or "",
        date_to=until or "",
        min_score=min_score,
        min_quality=min_quality,
    )

    output_dir = output or "memo_export"
    mode = "DRY RUN" if dry_run else ("EXPORT + UPLOAD" if upload else "EXPORT")

    if console:
        console.print(f"\n[{_theme.heading}]🧠 Memo Export ({mode})[/{_theme.heading}]\n")
        if agent:
            console.print(f"  Agent: {agent}")
        if memo_type:
            console.print(f"  Type: {memo_type}")
        if since or until:
            console.print(f"  Date range: {since or '...'} → {until or '...'}")
        console.print(f"  Min quality: {min_quality}")
        console.print(f"  Min score: {min_score}")
        console.print(f"  Output: {output_dir}")
        console.print()

    exporter = MemoExporter(config)

    try:
        result = asyncio.run(
            exporter.export_batch(filt, output_dir=output_dir,
                                  upload=upload, dry_run=dry_run))
    except Exception as e:
        if console:
            console.print(f"[{_theme.error}]Export failed: {e}[/{_theme.error}]")
        else:
            print(f"Export failed: {e}")
        return

    if console:
        console.print(f"[{_theme.success}]✓[/{_theme.success}] Export complete\n")
        console.print(f"  Scanned:          {result.total_scanned}")
        console.print(f"  Eligible:         {result.total_eligible}")
        console.print(f"  Exported:         {result.total_exported}")
        console.print(f"  Skipped (quality): {result.skipped_quality}")
        console.print(f"  Skipped (dup):     {result.skipped_duplicate}")
        if result.skipped_error:
            console.print(f"  Skipped (error):   {result.skipped_error}")
        if result.by_type:
            console.print(f"\n  [{_theme.heading}]By Type[/{_theme.heading}]")
            for t, c in result.by_type.items():
                console.print(f"    {t}: {c}")
        console.print(f"\n  Duration: {result.duration_seconds}s")
        if not dry_run:
            console.print(f"  Output: {result.output_path}/")
        if result.errors:
            console.print(f"\n  [{_theme.warning}]Errors ({len(result.errors)}):[/{_theme.warning}]")
            for err in result.errors[:5]:
                console.print(f"    {err}")
    else:
        print(f"Exported {result.total_exported}/{result.total_scanned} "
              f"({result.skipped_quality} skipped quality, "
              f"{result.skipped_duplicate} duplicates)")


def _memo_search(console, *, query, memo_type, min_quality):
    """Search the Memo platform."""
    if not query:
        if console:
            console.print(f"[{_theme.error}]Usage: cleo memo search <query>[/{_theme.error}]")
        return

    config = _get_memo_config()
    if not config.enabled:
        if console:
            console.print(f"[{_theme.warning}]Memo integration is disabled. "
                          f"Enable it in config/agents.yaml[/{_theme.warning}]")
        return

    from adapters.memo.client import MemoClient
    client = MemoClient(config)

    try:
        results = asyncio.run(
            client.search_memories(query, type=memo_type or "",
                                   min_quality=min_quality))
    except Exception as e:
        if console:
            console.print(f"[{_theme.error}]Search failed: {e}[/{_theme.error}]")
        return

    if console:
        console.print(f"\n[{_theme.heading}]🔍 Memo Search: \"{query}\"[/{_theme.heading}]\n")
        if not results:
            console.print(f"  [{_theme.muted}]No results found[/{_theme.muted}]")
        for i, r in enumerate(results[:10], 1):
            title = r.get("title", "Untitled")
            rtype = r.get("type", "?")
            score = r.get("quality_score", 0)
            mid = r.get("id", "?")
            console.print(f"  {i}. [{_theme.heading}]{title}[/{_theme.heading}]")
            console.print(f"     Type: {rtype}  Quality: {score:.2f}  ID: {mid}")
            summary = r.get("summary", "")
            if summary:
                console.print(f"     {summary[:120]}")
            console.print()


def _memo_skills(console):
    """Sync purchased skills from Memo."""
    config = _get_memo_config()
    if not config.enabled:
        if console:
            console.print(f"[{_theme.warning}]Memo integration is disabled[/{_theme.warning}]")
        return

    from adapters.memo.client import MemoClient
    from adapters.memo.importer import MemoImporter

    client = MemoClient(config)
    importer = MemoImporter(config, client)

    if console:
        console.print(f"\n[{_theme.heading}]🧠 Memo Skill Sync[/{_theme.heading}]\n")

    try:
        stats = asyncio.run(importer.sync_skills())
    except Exception as e:
        if console:
            console.print(f"[{_theme.error}]Sync failed: {e}[/{_theme.error}]")
        return

    if console:
        console.print(f"  Fetched:  {stats['fetched']}")
        console.print(f"  Written:  {stats['written']}")
        console.print(f"  Updated:  {stats['updated']}")
        if stats["errors"]:
            console.print(f"  Errors:   {stats['errors']}")

    # List local skills
    local = importer.list_local_skills()
    if console and local:
        console.print(f"\n  [{_theme.heading}]Local Memo Skills ({len(local)})[/{_theme.heading}]")
        for s in local[:10]:
            console.print(f"    • {s.get('name', s.get('filename', '?'))}")


def _memo_tracking(console):
    """Show export tracking records."""
    from adapters.memo.tracking import ExportTracker

    tracker = ExportTracker()
    stats = tracker.stats()

    if console:
        console.print(f"\n[{_theme.heading}]📋 Memo Export Tracking[/{_theme.heading}]\n")
        console.print(f"  Total Exports: {stats['total']}")
        if stats["by_type"]:
            console.print(f"\n  [{_theme.heading}]By Source Type[/{_theme.heading}]")
            for t, c in stats["by_type"].items():
                console.print(f"    {t}: {c}")

        if stats.get("created_at"):
            from datetime import datetime
            created = datetime.fromtimestamp(stats["created_at"])
            console.print(f"\n  Created: {created.strftime('%Y-%m-%d %H:%M')}")
        if stats.get("updated_at"):
            from datetime import datetime
            updated = datetime.fromtimestamp(stats["updated_at"])
            console.print(f"  Updated: {updated.strftime('%Y-%m-%d %H:%M')}")

        console.print(f"\n  Tracking file: {tracker.path}")
    else:
        print(f"Total exports: {stats['total']}")
        for t, c in stats.get("by_type", {}).items():
            print(f"  {t}: {c}")
