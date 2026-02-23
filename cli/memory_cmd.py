"""Memory and search CLI commands."""
from __future__ import annotations

import os

from core.theme import theme as _theme


def cmd_search(query: str = None, collection: str = None,
               limit: int = 10, reindex: bool = False):
    """Search documents and memory using QMD FTS5 engine."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        console = Console()
    except ImportError:
        console = None

    from core.search import QMD, Indexer

    if reindex:
        if console:
            console.print(f"[{_theme.heading}]Rebuilding search index...[/{_theme.heading}]")
        qmd = QMD()
        indexer = Indexer(qmd)
        counts = indexer.reindex_all()
        total = sum(counts.values())
        if console:
            console.print(f"[{_theme.success}]Reindexed {total} documents[/{_theme.success}]")
            for col, cnt in counts.items():
                console.print(f"  {col}: {cnt}")
        else:
            print(f"Reindexed {total} documents: {counts}")
        qmd.close()
        if not query:
            return

    if not query:
        qmd = QMD()
        stats = qmd.stats()
        qmd.close()
        if console:
            console.print(f"[{_theme.heading}]Search Index Stats[/{_theme.heading}]")
            for k, v in stats.items():
                console.print(f"  {k}: {v}")
        else:
            print(f"Search stats: {stats}")
        return

    qmd = QMD()
    results = qmd.search(query, collection=collection, limit=limit)
    qmd.close()

    if not results:
        msg = f"No results for: {query}"
        if console:
            console.print(f"[{_theme.muted}]{msg}[/{_theme.muted}]")
        else:
            print(msg)
        return

    if console:
        table = Table(title=f"Search: {query}", box=box.ROUNDED)
        table.add_column("#", style=_theme.muted, width=3)
        table.add_column("Title", style=_theme.heading)
        table.add_column("Collection")
        table.add_column("Snippet", max_width=60)
        table.add_column("Rank", justify="right")
        for i, r in enumerate(results, 1):
            table.add_row(
                str(i),
                r.get("title", "")[:50],
                r.get("collection", ""),
                r.get("snippet", "")[:60],
                f"{r.get('rank', 0):.2f}",
            )
        console.print(table)
    else:
        for i, r in enumerate(results, 1):
            print(f"{i}. [{r.get('collection','')}] {r.get('title','')}")
            print(f"   {r.get('snippet','')[:80]}")


def cmd_memory(action: str = "status", query: str = None,
               agent: str = None):
    """Memory management CLI."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        console = Console()
    except ImportError:
        console = None

    if action == "status":
        _memory_status(console, agent)
    elif action == "search":
        if not query:
            print("Usage: cleo memory search <query>")
            return
        _memory_search(console, query, agent)
    elif action == "rebuild":
        _memory_rebuild(console, agent)
    elif action == "reindex":
        cmd_search(reindex=True)
    elif action == "cleanup":
        _memory_cleanup(console, agent)


def _memory_status(console, agent: str = None):
    """Show memory statistics for all or specific agents."""
    agents_dir = "memory/agents"
    if not os.path.isdir(agents_dir):
        msg = "No agent memory directory found."
        if console:
            console.print(f"[{_theme.muted}]{msg}[/{_theme.muted}]")
        else:
            print(msg)
        return

    agent_ids = [agent] if agent else [
        d for d in os.listdir(agents_dir)
        if os.path.isdir(os.path.join(agents_dir, d)) and not d.startswith(".")
    ]

    for aid in agent_ids:
        try:
            from adapters.memory.episodic import EpisodicMemory
            ep = EpisodicMemory(aid)
            stats = ep.stats()
            storage = ep.get_storage_size()
            if console:
                console.print(f"\n[{_theme.heading}]{aid}[/{_theme.heading}]")
                console.print(f"  Episodes: {stats['episodes']}, "
                              f"Cases: {stats['cases']}, "
                              f"Patterns: {stats['patterns']}, "
                              f"Daily logs: {stats['daily_logs']}")
                console.print(f"  Storage: {storage['total_kb']} KB "
                              f"({storage['file_count']} files)")
                md_path = os.path.join("memory", "agents", aid, "MEMORY.md")
                if os.path.exists(md_path):
                    console.print(f"  MEMORY.md: [{_theme.success}]exists[/{_theme.success}]")
                else:
                    console.print(f"  MEMORY.md: [{_theme.muted}]not generated[/{_theme.muted}]")
            else:
                print(f"\n{aid}: {stats}")
        except Exception as e:
            if console:
                console.print(f"  [{_theme.error}]Error: {e}[/{_theme.error}]")
            else:
                print(f"  Error: {e}")

    try:
        from core.search import QMD
        qmd = QMD()
        stats = qmd.stats()
        qmd.close()
        if console:
            console.print(f"\n[{_theme.heading}]Search Index[/{_theme.heading}]")
            for k, v in stats.items():
                console.print(f"  {k}: {v}")
        else:
            print(f"\nSearch index: {stats}")
    except Exception:
        pass


