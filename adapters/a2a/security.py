"""
adapters/a2a/security.py — A2A Security Filter with 3-tier trust model.

Responsibilities:
  - Trust tiers:  verified / community / untrusted
  - Outbound:     sanitize content before sending to external agents
  - Inbound:      validate + filter responses from external agents
  - Redaction:    strip API keys, tokens, private keys from outbound messages

The SecurityFilter is used by both A2A Client (outbound) and A2A Bridge (inbound).
Agent internals remain unaware of security tiers — filtering happens at the adapter layer.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Trust Tiers
# ══════════════════════════════════════════════════════════════════════════════

class TrustLevel:
    """3-tier trust model for external agents."""
    VERIFIED = "verified"       # Pre-registered, authenticated agents
    COMMUNITY = "community"     # Known agents from registries, not fully verified
    UNTRUSTED = "untrusted"     # Unknown agents, max caution

    ALL = {VERIFIED, COMMUNITY, UNTRUSTED}


@dataclass
class TrustPolicy:
    """Per-tier security policy defining what's allowed."""
    allow_file_send: bool = False       # Can we send files to this agent?
    allow_file_receive: bool = False    # Can we receive files from this agent?
    max_text_length: int = 50000        # Max text length in messages
    max_rounds: int = 10                # Max input-required rounds
    require_confirmation: bool = True   # Require user confirmation for actions?
    redact_outbound: bool = True        # Redact secrets from outbound messages?
    score_penalty: int = 0              # CritiqueSpec score penalty

    @classmethod
    def for_level(cls, level: str) -> TrustPolicy:
        """Get the policy for a trust level."""
        if level == TrustLevel.VERIFIED:
            return cls(
                allow_file_send=True,
                allow_file_receive=True,
                max_text_length=100000,
                max_rounds=20,
                require_confirmation=False,
                redact_outbound=True,      # Always redact secrets
                score_penalty=0,
            )
        elif level == TrustLevel.COMMUNITY:
            return cls(
                allow_file_send=False,
                allow_file_receive=True,
                max_text_length=50000,
                max_rounds=10,
                require_confirmation=False,
                redact_outbound=True,
                score_penalty=1,           # calibration -1
            )
        else:  # UNTRUSTED
            return cls(
                allow_file_send=False,
                allow_file_receive=False,
                max_text_length=20000,
                max_rounds=3,
                require_confirmation=True,
                redact_outbound=True,
                score_penalty=2,           # all dimensions -2
            )


# ══════════════════════════════════════════════════════════════════════════════
#  Sensitive pattern detection
# ══════════════════════════════════════════════════════════════════════════════

# Patterns that indicate sensitive content (API keys, tokens, private keys)
_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # API keys (generic)
    ("api_key", re.compile(
        r'(?:api[_-]?key|apikey)\s*[:=]\s*["\']?([A-Za-z0-9_\-]{20,})',
        re.IGNORECASE)),
    # Bearer tokens
    ("bearer_token", re.compile(
        r'(?:bearer|token|auth)\s*[:=]\s*["\']?([A-Za-z0-9_\-\.]{20,})',
        re.IGNORECASE)),
    # Private keys (hex)
    ("private_key_hex", re.compile(
        r'(?:private[_-]?key|secret[_-]?key)\s*[:=]\s*["\']?(0x[a-fA-F0-9]{64})',
        re.IGNORECASE)),
    # Private keys (PEM)
    ("private_key_pem", re.compile(
        r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----',
        re.IGNORECASE)),
    # Mnemonics (12 or 24 word seed phrases — simplified detection)
    ("mnemonic", re.compile(
        r'(?:mnemonic|seed)\s*[:=]\s*["\']?([a-z]+(?:\s+[a-z]+){11,23})',
        re.IGNORECASE)),
    # AWS keys
    ("aws_key", re.compile(
        r'(?:AKIA|ASIA)[A-Z0-9]{16}',
        re.IGNORECASE)),
    # Environment variable references with sensitive names
    ("env_secret", re.compile(
        r'(?:export\s+)?(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)\s*=\s*["\']?([^\s"\']+)',
        re.IGNORECASE)),
]

