"""
adapters/chain/x402_middleware.py
x402 payment protocol — inbound payment gating middleware.
Protects gateway endpoints behind micropayments (USDC on Base).
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class X402Middleware:
    """
    HTTP middleware that gates endpoints behind x402 micropayments.
    When enabled, certain endpoints require valid payment headers.
    """

    def __init__(self, config: dict = None):
        x402_cfg = (config or {}).get("chain", {}).get("x402", {})
        self.enabled = x402_cfg.get("enabled", False)
        self.facilitator_url = x402_cfg.get(
            "facilitator_url", "https://x402.coinbase.com"
        )
        self.receiver_address = os.environ.get("X402_RECEIVER_ADDRESS", "")

        # endpoint -> {price_usd, description, network}
        self._gated_endpoints: dict[str, dict] = {}

        # Load endpoints from config
        for ep in x402_cfg.get("endpoints", []):
            if isinstance(ep, dict):
                self._gated_endpoints[ep["path"]] = {
                    "price_usd": float(ep.get("price_usd", "0.01")),
                    "description": ep.get("description", ""),
                    "network": ep.get("network", "base-mainnet"),
                }

    def gate_endpoint(self, path: str, price_usd: float,
                      description: str = ""):
        """Register an endpoint as requiring x402 payment."""
        self._gated_endpoints[path] = {
            "price_usd": price_usd,
            "description": description,
            "network": "base-mainnet",
        }

    def is_gated(self, path: str) -> bool:
        """Check if a path is gated by x402."""
        if not self.enabled:
            return False
        return path in self._gated_endpoints

    def check_payment(self, path: str, headers: dict) -> tuple[bool, dict]:
        """
        Check if a request has valid x402 payment.
        Returns: (is_paid, response_data)
          - If paid: (True, {})
          - If not paid: (False, {status_code, headers, body})
        """
        if not self.enabled or path not in self._gated_endpoints:
            return True, {}

        payment_header = headers.get("X-PAYMENT", headers.get("x-payment", ""))

        if not payment_header:
            # Return 402 with payment requirements
            endpoint_config = self._gated_endpoints[path]
            return False, self._build_402_response(path, endpoint_config)

        # Verify payment with facilitator
        if self._verify_payment(payment_header):
            return True, {}

        return False, {
            "status_code": 402,
            "body": {"error": "invalid_payment", "message": "Payment verification failed"},
        }

    def _build_402_response(self, path: str, config: dict) -> dict:
        """Build a 402 Payment Required response."""
        return {
            "status_code": 402,
            "headers": {
                "X-PAYMENT-REQUIRED": json.dumps({
                    "paymentRequirements": [{
                        "type": "x402",
                        "network": config.get("network", "base-mainnet"),
                        "token": "USDC",
                        "amount": str(config.get("price_usd", "0.01")),
                        "receiverAddress": self.receiver_address,
                        "description": config.get("description", f"Access to {path}"),
                        "facilitatorUrl": self.facilitator_url,
                    }]
                }),
            },
            "body": {
                "error": "payment_required",
                "paymentRequirements": [{
                    "type": "x402",
                    "network": config.get("network", "base-mainnet"),
                    "token": "USDC",
                    "amount": str(config.get("price_usd", "0.01")),
                    "receiverAddress": self.receiver_address,
                    "description": config.get("description", f"Access to {path}"),
                }],
            },
        }

    def _verify_payment(self, payment_header: str) -> bool:
        """Verify payment with the x402 facilitator service."""
        try:
            import httpx

            response = httpx.post(
                f"{self.facilitator_url}/verify",
                json={"payment": payment_header},
                timeout=10,
            )
            return response.status_code == 200
        except Exception as e:
            logger.error("[x402] Facilitator verification failed: %s", e)
            return False

    def get_info(self) -> dict:
        """Return info about gated endpoints for dashboard."""
        return {
            "enabled": self.enabled,
            "facilitator_url": self.facilitator_url,
            "receiver_address": self.receiver_address,
            "gated_endpoints": {
                path: {
                    "price_usd": cfg["price_usd"],
                    "description": cfg["description"],
                }
                for path, cfg in self._gated_endpoints.items()
            },
        }
