"""
adapters/chain/erc8004.py
ERC-8004 Identity & Reputation Registry adapter.
Uses web3.py for on-chain interactions on Base network.
Supports IPFS uploads for agent cards and signals.
Bidirectional sync: write scores to chain AND read them back.
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── IPFS Helper ──────────────────────────────────────────────────────────────

class IPFSHelper:
    """
    Upload JSON data to IPFS via Pinata or local node.
    Falls back to deterministic content-hash CID simulation
    when no IPFS service is configured.
    """

    def __init__(self):
        self.pinata_jwt = os.environ.get("PINATA_JWT", "")
        self.ipfs_api_url = os.environ.get(
            "IPFS_API_URL", "https://api.pinata.cloud")

    @property
    def available(self) -> bool:
        return bool(self.pinata_jwt)

    def upload_json(self, data: dict, name: str = "swarm-data") -> str:
        """
        Upload JSON to IPFS. Returns CID string.
        If Pinata is configured, uses Pinata pinning API.
        Otherwise returns a deterministic content-hash placeholder.
        """
        json_bytes = json.dumps(data, sort_keys=True,
                                ensure_ascii=False).encode("utf-8")

        if self.pinata_jwt:
            return self._upload_pinata(json_bytes, name)

        # Fallback: deterministic content-hash CID (not real IPFS, but
        # reproducible and useful for testing)
        content_hash = hashlib.sha256(json_bytes).hexdigest()
        cid = f"bafk_{content_hash[:46]}"
        logger.debug("[ipfs] simulated CID: %s (no IPFS service configured)", cid)
        return cid

    def _upload_pinata(self, data: bytes, name: str) -> str:
        """Upload to Pinata pinning service."""
        try:
            import httpx
            resp = httpx.post(
                f"{self.ipfs_api_url}/pinning/pinJSONToIPFS",
                headers={
                    "Authorization": f"Bearer {self.pinata_jwt}",
                    "Content-Type": "application/json",
                },
                json={
                    "pinataContent": json.loads(data),
                    "pinataMetadata": {"name": name},
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            result = resp.json()
            cid = result.get("IpfsHash", "")
            logger.info("[ipfs] pinned %s → %s", name, cid)
            return cid
        except Exception as e:
            logger.warning("[ipfs] Pinata upload failed: %s — using content hash", e)
            content_hash = hashlib.sha256(data).hexdigest()
            return f"bafk_{content_hash[:46]}"

# Minimal ABIs — only functions we actually call
IDENTITY_ABI = [
    {
        "inputs": [{"name": "agentCardCid", "type": "string"}],
        "name": "registerAgent",
        "outputs": [{"name": "agentId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "newWallet", "type": "address"},
            {"name": "sig", "type": "bytes"},
        ],
        "name": "setAgentWallet",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "wallet", "type": "address"}],
        "name": "isRegisteredAgent",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "wallet", "type": "address"}],
        "name": "getAgentId",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "tokenURI",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

REPUTATION_ABI = [
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "score", "type": "uint256"},
            {"name": "signalsCid", "type": "string"},
        ],
        "name": "submitReputation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "getReputation",
        "outputs": [
            {"name": "score", "type": "uint256"},
            {"name": "submissions", "type": "uint256"},
            {"name": "lastUpdate", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

USDC_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class ERC8004Adapter:
    """
    ERC-8004 Identity + Reputation on-chain adapter.
    Handles agent registration, identity verification,
    and reputation attestation on Base network.
    """

    def __init__(self, rpc_url: str = "", identity_registry: str = "",
                 reputation_registry: str = "", operator_key: str = "",
                 usdc_address: str = ""):
        self.rpc_url = rpc_url or os.environ.get("BASE_RPC_URL", "")
        self.identity_addr = identity_registry or os.environ.get(
            "ERC8004_IDENTITY_REGISTRY", ""
        )
        self.reputation_addr = reputation_registry or os.environ.get(
            "ERC8004_REPUTATION_REGISTRY", ""
        )
        self.operator_key = operator_key or os.environ.get("CHAIN_PRIVATE_KEY", "")
        self.usdc_addr = usdc_address or os.environ.get(
            "USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        )

        self._w3 = None
        self._identity_contract = None
        self._reputation_contract = None
        self._usdc_contract = None
        self._account = None
        self._ipfs = IPFSHelper()

        if not self.rpc_url:
            logger.warning("[erc8004] BASE_RPC_URL not set — on-chain ops will fail")

    # ── Connection ─────────────────────────────────────────────

    def _ensure_web3(self):
        """Lazy-init web3 connection and contracts."""
        if self._w3 is not None:
            return

        from web3 import Web3
        from eth_account import Account

        self._w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if not self._w3.is_connected():
            raise ConnectionError(f"Cannot connect to {self.rpc_url}")

        if self.operator_key:
            self._account = Account.from_key(self.operator_key)
            logger.info("[erc8004] Operator: %s", self._account.address)

        if self.identity_addr:
            self._identity_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.identity_addr),
                abi=IDENTITY_ABI,
            )
        if self.reputation_addr:
            self._reputation_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.reputation_addr),
                abi=REPUTATION_ABI,
            )
        if self.usdc_addr:
            self._usdc_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.usdc_addr),
                abi=USDC_ABI,
            )

    # ── Identity Operations ────────────────────────────────────

    def register_agent(self, agent_id: str, metadata: dict) -> str:
        """
        Register an agent on-chain via ERC-8004 Identity Registry.
        metadata should contain: name, capabilities, pkp_address, endpoint
        Returns: tx_hash
        """
        self._ensure_web3()
        if not self._identity_contract or not self._account:
            logger.warning("[erc8004] register_agent(%s) — missing contract or key", agent_id)
            return "0x_stub"

        # Build Agent Card JSON and upload to IPFS
        agent_card_cid = metadata.get("agent_card_cid", "")
        if not agent_card_cid:
            agent_card = {
                "name": agent_id,
                "description": metadata.get("description", f"{agent_id} — Swarm Agent"),
                "endpoint": metadata.get("endpoint", ""),
                "capabilities": metadata.get("capabilities", []),
                "pkpAddress": metadata.get("pkp_address", ""),
                "version": "1.0.0",
            }
            agent_card_cid = self._ipfs.upload_json(
                agent_card, name=f"agent-card-{agent_id}")

        try:
            tx = self._identity_contract.functions.registerAgent(
                agent_card_cid
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 500000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            hex_hash = tx_hash.hex()

            logger.info("[erc8004] registered %s — tx: %s", agent_id, hex_hash)
            return hex_hash

        except Exception as e:
            logger.error("[erc8004] register_agent(%s) failed: %s", agent_id, e)
            return f"0x_error_{e}"

    def submit_reputation(self, agent_id: str, score: int,
                          signals: dict) -> str:
        """
        Submit reputation attestation on-chain.
        Returns: tx_hash
        """
        self._ensure_web3()
        if not self._reputation_contract or not self._account:
            logger.warning("[erc8004] submit_reputation(%s) — missing contract or key",
                          agent_id)
            return "0x_stub"

        # Get the on-chain agentId from the identity registry
        chain_agent_id = self._get_chain_agent_id(agent_id)
        if chain_agent_id is None:
            logger.warning("[erc8004] Agent %s not registered on-chain", agent_id)
            return "0x_not_registered"

        # Upload signals to IPFS (falls back to content-hash if no service)
        signals_cid = self._ipfs.upload_json(
            signals, name=f"reputation-{agent_id}-{int(time.time())}")

        try:
            tx = self._reputation_contract.functions.submitReputation(
                chain_agent_id,
                int(score),
                signals_cid,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 200000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            hex_hash = tx_hash.hex()

            logger.info("[erc8004] reputation(%s, score=%d) — tx: %s",
                       agent_id, score, hex_hash)
            return hex_hash

        except Exception as e:
            logger.error("[erc8004] submit_reputation(%s) failed: %s", agent_id, e)
            return f"0x_error_{e}"

    # ── Read Operations ────────────────────────────────────────

    def is_registered(self, address: str) -> bool:
        """Check if an address has a registered ERC-8004 identity."""
        self._ensure_web3()
        if not self._identity_contract:
            return False
        try:
            from web3 import Web3
            return self._identity_contract.functions.isRegisteredAgent(
                Web3.to_checksum_address(address)
            ).call()
        except Exception as e:
            logger.warning("[erc8004] isRegisteredAgent failed: %s", e)
            return False

    def get_agent_id(self, address: str) -> Optional[int]:
        """Get the on-chain agentId for an address."""
        self._ensure_web3()
        if not self._identity_contract:
            return None
        try:
            from web3 import Web3
            return self._identity_contract.functions.getAgentId(
                Web3.to_checksum_address(address)
            ).call()
        except Exception:
            return None

    def get_reputation(self, chain_agent_id: int) -> dict:
        """Read on-chain reputation for an agent."""
        self._ensure_web3()
        if not self._reputation_contract:
            return {"score": 0, "submissions": 0, "last_update": 0}
        try:
            score, submissions, last_update = (
                self._reputation_contract.functions.getReputation(
                    chain_agent_id
                ).call()
            )
            return {
                "score": score,
                "submissions": submissions,
                "last_update": last_update,
            }
        except Exception as e:
            logger.warning("[erc8004] getReputation failed: %s", e)
            return {"score": 0, "submissions": 0, "last_update": 0}

    def get_usdc_balance(self, address: str) -> str:
        """Get USDC balance for an address (returns human-readable string)."""
        self._ensure_web3()
        if not self._usdc_contract:
            return "0.00"
        try:
            from web3 import Web3
            raw = self._usdc_contract.functions.balanceOf(
                Web3.to_checksum_address(address)
            ).call()
            return f"{raw / 1e6:.2f}"
        except Exception as e:
            logger.warning("[erc8004] balanceOf failed: %s", e)
            return "0.00"

    def set_agent_wallet(self, chain_agent_id: int, new_address: str,
                         proof: bytes) -> str:
        """
        Migrate agent to a new wallet address.
        agentId stays the same, only the bound address changes.
        """
        self._ensure_web3()
        if not self._identity_contract or not self._account:
            return "0x_stub"

        try:
            from web3 import Web3
            tx = self._identity_contract.functions.setAgentWallet(
                chain_agent_id,
                Web3.to_checksum_address(new_address),
                proof,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 200000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            return tx_hash.hex()

        except Exception as e:
            logger.error("[erc8004] setAgentWallet failed: %s", e)
            return f"0x_error_{e}"

    # ── Health ─────────────────────────────────────────────────

    def health_check(self) -> dict:
        """Check RPC connectivity and contract availability."""
        result = {
            "rpc_url": self.rpc_url[:40] + "..." if self.rpc_url else "",
            "has_operator_key": bool(self.operator_key),
            "identity_registry": self.identity_addr or "not set",
            "reputation_registry": self.reputation_addr or "not set",
        }
        try:
            self._ensure_web3()
            result["connected"] = self._w3.is_connected()
            result["chain_id"] = self._w3.eth.chain_id
            result["block_number"] = self._w3.eth.block_number
            result["status"] = "ok"
        except Exception as e:
            result["connected"] = False
            result["status"] = f"error: {e}"
        return result

    # ── Internal ───────────────────────────────────────────────

    def _get_chain_agent_id(self, agent_id: str) -> Optional[int]:
        """Look up chain agentId by reading chain_state or querying on-chain."""
        # Try chain_state first
        try:
            from adapters.chain.chain_state import ChainState
            state = ChainState()
            agent_data = state.get_agent(agent_id)
            if agent_data.get("erc8004_agent_id") is not None:
                return agent_data["erc8004_agent_id"]
        except Exception:
            pass

        # Query on-chain by PKP address
        try:
            from adapters.chain.chain_state import ChainState
            state = ChainState()
            agent_data = state.get_agent(agent_id)
            pkp_addr = agent_data.get("pkp_eth_address", "")
            if pkp_addr:
                return self.get_agent_id(pkp_addr)
        except Exception:
            pass

        return None
