"""
adapters/chain/gnosis_safe.py
Gnosis Safe multi-sig guardian — controls high-value on-chain operations.
Uses web3.py with Safe ABIs for multi-sig management.
"""

from __future__ import annotations
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Minimal Gnosis Safe ABIs (only functions we actually use)
SAFE_ABI = [
    {
        "inputs": [],
        "name": "getOwners",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getThreshold",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "name": "execTransaction",
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce", "type": "uint256"},
        ],
        "name": "getTransactionHash",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "dataHash", "type": "bytes32"}],
        "name": "approveHash",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

SAFE_PROXY_FACTORY_ABI = [
    {
        "inputs": [
            {"name": "_singleton", "type": "address"},
            {"name": "initializer", "type": "bytes"},
            {"name": "saltNonce", "type": "uint256"},
        ],
        "name": "createProxyWithNonce",
        "outputs": [{"name": "proxy", "type": "address"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


class GnosisSafeAdapter:
    """
    Gnosis Safe multi-sig guardian for Swarm on-chain operations.
    Controls PKP NFTs and ERC-8004 Identity NFTs.
    Day-to-day signing uses PKP (via Lit Actions); management operations
    (transfer, burn, upgrade) require Safe multi-sig approval.
    """

    def __init__(self, rpc_url: str = "", safe_address: str = "",
                 operator_key: str = ""):
        self.rpc_url = rpc_url or os.environ.get("BASE_RPC_URL", "")
        self.safe_address = safe_address or os.environ.get("GNOSIS_SAFE_ADDRESS", "")
        self.operator_key = operator_key or os.environ.get("CHAIN_PRIVATE_KEY", "")

        self._w3 = None
        self._safe_contract = None
        self._account = None

    # ── Connection ─────────────────────────────────────────────

    def _ensure_web3(self):
        """Lazy-init web3 connection."""
        if self._w3 is not None:
            return

        from web3 import Web3
        from eth_account import Account

        self._w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if not self._w3.is_connected():
            raise ConnectionError(f"Cannot connect to {self.rpc_url}")

        if self.operator_key:
            self._account = Account.from_key(self.operator_key)

        if self.safe_address:
            self._safe_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.safe_address),
                abi=SAFE_ABI,
            )

    # ── Read Operations ────────────────────────────────────────

    def get_owners(self) -> list[str]:
        """Get list of Safe owner addresses."""
        self._ensure_web3()
        if not self._safe_contract:
            return []
        try:
            return self._safe_contract.functions.getOwners().call()
        except Exception as e:
            logger.warning("[safe] getOwners failed: %s", e)
            return []

    def get_threshold(self) -> int:
        """Get required number of confirmations."""
        self._ensure_web3()
        if not self._safe_contract:
            return 0
        try:
            return self._safe_contract.functions.getThreshold().call()
        except Exception as e:
            logger.warning("[safe] getThreshold failed: %s", e)
            return 0

    def get_nonce(self) -> int:
        """Get current Safe nonce."""
        self._ensure_web3()
        if not self._safe_contract:
            return 0
        try:
            return self._safe_contract.functions.nonce().call()
        except Exception as e:
            logger.warning("[safe] nonce failed: %s", e)
            return 0

    def get_balance(self, token_address: str = "") -> str:
        """Get ETH or token balance of the Safe."""
        self._ensure_web3()
        if not self.safe_address:
            return "0.00"

        from web3 import Web3

        if not token_address:
            # ETH balance
            try:
                raw = self._w3.eth.get_balance(
                    Web3.to_checksum_address(self.safe_address)
                )
                return f"{raw / 1e18:.6f}"
            except Exception as e:
                logger.warning("[safe] ETH balance failed: %s", e)
                return "0.00"

        # ERC-20 token balance
        try:
            erc20_abi = [{
                "inputs": [{"name": "account", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }]
            token = self._w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=erc20_abi,
            )
            raw = token.functions.balanceOf(
                Web3.to_checksum_address(self.safe_address)
            ).call()
            return f"{raw / 1e6:.2f}"  # Assume 6 decimals (USDC)
        except Exception as e:
            logger.warning("[safe] Token balance failed: %s", e)
            return "0.00"

    # ── Transaction Operations ─────────────────────────────────

    def propose_transaction(self, to: str, value: int = 0,
                            data: bytes = b"") -> dict:
        """
        Propose a Safe transaction. Returns the tx hash for signing.
        Does NOT execute — requires threshold signatures first.
        """
        self._ensure_web3()
        if not self._safe_contract:
            return {"error": "Safe not configured"}

        from web3 import Web3

        nonce = self.get_nonce()
        zero_addr = "0x0000000000000000000000000000000000000000"

        try:
            tx_hash = self._safe_contract.functions.getTransactionHash(
                Web3.to_checksum_address(to),
                value,
                data,
                0,  # operation: CALL
                0,  # safeTxGas
                0,  # baseGas
                0,  # gasPrice
                Web3.to_checksum_address(zero_addr),  # gasToken
                Web3.to_checksum_address(zero_addr),  # refundReceiver
                nonce,
            ).call()

            return {
                "safe_tx_hash": tx_hash.hex(),
                "to": to,
                "value": value,
                "nonce": nonce,
            }

        except Exception as e:
            logger.error("[safe] propose_transaction failed: %s", e)
            return {"error": str(e)}

    def approve_hash(self, safe_tx_hash: str) -> str:
        """
        Approve a Safe transaction hash (as the operator).
        Returns the approval tx hash.
        """
        self._ensure_web3()
        if not self._safe_contract or not self._account:
            return "0x_stub"

        try:
            tx = self._safe_contract.functions.approveHash(
                bytes.fromhex(safe_tx_hash.replace("0x", ""))
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 100000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            return tx_hash.hex()

        except Exception as e:
            logger.error("[safe] approveHash failed: %s", e)
            return f"0x_error_{e}"

    # ── Execute Transaction (Multi-sig) ─────────────────────────

    def exec_transaction(self, to: str, value: int = 0,
                         data: bytes = b"",
                         signatures: bytes = b"") -> str:
        """
        Execute a Safe transaction with aggregated signatures.
        Requires threshold number of owner approvals.
        Returns tx_hash on success.
        """
        self._ensure_web3()
        if not self._safe_contract or not self._account:
            return "0x_not_configured"

        from web3 import Web3
        zero_addr = "0x0000000000000000000000000000000000000000"

        try:
            tx = self._safe_contract.functions.execTransaction(
                Web3.to_checksum_address(to),
                value,
                data,
                0,  # operation: CALL
                0,  # safeTxGas
                0,  # baseGas
                0,  # gasPrice
                Web3.to_checksum_address(zero_addr),
                Web3.to_checksum_address(zero_addr),
                signatures,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 500000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            hex_hash = tx_hash.hex()

            logger.info("[safe] execTransaction to %s — tx: %s", to, hex_hash)
            return hex_hash

        except Exception as e:
            logger.error("[safe] execTransaction failed: %s", e)
            return f"0x_error_{e}"

    def collect_and_execute(self, to: str, value: int = 0,
                             data: bytes = b"") -> dict:
        """
        Full multi-sig flow:
        1. Propose transaction -> get safe_tx_hash
        2. Approve hash (as operator)
        3. Attempt execution if threshold met
        """
        proposal = self.propose_transaction(to, value, data)
        if "error" in proposal:
            return {"status": "error", "detail": proposal["error"]}

        safe_tx_hash = proposal["safe_tx_hash"]
        approval_tx = self.approve_hash(safe_tx_hash)

        threshold = self.get_threshold()
        owners = self.get_owners()

        result = {
            "status": "pending",
            "safe_tx_hash": safe_tx_hash,
            "approval_tx": approval_tx,
            "threshold": threshold,
            "owners": len(owners),
        }

        # If threshold is 1, execute immediately with approved-hash signature
        if threshold <= 1 and self._account:
            from web3 import Web3
            addr_bytes = bytes.fromhex(self._account.address[2:])
            # r must be exactly 32 bytes: 12 zero-padding + 20-byte address
            r = (b'\x00' * 12 + addr_bytes)[:32]
            s = b'\x00' * 32
            v = b'\x01'
            sig = r + s + v  # 32 + 32 + 1 = 65 bytes

            exec_tx = self.exec_transaction(to, value, data, sig)
            result["status"] = "executed" if not exec_tx.startswith("0x_") else "failed"
            result["exec_tx"] = exec_tx

        return result

    # ── Deploy ─────────────────────────────────────────────────

    def deploy_safe(self, owners: list[str], threshold: int,
                    factory_address: str = "") -> str:
        """
        Deploy a new Gnosis Safe proxy.
        Returns the proxy (Safe) address.
        """
        self._ensure_web3()
        if not self._account:
            return "0x_no_operator_key"

        from web3 import Web3

        # Load factory address from chain_contracts.json if not provided
        if not factory_address:
            try:
                contracts_path = os.path.join("config", "chain_contracts.json")
                with open(contracts_path) as f:
                    contracts = json.load(f)
                # Detect network from chain_id
                chain_id = self._w3.eth.chain_id
                net = "base-sepolia" if chain_id == 84532 else "base"
                factory_address = contracts["contracts"]["gnosis_safe_proxy_factory"].get(net, "")
            except Exception:
                pass

        if not factory_address:
            return "0x_no_factory"

        try:
            # Safe singleton address (v1.3.0 on Base)
            safe_singleton = "0xd9Db270c1B5E3Bd161E8c8503c55cEABeE709552"

            # Encode setup call
            safe_setup_abi = [{
                "inputs": [
                    {"name": "_owners", "type": "address[]"},
                    {"name": "_threshold", "type": "uint256"},
                    {"name": "to", "type": "address"},
                    {"name": "data", "type": "bytes"},
                    {"name": "fallbackHandler", "type": "address"},
                    {"name": "paymentToken", "type": "address"},
                    {"name": "payment", "type": "uint256"},
                    {"name": "paymentReceiver", "type": "address"},
                ],
                "name": "setup",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            }]

            zero = "0x0000000000000000000000000000000000000000"
            checksum_owners = [Web3.to_checksum_address(o) for o in owners]

            setup_contract = self._w3.eth.contract(abi=safe_setup_abi)
            initializer = setup_contract.encode_abi(
                "setup",
                [
                    checksum_owners,
                    threshold,
                    Web3.to_checksum_address(zero),
                    b"",
                    Web3.to_checksum_address(zero),
                    Web3.to_checksum_address(zero),
                    0,
                    Web3.to_checksum_address(zero),
                ],
            )

            # Create proxy via factory
            factory = self._w3.eth.contract(
                address=Web3.to_checksum_address(factory_address),
                abi=SAFE_PROXY_FACTORY_ABI,
            )

            import time
            salt_nonce = int(time.time())

            tx = factory.functions.createProxyWithNonce(
                Web3.to_checksum_address(safe_singleton),
                bytes.fromhex(initializer[2:]),  # strip 0x
                salt_nonce,
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 500000,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)

            # Wait for receipt to get the proxy address
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.get("status") == 1 and receipt.get("logs"):
                # The ProxyCreation event contains the proxy address
                proxy_addr = receipt["logs"][0]["address"]
                logger.info("[safe] Deployed Safe at %s (tx: %s)",
                           proxy_addr, tx_hash.hex())
                return proxy_addr

            return tx_hash.hex()

        except Exception as e:
            logger.error("[safe] deploy_safe failed: %s", e)
            return f"0x_error_{e}"

    # ── Health ─────────────────────────────────────────────────

    def health_check(self) -> dict:
        """Check Safe connectivity and configuration."""
        result = {
            "safe_address": self.safe_address or "not configured",
            "has_operator_key": bool(self.operator_key),
        }
        if not self.safe_address:
            result["status"] = "not_configured"
            return result

        try:
            self._ensure_web3()
            owners = self.get_owners()
            threshold = self.get_threshold()
            result["connected"] = True
            result["owners"] = len(owners)
            result["threshold"] = threshold
            result["nonce"] = self.get_nonce()
            result["status"] = "ok"
        except Exception as e:
            result["connected"] = False
            result["status"] = f"error: {e}"

        return result

    def get_info(self) -> dict:
        """Return Safe info for dashboard."""
        return {
            "address": self.safe_address,
            "configured": bool(self.safe_address),
            "owners": self.get_owners() if self.safe_address else [],
            "threshold": self.get_threshold() if self.safe_address else 0,
        }
