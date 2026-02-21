"""
adapters/chain/erc8004.py
ERC-8004 Identity & Reputation Registry adapter.
Uses web3.py for on-chain interactions on Base network.

Supports BOTH:
  - Official ERC-8004 contracts on Base Mainnet (network="base")
  - Custom legacy contracts on Base Sepolia (network="base-sepolia")

The adapter auto-selects the correct ABI set based on the network parameter.
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import time
from threading import Thread
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

    def upload_json(self, data: dict, name: str = "cleo-data") -> str:
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


# ══════════════════════════════════════════════════════════════════════════════
#  ABIs — Official ERC-8004 (Base Mainnet)
# ══════════════════════════════════════════════════════════════════════════════

IDENTITY_ABI_OFFICIAL = [
    # register(string agentURI) → uint256 agentId
    {
        "inputs": [{"name": "agentURI", "type": "string"}],
        "name": "register",
        "outputs": [{"name": "agentId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # setAgentURI(uint256 agentId, string newURI)
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "newURI", "type": "string"},
        ],
        "name": "setAgentURI",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # setAgentWallet(uint256, address, uint256 deadline, bytes sig) — EIP-712
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "newWallet", "type": "address"},
            {"name": "deadline", "type": "uint256"},
            {"name": "signature", "type": "bytes"},
        ],
        "name": "setAgentWallet",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # getAgentWallet(uint256 agentId) → address
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "getAgentWallet",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    # getMetadata(uint256, string) → bytes
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "metadataKey", "type": "string"},
        ],
        "name": "getMetadata",
        "outputs": [{"name": "", "type": "bytes"}],
        "stateMutability": "view",
        "type": "function",
    },
    # setMetadata(uint256, string, bytes)
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "metadataKey", "type": "string"},
            {"name": "metadataValue", "type": "bytes"},
        ],
        "name": "setMetadata",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # ERC-721: ownerOf(uint256) → address
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    # ERC-721: tokenURI(uint256) → string
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "tokenURI",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    # event Registered(uint256 indexed agentId, string agentURI, address indexed owner)
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": False, "name": "agentURI", "type": "string"},
            {"indexed": True, "name": "owner", "type": "address"},
        ],
        "name": "Registered",
        "type": "event",
    },
]

REPUTATION_ABI_OFFICIAL = [
    # giveFeedback(uint256, int128, uint8, string, string, string, string, bytes32)
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "value", "type": "int128"},
            {"name": "valueDecimals", "type": "uint8"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
            {"name": "endpoint", "type": "string"},
            {"name": "feedbackURI", "type": "string"},
            {"name": "feedbackHash", "type": "bytes32"},
        ],
        "name": "giveFeedback",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # getSummary(uint256, address[], string, string) → (uint64, int128, uint8)
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "clientAddresses", "type": "address[]"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
        ],
        "name": "getSummary",
        "outputs": [
            {"name": "count", "type": "uint64"},
            {"name": "summaryValue", "type": "int128"},
            {"name": "summaryValueDecimals", "type": "uint8"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # readFeedback(uint256, address, uint64) → (int128, uint8, string, string, bool)
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "clientAddress", "type": "address"},
            {"name": "feedbackIndex", "type": "uint64"},
        ],
        "name": "readFeedback",
        "outputs": [
            {"name": "value", "type": "int128"},
            {"name": "valueDecimals", "type": "uint8"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
            {"name": "isRevoked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # getClients(uint256) → address[]
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "getClients",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    # event NewFeedback(...)
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": True, "name": "clientAddress", "type": "address"},
            {"indexed": False, "name": "feedbackIndex", "type": "uint64"},
            {"indexed": False, "name": "value", "type": "int128"},
            {"indexed": False, "name": "valueDecimals", "type": "uint8"},
            {"indexed": True, "name": "indexedTag1", "type": "string"},
            {"indexed": False, "name": "tag1", "type": "string"},
            {"indexed": False, "name": "tag2", "type": "string"},
            {"indexed": False, "name": "endpoint", "type": "string"},
            {"indexed": False, "name": "feedbackURI", "type": "string"},
            {"indexed": False, "name": "feedbackHash", "type": "bytes32"},
        ],
        "name": "NewFeedback",
        "type": "event",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
#  ABIs — Custom Legacy (Base Sepolia)
# ══════════════════════════════════════════════════════════════════════════════

IDENTITY_ABI_CUSTOM = [
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
    # event AgentRegistered(uint256 indexed agentId, address wallet, string cid)
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": False, "name": "wallet", "type": "address"},
            {"indexed": False, "name": "agentCardCid", "type": "string"},
        ],
        "name": "AgentRegistered",
        "type": "event",
    },
]

REPUTATION_ABI_CUSTOM = [
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
    # event ReputationSubmitted(uint256 indexed agentId, uint256 score, address submitter)
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": False, "name": "score", "type": "uint256"},
            {"indexed": False, "name": "submitter", "type": "address"},
        ],
        "name": "ReputationSubmitted",
        "type": "event",
    },
]


# ── USDC ABI (same for both networks) ────────────────────────────────────────

USDC_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
#  ERC-8004 Adapter
# ══════════════════════════════════════════════════════════════════════════════

class ERC8004Adapter:
    """
    ERC-8004 Identity + Reputation on-chain adapter.
    Handles agent registration, identity verification,
    and reputation attestation on Base network.

    Supports two modes:
      - Official (network="base"): Uses standard ERC-8004 contracts
        at 0x8004A169... and 0x8004BAa1...
      - Custom (network="base-sepolia"): Uses legacy custom contracts
        deployed via deploy_erc8004.py
    """

    def __init__(self, rpc_url: str = "", identity_registry: str = "",
                 reputation_registry: str = "", operator_key: str = "",
                 usdc_address: str = "", network: str = "base-sepolia"):
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
        self.network = network

        # Select ABI set based on network
        self._use_official = (network == "base")
        if self._use_official:
            self._identity_abi = IDENTITY_ABI_OFFICIAL
            self._reputation_abi = REPUTATION_ABI_OFFICIAL
        else:
            self._identity_abi = IDENTITY_ABI_CUSTOM
            self._reputation_abi = REPUTATION_ABI_CUSTOM

        self._w3 = None
        self._identity_contract = None
        self._reputation_contract = None
        self._usdc_contract = None
        self._account = None
        self._ipfs = IPFSHelper()

        if not self.rpc_url:
            logger.warning("[erc8004] BASE_RPC_URL not set — on-chain ops will fail")

        logger.debug("[erc8004] network=%s official=%s", network, self._use_official)

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
                abi=self._identity_abi,
            )
        if self.reputation_addr:
            self._reputation_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.reputation_addr),
                abi=self._reputation_abi,
            )
        if self.usdc_addr:
            self._usdc_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.usdc_addr),
                abi=USDC_ABI,
            )

    # ── Agent Registration JSON (ERC-8004 spec compliant) ─────

    def _build_agent_registration_json(self, agent_id: str,
                                        metadata: dict) -> dict:
        """
        Build ERC-8004 spec-compliant agent registration JSON.
        Schema: https://eips.ethereum.org/EIPS/eip-8004#registration-v1
        """
        services = []
        endpoint = metadata.get("endpoint", "")
        if endpoint:
            services.append({
                "name": "cleo-agent",
                "endpoint": endpoint,
                "version": "1.0.0",
            })

        return {
            "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
            "name": metadata.get("name", agent_id),
            "description": metadata.get(
                "description", f"{agent_id} — Cleo Agent"),
            "image": metadata.get("image", ""),
            "services": services,
            "x402Support": False,
            "active": True,
            "registrations": [],
            "supportedTrust": ["reputation"],
        }

    # ── Identity Operations ────────────────────────────────────

    def register_agent(self, agent_id: str, metadata: dict) -> str:
        """
        Register an agent on-chain via ERC-8004 Identity Registry.
        Official: calls register(agentURI)
        Custom:   calls registerAgent(agentCardCid)
        Returns: tx_hash
        """
        self._ensure_web3()
        if not self._identity_contract or not self._account:
            logger.warning(
                "[erc8004] register_agent(%s) — missing contract or key",
                agent_id)
            return "0x_stub"

        # Build and upload agent card to IPFS
        agent_card_cid = metadata.get("agent_card_cid", "")
        if not agent_card_cid:
            if self._use_official:
                # Spec-compliant JSON
                agent_card = self._build_agent_registration_json(
                    agent_id, metadata)
            else:
                # Legacy custom format
                agent_card = {
                    "name": agent_id,
                    "description": metadata.get(
                        "description", f"{agent_id} — Cleo Agent"),
                    "endpoint": metadata.get("endpoint", ""),
                    "capabilities": metadata.get("capabilities", []),
                    "pkpAddress": metadata.get("pkp_address", ""),
                    "version": "1.0.0",
                }
            agent_card_cid = self._ipfs.upload_json(
                agent_card, name=f"agent-card-{agent_id}")

        try:
            if self._use_official:
                # Official: register(string agentURI)
                agent_uri = f"ipfs://{agent_card_cid}"
                fn = self._identity_contract.functions.register(agent_uri)
            else:
                # Custom: registerAgent(string agentCardCid)
                fn = self._identity_contract.functions.registerAgent(
                    agent_card_cid)

            tx = fn.build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(
                    self._account.address),
                "gas": 500000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(
                signed.raw_transaction)
            hex_hash = tx_hash.hex()

            logger.info("[erc8004] registered %s (network=%s) — tx: %s",
                        agent_id, self.network, hex_hash)
            return hex_hash

        except Exception as e:
            logger.error("[erc8004] register_agent(%s) failed: %s",
                         agent_id, e)
            return f"0x_error_{e}"

    def parse_registered_event(self, receipt) -> Optional[int]:
        """
        Parse agentId from tx receipt.
        Official: Registered(agentId, agentURI, owner)
        Custom:   AgentRegistered(agentId, wallet, cid)
        """
        if not self._identity_contract:
            return None
        try:
            if self._use_official:
                events = (self._identity_contract.events
                          .Registered()
                          .process_receipt(receipt))
            else:
                events = (self._identity_contract.events
                          .AgentRegistered()
                          .process_receipt(receipt))
            if events:
                return int(events[0]["args"]["agentId"])
        except Exception as e:
            logger.debug("[erc8004] event parse failed: %s", e)
        return None

    # ── Reputation Operations ──────────────────────────────────

    def submit_reputation(self, agent_id: str, score: int,
                          signals: dict) -> str:
        """
        Submit reputation attestation on-chain.
        Official: giveFeedback with tags per dimension
        Custom:   submitReputation with single score + IPFS CID
        Returns: tx_hash
        """
        self._ensure_web3()
        if not self._reputation_contract or not self._account:
            logger.warning(
                "[erc8004] submit_reputation(%s) — missing contract or key",
                agent_id)
            return "0x_stub"

        chain_agent_id = self._get_chain_agent_id(agent_id)
        if chain_agent_id is None:
            logger.warning(
                "[erc8004] Agent %s not registered on-chain", agent_id)
            return "0x_not_registered"

        if self._use_official:
            return self._submit_feedback_official(
                agent_id, chain_agent_id, score, signals)
        else:
            return self._submit_reputation_custom(
                agent_id, chain_agent_id, score, signals)

    def _submit_feedback_official(self, agent_id: str,
                                   chain_agent_id: int,
                                   score: int,
                                   signals: dict) -> str:
        """
        Official ERC-8004 giveFeedback path.
        Submits composite score + per-dimension feedback.

        Score mapping: Cleo 0-100 → int128 with valueDecimals=2
          e.g. score 85 → value=8500, decimals=2
        Tags: tag1="cleo", tag2=<dimension_name> or "composite"
        """
        # Upload feedback details to IPFS
        feedback_data = {
            "agent_id": agent_id,
            "score": score,
            "dimensions": signals,
            "ts": int(time.time()),
        }
        feedback_cid = self._ipfs.upload_json(
            feedback_data,
            name=f"feedback-{agent_id}-{int(time.time())}")
        feedback_uri = f"ipfs://{feedback_cid}"
        feedback_hash = bytes.fromhex(
            hashlib.sha256(feedback_uri.encode()).hexdigest())

        # Pre-fetch nonce to avoid racing
        nonce = self._w3.eth.get_transaction_count(self._account.address)

        # Composite feedback (blocking — this is the main tx)
        composite_value = int(score) * 100  # 85 → 8500
        try:
            composite_tx = self._send_give_feedback(
                chain_agent_id=chain_agent_id,
                value=composite_value,
                value_decimals=2,
                tag1="cleo",
                tag2="composite",
                endpoint="",
                feedback_uri=feedback_uri,
                feedback_hash=feedback_hash,
                nonce=nonce,
            )
        except Exception as e:
            logger.error(
                "[erc8004] giveFeedback composite(%s) failed: %s",
                agent_id, e)
            return f"0x_error_{e}"

        logger.info(
            "[erc8004] feedback(%s, score=%d, network=%s) — tx: %s",
            agent_id, score, self.network, composite_tx)

        # Per-dimension feedback (background, best-effort)
        dim_nonce = nonce + 1

        def _submit_dims():
            n = dim_nonce
            for dim, dim_score in signals.items():
                if not isinstance(dim_score, (int, float)):
                    continue
                try:
                    self._send_give_feedback(
                        chain_agent_id=chain_agent_id,
                        value=int(float(dim_score) * 100),
                        value_decimals=2,
                        tag1="cleo",
                        tag2=dim[:32],
                        endpoint="",
                        feedback_uri=feedback_uri,
                        feedback_hash=feedback_hash,
                        nonce=n,
                    )
                    n += 1
                except Exception as e:
                    logger.debug(
                        "[erc8004] dim feedback %s failed: %s", dim, e)

        Thread(target=_submit_dims, daemon=True).start()
        return composite_tx

    def _send_give_feedback(self, chain_agent_id: int, value: int,
                            value_decimals: int, tag1: str, tag2: str,
                            endpoint: str, feedback_uri: str,
                            feedback_hash: bytes, nonce: int) -> str:
        """Single giveFeedback transaction. Returns tx_hash hex."""
        tx = self._reputation_contract.functions.giveFeedback(
            chain_agent_id,
            value,           # int128
            value_decimals,  # uint8
            tag1,
            tag2,
            endpoint,
            feedback_uri,
            feedback_hash,   # bytes32
        ).build_transaction({
            "from": self._account.address,
            "nonce": nonce,
            "gas": 300000,
            "gasPrice": self._w3.eth.gas_price,
        })
        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def _submit_reputation_custom(self, agent_id: str,
                                   chain_agent_id: int,
                                   score: int,
                                   signals: dict) -> str:
        """Legacy custom Sepolia submitReputation path."""
        signals_cid = self._ipfs.upload_json(
            signals, name=f"reputation-{agent_id}-{int(time.time())}")

        try:
            tx = self._reputation_contract.functions.submitReputation(
                chain_agent_id,
                int(score),
                signals_cid,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(
                    self._account.address),
                "gas": 200000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(
                signed.raw_transaction)
            hex_hash = tx_hash.hex()

            logger.info("[erc8004] reputation(%s, score=%d) — tx: %s",
                        agent_id, score, hex_hash)
            return hex_hash

        except Exception as e:
            logger.error("[erc8004] submit_reputation(%s) failed: %s",
                         agent_id, e)
            return f"0x_error_{e}"

    # ── Read Operations ────────────────────────────────────────

    def is_registered(self, address: str) -> bool:
        """Check if an address has a registered ERC-8004 identity."""
        self._ensure_web3()
        if not self._identity_contract:
            return False

        if self._use_official:
            # Official: use ERC-721 ownerOf via chain_state cache
            try:
                from adapters.chain.chain_state import ChainState
                from web3 import Web3
                state = ChainState()
                checksum = Web3.to_checksum_address(address)
                for _aid, agent_data in state.list_agents().items():
                    aid = agent_data.get("erc8004_agent_id")
                    if aid is not None:
                        try:
                            owner = self._identity_contract.functions.ownerOf(
                                aid).call()
                            if Web3.to_checksum_address(owner) == checksum:
                                return True
                        except Exception:
                            continue
            except Exception as e:
                logger.debug("[erc8004] is_registered check failed: %s", e)
            return False
        else:
            # Custom: isRegisteredAgent(address)
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

        if self._use_official:
            # Official: no direct wallet→agentId lookup.
            # Fall back to chain_state cache.
            try:
                from adapters.chain.chain_state import ChainState
                from web3 import Web3
                state = ChainState()
                checksum = Web3.to_checksum_address(address)
                for _aid, agent_data in state.list_agents().items():
                    aid = agent_data.get("erc8004_agent_id")
                    pkp = agent_data.get("pkp_eth_address", "")
                    if aid is not None and pkp:
                        if Web3.to_checksum_address(pkp) == checksum:
                            return aid
            except Exception:
                pass
            return None
        else:
            # Custom: getAgentId(address)
            try:
                from web3 import Web3
                return self._identity_contract.functions.getAgentId(
                    Web3.to_checksum_address(address)
                ).call()
            except Exception:
                return None

    def get_reputation(self, chain_agent_id: int) -> dict:
        """
        Read on-chain reputation for an agent.
        Official: getSummary with tag1="cleo", tag2="composite"
        Custom:   getReputation(agentId)
        """
        self._ensure_web3()
        if not self._reputation_contract:
            return {"score": 0, "submissions": 0, "last_update": 0}

        if self._use_official:
            return self._get_summary_official(chain_agent_id)
        else:
            return self._get_reputation_custom(chain_agent_id)

    def _get_summary_official(self, chain_agent_id: int) -> dict:
        """Official: read composite feedback via getSummary."""
        try:
            # First get all clients that have submitted feedback
            clients = self._reputation_contract.functions.getClients(
                chain_agent_id).call()
            if not clients:
                return {"score": 0, "submissions": 0, "last_update": 0}

            count, summary_value, summary_decimals = (
                self._reputation_contract.functions.getSummary(
                    chain_agent_id, clients, "cleo", "composite"
                ).call()
            )
            # Convert back: value 8500, decimals 2 → score 85.0
            divisor = 10 ** summary_decimals if summary_decimals else 1
            score = summary_value / divisor if count else 0
            return {
                "score": round(float(score), 2),
                "submissions": int(count),
                "last_update": 0,  # official contract doesn't expose ts
            }
        except Exception as e:
            logger.warning("[erc8004] getSummary failed: %s", e)
            return {"score": 0, "submissions": 0, "last_update": 0}

    def _get_reputation_custom(self, chain_agent_id: int) -> dict:
        """Legacy custom getReputation path."""
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
                         proof: bytes, deadline: int = 0) -> str:
        """
        Migrate agent to a new wallet address.
        Official: setAgentWallet(agentId, newWallet, deadline, signature)
        Custom:   setAgentWallet(agentId, newWallet, sig)
        """
        self._ensure_web3()
        if not self._identity_contract or not self._account:
            return "0x_stub"

        try:
            from web3 import Web3
            if self._use_official:
                tx = self._identity_contract.functions.setAgentWallet(
                    chain_agent_id,
                    Web3.to_checksum_address(new_address),
                    deadline or int(time.time()) + 3600,
                    proof,
                ).build_transaction({
                    "from": self._account.address,
                    "nonce": self._w3.eth.get_transaction_count(
                        self._account.address),
                    "gas": 200000,
                    "gasPrice": self._w3.eth.gas_price,
                })
            else:
                tx = self._identity_contract.functions.setAgentWallet(
                    chain_agent_id,
                    Web3.to_checksum_address(new_address),
                    proof,
                ).build_transaction({
                    "from": self._account.address,
                    "nonce": self._w3.eth.get_transaction_count(
                        self._account.address),
                    "gas": 200000,
                    "gasPrice": self._w3.eth.gas_price,
                })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(
                signed.raw_transaction)
            return tx_hash.hex()

        except Exception as e:
            logger.error("[erc8004] setAgentWallet failed: %s", e)
            return f"0x_error_{e}"

    # ── Health ─────────────────────────────────────────────────

    def health_check(self) -> dict:
        """Check RPC connectivity and contract availability."""
        result = {
            "network": self.network,
            "official": self._use_official,
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
        # Try chain_state first (works for both official and custom)
        try:
            from adapters.chain.chain_state import ChainState
            state = ChainState()
            agent_data = state.get_agent(agent_id)
            if agent_data.get("erc8004_agent_id") is not None:
                return agent_data["erc8004_agent_id"]
        except Exception:
            pass

        # For custom network: query on-chain by PKP address
        if not self._use_official:
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