def _memory_search(console, query: str, agent: str = None):
    """Search memory using QMD FTS5."""
    from core.search import MemorySearch
    ms = MemorySearch(agent_id=agent or "")
    results = ms.search_all(query, limit=5)
    ms.close()

    total = sum(len(v) for v in results.values())
    if not total:
        msg = f"No memory results for: {query}"
        if console:
            console.print(f"[{_theme.muted}]{msg}[/{_theme.muted}]")
        else:
            print(msg)
        return

    for col, items in results.items():
        if not items:
            continue
        if console:
            console.print(f"\n[{_theme.heading}]{col.upper()}[/{_theme.heading}] ({len(items)} results)")
            for r in items:
                console.print(f"  - {r.get('title','')[:60]}")
                snippet = r.get("snippet", "")[:80]
                if snippet:
                    console.print(f"    [{_theme.muted}]{snippet}[/{_theme.muted}]")
        else:
            print(f"\n{col.upper()} ({len(items)} results)")
            for r in items:
                print(f"  - {r.get('title','')[:60]}")


def _memory_rebuild(console, agent: str = None):
    """Rebuild MEMORY.md for all or specific agents."""
    agents_dir = "memory/agents"
    if not os.path.isdir(agents_dir):
        print("No agent memory directory found.")
        return

    agent_ids = [agent] if agent else [
        d for d in os.listdir(agents_dir)
        if os.path.isdir(os.path.join(agents_dir, d)) and not d.startswith(".")
    ]

    for aid in agent_ids:
        try:
            from adapters.memory.episodic import EpisodicMemory
            ep = EpisodicMemory(aid)
            content = ep.generate_memory_md()
            lines = len(content.split("\n"))
            if console:
                console.print(f"[{_theme.success}]{aid}[/{_theme.success}]: MEMORY.md generated ({lines} lines)")
            else:
                print(f"{aid}: MEMORY.md generated ({lines} lines)")
        except Exception as e:
            if console:
                console.print(f"[{_theme.error}]{aid}: {e}[/{_theme.error}]")
            else:
                print(f"{aid}: Error - {e}")


def _memory_cleanup(console, agent: str = None):
    """Clean up old episodes beyond TTL."""
    agents_dir = "memory/agents"
    if not os.path.isdir(agents_dir):
        print("No agent memory directory found.")
        return

    agent_ids = [agent] if agent else [
        d for d in os.listdir(agents_dir)
        if os.path.isdir(os.path.join(agents_dir, d)) and not d.startswith(".")
    ]

    for aid in agent_ids:
        try:
            from adapters.memory.episodic import EpisodicMemory
            ep = EpisodicMemory(aid)
            result = ep.cleanup()
            if console:
                console.print(f"[{_theme.success}]{aid}[/{_theme.success}]: archived {result['archived']} episodes")
            else:
                print(f"{aid}: archived {result['archived']} episodes")
        except Exception as e:
            if console:
                console.print(f"[{_theme.error}]{aid}: {e}[/{_theme.error}]")
            else:
                print(f"{aid}: Error - {e}")
