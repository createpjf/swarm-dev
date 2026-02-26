"""
adapters/memo/config.py — MemoConfig: Memo Protocol integration configuration.

Reads from the ``memo:`` section of ``config/agents.yaml``.
Sensitive values (API key, wallet, private key) are resolved from
environment variables at runtime, following the same ``*_env`` pattern
used by ``adapters/memory/embedding.py``.

When the ``memo:`` section is absent, ``MemoConfig()`` returns a default
instance with ``enabled=False`` — all Memo features are silently disabled.
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MemoConfig:
    """Memo Protocol integration settings."""

    # ── master switch ─────────────────────────────────────────────────────
    enabled: bool = False

    # ── API connection ────────────────────────────────────────────────────
    api_base_url: str = "https://api.memo.ac"
    api_key: str = ""                  # resolved from env
    wallet_address: str = ""           # resolved from env
    private_key: str = ""              # resolved from env

    # ── identity ──────────────────────────────────────────────────────────
    erc8004_agent_id: str = ""
    display_name: str = "Cleo Agent"

    # ── auto-upload (post-task hook) ──────────────────────────────────────
    auto_upload_enabled: bool = False
    auto_upload_min_quality: float = 0.6
    auto_upload_types: list[str] = field(
        default_factory=lambda: ["procedural", "semantic"])

    # ── export defaults ───────────────────────────────────────────────────
    default_domain: str = "python"
    default_language: str = "zh"
    default_access_tier: str = "developer"   # free | developer | team
    default_price_usdc: float = 0.0

    # ── deidentification ──────────────────────────────────────────────────
    deidentification_use_llm: bool = False
    deidentification_llm_model: str = "minimax-m2.5"

    # ── skill sync ────────────────────────────────────────────────────────
    skill_sync_enabled: bool = False
    skill_sync_interval_hours: int = 24

    # ── company names to redact (user-configurable) ───────────────────────
    company_names: list[str] = field(default_factory=list)

    # ──────────────────────────────────────────────────────────────────────
    #  Factory
    # ──────────────────────────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, config: dict[str, Any] | None) -> "MemoConfig":
        """Build MemoConfig from the top-level ``config/agents.yaml`` dict.

        If the ``memo:`` key is missing, returns a default (disabled) instance.
        """
        if not config:
            return cls()

        memo: dict = config.get("memo", {})
        if not memo:
            return cls()

        # Resolve sensitive values from environment variables
        def _env(key: str) -> str:
            env_name = memo.get(key, "")
            return os.environ.get(env_name, "") if env_name else ""

        auto = memo.get("auto_upload", {})
        export = memo.get("export", {})
        deident = memo.get("deidentification", {})
        skill = memo.get("skill_sync", {})

        cfg = cls(
            enabled=memo.get("enabled", False),
            api_base_url=memo.get("api_base_url", cls.api_base_url),
            api_key=_env("api_key_env"),
            wallet_address=_env("wallet_address_env"),
            private_key=_env("private_key_env"),
            erc8004_agent_id=memo.get("erc8004_agent_id", ""),
            display_name=memo.get("display_name", cls.display_name),
            # auto-upload
            auto_upload_enabled=auto.get("enabled", False),
            auto_upload_min_quality=auto.get("min_quality", 0.6),
            auto_upload_types=auto.get("types", ["procedural", "semantic"]),
            # export
            default_domain=export.get("default_domain", cls.default_domain),
            default_language=export.get("default_language", cls.default_language),
            default_access_tier=export.get("default_access_tier",
                                           cls.default_access_tier),
            default_price_usdc=export.get("default_price_usdc", 0.0),
            # deidentification
            deidentification_use_llm=deident.get("use_llm", False),
            deidentification_llm_model=deident.get("llm_model",
                                                    cls.deidentification_llm_model),
            # skill sync
            skill_sync_enabled=skill.get("enabled", False),
            skill_sync_interval_hours=skill.get("interval_hours", 24),
            # company names
            company_names=memo.get("company_names", []),
        )

        if cfg.enabled:
            missing = []
            if not cfg.api_key:
                missing.append("api_key_env")
            if not cfg.wallet_address:
                missing.append("wallet_address_env")
            if missing:
                logger.warning("[memo] enabled but missing env vars: %s",
                               ", ".join(missing))
        return cfg

    # ──────────────────────────────────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────────────────────────────────

    @property
    def author_info(self) -> dict:
        """Author block for Memo MemoryObject."""
        return {
            "erc8004_agent_id": self.erc8004_agent_id,
            "wallet_address": self.wallet_address,
            "display_name": self.display_name,
        }

    @property
    def default_access(self) -> dict:
        """Default access control block for Memo MemoryObject."""
        return {
            "tier": self.default_access_tier,
            "price_usdc": self.default_price_usdc,
            "subscription_bypass": True,
        }
