# Cleo 技术架构文档

> **版本**: V0.02 | **更新日期**: 2026-02-25 | **运行时模式**: LazyRuntime

---

## 目录

1. [系统概览](#1-系统概览)
2. [核心架构 (core/)](#2-核心架构)
3. [适配器层 (adapters/)](#3-适配器层)
4. [工具系统 (core/tools.py)](#4-工具系统)
5. [网关与 API](#5-网关与-api)
6. [支撑系统](#6-支撑系统)
7. [配置体系](#7-配置体系)
8. [数据流图](#8-数据流图)
9. [部署与运维](#9-部署与运维)

---

## 1. 系统概览

### 1.1 项目定位

Cleo 是一个**多智能体协作系统** (Multi-Agent System, MAS)，通过三个专职 Agent 协同工作：

| Agent | 角色 | 代号 | 工具权限 | 模型 |
|-------|------|------|----------|------|
| **Leo** | 🧠 BRAIN — 规划、分解、合成 | planner | `minimal` | MiniMax-M2.5 |
| **Jerry** | 🤚 HANDS — 执行、编码、搜索 | executor | `coding` | minimax-m2.5 |
| **Alic** | 👁️ EYES — 审查、评分、质量报告 | reviewer | `minimal` | minimax-m2.5 |

### 1.2 技术栈

```
Python 3.11+
├── LLM:       MiniMax-M2.5 (SSE streaming, 1M context window)
├── 向量数据库: ChromaDB (本地, 内置 embedding)
├── 进程模型:   multiprocessing (每 agent 独立进程)
├── HTTP 网关:  http.server (内置, 端口 19789)
├── WebSocket:  websockets (实时状态推送, 端口 19790)
├── 通道:       Telegram / Discord / 飞书 / Slack
├── 区块链:     Lit Protocol PKP + ERC-8004 (Base L2)
└── 依赖:       filelock, chromadb, pyyaml, requests, websockets
```

### 1.3 架构总图

```
┌─────────────────────────────────────────────────────────────┐
│                      用户界面层                              │
│  Telegram │ Discord │ 飞书 │ Slack │ HTTP API │ Dashboard   │
└──────┬────┴────┬────┴──────┴───────┴────┬─────┴─────┬──────┘
       │         │                        │           │
       ▼         ▼                        ▼           ▼
┌──────────────────────┐  ┌──────────────────┐ ┌───────────┐
│   ChannelManager     │  │  Gateway (HTTP)  │ │ WebSocket │
│ (持久化 agent 池)     │  │  30+ REST 端点   │ │ 1Hz 广播  │
└──────────┬───────────┘  └────────┬─────────┘ └───────────┘
           │                       │
           ▼                       ▼
┌──────────────────────────────────────────────────────────┐
│                    Orchestrator                           │
│  TaskRouter → 路由决策 → 任务分解 → 执行 → 审查 → 合成    │
└──────┬───────────┬───────────┬───────────────────────────┘
       │           │           │
       ▼           ▼           ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│  Leo 🧠  │ │ Jerry 🤚 │ │ Alic 👁️ │
│ (进程 1) │ │ (进程 2) │ │ (进程 3) │
└────┬─────┘ └────┬─────┘ └────┬─────┘
     │            │            │
     ▼            ▼            ▼
┌──────────────────────────────────────────────────────────┐
│              共享协调层                                    │
│  TaskBoard (.json)  │ ContextBus (.json) │ Mailbox (.jsonl)│
└──────────────────────────────────────────────────────────┘
     │            │            │
     ▼            ▼            ▼
┌──────────────────────────────────────────────────────────┐
│              适配器层                                      │
│  MiniMax LLM │ HybridMemory │ EpisodicMemory │ Chain     │
└──────────────────────────────────────────────────────────┘
```

### 1.4 目录结构

```
cleo-dev/
├── main.py                  # CLI 入口 (gateway start / chat / doctor)
├── config/
│   └── agents.yaml          # 全局配置 (runtime, agents, llm, memory, channels...)
├── core/
│   ├── orchestrator.py      # 任务编排引擎 (~1900 行)
│   ├── agent.py             # BaseAgent + AgentConfig
│   ├── task_board.py        # 文件锁任务看板
│   ├── context_bus.py       # 分层 KV 上下文总线
│   ├── protocols.py         # V0.02 结构化协议 (SubTaskSpec, CritiqueSpec...)
│   ├── task_router.py       # DIRECT_ANSWER vs MAS_PIPELINE 路由
│   ├── tools.py             # 37 个内置工具
│   ├── gateway.py           # HTTP REST 网关
│   ├── ws_gateway.py        # WebSocket 实时推送
│   ├── cron.py              # 定时任务调度器
│   ├── provider_router.py   # 跨 LLM 提供商故障转移
│   ├── doctor.py            # 系统健康检查 + 自动修复
│   ├── skill_loader.py      # 技能动态加载
│   ├── heartbeat.py         # Agent 心跳
│   └── runtime/
│       ├── __init__.py      # AgentRuntime ABC
│       ├── process.py       # ProcessRuntime (mp.Process)
│       ├── lazy.py          # LazyRuntime (按需启动)
│       ├── in_process.py    # InProcessRuntime (asyncio)
│       └── wakeup.py        # DualWakeupBus
├── adapters/
│   ├── llm/
│   │   └── minimax.py       # MiniMax SSE 流式适配器
│   ├── memory/
│   │   ├── hybrid.py        # BM25 + ChromaDB 混合检索
│   │   ├── episodic.py      # 三层情景记忆
│   │   ├── embedding.py     # Embedding 提供商工厂
│   │   └── knowledge_base.py # 共享知识库
│   └── channels/
│       ├── manager.py       # ChannelManager 中央协调
│       ├── telegram.py      # Telegram Bot 适配器
│       ├── discord.py       # Discord Bot 适配器
│       ├── feishu.py        # 飞书适配器
│       └── slack.py         # Slack Socket Mode 适配器
├── reputation/
│   └── scorer.py            # 5 维 EMA 声誉评分
├── skills/                  # 技能目录 (56+ 技能)
│   ├── shared/              # 共享技能
│   ├── agents/              # 每 agent 专有技能 + soul.md
│   └── team.md              # 自动生成的团队技能摘要
├── memory/                  # 运行时数据
│   ├── agents/{id}/         # 每 agent 情景记忆
│   ├── chroma/              # ChromaDB 向量库
│   └── reputation_cache.json
├── docs/                    # 文档 + agent 人格
├── tests/                   # 399 个测试
└── .logs/                   # 运行日志 (leo.log, jerry.log, alic.log)
```

---

## 2. 核心架构

### 2.1 Orchestrator — 任务编排引擎

**文件**: `core/orchestrator.py` (~1900 行)

Orchestrator 是整个系统的中枢，负责完整的任务生命周期管理。

#### 子进程入口

每个 Agent 在独立的 OS 进程中运行，通过 `_agent_process()` 入口启动：

```python
def _agent_process(agent_cfg_dict: dict, agent_def: dict, config: dict,
                    wakeup=None):
    """
    Runs in a child process.
    - Redirects stdout/stderr to .logs/{agent_id}.log
    - Registers SIGTERM/SIGINT for graceful shutdown
    - Builds per-agent adapters (LLM, memory, chain, episodic)
    - Enters asyncio event loop via _agent_loop()
    """
    # 信号处理
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # 构建适配器 (每 agent 独立实例)
    llm     = _build_llm_for_agent(agent_def, config)
    memory  = _build_memory(config, agent_id=agent_id)
    chain   = _build_chain(config)
    episodic, kb = _build_episodic_memory(config, agent_id)

    cfg   = AgentConfig(**agent_cfg_dict)
    agent = BaseAgent(cfg, llm, memory, SkillLoader(), chain,
                      episodic=episodic, kb=kb)

    bus   = ContextBus()
    board = TaskBoard()

    asyncio.run(_agent_loop(agent, bus, board, config, tracker, hb,
                            wakeup=wakeup))
```

#### 任务生命周期

```
用户消息
    │
    ▼
TaskRouter.classify_task()
    │
    ├── DIRECT_ANSWER ──► Leo 直接回答 (不启动 Jerry/Alic)
    │
    └── MAS_PIPELINE ──► Phase 1: Leo 分解为 SubTaskSpec
                             │
                             ▼
                         Phase 2: Jerry 逐个执行子任务
                             │
                             ▼
                         Phase 3: Alic 评审 (CritiqueSpec)
                             │
                             ▼
                         Phase 4: Leo 合成最终回复
```

#### `_wait()` — 等待所有任务完成

支持 ProcessRuntime (所有 agent 同时启动) 和 LazyRuntime (按需启动) 两种模式：

```python
def _wait(self):
    """Wait for all agent work to complete.

    Supports both ProcessRuntime and LazyRuntime.
    Polls until no active tasks remain and no agent processes are alive.
    """
    while True:
        alive = [p for p in self.runtime.procs if p.is_alive()]
        if alive:
            alive[0].join(timeout=3)  # 等待任一存活进程
            continue

        # 无存活进程 — 检查 TaskBoard 是否还有活跃任务
        data = self.board._read() or {}
        if any(t.get("status") in ("pending", "claimed", "review",
                                     "critique", "blocked", "paused",
                                     "synthesizing")
               for t in data.values()):
            time.sleep(2)  # 等待 lazy monitor 启动 agent
            continue

        break  # 全部完成

    logger.info("all agent processes finished")
```

### 2.2 Agent — 智能体基类

**文件**: `core/agent.py`

#### AgentConfig

```python
@dataclass
class AgentConfig:
    agent_id:         str
    role:             str
    model:            str
    skills:           list[str]       = field(default_factory=lambda: ["_base"])
    wallet_key:       str             = ""
    short_term_turns: int             = 20       # 短期记忆窗口大小
    long_term:        bool            = True     # 是否启用长期记忆
    recall_top_k:     int             = 3        # 记忆检索 top-k
    autonomy_level:   int             = 1        # 自主级别
    docs_dir:         str             = "docs"

    # 上下文压缩
    compaction_enabled:    bool = True
    max_context_tokens:    int  = 8000
    summary_target_tokens: int  = 1500
    keep_recent_turns:     int  = 4

    # 情景记忆 + 知识库
    episodic_recall_budget: int  = 1500   # token 预算
    kb_recall_budget:       int  = 800

    # 人格文件
    cognition_file:    str  = ""     # cognition.md (legacy)
    soul_file:         str  = ""     # soul.md (OpenClaw pattern)

    # 系统提示词预算
    max_system_prompt_tokens: int = 16000  # ~64K chars; 0 = 无限制

    # 工具配置
    tools_config: dict = field(default_factory=dict)  # {profile, allow, deny}
```

#### BaseAgent 初始化链

```python
class BaseAgent:
    """单 agent 实例 — 运行在子进程内。"""

    def __init__(self, cfg, llm, memory, skill_loader, chain,
                 episodic=None, kb=None):
        self.cfg          = cfg
        self.llm          = llm
        self.memory       = memory        # HybridAdapter (BM25 + ChromaDB)
        self.skill_loader = skill_loader
        self.chain        = chain          # 区块链适配器
        self.episodic     = episodic       # EpisodicMemory
        self.kb           = kb             # KnowledgeBase (共享)
        self._short_term: list[dict] = []  # 易失性对话窗口
        self._soul: str = ""               # soul.md 人格
        self._user_md: str = ""            # USER.md 用户画像

        # 加载人格 + 工具规范 + 用户画像
        self._load_soul()      # soul.md → cognition.md fallback
        self._load_tools_md()  # TOOLS.md
        self._load_user_md()   # USER.md
        self._load_short_term()  # 从磁盘恢复短期记忆
```

**人格加载搜索顺序** (`_load_soul()`):

```
soul.md 搜索路径 (优先):
  1. 配置中的 soul_file 路径
  2. skills/agents/{agent_id}/soul.md
  3. docs/{agent_id}/soul.md
  4. docs/shared/soul.md

cognition.md 回退 (legacy):
  1. 配置中的 cognition_file 路径
  2. skills/agents/{agent_id}/cognition.md
  3. docs/{agent_id}/cognition.md
  4. docs/shared/cognition.md
```

#### 系统提示词构建

系统提示词按优先级分层组装，总预算 **16000 tokens** (MiniMax 1M 上下文仅占 1.6%):

| 优先级 | 内容 | 估计 tokens |
|--------|------|-------------|
| P0 | Role (角色定义) | ~300 |
| P1 | Soul.md (人格) | ~500 |
| P2 | USER.md (用户画像) | ~200 |
| P3 | Skills (技能描述, 56个) | ~49000 (截断) |
| P4 | Episodic recall (情景记忆) | ~1500 |

> 当总 tokens 超出预算时，从 P4 开始逐级截断。

### 2.3 Runtime 抽象层

**目录**: `core/runtime/`

运行时抽象层支持三种 Agent 进程管理模式：

```
AgentRuntime (ABC)
├── ProcessRuntime    — mp.Process per agent (Phase 1, 稳定)
├── LazyRuntime       — 按需启动 + 空闲关停 (Phase 2, 当前启用)
└── InProcessRuntime  — asyncio.Task (Phase 3, 实验性)
```

#### ProcessRuntime (`core/runtime/process.py`)

每个 Agent 一个 `multiprocessing.Process`：

```python
class ProcessRuntime:
    def __init__(self):
        self._procs: dict[str, mp.Process] = {}  # agent_id → Process

    def start(self, agent_def, config, wakeup=None):
        cfg_dict = _build_agent_cfg_dict(agent_def, config)
        p = mp.Process(
            target=_agent_process,
            args=(cfg_dict, agent_def, config),
            kwargs={"wakeup": wakeup},
            name=f"agent-{agent_def['id']}",
        )
        p.start()
        self._procs[agent_def["id"]] = p

    @property
    def procs(self) -> list:
        return list(self._procs.values())
```

#### LazyRuntime (`core/runtime/lazy.py`) — 当前启用

**核心思想**: 只有 `always_on` 的 Agent (Leo) 立即启动，其余 Agent 在 TaskBoard 出现对应角色的 pending 任务时才按需启动，空闲后自动关停。

```python
class LazyRuntime:
    def __init__(self, config=None):
        runtime_cfg = config.get("runtime", {})
        self._always_on: set[str] = set(runtime_cfg.get("always_on", ["leo"]))
        self._idle_shutdown: int = runtime_cfg.get("idle_shutdown", 300)

        # 委托给 ProcessRuntime 执行实际的进程管理
        self._delegate = ProcessRuntime()

        self._agent_defs: dict[str, dict] = {}     # 注册的 agent 定义
        self._last_activity: dict[str, float] = {}  # 最后活动时间戳

    def start(self, agent_def, config, wakeup=None):
        agent_id = agent_def["id"]
        self._agent_defs[agent_id] = agent_def

        if agent_id in self._always_on:
            self._delegate.start(agent_def, config, wakeup)  # 立即启动
        else:
            logger.info("registered '%s' (lazy, not started)", agent_id)

    def ensure_running(self, agent_id, config=None, wakeup=None):
        """按需启动 — Orchestrator 在 MAS_PIPELINE 路由时调用"""
        if self.is_alive(agent_id):
            self._last_activity[agent_id] = time.time()
            return
        logger.info("[runtime:lazy] on-demand start for '%s'", agent_id)
        self._delegate.start(self._agent_defs[agent_id], config, wakeup)
```

**后台监控线程** (每 2 秒检查):

```python
def _start_idle_monitor(self):
    """两个职责:
    1. 检查 TaskBoard 的 pending 任务 → 按需启动对应 Agent
    2. 关停空闲超过 idle_shutdown 的 Agent
    """
    def _monitor():
        while not self._stop_monitor.is_set():
            self._stop_monitor.wait(timeout=2)
            self._check_pending_subtasks()  # 每 2s
            if int(time.time()) % 60 < 3:
                self._check_idle_agents()   # 每 ~60s

def _check_pending_subtasks(self):
    """读取 TaskBoard, 找到 pending 任务的 required_role,
    通过 _ROLE_TO_AGENTS 映射到 agent_id, 按需启动"""
    board = TaskBoard()
    data = board._read()
    for tid, t in data.items():
        if t.get("status") != "pending":
            continue
        role = t.get("required_role", "")
        candidate_ids = _ROLE_TO_AGENTS.get(role, set())
        for cid in candidate_ids:
            if cid in self._agent_defs and not self.is_alive(cid):
                self.ensure_running(cid)

def _check_idle_agents(self):
    """关停空闲超过 idle_shutdown 秒的 agent (always_on 除外)"""
    for agent_id in self._last_activity:
        if agent_id in self._always_on:
            continue
        idle_secs = time.time() - self._last_activity[agent_id]
        if idle_secs > self._idle_shutdown:
            self._delegate.stop(agent_id)
```

**LazyRuntime 资源节省效果**:

| 场景 | 运行的 Agent | 内存占用 |
|------|-------------|---------|
| 空闲 / 简单问答 | Leo only | ~600MB 节省 |
| MAS_PIPELINE 任务 | Leo + Jerry + Alic | 正常 |
| Jerry/Alic 空闲 5min | 自动关停 → Leo only | ~600MB 节省 |

#### DualWakeupBus (`core/runtime/wakeup.py`)

跨运行时唤醒机制 — 在 ProcessRuntime 用 `mp.Event`，在 InProcessRuntime 用 `asyncio.Event`：

```python
class DualWakeupBus:
    """Wakeup signal that works across both process and async runtimes."""
    def __init__(self):
        self._mp_event = mp.Event()
        self._async_event = asyncio.Event() if asyncio... else None

    def notify(self):
        self._mp_event.set()
    def wait(self, timeout=None):
        self._mp_event.wait(timeout)
        self._mp_event.clear()
```

### 2.4 TaskBoard — 任务看板

**文件**: `core/task_board.py`

基于文件锁的 JSON 存储，所有 Agent 进程通过文件锁并发安全地读写任务状态。

```python
BOARD_FILE = ".task_board.json"
BOARD_LOCK = ".task_board.lock"

CLAIMED_TIMEOUT = 180   # 3 min — agent 崩溃检测
REVIEW_TIMEOUT  = 300   # 5 min — reviewer 崩溃检测
```

#### 任务状态机

```
pending ──► claimed ──► review ──► completed
   │           │           │
   │           │           ▼
   │           │       critique ──► claimed (重做)
   │           │
   │           ▼
   │       blocked ──► pending (依赖完成后)
   │
   ▼
paused ──► pending (恢复)
   │
   ▼
cancelled

特殊状态:
  synthesizing — Leo 正在合成最终回复
  failed       — 执行失败
```

#### 角色路由映射

```python
_ROLE_TO_AGENTS = {
    "planner":    {"leo", "planner"},
    "plan":       {"leo", "planner"},
    "implement":  {"jerry", "executor", "coder", "developer", "builder"},
    "execute":    {"jerry", "executor", "coder", "developer", "builder"},
    "code":       {"jerry", "executor", "coder", "developer", "builder"},
    "review":     {"alic", "reviewer", "auditor"},
    "critique":   {"alic", "reviewer", "auditor"},
}

# 严格角色: 只有指定 agent 可认领
_STRICT_ROLES = {"planner", "plan", "review", "critique"}

# Agent 认领限制: reviewer 只能认领 review/critique 任务
_AGENT_CLAIM_RESTRICTIONS = {
    "alic":     {"review", "critique"},
    "reviewer": {"review", "critique"},
    "auditor":  {"review", "critique"},
}
```

#### 认领逻辑

```python
def _agent_may_claim(agent_id: str, required_role: str | None) -> bool:
    """受限 agent (alic/reviewer) 只能认领匹配角色的任务。
    非受限 agent (jerry/leo) 可认领任何任务。"""

def _role_matches(required_role: str, agent_id: str, agent_role: str) -> bool:
    """严格角色 (planner/review) → 只允许映射表内的 agent。
    其他角色 → 允许宽松匹配。"""
```

### 2.5 ContextBus — 上下文总线

**文件**: `core/context_bus.py`

分层 KV 存储，每个 Agent 在任务开始时读取，注入系统提示词。

```python
BUS_FILE = ".context_bus.json"

# 4 层上下文
LAYER_TASK    = 0   # 任务完成时清除
LAYER_SESSION = 1   # TTL = 3600s (1 小时)
LAYER_SHORT   = 2   # TTL = 86400s (1 天, 默认)
LAYER_LONG    = 3   # 永久

_DEFAULT_TTL = {
    LAYER_TASK:    None,     # 无自动过期, 显式清除
    LAYER_SESSION: 3600,     # 1 小时
    LAYER_SHORT:   86400,    # 1 天
    LAYER_LONG:    None,     # 永久
}
```

**发布接口**:

```python
class ContextBus:
    """文件锁 KV 存储, 命名空间: '{agent_id}:{key}'"""

    def publish(self, agent_id, key, value,
                layer=LAYER_SHORT, ttl=None, provenance=None):
        """
        Args:
            layer: 上下文层级 (LAYER_TASK..LAYER_LONG)
            provenance: 来源元数据
                kind: "external_user" | "inter_agent" | "system"
                source_agent, source_channel, source_task_id
        """
        entry = {
            "v": value,
            "layer": layer,
            "ttl": ttl or _DEFAULT_TTL.get(layer),
            "ts": time.time(),
        }
```

### 2.6 协议层

**文件**: `core/protocols.py`

V0.02 结构化协议定义 — 纯数据契约，零运行时依赖。

#### SubTaskSpec — 结构化任务工单

```python
@dataclass
class SubTaskSpec(JsonSerializable):
    """Leo → Jerry 的结构化任务工单 (替代 V0.01 的 TASK: 纯文本)"""
    objective: str                          # 目标描述
    constraints: list[str] = []             # 约束条件
    input: dict[str, Any] = {}              # 输入数据
    output_format: str = ""                 # markdown_table / json / code / file / text
    tool_hint: list[str] = []               # ToolCategory 值 (web/fs/automation...)
    complexity: str = "normal"              # simple / normal / complex
    parent_intent: str = ""                 # 原始用户意图
    a2a_hint: dict[str, Any] = {}           # A2A 外部委托提示

    def to_task_description(self) -> str:
        """序列化为 TaskBoard 的 description 字段"""
        lines = [f"[SubTaskSpec] {self.objective}"]
        if self.constraints:
            lines.append(f"Constraints: {'; '.join(self.constraints)}")
        if self.output_format:
            lines.append(f"Output format: {self.output_format}")
        if self.tool_hint:
            lines.append(f"Tool categories: {', '.join(self.tool_hint)}")
        return "\n".join(lines)
```

#### CritiqueSpec — 结构化审查协议

```python
@dataclass
class CritiqueDimensions:
    """5 维评分, 每维 1-10"""
    accuracy: int = 7       # 准确性 (30%)
    completeness: int = 7   # 完整性 (20%)
    technical: int = 7      # 技术质量 (20%)
    calibration: int = 7    # 校准度 (20%)
    efficiency: int = 7     # 资源效率 (10%)

    WEIGHTS = {
        "accuracy": 0.3, "completeness": 0.2,
        "technical": 0.2, "calibration": 0.2,
        "efficiency": 0.1,
    }

    @property
    def composite(self) -> float:
        """加权综合分 (1-10)"""
        return sum(getattr(self, dim) * w for dim, w in self.WEIGHTS.items())

class CritiqueVerdict(str, Enum):
    LGTM = "LGTM"              # 通过
    NEEDS_WORK = "NEEDS_WORK"  # 需要改进

@dataclass
class CritiqueSpec:
    """Alic 的结构化审查输出"""
    dimensions: CritiqueDimensions = field(default_factory=CritiqueDimensions)
    verdict: str = "LGTM"
    items: list[CritiqueItem] = field(default_factory=list)  # 改进项 (最多3个)
    confidence: float = 0.8
```

**审查规则**:
- 所有维度 ≥ 8 → `LGTM`, items 为空
- 任何维度 < 5 → `NEEDS_WORK`, 必须包含对应 item
- 最多 3 个 items

#### ToolCategory — 工具分类枚举

```python
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
    A2A = "a2a_delegate"
```

### 2.7 TaskRouter — 任务路由

**文件**: `core/task_router.py`

预路由逻辑 — 决定任务走 DIRECT_ANSWER (Leo 直接回答) 还是 MAS_PIPELINE (完整三 agent 流水线)。

```python
def classify_task(description: str) -> RouteDecision:
    """启发式任务分类:

    DIRECT_ANSWER 条件 (全部满足):
      1. 单一目标 (无多步骤指示器)
      2. 无工具/文件/执行信号
      3. 知识类问题或简短查询

    MAS_PIPELINE: 其他情况 (保守默认)
    """
    desc_lower = description.lower().strip()

    # 极短查询 → 直接回答
    if len(desc_lower) < 5:
        return RouteDecision.DIRECT_ANSWER

    # 多步骤指示器 → 必定 MAS
    if any(sig in desc_lower for sig in _MULTI_STEP_SIGNALS):
        return RouteDecision.MAS_PIPELINE

    # MAS 信号词 (工具/文件/执行) → MAS
    if any(sig in desc_lower for sig in _MAS_SIGNALS_ZH + _MAS_SIGNALS_EN):
        return RouteDecision.MAS_PIPELINE

    # 直接回答信号词 → DIRECT
    if any(sig in desc_lower for sig in _DIRECT_SIGNALS_ZH + _DIRECT_SIGNALS_EN):
        return RouteDecision.DIRECT_ANSWER

    # 短问句 → 直接回答
    if ("?" in description or "？" in description) and len(description) < 50:
        return RouteDecision.DIRECT_ANSWER

    # 默认: MAS (保守 — 不遗漏复杂任务)
    return RouteDecision.MAS_PIPELINE
```

**信号词表**:

```python
# MAS 信号 (中文)
_MAS_SIGNALS_ZH = ["写", "创建", "生成", "构建", "编写", "运行", "执行",
                   "搜索", "下载", "分析", "计算", "部署", "截图", ...]

# MAS 信号 (英文)
_MAS_SIGNALS_EN = ["write", "create", "generate", "build", "code",
                   "file", "run", "execute", "search", "download", ...]

# 多步骤信号
_MULTI_STEP_SIGNALS = [" and then ", "first ", "step 1", "步骤",
                       "然后再", "接着", "首先", "第一步", ...]

# 直接回答信号
_DIRECT_SIGNALS_ZH = ["什么是", "解释", "定义", "描述", "介绍", ...]
_DIRECT_SIGNALS_EN = ["what is", "explain", "define", "describe", ...]
```

---

## 3. 适配器层

### 3.1 LLM 适配器 — MiniMax

**文件**: `adapters/llm/minimax.py`

OpenAI 兼容的 SSE 流式适配器，支持模型：MiniMax-M2.5, MiniMax-M2.1, MiniMax-M2 及其 highspeed 变体。

```python
MINIMAX_BASE_URL = "https://api.minimax.io/v1"
```

**关键特性**:

1. **SSE 流式输出**: 与 OpenAI `/chat/completions` 协议完全兼容
2. **原生 Function Calling**: `tools` 参数传入工具 schema，`tool_calls` 响应转为 `<tool_code>` 文本格式
3. **截断恢复**: `_repair_truncated_json()` 修复 MiniMax 有时截断的 JSON 参数

```python
def _repair_truncated_json(raw: str) -> str | None:
    """修复截断的 JSON 工具调用参数。

    MiniMax 有时截断长字符串:
      {"content": "# Title\n\nsome text...   ← 缺少 "}

    策略:
      1. 找到最后一个完整句子边界
      2. 在该处截断值
      3. 关闭所有未闭合的 JSON 分隔符
    """
```

### 3.2 Provider Router — 跨提供商路由

**文件**: `core/provider_router.py`

位于 ResilientLLM 之上，实现跨 LLM 提供商的自动故障转移。

```
ProviderRouter
├── ProviderEntry(minimax, MinimaxAdapter, health, stats)  ← priority 1
├── ProviderEntry(openai,  OpenAIAdapter,  health, stats)  ← priority 2
└── ProviderEntry(ollama,  OllamaAdapter,  health, stats)  ← priority 3
    └── 每个 entry 内部 → ResilientLLM (模型级别故障转移)
```

**路由策略**:

| 策略 | 说明 |
|------|------|
| `latency` | EMA 加权延迟，选最快 |
| `cost` | 按 `cost_per_1k_tokens` 选最便宜 |
| `preference` | 优先选择配置的 `preferred` 提供商 |
| `round_robin` | 轮询 |

**断路器**: 连续失败 3 次 → 断路 120s → 自动恢复探测

```yaml
# agents.yaml 配置示例
provider_router:
  enabled: true
  strategy: "latency"
  preferred: "minimax"
  probe_interval: 60
  providers:
    minimax:
      models: ["MiniMax-M2.5-highspeed", "MiniMax-M2.5"]
      cost_per_1k_tokens: 0.001
      priority: 1
    openai:
      models: ["gpt-4o-mini", "gpt-4o"]
      cost_per_1k_tokens: 0.01
      priority: 2
    ollama:
      models: ["llama3.2", "qwen2.5"]
      cost_per_1k_tokens: 0
      priority: 3
```

### 3.3 记忆系统

#### HybridAdapter — 混合检索

**文件**: `adapters/memory/hybrid.py`

双路检索 + 倒数排名融合 (Reciprocal Rank Fusion, RRF):

```
Query
  ├──► ChromaDB 向量检索 ──► 语义相关结果 (排名 R₁)
  └──► BM25 关键字检索   ──► 关键词匹配结果 (排名 R₂)
       │
       ▼
  RRF 融合: score(d) = Σ 1/(k + rank_i(d))   k=60
       │
       ▼
  合并去重 → Top-K 结果
```

**BM25 自实现** (无外部依赖):

```python
class BM25Index:
    """自包含 BM25 索引, 支持增量文档添加和磁盘持久化"""
    def __init__(self, k1=1.5, b=0.75):
        self.k1 = k1; self.b = b
        self.docs: list[str] = []
        self.doc_ids: list[str] = []
        self.idf: dict[str, float] = {}
        self.avg_dl: float = 0.0

def _tokenize(text: str) -> list[str]:
    """分词器: 小写 + 按非字母数字拆分 + 过滤停词 (含 80+ 中文停词)"""
    tokens = re.findall(r'[a-z0-9\u4e00-\u9fff]+', text.lower())
    return [t for t in tokens if t not in _CHINESE_STOP_WORDS]
```

#### EpisodicMemory — 三层情景记忆

**文件**: `adapters/memory/episodic.py`

受 OpenViking 启发的渐进式加载架构:

| 层级 | 名称 | Token 预算 | 内容 |
|------|------|-----------|------|
| L0 | Atomic Index | ~100 tok | 标题 + 标签 + 评分 |
| L1 | Overview | ~500 tok | 摘要 + 关键决策 + 结果 |
| L2 | Full Detail | 完整 | 完整任务输入/输出 (按需加载) |

**存储布局**:

```
memory/agents/{agent_id}/
├── episodes/
│   └── {date}/
│       └── {task_id}.json     # L2 完整 episode
├── daily/
│   └── {date}.md              # 每日学习日志 (自动生成)
├── cases/
│   └── {case_hash}.json       # 提取的 问题→解决方案 案例
└── patterns/
    └── {pattern_hash}.json    # 跨任务重复模式
```

```python
def make_episode(agent_id, task_id, task_description, result,
                 score=None, tags=None, outcome=None,
                 error_type=None, model=None) -> dict:
    """从完成的任务创建结构化 episode

    outcome: "success" | "failure" | "partial"
    error_type: "timeout" | "tool_error" | "format_error" | "hallucination"
    """
    return {
        "l0": {  # Atomic (~100 tokens)
            "task_id": task_id,
            "title": task_description[:80],
            "tags": tags or [],
            "score": score,
            "ts": time.time(),
        },
        "l1": {  # Overview (~500 tokens)
            "summary": ...,
            "outcome": outcome,
            "key_decisions": ...,
        },
        "l2": {  # Full Detail
            "input": task_description,
            "output": result,
            "model": model,
        }
    }
```

#### Embedding 提供商

**文件**: `adapters/memory/embedding.py`

```python
def get_embedding_provider(config) -> EmbeddingProvider:
    """工厂函数: 根据配置选择 embedding 提供商

    当前支持:
      - chromadb_default: ChromaDB 内置 embedding (无需 API key)
      - openai: OpenAI text-embedding-3-small (需 OPENAI_API_KEY)
    """
```

### 3.4 通道系统

#### ChannelManager — 中央协调

**文件**: `adapters/channels/manager.py`

```python
PLATFORM_LIMITS = {
    "telegram": 4096,   # 消息长度限制
    "discord":  2000,
    "feishu":   10000,
    "slack":    4000,
}
TASK_TIMEOUT   = 600   # 10 分钟
POLL_INTERVAL  = 2     # TaskBoard 轮询间隔 (秒)
STATUS_INTERVAL = 30   # "仍在处理中" 提示间隔 (秒)
```

**任务处理流程**:

```
用户消息 (Telegram/Discord/...)
    │
    ▼
ChannelManager._submit_task()
    │
    ├── _ensure_agents_running()  ← Lazy-aware 健康检查
    │       │
    │       └── LazyRuntime: 只检查 always_on agent 健康
    │           ProcessRuntime: 检查所有 agent
    │
    ├── Orchestrator._launch_all() → TaskBoard 写入任务
    │
    └── _wait_for_result()  ← 轮询 TaskBoard 直到完成
            │
            ├── 每 2s 检查 TaskBoard 状态
            ├── 每 30s 发送 "仍在处理中" 提示
            └── 超时 600s → 返回超时错误
```

**Lazy-aware 健康检查**:

```python
# _ensure_agents_running() 中的健康检查
runtime = self._persistent_orch.runtime
alive_map = runtime.all_alive()

from core.runtime.lazy import LazyRuntime
if isinstance(runtime, LazyRuntime):
    # Lazy 模式: 只检查 always_on agent
    always_on = runtime._always_on
    check_map = {aid: v for aid, v in alive_map.items()
                 if aid in always_on}
else:
    check_map = alive_map

alive_count = sum(1 for v in check_map.values() if v)
total_count = len(check_map)

if total_count > 0 and alive_count == 0:
    # 所有 agent 退出 → 重启池
    runtime.clear()
    self._persistent_orch._launch_all()
```

#### 支持的通道

| 通道 | 认证模式 | 配置 |
|------|---------|------|
| Telegram | pairing (配对码) | `TELEGRAM_BOT_TOKEN` |
| Discord | pairing | `DISCORD_BOT_TOKEN` |
| 飞书 (Feishu) | pairing | `FEISHU_APP_ID` + `FEISHU_APP_SECRET` |
| Slack | pairing (Socket Mode) | `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` |

---

## 4. 工具系统

**文件**: `core/tools.py`

### 4.1 工具清单 (37 个工具, 10 组)

| 分组 | 工具 | 说明 |
|------|------|------|
| **Web** | `web_search`, `web_fetch` | Brave + Perplexity 搜索, 网页抓取 |
| **Filesystem** | `read_file`, `write_file`, `edit_file`, `list_dir` | 文件操作 |
| **Memory** | `memory_search`, `memory_save`, `kb_search`, `kb_write` | 记忆读写 |
| **Task** | `task_create`, `task_status`, `spawn_subagent` | 任务管理 |
| **Automation** | `exec`, `cron`, `process` | 命令执行, 定时任务 |
| **Skill** | `check_skill_deps`, `install_skill_cli`, `search_skills`, `install_remote_skill` | 技能管理 |
| **Browser** | `browser_navigate`, `browser_click`, `browser_fill`, `browser_get_text`, `browser_screenshot`, `browser_evaluate`, `browser_page_info` | 浏览器自动化 |
| **Media** | `screenshot`, `notify`, `analyze_image` | 截图, 通知, 图片分析 |
| **Messaging** | `send_mail`, `send_file`, `message` | 邮件, 文件, 消息 |
| **A2A** | `a2a_delegate` | 委托给外部 A2A 协议 Agent |

### 4.2 访问控制

```yaml
# agents.yaml 中的工具配置
agents:
  - id: leo
    tools:
      profile: minimal     # 预设: minimal / coding / full
      allow: []            # 额外允许
      deny: []             # 显式拒绝 (deny 优先于 allow)
  - id: jerry
    tools:
      profile: coding      # coding = minimal + exec/write_file/edit_file
```

### 4.3 审计日志

敏感工具调用 (exec, write_file 等) 自动记录到审计日志:

```python
_AUDIT_LOG = ".logs/tool_audit.log"

def _audit_log(tool_name, agent_id="unknown", **details):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "agent": agent_id,
        **details,
    }
    with open(_AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

### 4.4 技能系统

技能 (Skills) 是可热加载的 markdown 文档，注入 Agent 的系统提示词:

```
skills/
├── shared/           # 共享技能 (所有 agent 可用)
│   ├── _base.md      # 基础技能
│   ├── coding.md     # 编码技能
│   ├── review.md     # 审查技能
│   └── ...           # 56+ 技能
├── agents/           # 每 agent 专有
│   ├── leo/
│   │   └── soul.md   # Leo 人格
│   ├── jerry/
│   │   └── soul.md
│   └── alic/
│       └── soul.md
└── team.md           # 自动生成的团队技能摘要
```

---

## 5. 网关与 API

### 5.1 HTTP 网关

**文件**: `core/gateway.py`

轻量级 HTTP REST 网关，暴露 Cleo 为本地 API 服务 (默认端口 19789)。

**完整端点列表**:

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web Dashboard |
| GET | `/health` | 健康检查 |
| POST | `/v1/task` | 提交任务 |
| GET | `/v1/task/:id` | 查询任务状态 |
| GET | `/v1/status` | 完整任务面板 |
| POST | `/v1/agents` | 创建 Agent |
| DELETE | `/v1/agents/:id` | 删除 Agent |
| GET | `/v1/agents` | Agent 团队信息 |
| PUT | `/v1/agents/:id` | 更新 Agent 配置 |
| POST | `/v1/exec` | 执行命令 (需审批) |
| GET | `/v1/exec/approvals` | 审批白名单 |
| POST | `/v1/exec/approve` | 添加白名单 |
| GET | `/v1/cron` | 定时任务列表 |
| POST | `/v1/cron` | 创建定时任务 |
| DELETE | `/v1/cron/:id` | 删除定时任务 |
| POST | `/v1/cron/:id/run` | 手动触发任务 |
| GET | `/v1/scores` | 声誉评分 |
| GET | `/v1/usage` | 使用量统计 |
| GET | `/v1/config` | 配置信息 (脱敏) |
| GET | `/v1/doctor` | 健康检查 |
| GET | `/v1/skills` | 技能列表 |
| GET/PUT/DELETE | `/v1/skills/*` | 技能 CRUD |
| GET | `/v1/heartbeat` | Agent 心跳状态 |
| GET | `/v1/chain/*` | 区块链状态/余额/身份 |
| POST | `/v1/chain/*` | 链上初始化/注册 |
| GET | `/v1/memory/*` | 记忆系统状态/episodes/cases |
| GET | `/v1/logs/:agent_id` | Agent 日志 |

### 5.2 两条任务提交路径

```
路径 A — HTTP API:
  POST /v1/task
    → gateway._handle_submit_task()
    → 新建 Orchestrator
    → _launch_all() + _wait() (后台线程)
    → 返回 task_id, 客户端轮询 GET /v1/task/:id

路径 B — Telegram/Discord 通道:
  用户消息 → 通道适配器
    → ChannelManager._submit_task()
    → 持久化 Orchestrator (复用)
    → _ensure_agents_running() + _wait_for_result() (轮询)
    → 自动推送结果到聊天
```

### 5.3 WebSocket 网关

**文件**: `core/ws_gateway.py` (端口 19790)

```python
# 连接协议
# ws://localhost:19790?token={gateway_token}

class WSEvent:
    STATE = "state"              # 完整状态快照
    TASK_UPDATE = "task_update"  # 单任务更新
    ALERT = "alert"              # 告警
    AGENT_LOG = "agent_log"      # Agent 日志行

# Server → Client: {"event": "state", "data": {board, bus, agents}}
# Client → Server: {"action": "submit_task", "data": {...}}
```

**状态广播**: 每秒 (`1Hz`) 构建快照，读取 `.task_board.json` + `.context_bus.json`，推送给所有连接的 Dashboard 客户端。

---

## 6. 支撑系统

### 6.1 Cron 调度器

**文件**: `core/cron.py`

```python
JOBS_PATH = "memory/cron_jobs.json"
DEFAULT_JOB_TIMEOUT = 600  # 10 分钟

def _new_job(name, action, payload, schedule_type, schedule, ...):
    """创建任务:
    action:        "task" | "exec" | "webhook"
    schedule_type: "once" | "interval" | "cron"
    schedule:      ISO 时间戳 / 秒数 / cron 表达式 (5字段)
    """
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "action": action,         # task → 提交给 Orchestrator
        "payload": payload,       # exec → 执行命令
        "schedule_type": schedule_type,  # webhook → POST 请求
        "schedule": schedule,
        "enabled": True,
    }
```

### 6.2 声誉评分

**文件**: `reputation/scorer.py`

5 维 EMA (指数移动平均) 评分引擎:

```python
WEIGHTS = {
    "task_completion":  0.25,   # 任务完成率
    "output_quality":   0.30,   # 输出质量
    "improvement_rate": 0.25,   # 改进速率
    "consistency":      0.10,   # 一致性
    "review_accuracy":  0.10,   # 评审准确性
}
ALPHA = 0.3          # EMA 平滑因子
DEFAULT_SCORE = 70.0  # 新 Agent 起始分

class ScoreAggregator:
    """EMA 更新: new = α × signal + (1 - α) × old
    综合分 = Σ(dimension × weight)
    持久化到 memory/reputation_cache.json"""

    def update(self, agent_id, dimension, signal):
        with self.lock:
            cache = self._read_cache()
            agent = cache.setdefault(agent_id, self._default_entry())
            old = agent["dimensions"][dimension]
            new = ALPHA * signal + (1 - ALPHA) * old
            agent["dimensions"][dimension] = new
            agent["composite"] = sum(
                agent["dimensions"][d] * w for d, w in WEIGHTS.items()
            )
```

**可选区块链同步**: 分数变化超过 `min_score_delta` (5.0) 时同步到 ERC-8004 声誉注册表。

### 6.3 Doctor — 健康检查

**文件**: `core/doctor.py`

```python
def run_preflight() -> list[str]:
    """启动前快速预检:
    1. API key 是否配置?
    2. LLM 端点是否可达? (3s 超时)
    3. Gateway 端口是否空闲?
    返回问题列表 (空 = 全部通过)
    """
```

### 6.4 其他支撑

| 模块 | 文件 | 说明 |
|------|------|------|
| 速率限制 | `core/rate_limiter.py` | 令牌桶限流 |
| 用户认证 | `adapters/channels/session.py` | 配对码认证, 会话管理 |
| 使用量追踪 | `core/usage_tracker.py` | Token 用量 + 工具调用统计 |
| 用户档案 | `core/user_profile.py` | 用户偏好持久化 |
| 心跳 | `core/heartbeat.py` | Agent 存活检测 |
| 任务历史 | `core/task_history.py` | 已完成任务归档 |
| 异步包装 | `core/async_wrappers.py` | AsyncTaskBoardWrapper 等 |

---

## 7. 配置体系

**文件**: `config/agents.yaml`

```yaml
# ─── 运行时 ───
runtime:
  mode: lazy                        # process | in_process | lazy
  always_on: [leo]                  # LazyRuntime: 永不停止的 agent
  idle_shutdown: 300                # LazyRuntime: 空闲关停阈值 (秒)

# ─── A2A 协议 ───
a2a:
  server:
    enabled: false
    path: /a2a
  client:
    enabled: false
    remotes: []                     # 预注册的外部 Agent
    security:
      max_timeout: 600
      untrusted_require_confirmation: true

# ─── LLM ───
llm:
  provider: minimax                 # minimax | openai | ollama | flock

# ─── 记忆 ───
memory:
  backend: hybrid                   # hybrid = BM25 + ChromaDB
  long_term: true
  embedding:
    provider: chromadb_default      # chromadb_default | openai
  episodic:
    enabled: true
    recall_budget_tokens: 1500
  knowledge_base:
    enabled: true
    recall_budget_tokens: 800

# ─── 区块链 ───
chain:
  enabled: true
  network: base                     # Base L2
  lit:
    network: naga-dev               # Lit Protocol PKP
  erc8004:
    identity_registry_env: ERC8004_IDENTITY_REGISTRY
    reputation_registry_env: ERC8004_REPUTATION_REGISTRY
  reputation_sync:
    enabled: true
    min_score_delta: 5.0
    max_writes_per_hour: 10

# ─── 声誉 ───
reputation:
  peer_review_agents: [alic]
  evolution:
    prompt_auto_apply: true
    model_swap_require_confirm: true

# ─── 通道 ───
channels:
  telegram:
    enabled: true
    auth_mode: pairing
    bot_token_env: TELEGRAM_BOT_TOKEN
    mention_required: true
  discord:
    enabled: false
    auth_mode: pairing
  feishu:
    enabled: false
  slack:
    enabled: false

# ─── 工作空间 ───
workspace:
  path: workspace
  shared: true
max_idle_cycles: 120

# ─── 弹性 ───
resilience:
  base_delay: 1.0
  max_delay: 30.0
  jitter: 0.5
  circuit_breaker_threshold: 3
  circuit_breaker_cooldown: 120

# ─── 上下文压缩 ───
compaction:
  enabled: true
  max_context_tokens: 30000
  summary_target_tokens: 2000
  keep_recent_turns: 4

# ─── Agent 定义 (3个) ───
agents:
- id: leo
  role: "You are Leo, the BRAIN of the Cleo system..."
  model: MiniMax-M2.5
  fallback_models: [MiniMax-M2.1]
  skills: [_base, brainstorming, planning, ...]  # 56 个技能
  tools:
    profile: minimal
  memory:
    short_term_turns: 6
    episodic_recall_budget: 1500
    kb_recall_budget: 800
  llm:
    provider: minimax
    api_key_env: LEO_API_KEY

- id: jerry
  role: "You are Jerry, the HANDS of the Cleo system..."
  model: minimax-m2.5
  skills: [_base, coding, copywriting, ...]  # 56 个技能
  tools:
    profile: coding
  memory:
    short_term_turns: 20
    episodic_recall_budget: 2000

- id: alic
  role: "You are Alic, the EYES of the Cleo system..."
  model: minimax-m2.5
  skills: [_base, review, copywriting, ...]
  tools:
    profile: minimal
  memory:
    short_term_turns: 20
    episodic_recall_budget: 1000
    kb_recall_budget: 1000
```

---

## 8. 数据流图

### 8.1 完整请求处理链

```
┌──────────┐     ┌──────────────┐     ┌──────────────────────────┐
│  用户     │────►│  Telegram    │────►│  ChannelManager          │
│  (消息)   │     │  Bot         │     │  _submit_task()          │
└──────────┘     └──────────────┘     └────────────┬─────────────┘
                                                    │
                                      ┌─────────────▼──────────────┐
                                      │  Orchestrator               │
                                      │                             │
                                      │  1. TaskRouter.classify()   │
                                      │     ├─ DIRECT_ANSWER ──────►│── Leo 直接回答
                                      │     └─ MAS_PIPELINE         │
                                      │                             │
                                      │  2. Leo: 分解 SubTaskSpec   │
                                      │     (写入 TaskBoard)        │
                                      │                             │
                                      │  3. LazyRuntime 监控线程    │
                                      │     检测 pending subtask    │
                                      │     → ensure_running(jerry) │
                                      │                             │
                                      │  4. Jerry: 认领 + 执行      │
                                      │     (结果写回 TaskBoard)    │
                                      │                             │
                                      │  5. LazyRuntime 监控线程    │
                                      │     检测 review subtask     │
                                      │     → ensure_running(alic)  │
                                      │                             │
                                      │  6. Alic: 评审 CritiqueSpec │
                                      │                             │
                                      │  7. Leo: 合成最终回复       │
                                      │     (写入 ContextBus)       │
                                      └─────────────┬───────────────┘
                                                    │
                                      ┌─────────────▼──────────────┐
                                      │  ChannelManager             │
                                      │  _wait_for_result()         │
                                      │  (每 2s 轮询 TaskBoard)    │
                                      └─────────────┬───────────────┘
                                                    │
                                      ┌─────────────▼──────────────┐
                                      │  Telegram Bot               │
                                      │  发送回复给用户              │
                                      └────────────────────────────┘
```

### 8.2 TaskBoard 状态机

```
                    ┌─────────┐
                    │ PENDING │◄──────────────────────┐
                    └────┬────┘                       │
                         │ agent.claim()              │ 依赖完成
                         ▼                            │
                    ┌─────────┐                  ┌────┴─────┐
                    │ CLAIMED │──────────────────►│ BLOCKED  │
                    └────┬────┘ blocked_by未完成   └──────────┘
                         │ agent 完成执行
                         ▼
                    ┌─────────┐    NEEDS_WORK    ┌──────────┐
                    │ REVIEW  │─────────────────►│ CRITIQUE │
                    └────┬────┘                  └────┬─────┘
                         │ LGTM                       │ 重新执行
                         ▼                            ▼
                   ┌───────────┐              回到 CLAIMED
                   │ COMPLETED │
                   └───────────┘

  超时恢复:
    CLAIMED > 180s → PENDING (agent 崩溃)
    REVIEW  > 300s → PENDING (reviewer 崩溃)

  用户控制:
    任意状态 → PAUSED → PENDING (恢复)
    任意状态 → CANCELLED
```

### 8.3 LazyRuntime 生命周期

```
系统启动
    │
    ▼
LazyRuntime.start_all()
    │
    ├── Leo (always_on) ──► ProcessRuntime.start() ──► mp.Process 运行
    ├── Jerry (lazy) ──► 注册, 不启动
    └── Alic (lazy)  ──► 注册, 不启动
    │
    ▼
_start_idle_monitor() ──► 后台线程 (每 2s 循环)
    │
    │  检测到 pending subtask (required_role: "implement")
    │  └─► _ROLE_TO_AGENTS["implement"] → jerry
    │      └─► ensure_running("jerry") → mp.Process 启动
    │
    │  Jerry 完成 → TaskBoard 写入 review 任务
    │  └─► _ROLE_TO_AGENTS["review"] → alic
    │      └─► ensure_running("alic") → mp.Process 启动
    │
    │  Alic 完成审查 → Leo 合成回复
    │
    │  Jerry/Alic 空闲 > 300s
    │  └─► _check_idle_agents() → stop("jerry"), stop("alic")
    │
    │  下次 MAS_PIPELINE 任务到来 → 重复上述流程
    │
    ▼
LazyRuntime.stop_all() ──► 关停监控线程 + 所有进程
```

---

## 9. 部署与运维

### 9.1 启动命令

```bash
# 启动网关 (HTTP + WebSocket + 通道适配器)
python main.py gateway start

# 前台交互模式
python main.py chat

# 健康检查
python main.py doctor
```

### 9.2 日志体系

```
.logs/
├── leo.log       # Leo 进程日志 (stdout/stderr 重定向)
├── jerry.log     # Jerry 进程日志
├── alic.log      # Alic 进程日志
└── tool_audit.log  # 敏感工具调用审计
```

### 9.3 测试

```bash
# 运行全部测试 (399 个)
python -m pytest tests/ -x -q

# 测试覆盖模块:
# - core/ (orchestrator, agent, task_board, context_bus, protocols, tools...)
# - adapters/ (llm, memory, channels...)
# - reputation/
# - runtime/
```

### 9.4 DUAL-sync 规则

所有代码变更必须同步到两个位置:

```bash
# 源码目录
/Users/leomacmini/cleo-dev/

# pip editable install 目录
/Users/leomacmini/cleo-dev/.venv/src/cleo-agent-stack/

# 同步命令
cp /Users/leomacmini/cleo-dev/{file} \
   /Users/leomacmini/cleo-dev/.venv/src/cleo-agent-stack/{file}
```

### 9.5 关键环境变量

| 变量 | 用途 |
|------|------|
| `MINIMAX_API_KEY` | MiniMax LLM API 密钥 |
| `LEO_API_KEY` / `JERRY_API_KEY` / `ALIC_API_KEY` | 每 Agent 独立密钥 (可选) |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `DISCORD_BOT_TOKEN` | Discord Bot Token |
| `CHAIN_PRIVATE_KEY` | 区块链操作者私钥 |
| `BASE_RPC_URL` | Base L2 RPC URL |
| `GATEWAY_TOKEN` | HTTP/WebSocket 网关认证令牌 |

---

> **文档结束** — Cleo V0.02 技术架构文档
