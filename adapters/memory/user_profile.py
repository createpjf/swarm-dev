"""
adapters/memory/user_profile.py
User preference auto-learning — tracks interaction patterns, language
preferences, formatting style, and domain interests per user.

Writes to memory/user_profiles/{user_id}.yaml and injects a compact
summary into the agent's system prompt (P1 layer) for personalization.

Usage:
    from adapters.memory.user_profile import UserProfileStore
    store = UserProfileStore()
    store.record_interaction(user_id, message_text, channel="telegram")
    profile = store.get_profile(user_id)
    prompt_fragment = store.to_prompt(user_id)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)

PROFILES_DIR = "memory/user_profiles"
INTERACTION_THRESHOLD = 10  # summarize after this many interactions


@dataclass
class UserProfile:
    """Tracked user preferences."""
    user_id: str
    display_name: str = ""
    # Language preferences
    primary_language: str = ""       # detected from messages (e.g. "zh", "en")
    preferred_reply_lang: str = ""   # user-specified or inferred
    # Communication style
    verbosity: str = "normal"        # "brief", "normal", "detailed"
    format_preference: str = ""      # "markdown", "plain", "code-heavy"
    # Domain interests (counted)
    interests: dict = field(default_factory=dict)  # topic -> count
    # Tool usage patterns
    frequent_tools: dict = field(default_factory=dict)  # tool_name -> count
    # Timing
    active_hours: list = field(default_factory=list)  # hours of day (0-23)
    # Stats
    interaction_count: int = 0
    last_interaction: float = 0.0
    last_summarized: float = 0.0
    created_at: float = field(default_factory=time.time)
    # Free-form notes from LLM summarization
    notes: str = ""


class UserProfileStore:
    """File-backed user preference store."""

    def __init__(self, profiles_dir: str = PROFILES_DIR):
        self.profiles_dir = profiles_dir
        os.makedirs(profiles_dir, exist_ok=True)

    def record_interaction(self, user_id: str, text: str,
                           channel: str = "",
                           display_name: str = "",
                           tools_used: list[str] | None = None):
        """Record a user interaction and update preference signals."""
        profile = self.get_profile(user_id)
        if not profile:
            profile = UserProfile(user_id=user_id)
        if display_name:
            profile.display_name = display_name

        profile.interaction_count += 1
        profile.last_interaction = time.time()

        # Detect language
        lang = self._detect_language(text)
        if lang:
            profile.primary_language = lang

        # Track active hours
        hour = time.localtime().tm_hour
        if hour not in profile.active_hours:
            profile.active_hours.append(hour)
            # Keep sorted and limited
            profile.active_hours = sorted(set(profile.active_hours))[-12:]

        # Track domain interests from keywords
        topics = self._extract_topics(text)
        for topic in topics:
            profile.interests[topic] = profile.interests.get(topic, 0) + 1
        # Keep top 20 interests
        if len(profile.interests) > 20:
            sorted_interests = sorted(
                profile.interests.items(), key=lambda x: x[1], reverse=True)
            profile.interests = dict(sorted_interests[:20])

        # Track tool usage
        for tool in (tools_used or []):
            profile.frequent_tools[tool] = \
                profile.frequent_tools.get(tool, 0) + 1

        # Detect verbosity preference
        if len(text) < 20:
            # Very short messages suggest preference for brevity
            pass  # Don't change from a single short message
        elif profile.interaction_count >= 5:
            # After 5+ interactions, detect average message length pattern
            if len(text) > 500:
                profile.verbosity = "detailed"
            elif len(text) < 50:
                profile.verbosity = "brief"

        self._save_profile(profile)

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        """Load a user profile from disk."""
        path = self._profile_path(user_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._from_dict(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[user_profile] Failed to load %s: %s", user_id, e)
            return None

    def to_prompt(self, user_id: str) -> str:
        """Generate a prompt fragment for system prompt injection.

        Returns empty string if no profile exists or insufficient data.
        """
        profile = self.get_profile(user_id)
        if not profile or profile.interaction_count < 3:
            return ""

        lines = ["## User Preferences"]
        if profile.display_name:
            lines.append(f"- Name: {profile.display_name}")
        if profile.primary_language:
            lang_names = {"zh": "Chinese", "en": "English", "ja": "Japanese",
                          "ko": "Korean", "es": "Spanish", "de": "German",
                          "fr": "French", "ru": "Russian"}
            lang_name = lang_names.get(profile.primary_language,
                                       profile.primary_language)
            lines.append(f"- Primary language: {lang_name}")
        if profile.preferred_reply_lang:
            lines.append(f"- Prefers replies in: {profile.preferred_reply_lang}")
        if profile.verbosity != "normal":
            lines.append(f"- Communication style: {profile.verbosity}")

        # Top interests
        if profile.interests:
            top = sorted(profile.interests.items(),
                         key=lambda x: x[1], reverse=True)[:5]
            topics = ", ".join(t for t, _ in top)
            lines.append(f"- Frequently discusses: {topics}")

        # Top tools
        if profile.frequent_tools:
            top_tools = sorted(profile.frequent_tools.items(),
                               key=lambda x: x[1], reverse=True)[:5]
            tools = ", ".join(t for t, _ in top_tools)
            lines.append(f"- Often uses tools: {tools}")

        if profile.notes:
            lines.append(f"- Notes: {profile.notes}")

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def needs_summarization(self, user_id: str) -> bool:
        """Check if a profile has enough new interactions to warrant
        an LLM summarization pass."""
        profile = self.get_profile(user_id)
        if not profile:
            return False
        since_last = profile.interaction_count
        if profile.last_summarized > 0:
            # Rough check: summarize every INTERACTION_THRESHOLD interactions
            time_since = time.time() - profile.last_summarized
            return (since_last >= INTERACTION_THRESHOLD and
                    time_since > 3600)  # At least 1 hour between summaries
        return since_last >= INTERACTION_THRESHOLD

    def update_notes(self, user_id: str, notes: str):
        """Update the free-form notes from an LLM summarization."""
        profile = self.get_profile(user_id)
        if not profile:
            return
        profile.notes = notes[:500]  # Cap at 500 chars
        profile.last_summarized = time.time()
        self._save_profile(profile)

    # ── Internal ──

    def _profile_path(self, user_id: str) -> str:
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', user_id)
        return os.path.join(self.profiles_dir, f"{safe_id}.json")

    def _save_profile(self, profile: UserProfile):
        path = self._profile_path(profile.user_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(asdict(profile), f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("[user_profile] Failed to save %s: %s",
                           profile.user_id, e)

    @staticmethod
    def _detect_language(text: str) -> str:
        """Simple heuristic language detection."""
        if not text:
            return ""
        # Count CJK characters
        cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        jp = sum(1 for c in text if '\u3040' <= c <= '\u30ff')
        kr = sum(1 for c in text if '\uac00' <= c <= '\ud7af')
        total = len(text)
        if total == 0:
            return ""
        if cjk / total > 0.2:
            return "zh"
        if jp / total > 0.1:
            return "ja"
        if kr / total > 0.1:
            return "ko"
        # Cyrillic
        cyr = sum(1 for c in text if '\u0400' <= c <= '\u04ff')
        if cyr / total > 0.2:
            return "ru"
        return "en"

    @staticmethod
    def _extract_topics(text: str) -> list[str]:
        """Extract topic keywords from user message."""
        # Simple keyword extraction — production would use NLP
        topics = []
        keywords = {
            "code": ["代码", "code", "编程", "programming", "debug", "bug"],
            "web": ["网站", "website", "web", "API", "HTTP", "URL"],
            "data": ["数据", "data", "database", "SQL", "csv", "excel"],
            "ai": ["AI", "模型", "model", "GPT", "LLM", "训练", "training"],
            "design": ["设计", "design", "UI", "UX", "界面", "layout"],
            "devops": ["部署", "deploy", "docker", "CI/CD", "服务器", "server"],
            "research": ["研究", "research", "论文", "paper", "分析", "analysis"],
            "writing": ["写", "write", "文章", "article", "文档", "document"],
            "finance": ["金融", "finance", "投资", "trading", "股票", "stock"],
        }
        text_lower = text.lower()
        for topic, kws in keywords.items():
            if any(kw.lower() in text_lower for kw in kws):
                topics.append(topic)
        return topics

    @staticmethod
    def _from_dict(d: dict) -> UserProfile:
        return UserProfile(
            user_id=d.get("user_id", ""),
            display_name=d.get("display_name", ""),
            primary_language=d.get("primary_language", ""),
            preferred_reply_lang=d.get("preferred_reply_lang", ""),
            verbosity=d.get("verbosity", "normal"),
            format_preference=d.get("format_preference", ""),
            interests=d.get("interests", {}),
            frequent_tools=d.get("frequent_tools", {}),
            active_hours=d.get("active_hours", []),
            interaction_count=d.get("interaction_count", 0),
            last_interaction=d.get("last_interaction", 0),
            last_summarized=d.get("last_summarized", 0),
            created_at=d.get("created_at", 0),
            notes=d.get("notes", ""),
        )