# Content patterns that indicate potential injection attacks
_INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Prompt injection attempts
    ("prompt_injection", re.compile(
        r'(?:ignore\s+(?:all\s+)?previous\s+instructions|'
        r'system\s*:\s*you\s+are|'
        r'forget\s+(?:all\s+)?(?:your\s+)?instructions|'
        r'new\s+system\s+prompt)',
        re.IGNORECASE)),
    # Command injection
    ("command_injection", re.compile(
        r'(?:;\s*(?:rm|del|format|sudo|chmod|chown|curl|wget)\s|'
        r'\|\s*(?:bash|sh|zsh|python|node)\s)',
        re.IGNORECASE)),
    # Encoded payloads
    ("encoded_payload", re.compile(
        r'(?:eval\s*\(\s*(?:atob|Buffer\.from|base64\.decode))',
        re.IGNORECASE)),
]


# ══════════════════════════════════════════════════════════════════════════════
#  Security Filter
# ══════════════════════════════════════════════════════════════════════════════

class SecurityFilter:
    """Bidirectional security filter for A2A communications.

    Usage::

        sf = SecurityFilter(config)
        # Outbound: sanitize before sending to external agent
        clean_text = sf.sanitize_outbound(text, trust_level="community")
        # Inbound: validate response from external agent
        result = sf.validate_inbound(response_text, trust_level="community")
    """

    def __init__(self, config: dict = None):
        """
        Args:
            config: a2a.client.security config section.
        """
        config = config or {}
        self._redact_patterns = config.get("redact_patterns", True)
        self._untrusted_require_confirmation = config.get(
            "untrusted_require_confirmation", True)
        self._max_timeout = config.get("max_timeout", 600)

        # Custom redaction patterns from config
        self._custom_redact: list[re.Pattern] = []
        for pattern_str in config.get("custom_redact_patterns", []):
            try:
                self._custom_redact.append(re.compile(pattern_str, re.IGNORECASE))
            except re.error:
                logger.warning("[a2a:security] invalid redact pattern: %s",
                               pattern_str)

        logger.debug("[a2a:security] initialized (redact=%s, confirm_untrusted=%s)",
                     self._redact_patterns, self._untrusted_require_confirmation)

    # ── Outbound sanitization ─────────────────────────────────────────────

    def sanitize_outbound(self, text: str,
                          trust_level: str = TrustLevel.UNTRUSTED) -> str:
        """Sanitize text before sending to an external agent.

        Args:
            text: Raw text to sanitize.
            trust_level: Trust level of the target agent.

        Returns:
            Sanitized text with secrets redacted.
        """
        if not text:
            return text

        policy = TrustPolicy.for_level(trust_level)

        # 1. Redact sensitive patterns
        if policy.redact_outbound and self._redact_patterns:
            text = self._redact_secrets(text)

        # 2. Truncate if needed
        if len(text) > policy.max_text_length:
            text = text[:policy.max_text_length] + "\n[truncated]"
            logger.info("[a2a:security] outbound text truncated to %d chars",
                        policy.max_text_length)

        # 3. Strip internal markers
        text = self._strip_internal_markers(text)

        return text

    def can_send_files(self, trust_level: str) -> bool:
        """Check if files can be sent to this trust level."""
        return TrustPolicy.for_level(trust_level).allow_file_send

    def can_receive_files(self, trust_level: str) -> bool:
        """Check if files can be received from this trust level."""
        return TrustPolicy.for_level(trust_level).allow_file_receive

    # ── Inbound validation ────────────────────────────────────────────────

    def validate_inbound(self, text: str,
                         trust_level: str = TrustLevel.UNTRUSTED
                         ) -> InboundValidation:
        """Validate and filter inbound response from external agent.

        Args:
            text: Response text from external agent.
            trust_level: Trust level of the source agent.

        Returns:
            InboundValidation with clean text, warnings, and block status.
        """
        if not text:
            return InboundValidation(text="", clean=True)

        policy = TrustPolicy.for_level(trust_level)
        warnings: list[str] = []
        blocked = False

        # 1. Check for injection attempts
        injection_hits = self._check_injections(text)
        if injection_hits:
            warnings.extend(
                f"injection detected: {name}" for name in injection_hits)
            if trust_level == TrustLevel.UNTRUSTED:
                blocked = True
                logger.warning(
                    "[a2a:security] BLOCKED inbound from untrusted: %s",
                    injection_hits)

        # 2. Truncate excessive responses
        if len(text) > policy.max_text_length:
            text = text[:policy.max_text_length] + "\n[truncated by security filter]"
            warnings.append(f"response truncated to {policy.max_text_length} chars")

        # 3. Strip any secrets that shouldn't be in responses
        secret_count = len(self._find_secrets(text))
        if secret_count > 0:
            warnings.append(f"response contains {secret_count} potential secrets")
            # Don't redact inbound — just warn. The content might legitimately
            # contain API-key-like strings (e.g. documentation about keys)

        return InboundValidation(
            text=text,
            clean=len(warnings) == 0,
            blocked=blocked,
            warnings=warnings,
            score_penalty=policy.score_penalty,
        )

    def get_max_rounds(self, trust_level: str) -> int:
        """Max input-required negotiation rounds for this trust level."""
        return TrustPolicy.for_level(trust_level).max_rounds

    def requires_confirmation(self, trust_level: str) -> bool:
        """Whether this trust level requires user confirmation."""
        if not self._untrusted_require_confirmation:
            return False
        return TrustPolicy.for_level(trust_level).require_confirmation

    def get_score_penalty(self, trust_level: str) -> int:
        """CritiqueSpec score penalty for this trust level."""
        return TrustPolicy.for_level(trust_level).score_penalty

    # ── Internal helpers ──────────────────────────────────────────────────

    def _redact_secrets(self, text: str) -> str:
        """Replace sensitive patterns with [REDACTED]."""
        for name, pattern in _SENSITIVE_PATTERNS:
            if pattern.search(text):
                text = pattern.sub(f"[REDACTED:{name}]", text)
                logger.debug("[a2a:security] redacted %s pattern", name)

        # Custom patterns
        for pattern in self._custom_redact:
            if pattern.search(text):
                text = pattern.sub("[REDACTED:custom]", text)

        return text

    def _find_secrets(self, text: str) -> list[str]:
        """Find (but don't redact) sensitive patterns."""
        found = []
        for name, pattern in _SENSITIVE_PATTERNS:
            if pattern.search(text):
                found.append(name)
        return found

    def _check_injections(self, text: str) -> list[str]:
        """Check for injection attack patterns."""
        hits = []
        for name, pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                hits.append(name)
        return hits

    def _strip_internal_markers(self, text: str) -> str:
        """Remove Cleo internal markers that shouldn't leak to external agents."""
        # Strip [A2A source: ...] markers
        text = re.sub(r'\[A2A source: [^\]]+\]\s*', '', text)
        # Strip [SubTaskSpec] markers
        text = re.sub(r'\[SubTaskSpec\]\s*', '', text)
        # Strip internal Cleo references
        text = re.sub(r'\[cleo_task_id: [^\]]+\]\s*', '', text)
        return text


