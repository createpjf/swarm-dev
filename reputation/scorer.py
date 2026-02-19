"""
reputation/scorer.py
5-dimension EMA scoring engine.
Dimensions: task_completion (25%), output_quality (30%),
            improvement_rate (25%), consistency (10%), review_accuracy (10%)
"""

from __future__ import annotations
import json
import logging
import os
import time

try:
    from filelock import FileLock
except ImportError:
    class FileLock:  # type: ignore
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

logger = logging.getLogger(__name__)

WEIGHTS = {
    "task_completion":  0.25,
    "output_quality":   0.30,
    "improvement_rate": 0.25,
    "consistency":      0.10,
    "review_accuracy":  0.10,
}
DIMENSIONS = list(WEIGHTS.keys())
ALPHA = 0.3  # EMA smoothing factor
DEFAULT_SCORE = 70.0  # starting score for new agents

CACHE_FILE = "memory/reputation_cache.json"
LOG_FILE   = "memory/score_log.jsonl"


class ScoreAggregator:
    """
    Multi-dimensional reputation scoring with EMA updates.
    Persisted to memory/reputation_cache.json.
    """

    def __init__(self, cache_path: str = CACHE_FILE,
                 log_path: str = LOG_FILE):
        self.cache_path = cache_path
        self.log_path   = log_path
        self.lock = FileLock(cache_path + ".lock")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    # ── Update ────────────────────────────────────────────────────────────────

    def update(self, agent_id: str, dimension: str, signal: float):
        """
        EMA update: new = alpha * signal + (1 - alpha) * old
        Then recompute composite = sum(dim * weight).
        """
        if dimension not in WEIGHTS:
            logger.warning("Unknown dimension: %s", dimension)
            return

        with self.lock:
            cache = self._read_cache()
            agent = cache.setdefault(agent_id, self._default_entry())

            old = agent["dimensions"].get(dimension, DEFAULT_SCORE)
            new = ALPHA * signal + (1 - ALPHA) * old
            agent["dimensions"][dimension] = round(new, 2)

            # Recompute composite
            composite = sum(
                agent["dimensions"].get(d, DEFAULT_SCORE) * w
                for d, w in WEIGHTS.items()
            )
            agent["composite"] = round(composite, 2)

            # Track history for trend (keep last 50)
            history = agent.setdefault("history", [])
            history.append({"composite": agent["composite"], "ts": time.time()})
            if len(history) > 50:
                agent["history"] = history[-50:]

            agent["updated_at"] = time.time()
            self._write_cache(cache)

        # Audit log
        self._log(agent_id, dimension, signal, cache[agent_id]["composite"])

        # Chain sync — async, non-blocking, failure-tolerant
        self._maybe_sync_chain(agent_id, cache[agent_id])

    def _maybe_sync_chain(self, agent_id: str, agent_data: dict):
        """Sync reputation to chain if delta exceeds threshold."""
        try:
            import yaml
            if not os.path.exists("config/agents.yaml"):
                return
            with open("config/agents.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            chain_cfg = cfg.get("chain", {})
            if not chain_cfg.get("enabled", False):
                return
            rep_sync = chain_cfg.get("reputation_sync", {})
            if not rep_sync.get("enabled", True):
                return

            min_delta = rep_sync.get("min_score_delta", 5.0)

            # Check if score delta is significant enough
            from adapters.chain.chain_state import ChainState
            state = ChainState()
            chain_agent = state.get_agent(agent_id)
            last_synced = chain_agent.get("last_synced_score", 0)
            current = agent_data.get("composite", 0)

            if abs(current - last_synced) < min_delta:
                return

            # Submit to chain (non-blocking via thread)
            from threading import Thread
            def _sync():
                try:
                    from adapters.chain.chain_manager import ChainManager
                    mgr = ChainManager(cfg)
                    tx = mgr.submit_reputation(
                        agent_id,
                        int(current),
                        agent_data.get("dimensions", {}),
                    )
                    if tx and not tx.startswith("0x_"):
                        state.set_agent(agent_id, {"last_synced_score": current})
                        logger.info("[chain-sync] %s score=%d tx=%s", agent_id, int(current), tx[:16])
                except Exception as e:
                    logger.debug("[chain-sync] Failed for %s: %s", agent_id, e)

            Thread(target=_sync, daemon=True).start()

        except Exception:
            pass  # Never let chain sync break local reputation

    def get_chain_verified(self, agent_id: str) -> dict:
        """
        Return local score alongside on-chain verification.
        Enables chain data to influence trust decisions.
        """
        local_score = self.get(agent_id)
        result = {
            "agent_id": agent_id,
            "local_score": local_score,
            "chain_score": None,
            "verified": False,
        }
        try:
            import yaml
            if not os.path.exists("config/agents.yaml"):
                return result
            with open("config/agents.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            if not cfg.get("chain", {}).get("enabled", False):
                return result
            from adapters.chain.chain_manager import ChainManager
            mgr = ChainManager(cfg)
            verification = mgr.verify_reputation(agent_id, local_score)
            result.update(verification)
        except Exception as e:
            logger.debug("[scorer] chain verification failed for %s: %s", agent_id, e)
        return result

    # ── Query ─────────────────────────────────────────────────────────────────

    def get(self, agent_id: str) -> float:
        """Return composite score. Default 70.0 for unknown agents."""
        cache = self._read_cache()
        entry = cache.get(agent_id)
        if entry:
            return entry.get("composite", DEFAULT_SCORE)
        return DEFAULT_SCORE

    def get_all(self, agent_id: str) -> dict:
        """Return all dimension scores as dict."""
        cache = self._read_cache()
        entry = cache.get(agent_id)
        if entry:
            return dict(entry.get("dimensions", {}))
        return {d: DEFAULT_SCORE for d in DIMENSIONS}

    def trend(self, agent_id: str) -> str:
        """
        Compare first-half vs second-half of last 10 composites.
        Returns 'improving', 'declining', or 'stable'.
        """
        cache = self._read_cache()
        entry = cache.get(agent_id)
        if not entry:
            return "stable"

        history = entry.get("history", [])
        if len(history) < 4:
            return "stable"

        recent = history[-10:]
        mid = len(recent) // 2
        first_half = [h["composite"] for h in recent[:mid]]
        second_half = [h["composite"] for h in recent[mid:]]

        avg_first  = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        diff = avg_second - avg_first

        if diff > 3:
            return "improving"
        elif diff < -3:
            return "declining"
        return "stable"

    def threshold_status(self, agent_id: str) -> str:
        """
        >= 80: healthy, 60-79: watch, 40-59: warning, < 40: evolve
        """
        score = self.get(agent_id)
        if score >= 80:
            return "healthy"
        elif score >= 60:
            return "watch"
        elif score >= 40:
            return "warning"
        return "evolve"

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _default_entry() -> dict:
        return {
            "dimensions": {d: DEFAULT_SCORE for d in DIMENSIONS},
            "composite":  DEFAULT_SCORE,
            "history":    [],
            "updated_at": time.time(),
        }

    def _read_cache(self) -> dict:
        try:
            with open(self.cache_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_cache(self, cache: dict):
        with open(self.cache_path, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    def _log(self, agent_id: str, dimension: str,
             signal: float, composite: float):
        entry = json.dumps({
            "agent_id":  agent_id,
            "dimension": dimension,
            "signal":    signal,
            "composite": composite,
            "ts":        time.time(),
        })
        try:
            with open(self.log_path, "a") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning("Failed to write score log: %s", e)
