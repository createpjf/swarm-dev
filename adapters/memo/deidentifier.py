"""
adapters/memo/deidentifier.py — PII removal for Memo export.

Two layers:
  1. Regex layer (mandatory, zero cost)  — emails, IPs, API keys, etc.
  2. LLM-assisted layer (optional)       — business context, product names.

Usage::

    text_out, stats = deidentify_regex(text)
    # or with LLM:
    text_out, stats = await deidentify(text, config, llm_adapter)
"""

from __future__ import annotations

import re
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from adapters.memo.config import MemoConfig

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  Regex patterns
# ══════════════════════════════════════════════════════════════════════════════

_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # ── credentials & secrets ─────────────────────────────────────────────
    ("private_key_block",
     re.compile(
         r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
         r".*?"
         r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
         re.DOTALL),
     "[PRIVATE_KEY_REDACTED]"),

    ("env_var_secret",
     re.compile(
         r"(?:export\s+)?"
         r"(?:API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|ACCESS_KEY|AUTH)"
         r"\s*=\s*[\"']?[^\s\"']+[\"']?",
         re.IGNORECASE),
     "[ENV_REDACTED]"),

    ("bearer_token",
     re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
     "Bearer [REDACTED]"),

    ("api_key_inline",
     re.compile(
         r"(?:sk|pk|api|key|token|secret|password|auth)[_-]?"
         r"[A-Za-z0-9]{20,}",
         re.IGNORECASE),
     "[REDACTED]"),

    # ── URLs with credentials ─────────────────────────────────────────────
    ("url_with_token",
     re.compile(
         r"https?://[^\s]*[?&]"
         r"(?:token|key|secret|api_key|access_token|auth)"
         r"=[^\s&]+"),
     "[URL_WITH_CREDENTIALS]"),

    # ── personal identifiers ──────────────────────────────────────────────
    ("email",
     re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "[EMAIL]"),

    ("ip_v4",
     re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
     "[IP]"),

    ("ip_v6",
     re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"),
     "[IP]"),

    ("wallet_address",
     re.compile(r"\b0x[0-9a-fA-F]{40}\b"),
     "[WALLET]"),

    # ── UUIDs (task IDs, user IDs etc) ────────────────────────────────────
    ("uuid",
     re.compile(
         r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
         r"[0-9a-f]{4}-[0-9a-f]{12}\b",
         re.IGNORECASE),
     "[UUID]"),
]

# ── company name patterns (populated at runtime) ─────────────────────────

_company_patterns: list[tuple[re.Pattern, str]] = []


def set_company_names(names: list[str]):
    """Configure company / product names to redact."""
    _company_patterns.clear()
    for name in names:
        if name.strip():
            _company_patterns.append((
                re.compile(re.escape(name.strip()), re.IGNORECASE),
                "[Company]",
            ))


# ══════════════════════════════════════════════════════════════════════════════
#  Regex layer (mandatory)
# ══════════════════════════════════════════════════════════════════════════════

def deidentify_regex(text: str) -> tuple[str, dict[str, int]]:
    """Apply regex-based PII removal.

    Returns ``(cleaned_text, replacement_stats)``.
    """
    stats: dict[str, int] = {}
    result = text

    for name, pattern, replacement in _PATTERNS:
        matches = pattern.findall(result)
        if matches:
            stats[name] = len(matches)
            result = pattern.sub(replacement, result)

    for pattern, replacement in _company_patterns:
        matches = pattern.findall(result)
        if matches:
            stats["company_name"] = stats.get("company_name", 0) + len(matches)
            result = pattern.sub(replacement, result)

    return result, stats


# ══════════════════════════════════════════════════════════════════════════════
#  LLM-assisted layer (optional)
# ══════════════════════════════════════════════════════════════════════════════

_LLM_PROMPT = """\
请对以下文本进行脱敏处理，用于公开发布到 AI 记忆市场。

规则：
1. 将所有公司名替换为 [Company]
2. 将所有人名替换为 [User]
3. 将所有内部项目名替换为 [Project]
4. 将所有具体业务金额/数据替换为 [DATA]
5. 保留所有技术细节（代码、算法、错误消息、架构模式、框架名称）
6. 保留所有通用技术知识和最佳实践

输出要求：直接返回脱敏后的文本，不要添加任何说明性文字。

原文：
{text}"""


async def _deidentify_llm(text: str, llm_adapter, model: str) -> str:
    """Run LLM-assisted deidentification pass (business context removal)."""
    # Truncate to avoid blowing up context
    truncated = text[:4000]
    messages = [
        {"role": "system", "content": "你是一个数据脱敏专家。直接输出脱敏后的文本。"},
        {"role": "user", "content": _LLM_PROMPT.format(text=truncated)},
    ]
    result = await llm_adapter.chat(messages, model)
    if isinstance(result, dict):
        result = result.get("content", "")
    return result.strip()


# ══════════════════════════════════════════════════════════════════════════════
#  Combined pipeline
# ══════════════════════════════════════════════════════════════════════════════

async def deidentify(
    text: str,
    config: "MemoConfig",
    llm_adapter=None,
) -> tuple[str, dict]:
    """Full deidentification: regex first, then optional LLM pass.

    Returns ``(cleaned_text, stats_dict)``.
    """
    # Populate company names from config (idempotent)
    if config.company_names and not _company_patterns:
        set_company_names(config.company_names)

    # Step 1 — regex (mandatory)
    result, stats = deidentify_regex(text)

    # Step 2 — LLM (optional)
    if config.deidentification_use_llm and llm_adapter:
        try:
            result = await _deidentify_llm(
                result, llm_adapter, config.deidentification_llm_model)
            stats["llm_pass"] = 1
        except Exception as e:
            logger.debug("[memo-deident] LLM pass failed: %s", e)
            stats["llm_pass_error"] = str(e)

    return result, stats