# ══════════════════════════════════════════════════════════════════════════════
#  Inbound Validation Result
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class InboundValidation:
    """Result of inbound security validation."""
    text: str = ""                          # Validated (possibly truncated) text
    clean: bool = True                      # No warnings?
    blocked: bool = False                   # Content blocked?
    warnings: list[str] = field(default_factory=list)
    score_penalty: int = 0                  # CritiqueSpec score penalty

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "clean": self.clean,
            "blocked": self.blocked,
            "warnings": self.warnings,
            "score_penalty": self.score_penalty,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Trust Resolution
# ══════════════════════════════════════════════════════════════════════════════

def resolve_trust_level(agent_url: str,
                        remotes: list[dict] = None,
                        registries: list[dict] = None) -> str:
    """Determine the trust level for an external agent URL.

    Resolution order:
      1. Check pre-registered remotes (config.a2a.client.remotes)
      2. Check known registries (config.a2a.client.registries)
      3. Default to untrusted

    Args:
        agent_url: The external agent's URL.
        remotes: Pre-registered agent entries from config.
        registries: Known registry entries from config.

    Returns:
        Trust level string: "verified" / "community" / "untrusted"
    """
    if not agent_url:
        return TrustLevel.UNTRUSTED

    # Normalize URL for matching
    normalized = agent_url.rstrip("/").lower()

    # 1. Check pre-registered remotes
    for remote in (remotes or []):
        remote_url = remote.get("url", "").rstrip("/").lower()
        if remote_url and normalized.startswith(remote_url):
            level = remote.get("trust_level", TrustLevel.VERIFIED)
            if level in TrustLevel.ALL:
                return level

    # 2. Check registries — agents from known registries get community trust
    for registry in (registries or []):
        registry_url = registry.get("url", "").rstrip("/").lower()
        if registry_url:
            # Agent URL contains registry domain → community trust
            from urllib.parse import urlparse
            try:
                registry_host = urlparse(registry_url).hostname or ""
                agent_host = urlparse(normalized).hostname or ""
                if registry_host and agent_host and registry_host == agent_host:
                    return registry.get("trust_level", TrustLevel.COMMUNITY)
            except Exception:
                pass

    # 3. Default
    return TrustLevel.UNTRUSTED
