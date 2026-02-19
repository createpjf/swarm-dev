"""
adapters/chain/x402_client.py
x402 payment protocol — outbound payment client.
Automatically handles HTTP 402 (Payment Required) responses
by constructing USDC payment authorizations signed via PKP.
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class X402Client:
    """
    HTTP client wrapper that handles x402 payment flows.
    When a request returns 402, it parses payment requirements,
    constructs a payment authorization, signs it with PKP, and retries.
    """

    def __init__(self, lit_adapter=None, pkp_public_key: str = "",
                 usdc_address: str = "",
                 facilitator_url: str = "https://x402.coinbase.com",
                 max_payment_usd: float = 0.10):
        self.lit = lit_adapter
        self.pkp_public_key = pkp_public_key
        self.usdc_address = usdc_address or os.environ.get(
            "USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        )
        self.facilitator_url = facilitator_url
        self.max_payment_usd = max_payment_usd

    async def fetch(self, url: str, method: str = "GET",
                    headers: dict = None, body: Any = None,
                    **kwargs) -> dict:
        """
        Make an HTTP request. If 402 is returned, automatically pay and retry.
        Returns: {status, headers, body}
        """
        import httpx

        req_headers = dict(headers or {})
        async with httpx.AsyncClient() as client:
            # First attempt
            response = await client.request(
                method, url, headers=req_headers,
                content=body if isinstance(body, (str, bytes)) else json.dumps(body) if body else None,
                **kwargs,
            )

            if response.status_code != 402:
                return {
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body": response.text,
                }

            # Parse 402 payment requirements
            requirements = self._parse_payment_requirements(response)
            if not requirements:
                logger.warning("[x402] 402 received but no payment requirements found")
                return {
                    "status": 402,
                    "headers": dict(response.headers),
                    "body": response.text,
                    "error": "no_payment_requirements",
                }

            # Check amount limit
            amount_usd = float(requirements.get("amount", "0"))
            if amount_usd > self.max_payment_usd:
                logger.warning("[x402] Payment amount $%.4f exceeds limit $%.2f",
                             amount_usd, self.max_payment_usd)
                return {
                    "status": 402,
                    "error": "amount_exceeds_limit",
                    "required": amount_usd,
                    "limit": self.max_payment_usd,
                }

            # Construct and sign payment
            payment_header = await self._construct_payment(requirements)
            if not payment_header:
                return {
                    "status": 402,
                    "error": "payment_construction_failed",
                }

            # Retry with payment
            req_headers["X-PAYMENT"] = payment_header
            response = await client.request(
                method, url, headers=req_headers,
                content=body if isinstance(body, (str, bytes)) else json.dumps(body) if body else None,
                **kwargs,
            )

            return {
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": response.text,
                "payment_made": True,
                "payment_amount_usd": amount_usd,
            }

    def _parse_payment_requirements(self, response) -> Optional[dict]:
        """Parse x402 payment requirements from 402 response."""
        # x402 puts requirements in response body or specific headers
        try:
            body = response.json()
            if "paymentRequirements" in body:
                reqs = body["paymentRequirements"]
                if isinstance(reqs, list) and reqs:
                    return reqs[0]  # Take first payment option
                return reqs
        except Exception:
            pass

        # Try headers
        pay_header = response.headers.get("X-PAYMENT-REQUIRED", "")
        if pay_header:
            try:
                return json.loads(pay_header)
            except Exception:
                pass

        return None

    async def _construct_payment(self, requirements: dict) -> Optional[str]:
        """
        Construct payment authorization using PKP signature.
        Uses EIP-3009 transferWithAuthorization for USDC.
        """
        if not self.lit or not self.pkp_public_key:
            logger.error("[x402] No PKP configured for payment signing")
            return None

        try:
            # Build EIP-3009 transferWithAuthorization payload
            payment_data = {
                "type": "transferWithAuthorization",
                "token": self.usdc_address,
                "from": requirements.get("receiverAddress", ""),
                "to": requirements.get("receiverAddress", ""),
                "value": requirements.get("amount", "0"),
                "validAfter": 0,
                "validBefore": int(time.time()) + 300,  # 5 min validity
                "nonce": os.urandom(32).hex(),
            }

            # Sign with PKP via Lit Action
            message = json.dumps(payment_data).encode()
            sig_result = self.lit.sign_message(
                self.pkp_public_key, message
            )

            # Encode as x402 payment header
            payment_header = json.dumps({
                "payment": payment_data,
                "signature": sig_result.get("signature", ""),
            })

            return payment_header

        except Exception as e:
            logger.error("[x402] Payment construction failed: %s", e)
            return None
