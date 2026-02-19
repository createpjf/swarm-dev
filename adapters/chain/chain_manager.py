"""
adapters/chain/chain_manager.py
Unified chain operations manager — orchestrates Lit PKP, ERC-8004, x402, and Safe.
Drop-in replacement for MockChain with same interface + additional methods.
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Optional

from adapters.chain.chain_state import ChainState
from adapters.chain.lit_pkp import LitPKPAdapter
from adapters.chain.erc8004 import ERC8004Adapter

logger = logging.getLogger(__name__)


class ChainManager:
    """
    Top-level chain adapter that coordinates:
    - Lit Protocol PKP (key management + signing)
    - ERC-8004 (identity + reputation on-chain)
    - x402 (payment protocol) — when enabled
    - Gnosis Safe (guardian multi-sig) — when configured

    Interface-compatible with MockChain (register_agent, submit_reputation).
    """

    def __init__(self, config: dict):
        self.config = config
        chain_cfg = config.get("chain", {})
        self.enabled = chain_cfg.get("enabled", False)

        # State manager
        self.state = ChainState()

        # Sub-adapters (lazy init)
        self._lit: Optional[LitPKPAdapter] = None
        self._erc8004: Optional[ERC8004Adapter] = None
        self._safe = None  # GnosisSafeAdapter
        self._x402_client = None

        # Config shortcuts
        self.network = chain_cfg.get("network", "base-sepolia")
        self.lit_network = chain_cfg.get("lit", {}).get("network", "naga-dev")
        self.x402_enabled = chain_cfg.get("x402", {}).get("enabled", False)

        # Reputation sync settings
        rep_sync = chain_cfg.get("reputation_sync", {})
        self.rep_sync_enabled = rep_sync.get("enabled", True)
        self.rep_min_delta = rep_sync.get("min_score_delta", 5.0)
        self.rep_max_writes_hr = rep_sync.get("max_writes_per_hour", 10)
        self._rep_write_count = 0
        self._rep_write_hour = 0

    # ── Lazy Initialization ────────────────────────────────────

    @property
    def lit(self) -> LitPKPAdapter:
        if self._lit is None:
            operator_key = os.environ.get(
                self.config.get("chain", {}).get("operator_key_env", "CHAIN_PRIVATE_KEY"),
                ""
            )
            self._lit = LitPKPAdapter(
                network=self.lit_network,
                operator_key=operator_key,
            )
        return self._lit

    @property
    def erc8004(self) -> ERC8004Adapter:
        if self._erc8004 is None:
            chain_cfg = self.config.get("chain", {})
            erc_cfg = chain_cfg.get("erc8004", {})
            self._erc8004 = ERC8004Adapter(
                rpc_url=os.environ.get(
                    chain_cfg.get("rpc_url_env", "BASE_RPC_URL"), ""
                ),
                identity_registry=os.environ.get(
                    erc_cfg.get("identity_registry_env", "ERC8004_IDENTITY_REGISTRY"), ""
                ),
                reputation_registry=os.environ.get(
                    erc_cfg.get("reputation_registry_env", "ERC8004_REPUTATION_REGISTRY"), ""
                ),
                operator_key=os.environ.get(
                    chain_cfg.get("operator_key_env", "CHAIN_PRIVATE_KEY"), ""
                ),
            )
        return self._erc8004

    # ── MockChain-Compatible Interface ─────────────────────────

    def register_agent(self, agent_id: str, metadata: dict) -> str:
        """Register agent on-chain. Compatible with MockChain interface."""
        tx_hash = self.erc8004.register_agent(agent_id, metadata)
        self.state.add_transaction({
            "action": "register_agent",
            "agent_id": agent_id,
            "tx_hash": tx_hash,
        })
        return tx_hash

    def submit_reputation(self, agent_id: str, score: int,
                          signals: dict) -> str:
        """Submit reputation on-chain. Compatible with MockChain interface."""
        if not self._should_write_reputation():
            logger.debug("[chain] Skipping reputation write — rate limited")
            return "0x_rate_limited"

        tx_hash = self.erc8004.submit_reputation(agent_id, score, signals)
        self.state.add_transaction({
            "action": "submit_reputation",
            "agent_id": agent_id,
            "score": score,
            "tx_hash": tx_hash,
        })
        self._rep_write_count += 1
        return tx_hash

    # ── Agent Lifecycle ────────────────────────────────────────

    def initialize_agent(self, agent_id: str,
                         agent_config: dict = None) -> dict:
        """
        Full agent on-chain initialization:
        1. Mint PKP on Lit network
        2. Register on ERC-8004 Identity Registry
        3. Save state
        Returns: agent chain state dict
        """
        logger.info("[chain] Initializing agent '%s'...", agent_id)

        # Step 1: Mint PKP
        logger.info("[chain] [1/3] Minting PKP on %s...", self.lit_network)
        self.lit.connect()
        pkp = self.lit.mint_pkp()

        # Save PKP to state immediately
        self.state.set_agent(agent_id, {
            "pkp_token_id": pkp["token_id"],
            "pkp_public_key": pkp["public_key"],
            "pkp_eth_address": pkp["eth_address"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

        # Save PKP env vars
        prefix = agent_id.upper()
        from core.gateway import _save_env_var
        _save_env_var(f"{prefix}_PKP_TOKEN_ID", pkp["token_id"])
        _save_env_var(f"{prefix}_PKP_PUBLIC_KEY", pkp["public_key"])
        _save_env_var(f"{prefix}_PKP_ETH_ADDRESS", pkp["eth_address"])

        # Step 2: Register on ERC-8004
        logger.info("[chain] [2/3] Registering on ERC-8004...")
        metadata = {
            "pkp_address": pkp["eth_address"],
            "description": f"{agent_id} — Swarm Agent",
            "capabilities": (agent_config or {}).get("skills", []),
        }
        tx_hash = self.erc8004.register_agent(agent_id, metadata)

        # Try to get the on-chain agent ID from the tx receipt
        chain_agent_id = None
        try:
            self.erc8004._ensure_web3()
            receipt = self.erc8004._w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=30
            )
            if receipt.get("status") == 1:
                # Parse agentId from AgentRegistered event
                chain_agent_id = receipt.get("logs", [{}])[0].get("topics", [None, None])[1]
                if chain_agent_id:
                    chain_agent_id = int(chain_agent_id.hex(), 16)
        except Exception as e:
            logger.warning("[chain] Could not get agentId from receipt: %s", e)

        # Step 3: Save full state
        logger.info("[chain] [3/3] Saving state...")
        self.state.set_agent(agent_id, {
            "registered": True,
            "erc8004_agent_id": chain_agent_id,
            "register_tx": tx_hash,
        })

        self.state.add_transaction({
            "action": "initialize_agent",
            "agent_id": agent_id,
            "pkp_address": pkp["eth_address"],
            "tx_hash": tx_hash,
        })

        result = self.state.get_agent(agent_id)
        logger.info("[chain] Agent '%s' initialized! PKP: %s",
                   agent_id, pkp["eth_address"])
        return result

    # ── Bidirectional Reputation Sync ────────────────────────────

    def read_chain_reputation(self, agent_id: str) -> dict:
        """
        Read reputation back from chain for an agent.
        Returns: {score, submissions, last_update, synced}
        This closes the chain verification loop.
        """
        agent_data = self.state.get_agent(agent_id)
        chain_agent_id = agent_data.get("erc8004_agent_id")
        if chain_agent_id is None:
            return {"score": 0, "submissions": 0, "last_update": 0, "synced": False}

        try:
            chain_rep = self.erc8004.get_reputation(chain_agent_id)
            self.state.set_agent(agent_id, {
                "chain_reputation_score": chain_rep.get("score", 0),
                "chain_reputation_submissions": chain_rep.get("submissions", 0),
                "chain_reputation_last_update": chain_rep.get("last_update", 0),
                "chain_reputation_read_at": time.time(),
            })
            chain_rep["synced"] = True
            return chain_rep
        except Exception as e:
            logger.warning("[chain] read_chain_reputation(%s) failed: %s", agent_id, e)
            return {"score": 0, "submissions": 0, "last_update": 0, "synced": False}

    def verify_reputation(self, agent_id: str, local_score: float) -> dict:
        """
        Compare local reputation with on-chain score.
        Returns verification result with divergence info.
        """
        chain_rep = self.read_chain_reputation(agent_id)
        chain_score = chain_rep.get("score", 0)
        divergence = abs(local_score - chain_score)

        result = {
            "agent_id": agent_id,
            "local_score": round(local_score, 1),
            "chain_score": chain_score,
            "divergence": round(divergence, 1),
            "synced": chain_rep.get("synced", False),
            "submissions": chain_rep.get("submissions", 0),
            "verified": divergence <= 15,
        }

        if divergence > 15 and chain_rep.get("synced"):
            logger.warning(
                "[chain] Reputation divergence for %s: local=%.1f chain=%d (delta=%.1f)",
                agent_id, local_score, chain_score, divergence)

        return result

    # ── Status & Queries ───────────────────────────────────────

    def get_status(self) -> dict:
        """Full chain status for dashboard/CLI."""
        state = self.state.read()
        agents_status = {}

        for agent_id, agent_data in state.get("agents", {}).items():
            pkp_addr = agent_data.get("pkp_eth_address", "")
            agents_status[agent_id] = {
                "registered": agent_data.get("registered", False),
                "pkp_address": pkp_addr,
                "erc8004_agent_id": agent_data.get("erc8004_agent_id"),
                "usdc_balance": agent_data.get("usdc_balance_cache", "0.00"),
            }

        return {
            "enabled": self.enabled,
            "network": self.network,
            "lit_network": self.lit_network,
            "x402_enabled": self.x402_enabled,
            "team": state.get("team", {}),
            "agents": agents_status,
            "recent_transactions": state.get("transactions", [])[-10:],
        }

    def get_balance(self, agent_id: str) -> str:
        """Get USDC balance for an agent's PKP address."""
        agent_data = self.state.get_agent(agent_id)
        pkp_addr = agent_data.get("pkp_eth_address", "")
        if not pkp_addr:
            return "0.00"

        balance = self.erc8004.get_usdc_balance(pkp_addr)

        # Cache the balance
        self.state.set_agent(agent_id, {
            "usdc_balance_cache": balance,
            "last_balance_check": time.time(),
        })

        return balance

    def health_check(self) -> dict:
        """Aggregate health from all sub-adapters."""
        result = {
            "chain_enabled": self.enabled,
            "network": self.network,
        }

        # Lit health
        try:
            result["lit"] = self.lit.health_check()
        except Exception as e:
            result["lit"] = {"status": f"error: {e}"}

        # ERC-8004 health
        try:
            result["erc8004"] = self.erc8004.health_check()
        except Exception as e:
            result["erc8004"] = {"status": f"error: {e}"}

        # Overall status
        lit_ok = result.get("lit", {}).get("status") == "ok"
        erc_ok = result.get("erc8004", {}).get("status") == "ok"
        if lit_ok and erc_ok:
            result["status"] = "ok"
        elif lit_ok or erc_ok:
            result["status"] = "partial"
        else:
            result["status"] = "degraded"

        return result

    # ── Internal ───────────────────────────────────────────────

    def _should_write_reputation(self) -> bool:
        """Rate-limit on-chain reputation writes."""
        if not self.rep_sync_enabled:
            return False
        current_hour = int(time.time() / 3600)
        if current_hour != self._rep_write_hour:
            self._rep_write_hour = current_hour
            self._rep_write_count = 0
        return self._rep_write_count < self.rep_max_writes_hr
