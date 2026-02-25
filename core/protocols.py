"""
core/protocols.py — Cleo V0.02 Structured Protocol Definitions

Data contracts for all inter-agent communication. Pure data definitions, zero runtime dependencies.
"""

from __future__ import annotations

import json
import logging
import re
import time
import warnings
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Shared utilities — imported by agent.py, orchestrator.py, and others
# ══════════════════════════════════════════════════════════════════════════════

# ── Think-tag stripper ──
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _strip_think(text: str) -> str:
    """Strip ``<think>...</think>`` blocks from LLM output.

    If stripping leaves nothing, extract the think content as the result
    (some models wrap their entire response in ``<think>`` tags).
    """
    think_contents = _THINK_RE.findall(text)
    stripped = _THINK_RE.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    if stripped:
        return stripped
    # Entire output was think blocks — use the content rather than returning empty
    if think_contents:
        combined = "\n\n".join(c.strip() for c in think_contents if c.strip())
        if combined:
            logger.info("[_strip_think] entire output was <think> — recovering %d chars",
                        len(combined))
            return re.sub(r"\n{3,}", "\n\n", combined).strip()
    return stripped


# ── FileLock with graceful fallback ──
try:
    from filelock import FileLock
except ImportError:
    warnings.warn(
        "filelock package not installed. File operations are NOT process-safe. "
        "Install with: pip install filelock",
        RuntimeWarning, stacklevel=2,
    )

    class FileLock:  # type: ignore[no-redef]
        """No-op fallback when filelock is not installed."""
        def __init__(self, path: str):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass


# ── JsonSerializable mixin for dataclasses ──
class JsonSerializable:
    """Mixin providing standard ``to_json()`` / ``from_json()`` for dataclasses."""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw):
        data = json.loads(raw) if isinstance(raw, str) else raw
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})


# ══════════════════════════════════════════════════════════════════════════════
#  Tool category enum (ToolScope improvement 4)
# ══════════════════════════════════════════════════════════════════════════════

class ToolCategory(str, Enum):
    WEB = "web"
    FS = "fs"
    AUTOMATION = "automation"
    MEDIA = "media"
    BROWSER = "browser"
    MEMORY = "memory"
    MESSAGING = "messaging"
    TASK = "task"
    SKILL = "skill"
    A2A = "a2a_delegate"       # Phase 5: delegate to external A2A agent


# ══════════════════════════════════════════════════════════════════════════════
#  Improvement 1: SubTaskSpec — Structured task ticket
# ══════════════════════════════════════════════════════════════════════════════

class Complexity(str, Enum):
    SIMPLE = "simple"
    NORMAL = "normal"
    COMPLEX = "complex"


