"""
adapters/a2a/models.py — A2A protocol data structures.

Implements Google A2A 0.3 specification types:
  - AgentCard: Agent capability description (/.well-known/agent.json)
  - A2AMessage: JSON-RPC message with Parts (Text/File/Data)
  - A2ATask: Task lifecycle with 5 states
  - A2AArtifact: Output artifact with Parts
  - A2APart: Content unit (TextPart, FilePart, DataPart)

All types are pure dataclasses — no runtime dependencies.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# ══════════════════════════════════════════════════════════════════════════════
#  A2A Parts — smallest content unit
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class A2APart:
    """A2A Message Part — Text, File, or Data.

    kind: "text" | "file" | "data"

    For text:  text is set
    For file:  name, mimeType, data (base64) or uri
    For data:  data (JSON dict)
    """
    kind: str = "text"
    text: str = ""
    # File fields
    name: str = ""
    mimeType: str = ""
    data: str = ""       # base64 for file, JSON str for data
    uri: str = ""        # alternative to inline data

    def to_dict(self) -> dict:
        result: dict[str, Any] = {"kind": self.kind}
        if self.kind == "text":
            result["text"] = self.text
        elif self.kind == "file":
            result["name"] = self.name
            result["mimeType"] = self.mimeType
            if self.uri:
                result["uri"] = self.uri
            elif self.data:
                result["data"] = self.data
        elif self.kind == "data":
            result["data"] = self.data
        return result

    @classmethod
    def from_dict(cls, d: dict) -> A2APart:
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})

    @classmethod
    def text_part(cls, text: str) -> A2APart:
        return cls(kind="text", text=text)

    @classmethod
    def file_part(cls, name: str, mime: str, data: str = "",
                  uri: str = "") -> A2APart:
        return cls(kind="file", name=name, mimeType=mime,
                   data=data, uri=uri)


# ══════════════════════════════════════════════════════════════════════════════
#  A2A Message
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class A2AMessage:
    """A2A Message — the primary communication unit.

    role: "user" (client→server) or "agent" (server→client)
    """
    role: str = "user"
    parts: list[A2APart] = field(default_factory=list)
    messageId: str = ""

    def __post_init__(self):
        if not self.messageId:
            self.messageId = f"msg-{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "parts": [p.to_dict() for p in self.parts],
            "messageId": self.messageId,
        }

    @classmethod
    def from_dict(cls, d: dict) -> A2AMessage:
        parts = [A2APart.from_dict(p) for p in d.get("parts", [])]
        return cls(
            role=d.get("role", "user"),
            parts=parts,
            messageId=d.get("messageId", ""),
        )

    def get_text(self) -> str:
        """Extract concatenated text from all TextParts."""
        return "\n".join(p.text for p in self.parts if p.kind == "text")

    def get_files(self) -> list[A2APart]:
        """Extract all FileParts."""
        return [p for p in self.parts if p.kind == "file"]


# ══════════════════════════════════════════════════════════════════════════════
#  A2A Task Status
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class A2ATaskStatus:
    """A2A Task status envelope.

    state: submitted | working | input-required | completed | failed | canceled
    """
    state: str = "submitted"
    message: Optional[A2AMessage] = None
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"state": self.state}
        if self.message:
            d["message"] = self.message.to_dict()
        if self.timestamp:
            d["timestamp"] = self.timestamp
        return d

    @classmethod
    def from_dict(cls, d: dict) -> A2ATaskStatus:
        msg = None
        if "message" in d:
            msg = A2AMessage.from_dict(d["message"])
        return cls(
            state=d.get("state", "submitted"),
            message=msg,
            timestamp=d.get("timestamp", ""),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  A2A Artifact
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class A2AArtifact:
    """A2A Artifact — output of a completed task."""
    artifactId: str = ""
    name: str = "result"
    description: str = ""
    parts: list[A2APart] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.artifactId:
            self.artifactId = f"art-{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "artifactId": self.artifactId,
            "name": self.name,
            "parts": [p.to_dict() for p in self.parts],
        }
        if self.description:
            d["description"] = self.description
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, d: dict) -> A2AArtifact:
        parts = [A2APart.from_dict(p) for p in d.get("parts", [])]
        return cls(
            artifactId=d.get("artifactId", ""),
            name=d.get("name", "result"),
            description=d.get("description", ""),
            parts=parts,
            metadata=d.get("metadata", {}),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  A2A Task
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class A2ATask:
    """A2A Task — the main lifecycle object.

    States: submitted → working → completed/failed/canceled
                     ↗ input-required ↘
    """
    id: str = ""
    contextId: str = ""
    status: A2ATaskStatus = field(default_factory=A2ATaskStatus)
    artifacts: list[A2AArtifact] = field(default_factory=list)
    history: list[A2AMessage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: str = "task"

    def __post_init__(self):
        if not self.id:
            self.id = f"a2a-{uuid.uuid4().hex[:12]}"
        if not self.contextId:
            self.contextId = f"ctx-{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "contextId": self.contextId,
            "status": self.status.to_dict(),
            "kind": self.kind,
        }
        if self.artifacts:
            d["artifacts"] = [a.to_dict() for a in self.artifacts]
        if self.history:
            d["history"] = [m.to_dict() for m in self.history]
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, d: dict) -> A2ATask:
        status = A2ATaskStatus.from_dict(d.get("status", {}))
        artifacts = [A2AArtifact.from_dict(a) for a in d.get("artifacts", [])]
        history = [A2AMessage.from_dict(m) for m in d.get("history", [])]
        return cls(
            id=d.get("id", ""),
            contextId=d.get("contextId", ""),
            status=status,
            artifacts=artifacts,
            history=history,
            metadata=d.get("metadata", {}),
            kind=d.get("kind", "task"),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Agent Card — published at /.well-known/agent.json
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentSkill:
    """A single capability advertised in the Agent Card."""
    id: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "examples": self.examples,
        }


@dataclass
class AgentCard:
    """A2A Agent Card — describes an agent's capabilities.

    Published at ``/.well-known/agent.json`` per A2A spec.
    """
    name: str = "Cleo"
    description: str = (
        "Self-evolving multi-agent AI system with planning, execution, "
        "and quality review. Equipped with 36+ tools for web search, "
        "code execution, file management, and browser automation."
    )
    url: str = ""
    version: str = "0.2.0"
    protocol: str = "a2a/0.3"
    capabilities: dict[str, bool] = field(default_factory=lambda: {
        "streaming": True,
        "pushNotifications": False,
        "stateTransitionHistory": True,
    })
    skills: list[AgentSkill] = field(default_factory=list)
    authentication: dict[str, Any] = field(default_factory=lambda: {
        "schemes": ["bearer"],
    })
    defaultInputModes: list[str] = field(default_factory=lambda: ["text", "file"])
    defaultOutputModes: list[str] = field(default_factory=lambda: ["text", "file"])

    def __post_init__(self):
        if not self.skills:
            self.skills = _default_skills()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "protocol": self.protocol,
            "capabilities": self.capabilities,
            "skills": [s.to_dict() for s in self.skills],
            "authentication": self.authentication,
            "defaultInputModes": self.defaultInputModes,
            "defaultOutputModes": self.defaultOutputModes,
        }


def _default_skills() -> list[AgentSkill]:
    """Cleo's default skills advertised in the Agent Card."""
    return [
        AgentSkill(
            id="research",
            name="Web Research & Analysis",
            description=(
                "Search the web, fetch pages, analyze content, and "
                "synthesize findings into structured reports. "
                "Supports Chinese and English."
            ),
            tags=["research", "web-search", "analysis", "report"],
            examples=[
                "Research the top 5 DeFi protocols on Base chain by TVL",
                "Compare Arbitrum vs Optimism ecosystem development",
            ],
        ),
        AgentSkill(
            id="coding",
            name="Code Generation & Execution",
            description=(
                "Write, execute, and test code. Supports Python, Node.js, "
                "shell scripts with sandboxed execution."
            ),
            tags=["code", "programming", "automation"],
            examples=[
                "Write a Python script to analyze CSV data",
                "Create a web scraper for product prices",
            ],
        ),
        AgentSkill(
            id="content",
            name="Content Creation",
            description=(
                "Generate structured documents, reports, and analysis. "
                "Multi-step tasks are automatically decomposed, executed, "
                "and quality-reviewed."
            ),
            tags=["writing", "report", "document"],
            examples=[
                "Write a competitive analysis report",
                "Create a technical specification document",
            ],
        ),
        AgentSkill(
            id="browser-automation",
            name="Browser Automation",
            description=(
                "Navigate websites, fill forms, extract data, take "
                "screenshots via headless browser."
            ),
            tags=["browser", "scraping", "automation"],
            examples=[
                "Navigate to a website and extract pricing information",
                "Fill out a web form with provided data",
            ],
        ),
    ]
