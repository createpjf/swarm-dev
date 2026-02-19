"""
adapters/chain/mock.py
Mock chain adapter — logs to JSONL instead of writing on-chain.
For Level 0/1 deployments without blockchain.
"""

from __future__ import annotations
import json
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

MOCK_LOG = "memory/chain_mock.jsonl"


class MockChain:

    def __init__(self, log_path: str = MOCK_LOG):
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    def register_agent(self, agent_id: str, metadata: dict) -> str:
        """Register an agent. Returns mock tx hash."""
        tx_hash = f"0xmock_{uuid.uuid4().hex[:16]}"
        self._log("register_agent", {
            "agent_id": agent_id,
            "metadata": metadata,
            "tx_hash":  tx_hash,
        })
        logger.info("[mock_chain] registered agent %s → %s", agent_id, tx_hash)
        return tx_hash

    def submit_reputation(self, agent_id: str, score: int,
                          signals: dict) -> str:
        """Submit reputation score. Returns mock tx hash."""
        tx_hash = f"0xmock_{uuid.uuid4().hex[:16]}"
        self._log("submit_reputation", {
            "agent_id": agent_id,
            "score":    score,
            "signals":  signals,
            "tx_hash":  tx_hash,
        })
        logger.info("[mock_chain] submitted score %d for %s → %s",
                    score, agent_id, tx_hash)
        return tx_hash

    def _log(self, action: str, data: dict):
        entry = json.dumps({
            "action": action,
            "ts":     time.time(),
            **data,
        })
        try:
            with open(self.log_path, "a") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning("Failed to write chain mock log: %s", e)