@dataclass
class SubTaskSpec(JsonSerializable):
    """Structured task ticket from Leo to Jerry.

    Replaces V0.01's ``TASK: <natural language description>`` format.
    """
    objective: str
    constraints: list[str] = field(default_factory=list)
    input: dict[str, Any] = field(default_factory=dict)
    output_format: str = ""          # markdown_table / json / code / file / text
    tool_hint: list[str] = field(default_factory=list)  # ToolCategory values
    complexity: str = "normal"       # simple / normal / complex
    parent_intent: str = ""          # Original user intent (IntentAnchor improvement 5)
    a2a_hint: dict[str, Any] = field(default_factory=dict)  # Phase 5: A2A delegation hint
    # a2a_hint fields:
    #   preferred_agent: str — recommended external Agent URL (optional)
    #   required_skills: list[str] — needed capability tags (required if a2a)
    #   fallback: str — fallback plan if external agent unavailable (optional)

    def to_task_description(self) -> str:
        """Serialize to TaskBoard description field (human-readable + parseable)."""
        lines = [f"[SubTaskSpec] {self.objective}"]
        if self.constraints:
            lines.append(f"Constraints: {'; '.join(self.constraints)}")
        if self.output_format:
            lines.append(f"Output format: {self.output_format}")
        if self.tool_hint:
            lines.append(f"Tool categories: {', '.join(self.tool_hint)}")
        return "\n".join(lines)

    @classmethod
    def from_legacy_task(cls, description: str, complexity: str = "normal") -> SubTaskSpec:
        """Construct SubTaskSpec from V0.01 TASK: line (backward compatible)."""
        return cls(
            objective=description,
            complexity=complexity,
            tool_hint=[],  # empty → ToolScope falls back to full profile
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Improvement 2: CritiqueSpec — Structured review protocol
# ══════════════════════════════════════════════════════════════════════════════

class CritiqueVerdict(str, Enum):
    LGTM = "LGTM"
    NEEDS_WORK = "NEEDS_WORK"


@dataclass
class CritiqueDimensions:
    """5-dimension scoring, 1-10 each."""
    accuracy: int = 7       # accuracy (30%)
    completeness: int = 7   # completeness (20%)
    technical: int = 7      # technical quality (20%)
    calibration: int = 7    # calibration (20%)
    efficiency: int = 7     # resource efficiency (10%)

    WEIGHTS = {
        "accuracy": 0.3,
        "completeness": 0.2,
        "technical": 0.2,
        "calibration": 0.2,
        "efficiency": 0.1,
    }

    @property
    def composite(self) -> float:
        """Weighted composite score (1-10)."""
        return sum(
            getattr(self, dim) * w
            for dim, w in self.WEIGHTS.items()
        )

    @property
    def all_high(self) -> bool:
        """All dimensions >= 8?"""
        return all(
            getattr(self, dim) >= 8
            for dim in self.WEIGHTS
        )

    @property
    def any_low(self) -> bool:
        """Any dimension < 5?"""
        return any(
            getattr(self, dim) < 5
            for dim in self.WEIGHTS
        )


@dataclass
class CritiqueItem:
    """Actionable improvement item."""
    dimension: str = ""    # which dimension
    issue: str = ""        # issue description
    suggestion: str = ""   # improvement suggestion


@dataclass
class CritiqueSpec:
    """Alic's structured review output.

    Replaces V0.01's ``{"score": N, "comment": "...", "suggestions": [...]}``.
    """
    dimensions: CritiqueDimensions = field(default_factory=CritiqueDimensions)
    verdict: str = "LGTM"       # LGTM / NEEDS_WORK
    items: list[CritiqueItem] = field(default_factory=list)
    confidence: float = 0.8     # 0.0-1.0
    task_id: str = ""
    reviewer_id: str = ""
    timestamp: float = 0.0
    source_trust: dict[str, Any] = field(default_factory=dict)  # Phase 5: A2A source info
    # source_trust fields:
    #   agent_url: str — external agent URL
    #   trust_level: str — verified / community / untrusted
    #   data_freshness: str — ISO timestamp of external result
    #   cross_validated: bool — whether cross-validated with another source

    @property
    def composite_score(self) -> float:
        return self.dimensions.composite

    def auto_simplify(self) -> None:
        """Auto-simplify: all >= 8 → LGTM + empty items."""
        if self.dimensions.all_high:
            self.verdict = CritiqueVerdict.LGTM.value
            self.items = []

    def to_json(self) -> str:
        data = {
            "dimensions": asdict(self.dimensions),
            "verdict": self.verdict,
            "items": [asdict(item) for item in self.items],
            "confidence": self.confidence,
            "task_id": self.task_id,
            "reviewer_id": self.reviewer_id,
            "timestamp": self.timestamp,
        }
        # Remove WEIGHTS (non-data field)
        data["dimensions"].pop("WEIGHTS", None)
        # Phase 5: include source_trust if present
        if self.source_trust:
            data["source_trust"] = self.source_trust
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> CritiqueSpec:
        data = json.loads(raw)
        dims_data = data.get("dimensions", {})
        dims_data.pop("WEIGHTS", None)
        dims = CritiqueDimensions(**{
            k: v for k, v in dims_data.items()
            if k in CritiqueDimensions.__dataclass_fields__
        })
        items = [
            CritiqueItem(**{k: v for k, v in item.items()
                           if k in CritiqueItem.__dataclass_fields__})
            for item in data.get("items", [])
        ]
        return cls(
            dimensions=dims,
            verdict=data.get("verdict", "LGTM"),
            items=items,
            confidence=data.get("confidence", 0.8),
            task_id=data.get("task_id", ""),
            reviewer_id=data.get("reviewer_id", ""),
            timestamp=data.get("timestamp", 0.0),
            source_trust=data.get("source_trust", {}),
        )

    @classmethod
    def from_legacy_score(cls, score: int, comment: str = "",
                          suggestions: list[str] | None = None) -> CritiqueSpec:
        """Construct CritiqueSpec from V0.01 {"score": N} (backward compatible)."""
        dims = CritiqueDimensions(
            accuracy=score,
            completeness=score,
            technical=score,
            calibration=score,
            efficiency=score,
        )
        items = []
        if suggestions:
            for s in suggestions[:3]:
                items.append(CritiqueItem(suggestion=s))
        spec = cls(
            dimensions=dims,
            items=items,
            timestamp=time.time(),
        )
        spec.auto_simplify()
        return spec


# ══════════════════════════════════════════════════════════════════════════════
#  Improvement 3: TaskRouter — Routing decision
# ══════════════════════════════════════════════════════════════════════════════

class RouteDecision(str, Enum):
    DIRECT_ANSWER = "DIRECT_ANSWER"
    MAS_PIPELINE = "MAS_PIPELINE"


@dataclass
class RoutingResult:
    decision: RouteDecision = RouteDecision.MAS_PIPELINE
    reason: str = ""
    direct_answer: str = ""
    subtask_specs: list[SubTaskSpec] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
#  Improvement 5: IntentAnchor — Intent anchor
# ══════════════════════════════════════════════════════════════════════════════

INTENT_KEY_PREFIX = "intent:"


@dataclass
class IntentAnchor(JsonSerializable):
    """User intent stored at ContextBus L0 (TASK layer)."""
    user_message: str           # user's original message
    core_goal: str = ""         # core goal distilled by Leo
    success_criteria: list[str] = field(default_factory=list)
    task_id: str = ""


# ══════════════════════════════════════════════════════════════════════════════
#  Improvement 6: DensityTag — Information density tag
# ══════════════════════════════════════════════════════════════════════════════

class DensityLevel(str, Enum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


DENSITY_MULTIPLIERS: dict[DensityLevel, float] = {
    DensityLevel.HIGH: 1.5,
    DensityLevel.NORMAL: 1.0,
    DensityLevel.LOW: 0.5,
}

# High-density signals: causation, actionable advice, lessons learned
DENSITY_HIGH_SIGNALS: list[str] = [
    # Chinese
    "因为", "所以", "导致", "解决方案", "建议", "经验", "教训", "原则",
    "根因", "关键", "重要", "必须", "架构", "安全", "生产",
    # English
    "because", "therefore", "caused by", "solution", "recommendation",
    "lesson learned", "principle", "root cause", "critical", "important",
    "must", "architecture", "security", "production",
]

# Low-density signals: pure description, no insight
DENSITY_LOW_SIGNALS: list[str] = [
    # Chinese
    "也许", "可能", "尝试", "草稿", "临时", "占位",
    # English
    "maybe", "perhaps", "experiment", "try", "draft", "wip",
    "temporary", "placeholder", "todo", "might", "possibly",
]


def classify_density(content: str, tags: list[str] | None = None) -> DensityLevel:
    """Auto-classify information density based on signal words."""
    text_lower = (content + " " + " ".join(tags or [])).lower()

    high_count = sum(1 for s in DENSITY_HIGH_SIGNALS if s in text_lower)
    low_count = sum(1 for s in DENSITY_LOW_SIGNALS if s in text_lower)

    # 2+ high-density signals → HIGH
    # 1 high-density + 0 low-density → HIGH
    if high_count >= 2 or (high_count >= 1 and low_count == 0 and len(content) > 50):
        return DensityLevel.HIGH

    # 2+ low-density signals or too short → LOW
    if low_count >= 2 or len(content) < 50:
        return DensityLevel.LOW

    return DensityLevel.NORMAL


# ══════════════════════════════════════════════════════════════════════════════
#  Improvement 8: TextGrad Pipeline — Text gradient signal
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GradientSignal(JsonSerializable):
    """Recurring issues and improvement patches extracted from CritiqueSpec logs."""
    agent_id: str
    recurring_issues: list[str] = field(default_factory=list)
    improvement_patches: list[str] = field(default_factory=list)
    source_critique_ids: list[str] = field(default_factory=list)
    generated_at: float = 0.0
    decayed_issues: list[str] = field(default_factory=list)
