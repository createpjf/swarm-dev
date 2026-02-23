"""
adapters/memory/knowledge_graph.py
Build a knowledge graph from Alic's episodic memory using networkx.

Node types:
  - agent:   each agent (leo, jerry, alic)
  - task:    evaluated tasks (from critique episodes)
  - model:   AI models used
  - tag:     task tags / categories

Edge types:
  - EVALUATED:    alic → task   (review relationship)
  - EXECUTED:     agent → task  (execution relationship)
  - USED_MODEL:   agent → model (model usage)
  - SCORED:       task  ← score weight on EVALUATED edge
  - TAGGED:       task → tag
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import networkx as nx
    HAS_NX = True
except ImportError:
    HAS_NX = False


class KnowledgeGraph:
    """Generate a knowledge graph from an agent's episodic memory."""

    def __init__(self, agent_id: str = "alic",
                 base_dir: str = "memory/agents"):
        self.agent_id = agent_id
        self.base = os.path.join(base_dir, agent_id)
        self.G: Optional["nx.DiGraph"] = None
        self._episodes: list[dict] = []
        self._cases: list[dict] = []

    # ── Build ─────────────────────────────────────────────────────────────

    def build(self) -> dict:
        """Load all data and construct the full knowledge graph."""
        if not HAS_NX:
            return {"ok": False, "error": "networkx not installed"}

        self.G = nx.DiGraph()
        self._load_episodes()
        self._load_cases()
        self._infer_patterns()
        return self.export()

    def _load_episodes(self):
        """Load all episodes and create nodes + edges."""
        episodes_dir = os.path.join(self.base, "episodes")
        if not os.path.isdir(episodes_dir):
            return

        for date_dir in sorted(os.listdir(episodes_dir)):
            day_path = os.path.join(episodes_dir, date_dir)
            if not os.path.isdir(day_path):
                continue
            for fname in os.listdir(day_path):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(day_path, fname)) as f:
                        ep = json.load(f)
                    self._episodes.append(ep)
                    self._add_episode_to_graph(ep)
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Skip bad episode %s: %s", fname, e)

    def _add_episode_to_graph(self, ep: dict):
        """Add a single episode's nodes and edges to the graph."""
        task_id = ep.get("task_id", "unknown")
        agent_id = ep.get("agent_id", "unknown")
        score = ep.get("score")
        model = ep.get("model")
        tags = ep.get("tags", [])
        ctx = ep.get("context", {})

        # Task node
        self.G.add_node(task_id, type="task",
                        label=ep.get("title", task_id)[:80],
                        score=score,
                        date=ep.get("date"),
                        outcome=ep.get("outcome"))

        # Reviewer agent node + EVALUATED edge
        self.G.add_node(agent_id, type="agent", label=agent_id)
        self.G.add_edge(agent_id, task_id, relation="EVALUATED",
                        score=score,
                        ts=ep.get("ts"))

        # Evaluated agent node + EXECUTED edge
        evaluated_agent = ctx.get("evaluated_agent")
        if evaluated_agent:
            self.G.add_node(evaluated_agent, type="agent",
                            label=evaluated_agent)
            self.G.add_edge(evaluated_agent, task_id,
                            relation="EXECUTED")

        # Model nodes + USED_MODEL edges
        reviewer_model = ctx.get("reviewer_model") or model
        evaluated_model = ctx.get("evaluated_model")

        if reviewer_model:
            mid = f"model:{reviewer_model}"
            self.G.add_node(mid, type="model", label=reviewer_model)
            self.G.add_edge(agent_id, mid, relation="USED_MODEL")

        if evaluated_model and evaluated_agent:
            mid = f"model:{evaluated_model}"
            self.G.add_node(mid, type="model", label=evaluated_model)
            self.G.add_edge(evaluated_agent, mid, relation="USED_MODEL")

        # Tag nodes + TAGGED edges
        for tag in tags:
            if tag in ("critique",):
                continue  # skip generic meta-tags
            tid = f"tag:{tag}"
            self.G.add_node(tid, type="tag", label=tag)
            self.G.add_edge(task_id, tid, relation="TAGGED")

    def _load_cases(self):
        """Load cases and add as nodes."""
        cases_dir = os.path.join(self.base, "cases")
        if not os.path.isdir(cases_dir):
            return

        for fname in os.listdir(cases_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(cases_dir, fname)) as f:
                    case = json.load(f)
                self._cases.append(case)
                self._add_case_to_graph(case)
            except (json.JSONDecodeError, OSError):
                continue

    def _add_case_to_graph(self, case: dict):
        """Add a case node and link to its source."""
        cid = f"case:{case.get('id', 'unknown')}"
        self.G.add_node(cid, type="case",
                        label=case.get("problem", "")[:80],
                        use_count=case.get("use_count", 0))

        agent_id = case.get("agent_id", self.agent_id)
        self.G.add_node(agent_id, type="agent", label=agent_id)
        self.G.add_edge(agent_id, cid, relation="LEARNED")

        # Link to source task if available
        source = case.get("source_task_id")
        if source and self.G.has_node(source):
            self.G.add_edge(cid, source, relation="DERIVED_FROM")

        for tag in case.get("tags", []):
            tid = f"tag:{tag}"
            self.G.add_node(tid, type="tag", label=tag)
            self.G.add_edge(cid, tid, relation="TAGGED")

    def _infer_patterns(self):
        """Generate aggregate pattern nodes from episode data."""
        if not self._episodes:
            return

        # Aggregate scores by evaluated agent
        agent_scores: dict[str, list[int]] = defaultdict(list)
        model_scores: dict[str, list[int]] = defaultdict(list)
        tag_scores: dict[str, list[int]] = defaultdict(list)

        for ep in self._episodes:
            score = ep.get("score")
            if score is None:
                continue
            ctx = ep.get("context", {})

            ea = ctx.get("evaluated_agent")
            if ea:
                agent_scores[ea].append(score)

            em = ctx.get("evaluated_model")
            if em:
                model_scores[em].append(score)

            for tag in ep.get("tags", []):
                if tag != "critique":
                    tag_scores[tag].append(score)

        # Create pattern nodes for agents with enough data
        for agent, scores in agent_scores.items():
            if len(scores) >= 2:
                avg = sum(scores) / len(scores)
                pid = f"pattern:agent_quality:{agent}"
                self.G.add_node(pid, type="pattern",
                                label=f"{agent} avg={avg:.1f} "
                                      f"(n={len(scores)})",
                                avg_score=round(avg, 2),
                                count=len(scores),
                                min_score=min(scores),
                                max_score=max(scores))
                self.G.add_edge(agent, pid, relation="HAS_PATTERN")

        # Pattern nodes for models
        for model, scores in model_scores.items():
            if len(scores) >= 2:
                avg = sum(scores) / len(scores)
                pid = f"pattern:model_quality:{model}"
                self.G.add_node(pid, type="pattern",
                                label=f"{model} avg={avg:.1f} "
                                      f"(n={len(scores)})",
                                avg_score=round(avg, 2),
                                count=len(scores))
                mid = f"model:{model}"
                if self.G.has_node(mid):
                    self.G.add_edge(mid, pid, relation="HAS_PATTERN")

    # ── Export ────────────────────────────────────────────────────────────

    def export(self, fmt: str = "json") -> dict:
        """Export the graph as a JSON-serializable dict."""
        if not self.G:
            return {"ok": False, "error": "Graph not built. Call build()."}

        nodes = []
        for nid, attrs in self.G.nodes(data=True):
            nodes.append({"id": nid, **attrs})

        edges = []
        for src, tgt, attrs in self.G.edges(data=True):
            edges.append({"source": src, "target": tgt, **attrs})

        return {
            "ok": True,
            "meta": {
                "agent_id": self.agent_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "node_count": len(nodes),
                "edge_count": len(edges),
                "episode_count": len(self._episodes),
                "case_count": len(self._cases),
            },
            "nodes": nodes,
            "edges": edges,
        }

    def export_dot(self) -> str:
        """Export graph as Graphviz DOT string."""
        if not self.G:
            return ""

        lines = ["digraph KnowledgeGraph {",
                 '  rankdir=LR;',
                 '  node [fontname="Arial"];']

        # Style by type
        type_styles = {
            "agent":   'shape=box, style=filled, fillcolor="#4A90D9",'
                       ' fontcolor=white',
            "task":    'shape=ellipse, style=filled, fillcolor="#F5A623"',
            "model":   'shape=diamond, style=filled, fillcolor="#7ED321"',
            "tag":     'shape=note, style=filled, fillcolor="#D0D0D0"',
            "case":    'shape=folder, style=filled, fillcolor="#BD10E0",'
                       ' fontcolor=white',
            "pattern": 'shape=octagon, style=filled, fillcolor="#FF6B6B",'
                       ' fontcolor=white',
        }

        for nid, attrs in self.G.nodes(data=True):
            ntype = attrs.get("type", "task")
            label = attrs.get("label", nid)[:40]
            style = type_styles.get(ntype, "")
            safe_id = nid.replace('"', '\\"')
            safe_label = label.replace('"', '\\"')
            lines.append(f'  "{safe_id}" [label="{safe_label}", {style}];')

        for src, tgt, attrs in self.G.edges(data=True):
            rel = attrs.get("relation", "")
            score = attrs.get("score")
            label = rel
            if score is not None:
                label = f"{rel} ({score}/10)"
            safe_src = src.replace('"', '\\"')
            safe_tgt = tgt.replace('"', '\\"')
            safe_lbl = label.replace('"', '\\"')
            lines.append(f'  "{safe_src}" -> "{safe_tgt}" '
                         f'[label="{safe_lbl}"];')

        lines.append("}")
        return "\n".join(lines)

    def stats(self) -> dict:
        """Return aggregate statistics from the graph."""
        if not self.G:
            return {}

        type_counts: dict[str, int] = defaultdict(int)
        for _, attrs in self.G.nodes(data=True):
            type_counts[attrs.get("type", "unknown")] += 1

        # Score distribution
        scores = [ep.get("score") for ep in self._episodes
                  if ep.get("score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0

        # Per-agent scores
        agent_stats: dict[str, dict] = {}
        for ep in self._episodes:
            ctx = ep.get("context", {})
            ea = ctx.get("evaluated_agent")
            s = ep.get("score")
            if ea and s is not None:
                if ea not in agent_stats:
                    agent_stats[ea] = {"scores": [], "models": set()}
                agent_stats[ea]["scores"].append(s)
                em = ctx.get("evaluated_model")
                if em:
                    agent_stats[ea]["models"].add(em)

        agent_summary = {}
        for aid, data in agent_stats.items():
            ss = data["scores"]
            agent_summary[aid] = {
                "count": len(ss),
                "avg_score": round(sum(ss) / len(ss), 2),
                "min": min(ss),
                "max": max(ss),
                "models": list(data["models"]),
            }

        return {
            "total_episodes": len(self._episodes),
            "total_cases": len(self._cases),
            "node_counts": dict(type_counts),
            "edge_count": self.G.number_of_edges() if self.G else 0,
            "overall_avg_score": round(avg_score, 2),
            "score_distribution": {
                "9-10 (Elite)": sum(1 for s in scores if s >= 9),
                "7-8 (Solid)": sum(1 for s in scores if 7 <= s < 9),
                "5-6 (Acceptable)": sum(1 for s in scores if 5 <= s < 7),
                "3-4 (Substandard)": sum(1 for s in scores if 3 <= s < 5),
                "1-2 (Failed)": sum(1 for s in scores if s < 3),
            },
            "per_agent": agent_summary,
        }
