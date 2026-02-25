"""
adapters/a2a/ — A2A (Agent-to-Agent) protocol adapter.

Implements Google's A2A standard for inter-agent communication:
  - **Server**: Cleo as an A2A-compliant agent (Agent Card + message/send)
  - **Client**: Cleo calling external A2A agents (discovery + delegation)
  - **Bridge**: A2A Task ↔ Cleo TaskBoard bidirectional state mapping
  - **Models**: A2A protocol data structures (AgentCard, Message, Task, Artifact)
  - **Security**: 3-tier trust model + content sanitization
  - **Registry**: Agent discovery + capability matching

Phase 4 (Server) + Phase 5 (Client) of the Cleo V0.02 architecture upgrade.
"""

from adapters.a2a.models import (
    AgentCard,
    A2AMessage,
    A2ATask,
    A2AArtifact,
    A2APart,
)
from adapters.a2a.bridge import A2ABridge
from adapters.a2a.server import A2AServer
from adapters.a2a.client import A2AClient, DelegationResult
from adapters.a2a.security import SecurityFilter, TrustLevel, TrustPolicy
from adapters.a2a.registry import AgentRegistry, AgentEntry

__all__ = [
    # Models
    "AgentCard",
    "A2AMessage",
    "A2ATask",
    "A2AArtifact",
    "A2APart",
    # Phase 4: Server
    "A2ABridge",
    "A2AServer",
    # Phase 5: Client
    "A2AClient",
    "DelegationResult",
    "SecurityFilter",
    "TrustLevel",
    "TrustPolicy",
    "AgentRegistry",
    "AgentEntry",
]
