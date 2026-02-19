"""
adapters/chain/chain_state.py
File-backed chain state with filelock.
Follows the same pattern as reputation/scorer.py.
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Optional

from filelock import FileLock

logger = logging.getLogger(__name__)

STATE_FILE = "chain_state.json"
LOCK_FILE = "chain_state.lock"
MAX_TRANSACTIONS = 100


def _default_state() -> dict:
    return {
        "team": {
            "gnosis_safe_address": "",
            "network": "",
            "rpc_url": "",
        },
        "agents": {},
        "transactions": [],
    }


def _default_agent() -> dict:
    return {
        "pkp_token_id": "",
        "pkp_public_key": "",
        "pkp_eth_address": "",
        "erc8004_agent_id": None,
        "erc8004_token_id": None,
        "agent_card_cid": "",
        "lit_action_cid": "",
        "registered": False,
        "usdc_balance_cache": "0.00",
        "last_balance_check": 0.0,
        "created_at": "",
        "updated_at": "",
    }


class ChainState:
    """File-backed chain identity state with process-safe locking."""

    def __init__(self, state_file: str = STATE_FILE,
                 lock_file: str = LOCK_FILE):
        self.state_file = state_file
        self.lock_file = lock_file
        self._lock = FileLock(lock_file, timeout=10)

    def read(self) -> dict:
        """Read full chain state. Returns default if file doesn't exist."""
        with self._lock:
            return self._read_unlocked()

    def write(self, state: dict):
        """Atomically write full chain state."""
        with self._lock:
            self._write_unlocked(state)

    def get_team(self) -> dict:
        """Get team-level chain info."""
        state = self.read()
        return state.get("team", {})

    def set_team(self, data: dict):
        """Update team-level chain info (merge)."""
        with self._lock:
            state = self._read_unlocked()
            state.setdefault("team", {}).update(data)
            self._write_unlocked(state)

    def get_agent(self, agent_id: str) -> dict:
        """Get chain state for a specific agent."""
        state = self.read()
        return state.get("agents", {}).get(agent_id, _default_agent())

    def set_agent(self, agent_id: str, data: dict):
        """Update chain state for a specific agent (merge)."""
        with self._lock:
            state = self._read_unlocked()
            agents = state.setdefault("agents", {})
            current = agents.get(agent_id, _default_agent())
            current.update(data)
            current["updated_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            agents[agent_id] = current
            self._write_unlocked(state)

    def list_agents(self) -> dict[str, dict]:
        """List all agents with chain state."""
        state = self.read()
        return state.get("agents", {})

    def add_transaction(self, tx: dict):
        """Append a transaction to the log (capped at MAX_TRANSACTIONS)."""
        with self._lock:
            state = self._read_unlocked()
            txs = state.setdefault("transactions", [])
            tx.setdefault("timestamp", time.time())
            txs.append(tx)
            if len(txs) > MAX_TRANSACTIONS:
                state["transactions"] = txs[-MAX_TRANSACTIONS:]
            self._write_unlocked(state)

    def get_transactions(self, limit: int = 20) -> list[dict]:
        """Get recent transactions."""
        state = self.read()
        txs = state.get("transactions", [])
        return txs[-limit:]

    # ── Internal ───────────────────────────────────────────────

    def _read_unlocked(self) -> dict:
        """Read without acquiring lock (caller must hold lock)."""
        if not os.path.exists(self.state_file):
            return _default_state()
        try:
            with open(self.state_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read chain state: %s", e)
            return _default_state()

    def _write_unlocked(self, state: dict):
        """Write without acquiring lock (caller must hold lock)."""
        try:
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as e:
            logger.error("Failed to write chain state: %s", e)
