"""
core/task_router.py — Cleo V0.02 Improvement 3: TaskRouter intelligent task routing

Pre-routing logic: decides whether a task needs the full MAS pipeline
(Leo → Jerry → Alic → Leo closeout) or if Leo can answer directly.

DIRECT_ANSWER: Simple knowledge Q&A, Leo answers directly
MAS_PIPELINE:  Complex tasks requiring execution, search, analysis, or file generation
"""

from __future__ import annotations

import logging
import re

from core.protocols import RouteDecision

logger = logging.getLogger(__name__)

# ── MAS_PIPELINE signal words (needs tools/files/multi-step) ──────────────

_MAS_SIGNALS_ZH = [
    "写", "创建", "生成", "构建", "编写", "运行", "执行", "搜索",
    "下载", "分析", "计算", "部署", "截图", "安装", "配置",
    "修改", "编辑", "删除", "上传", "翻译", "对比", "报告",
    "代码", "文件", "脚本", "网站", "数据库",
]

_MAS_SIGNALS_EN = [
    "write", "create", "generate", "build", "code", "file", "run",
    "execute", "search", "download", "analyze", "compute", "calculate",
    "deploy", "install", "configure", "screenshot", "browser",
    "edit", "delete", "upload", "compare", "report", "script",
    "database", "website", "translate",
]

# ── Multi-step signals (require task decomposition) ───────────────────

_MULTI_STEP_SIGNALS = [
    " and then ", "first ", "step 1", "步骤",
    "然后再", "接着", "首先", "第一步", "分别",
    "一方面", "另一方面", "同时",
]

# ── DIRECT_ANSWER signal words (simple knowledge Q&A) ─────────────────

_DIRECT_SIGNALS_ZH = [
    "什么是", "解释", "定义", "描述", "介绍", "说说",
    "是什么", "怎么理解", "含义",
]

_DIRECT_SIGNALS_EN = [
    "what is", "explain", "define", "describe", "tell me about",
    "how does", "what does", "meaning of",
]


def classify_task(description: str) -> RouteDecision:
    """Heuristic pre-classification of task complexity.

    DIRECT_ANSWER criteria (all must be true):
      1. Single goal (no multi-step indicators)
      2. No tool/file/execution signals
      3. Knowledge-type question or trivial query

    MAS_PIPELINE: everything else (conservative default).

    Returns:
        RouteDecision.DIRECT_ANSWER or RouteDecision.MAS_PIPELINE
    """
    desc_lower = description.lower().strip()

    # Very short queries are likely simple
    if len(desc_lower) < 5:
        return RouteDecision.DIRECT_ANSWER

    # Multi-step indicators → always MAS
    if any(sig in desc_lower for sig in _MULTI_STEP_SIGNALS):
        return RouteDecision.MAS_PIPELINE

    # MAS signals (tools, files, execution) → MAS
    all_mas = _MAS_SIGNALS_ZH + _MAS_SIGNALS_EN
    if any(sig in desc_lower for sig in all_mas):
        return RouteDecision.MAS_PIPELINE

    # Direct answer signals → DIRECT
    all_direct = _DIRECT_SIGNALS_ZH + _DIRECT_SIGNALS_EN
    if any(sig in desc_lower for sig in all_direct):
        return RouteDecision.DIRECT_ANSWER

    # Question marks with short length → likely simple
    if ("?" in description or "？" in description) and len(description) < 50:
        return RouteDecision.DIRECT_ANSWER

    # Default: MAS pipeline (conservative — don't risk missing complex tasks)
    return RouteDecision.MAS_PIPELINE


def parse_route_from_output(planner_output: str) -> RouteDecision | None:
    """Check if Leo explicitly declared ROUTE: DIRECT_ANSWER or ROUTE: MAS_PIPELINE.

    Returns:
        RouteDecision if found, None otherwise.
    """
    for line in planner_output.strip().split("\n"):
        stripped = line.strip()
        # Match both ROUTE: and route: variants
        match = re.match(r'^ROUTE:\s*(\S+)', stripped, re.IGNORECASE)
        if match:
            route_str = match.group(1).upper()
            if route_str == "DIRECT_ANSWER":
                return RouteDecision.DIRECT_ANSWER
            elif route_str == "MAS_PIPELINE":
                return RouteDecision.MAS_PIPELINE
            else:
                logger.warning("parse_route_from_output: unrecognized route '%s'", route_str)
    return None
