"""
adapters/chain/lit_pkp.py
Lit Protocol PKP adapter — decentralized key management via Naga network.
Uses lit-python-sdk for PKP minting, signing, and Lit Action execution.
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LitPKPAdapter:
    """
    Manages Lit Protocol PKP operations for Swarm agents.
    Each agent can have its own PKP for signing transactions.
    """

    def __init__(self, network: str = "naga-dev",
                 operator_key: str = "",
                 debug: bool = False):
        self.network = network
        self.operator_key = operator_key or os.environ.get("CHAIN_PRIVATE_KEY", "")
        self.debug = debug
        self._client = None
        self._connected = False
        self._session_sigs_cache: dict[str, tuple[dict, float]] = {}
        self._session_ttl = 600  # 10 minutes

    # ── Lifecycle ──────────────────────────────────────────────

    def connect(self):
        """Connect to Lit Naga network."""
        if self._connected:
            return

        try:
            from lit_python_sdk import LitClient
        except ImportError:
            logger.error("lit-python-sdk not installed. Run: pip install lit-python-sdk")
            raise

        self._client = LitClient()
        self._client.new(lit_network=self.network, debug=self.debug)
        self._client.connect()

        # Initialize contracts client for minting
        if self.operator_key:
            self._client.new_lit_contracts_client(
                private_key=self.operator_key,
                network=self.network,
                debug=self.debug,
            )

        self._connected = True
        logger.info("[lit] Connected to %s", self.network)

    def disconnect(self):
        """Disconnect from Lit network."""
        if self._client and self._connected:
            try:
                self._client.disconnect()
            except Exception as e:
                logger.warning("[lit] Disconnect error: %s", e)
            self._connected = False

    def _ensure_connected(self):
        if not self._connected:
            self.connect()

    # ── PKP Minting ────────────────────────────────────────────

    def mint_pkp(self, scopes: list[int] = None) -> dict:
        """
        Mint a new PKP on the Lit network.
        Returns: {token_id, public_key, eth_address}
        """
        self._ensure_connected()
        if scopes is None:
            scopes = [1, 2]  # SignAnything, PersonalSign

        auth_method = {
            "authMethodType": 1,  # EthWallet
            "accessToken": "",
        }

        result = self._client.mint_with_auth(
            auth_method=auth_method,
            scopes=scopes,
        )

        # Normalize response — SDK may nest under "pkp"
        pkp = result.get("pkp", result)
        info = {
            "token_id": pkp.get("tokenId", ""),
            "public_key": pkp.get("publicKey", ""),
            "eth_address": pkp.get("ethAddress", ""),
        }

        logger.info("[lit] Minted PKP: %s", info["eth_address"])
        return info

    # ── Session Management ─────────────────────────────────────

    def get_session_sigs(self, pkp_public_key: str,
                         chain: str = "base") -> dict:
        """
        Get session signatures for a PKP. Cached for 10 minutes.
        """
        self._ensure_connected()

        cache_key = f"{pkp_public_key}:{chain}"
        if cache_key in self._session_sigs_cache:
            sigs, expires = self._session_sigs_cache[cache_key]
            if time.time() < expires:
                return sigs

        expiration = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + self._session_ttl),
        )

        sigs = self._client.get_session_sigs(
            chain=chain,
            expiration=expiration,
            resource_ability_requests=[{
                "resource": {"resource": "*", "resourcePrefix": "lit-pkp"},
                "ability": "pkp-signing",
            }],
        )

        self._session_sigs_cache[cache_key] = (sigs, time.time() + self._session_ttl - 30)
        return sigs

    # ── Signing ────────────────────────────────────────────────

    def sign_message(self, pkp_public_key: str, message: bytes,
                     chain: str = "base") -> dict:
        """
        Sign a message using a PKP.
        Returns: {signature, recid, publicKey}
        """
        self._ensure_connected()
        session_sigs = self.get_session_sigs(pkp_public_key, chain)

        to_sign = list(message) if isinstance(message, bytes) else message

        result = self._client.pkp_sign(
            pub_key=pkp_public_key,
            to_sign=to_sign,
            session_sigs=session_sigs,
        )

        return result

    def sign_transaction(self, pkp_public_key: str, tx_data: dict,
                         chain: str = "base") -> str:
        """
        Sign an EVM transaction using a PKP via Lit Action.
        Returns: signed transaction hex
        """
        self._ensure_connected()
        session_sigs = self.get_session_sigs(pkp_public_key, chain)

        # Use Lit Action to sign the transaction
        code = """
        const ethers = Lit.Actions.importModule("ethers");
        const tx = JSON.parse(params.txData);
        const serialized = ethers.utils.serializeTransaction(tx);
        const hash = ethers.utils.keccak256(serialized);
        const toSign = ethers.utils.arrayify(hash);

        const sig = await Lit.Actions.signEcdsa({
            toSign,
            publicKey: params.pkpPublicKey,
            sigName: "txSig",
        });
        """

        result = self._client.execute_js(
            code=code,
            js_params={
                "txData": json.dumps(tx_data),
                "pkpPublicKey": pkp_public_key,
            },
            session_sigs=session_sigs,
        )

        return result.get("signatures", {}).get("txSig", {}).get("signature", "")

    # ── Lit Action Execution ───────────────────────────────────

    def execute_lit_action(self, code: str = None,
                           ipfs_cid: str = None,
                           js_params: dict = None,
                           pkp_public_key: str = None,
                           chain: str = "base") -> dict:
        """
        Execute a Lit Action (JS code) on the Lit network.
        Either provide code directly or an IPFS CID.
        """
        self._ensure_connected()
        session_sigs = None
        if pkp_public_key:
            session_sigs = self.get_session_sigs(pkp_public_key, chain)

        kwargs = {}
        if code:
            kwargs["code"] = code
        elif ipfs_cid:
            kwargs["ipfs_id"] = ipfs_cid
        else:
            raise ValueError("Must provide either code or ipfs_cid")

        if js_params:
            kwargs["js_params"] = js_params
        if session_sigs:
            kwargs["session_sigs"] = session_sigs

        return self._client.execute_js(**kwargs)

    # ── Health ─────────────────────────────────────────────────

    def health_check(self) -> dict:
        """Check Lit network connectivity and PKP status."""
        result = {
            "connected": self._connected,
            "network": self.network,
            "has_operator_key": bool(self.operator_key),
        }

        if not self._connected:
            try:
                self.connect()
                result["connected"] = True
                result["status"] = "ok"
            except Exception as e:
                result["status"] = f"connection_failed: {e}"
                return result

        result["status"] = "ok"
        return result
