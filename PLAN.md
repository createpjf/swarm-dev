# Swarm 全面排查与修复方案

## 审计范围
- 核心模块 (core/) — 8 个文件
- 适配器 (adapters/) — 10 个文件
- 信誉系统 (reputation/) — 3 个文件
- CLI 入口 (main.py, onboard.py, daemon.py)
- 网关 (gateway.py)
- 测试套件 (tests/)

---

## P0 — 会崩溃的 Bug（必须立即修复）

### P0-1: orchestrator.py:172 — 空指针崩溃
```python
completed = board.complete(task_id)
if "review_failed" in completed.evolution_flags:  # completed 可能是 None
```
`board.complete()` 可能返回 None，直接访问 `.evolution_flags` 会抛 AttributeError，导致 reviewer 进程崩溃。

**修复**: 加 `if completed and "review_failed" in completed.evolution_flags:`

### P0-2: openai.py — chat() 和 chat_stream() 无任何错误处理
flock.py 有完整的 try/except 包裹，而 openai.py 的 `chat()` 和 `chat_stream()` 完全没有错误处理。HTTP 错误、超时、JSON 解析失败都会直接崩溃。

**修复**: 对齐 flock.py 的错误处理模式：捕获 HTTPStatusError、ConnectError、TimeoutException

### P0-3: openai.py — chat_with_usage() 错误消息写错了 provider 名
```python
raise RuntimeError(f"FLock API connection error: {e}")  # 应该是 OpenAI
```
复制粘贴错误，用户看到错误消息指向 FLock 但实际用的是 OpenAI。

**修复**: 改成 `"OpenAI API connection error: {e}"`

### P0-4: main.py — `swarm`(无参数) 显示 help 而不是进入交互模式
```python
else:
    parser.print_help()  # 应该是 interactive_main()
```
用户运行 `swarm` 期望进入聊天模式（文档也这样说），实际显示 help。

**修复**: `args.cmd is None` 时调用 `interactive_main()`

### P0-5: gnosis_safe.py:357-363 — 签名格式错误
```python
r = b'\x00' * 12 + addr_bytes  # r 应该是 32 字节, 这里是 44 字节
s = b'\x00' * 32
v = b'\x01'
sig = r + s + v  # 总共 77 字节（应该是 65）
```
签名总长度应为 65 字节 (32+32+1)，但当前代码生成 77 字节，合约校验永远失败。

**修复**: `r = b'\x00' * 12 + addr_bytes` → 确保 r 恰好 32 字节

---

## P1 — 高优先级逻辑错误

### P1-1: usage_tracker.py — Budget 检查在锁外
```python
with self.lock:
    data = self._read()
    self._write(data)
# ↓ 在锁外检查预算！并发写可能漏判
self._check_budget(agg)
```
多 agent 同时消费 token 时，预算检查可能漏判，允许超支。

**修复**: 将 `_check_budget()` 移入 `with self.lock:` 块内

### P1-2: orchestrator.py:359-367 — BudgetExceeded 绕过审核流程
budget 超限时直接 `board.complete(task_id)` 跳过了 reviewer 审核。

**修复**: 遵循正常 submit_for_review → review → complete 流程，或至少标记为 failed

### P1-3: evolution.py:270-336 — cast_vote() TOCTOU 竞争
```python
if not os.path.exists(path):      # 检查
    return {"error": "no pending vote"}
lock = self._get_lock(path)       # 文件可能在此间被删除
with lock:
    with open(path, "r") as f:    # FileNotFoundError!
```

**修复**: 将 exists 检查移入 lock 内部

### P1-4: config_manager.py — snapshot 无文件锁
多个进程同时 snapshot 时，dedup 检查（比较 hash）不是原子的，可能生成重复备份。

**修复**: 在 snapshot() 中使用 FileLock

### P1-5: daemon.py — LaunchAgent KeepAlive 配置错误
```xml
<key>SuccessfulExit</key>
<false/>  <!-- 只在异常退出时重启，正常退出不重启 -->
```
网关正常停止（exit code 0）后 LaunchAgent 不会重启它。

**修复**: 改为 `<true/>` 或使用 `<key>KeepAlive</key><true/>`

### P1-6: task_board.py — 恢复 stale REVIEW 任务时未清除 agent_id
当 review 超时且分数 <60 时，任务回到 PENDING 但 agent_id 仍指向旧 agent。

