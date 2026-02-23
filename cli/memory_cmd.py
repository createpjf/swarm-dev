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
               agent: str = None, output: str = None,
               fmt: str = "json"):
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
    elif action == "graph":
        _memory_graph(console, agent, fmt)
    elif action == "package":
        _memory_package(console, agent, output)


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


def _memory_graph(console, agent: str = None, fmt: str = "json"):
    """Generate knowledge graph from agent memory."""
    import json as _json

    agent_id = agent or "alic"
    try:
        from adapters.memory.knowledge_graph import KnowledgeGraph
    except ImportError as e:
        msg = f"Knowledge graph unavailable: {e}"
        if console:
            console.print(f"[{_theme.error}]{msg}[/{_theme.error}]")
        else:
            print(msg)
        return

    kg = KnowledgeGraph(agent_id=agent_id)
    result = kg.build()

    if not result.get("ok"):
        msg = f"Graph build failed: {result.get('error', 'unknown')}"
        if console:
            console.print(f"[{_theme.error}]{msg}[/{_theme.error}]")
        else:
            print(msg)
        return

    # Save graph
    agent_dir = os.path.join("memory", "agents", agent_id)
    os.makedirs(agent_dir, exist_ok=True)

    if fmt == "dot":
        out_path = os.path.join(agent_dir, "knowledge_graph.dot")
        with open(out_path, "w") as f:
            f.write(kg.export_dot())
    else:
        out_path = os.path.join(agent_dir, "knowledge_graph.json")
        with open(out_path, "w") as f:
            _json.dump(result, f, ensure_ascii=False, indent=2)

    meta = result.get("meta", {})
    stats = kg.stats()

    if console:
        console.print(f"\n[{_theme.heading}]Knowledge Graph — {agent_id}[/{_theme.heading}]")
        console.print(f"  Nodes: {meta.get('node_count', 0)}")
        console.print(f"  Edges: {meta.get('edge_count', 0)}")
        console.print(f"  Episodes: {meta.get('episode_count', 0)}")
        console.print(f"  Cases: {meta.get('case_count', 0)}")
        if stats.get("overall_avg_score"):
            console.print(f"  Avg Score: {stats['overall_avg_score']}")
        dist = stats.get("score_distribution", {})
        if any(dist.values()):
            console.print(f"  Score Distribution:")
            for label, count in dist.items():
                if count:
                    console.print(f"    {label}: {count}")
        per_agent = stats.get("per_agent", {})
        if per_agent:
            console.print(f"\n  [{_theme.heading}]Per-Agent Quality[/{_theme.heading}]")
            for aid, info in per_agent.items():
                console.print(f"    {aid}: avg={info['avg_score']}, "
                              f"n={info['count']}, "
                              f"range={info['min']}-{info['max']}")
        console.print(f"\n  [{_theme.success}]Saved:[/{_theme.success}] {out_path}")
    else:
        print(f"Graph: {meta.get('node_count', 0)} nodes, "
              f"{meta.get('edge_count', 0)} edges → {out_path}")


def _memory_package(console, agent: str = None, output: str = None):
    """Package agent memory into a ZIP archive."""
    import json as _json
    import zipfile
    from datetime import datetime

    agent_id = agent or "alic"
    agent_dir = os.path.join("memory", "agents", agent_id)

    if not os.path.isdir(agent_dir):
        msg = f"Agent memory not found: {agent_dir}"
        if console:
            console.print(f"[{_theme.error}]{msg}[/{_theme.error}]")
        else:
            print(msg)
        return

    # Generate latest knowledge graph first
    graph_stats = {}
    try:
        from adapters.memory.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph(agent_id=agent_id)
        graph_data = kg.build()
        if graph_data.get("ok"):
            graph_path = os.path.join(agent_dir, "knowledge_graph.json")
            with open(graph_path, "w") as f:
                _json.dump(graph_data, f, ensure_ascii=False, indent=2)
            graph_stats = kg.stats()
    except Exception as e:
        if console:
            console.print(f"  [{_theme.muted}]Graph generation skipped: {e}[/{_theme.muted}]")

    # Build manifest
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    manifest = {
        "agent_id": agent_id,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "stats": graph_stats,
        "files": [],
    }

    # Create ZIP
    zip_name = output or f"{agent_id}_memory_{timestamp}.zip"
    file_count = 0
    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(agent_dir):
            # Skip chroma/ (vector index — large, can be rebuilt)
            if "chroma" in root:
                continue
            for fname in files:
                if fname.startswith("."):
                    continue
                filepath = os.path.join(root, fname)
                arcname = os.path.relpath(filepath, agent_dir)
                zf.write(filepath, arcname)
                manifest["files"].append(arcname)
                file_count += 1

        # Write manifest into ZIP
        zf.writestr("manifest.json",
                     _json.dumps(manifest, ensure_ascii=False, indent=2))

    size_kb = os.path.getsize(zip_name) / 1024

    if console:
        console.print(f"\n[{_theme.success}]✓[/{_theme.success}] Packaged [{_theme.heading}]{agent_id}[/{_theme.heading}] memory")
        console.print(f"  Output: {zip_name}")
        console.print(f"  Files: {file_count}")
        console.print(f"  Size: {size_kb:.1f} KB")
        if graph_stats:
            console.print(f"  Episodes: {graph_stats.get('total_episodes', 0)}")
            console.print(f"  Cases: {graph_stats.get('total_cases', 0)}")
    else:
        print(f"Packaged to {zip_name} ({file_count} files, {size_kb:.1f} KB)")