**修复**: 在 reset 分支中加 `t["agent_id"] = None`

---

## P2 — 中等优先级设计问题

### P2-1: gateway.py 全局变量线程安全
`_token`, `_config`, `_start_time` 是模块级全局变量，多线程 start_gateway() 会互相覆盖。

**修复**: 封装到 `_GatewayState` dataclass 中，或使用 threading.Lock 保护写入

### P2-2: gateway.py — Content-Length 无上限
`length = int(self.headers.get("Content-Length", 0))` 不限大小，恶意请求可耗尽内存。

**修复**: 加 `MAX_BODY_SIZE = 10 * 1024 * 1024`（10MB），超出返回 413

### P2-3: gateway.py — SSE 连接无超时
`/v1/events` 的 `while True` 循环无最大迭代，客户端断开后连接可能泄漏。

**修复**: 加 max_iterations (如 3600 次 × 1.5s = 约 90 分钟) 或心跳检测

### P2-4: onboard.py — .env 写入不是原子操作
`_write_env()` 直接 read → modify → write，如果写入中途崩溃 .env 会损坏。

**修复**: 写 .env.tmp → os.replace() 原子替换

### P2-5: peer_review.py — 分数无边界校验
`record_review()` 不验证 score 范围，负数或 >100 的分数被静默接受。

**修复**: 加 `score = max(0, min(100, score))`

### P2-6: chain adapter — 错误返回值用字符串伪装 tx hash
`"0x_stub"`, `"0x_not_registered"` 看起来像 tx hash 但不是，调用方无法区分成功/失败。

**修复**: 失败时 raise 异常或返回 `{"error": "...", "tx_hash": None}`

### P2-7: workflow.py — 失败依赖不传播
一个 step 失败后，依赖它的下游 step 永远停在 PENDING，不会被标记为 SKIPPED。

**修复**: 在 runnable 检查时加入 failed-dependency → SKIPPED 逻辑

### P2-8: logging_config.py — correlation_id 不跨子进程
使用 `threading.local()` 存储 correlation_id，子进程继承不到。

**修复**: 通过环境变量 `SWARM_CORRELATION_ID` 传递

---

## P3 — 低优先级改进

### P3-1: context_bus.py — snapshot() 无文件锁
### P3-2: heartbeat.py — OSError 被静默吞掉（仅 debug 级日志）
### P3-3: env_loader.py — 无转义序列处理（\n 被存为字面值）
### P3-4: daemon.py — 无端口范围校验 (1024 < port < 65536)
### P3-5: gateway.py — SSE state hash 用了不稳定的 Python hash()
### P3-6: gateway.py — skill 文件大小无限制

---

## P4 — 测试覆盖率严重不足

当前状态: **58 个测试，约 25-30% 覆盖率，12 个核心模块 + 13 个适配器零测试**

### P4-1: 弱断言修复（5处）
- `assert len(pending) >= 0` — 永远为真
- `assert "error" not in result or ...` — 逻辑 OR 几乎全通过
- `assert 0.5 <= weight <= 1.0` — 范围过宽

### P4-2: 缺失的关键测试模块
| 模块 | 行数 | 重要性 |
|------|------|--------|
| core/orchestrator.py | 715 | 核心编排 |
| core/agent.py | 363 | Agent 基类 |
| core/workflow.py | 369 | 工作流引擎 |
| reputation/scheduler.py | 121 | 任务生命周期 → 信誉触发 |
| adapters/llm/*.py | ~400 | LLM 调用 |
| core/gateway.py | 1600 | HTTP API |

### P4-3: 缺少的测试场景
- 并发竞争（多 agent 抢同一 task）
- 文件锁冲突
- JSON 损坏恢复
- 信誉计算边界值（除零、负分、溢出）

---

## 实施计划

| 阶段 | 内容 | 预估改动 |
|------|------|---------|
| **Phase 1** | P0 (5项 崩溃级 Bug) | ~50 行 |
| **Phase 2** | P1 (6项 高优先级) | ~80 行 |
| **Phase 3** | P2 (8项 中等优先级) | ~120 行 |
| **Phase 4** | P3+P4 测试修复+弱断言 | ~60 行 |

Phase 1-3 修改后立即运行全量测试验证不引入回归。
