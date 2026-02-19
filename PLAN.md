# Swarm 架構重構計劃：從串行 Review 到智能協作

## 目標

將 Planner → Executor → Reviewer 的固定串行 pipeline，改為：
- Planner 前後都管（拆解 + 收口合成）
- Reviewer 從必經的打分機器 → 按需的專家顧問（智庫角色）
- 打回不是全部重做，而是定向修正（critique → fix 循環）
- 加入複雜度判斷 — 簡單任務跳過 review

## 架構變化總覽

```
舊: Planner(拆解) → Executor(執行) → Reviewer(打分 pass/fail)

新: Planner(拆解+派工)
    → Executor(執行)
    → [簡單任務] Planner 收口合成 → 完成
    → [複雜任務] Reviewer 給 critique + 修復建議
      → Executor 定向修正（不是全部重做）
      → Planner 收口合成 → 完成
```

---

## Phase 1: TaskBoard 新增狀態 + 定向修正流程

**文件**: `core/task_board.py`

### 1.1 新增 TaskStatus.CRITIQUE (line ~90)

```python
CRITIQUE  = "critique"   # 智庫給了修復建議，等 Executor 定向修正
```

### 1.2 Task dataclass 新增欄位 (line ~96)

```python
complexity: str = "normal"       # "simple" | "normal" | "complex"
critique: dict | None = None     # {reviewer, passed, suggestions, comment, ts}
critique_round: int = 0          # 當前修正輪次 (max=1)
```

### 1.3 新增 `add_critique()` 方法

替代原來純分數的 `add_review()`，智庫反饋變為結構化 critique：

```python
def add_critique(self, task_id, reviewer_id, passed, suggestions, comment):
    """智庫提交 critique：通過 or 帶修復建議的打回"""
    t["critique"] = {
        "reviewer": reviewer_id,
        "passed": passed,
        "suggestions": suggestions or [],
        "comment": comment,
        "ts": time.time(),
    }
    if passed:
        t["status"] = TaskStatus.COMPLETED.value
        t["completed_at"] = time.time()
    else:
        t["status"] = TaskStatus.CRITIQUE.value
        t["critique_round"] = t.get("critique_round", 0) + 1
```

### 1.4 新增 `claim_critique()` — Executor 認領修正任務

```python
def claim_critique(self, agent_id, agent_role=None):
    """Executor 認領 status=CRITIQUE 的任務做定向修正"""
    # 找 status=CRITIQUE 且 agent_id 匹配原執行者的任務
    # 設 status=CLAIMED，保留 critique 和原 result
```

### 1.5 修改 `complete()` (line 254)

```python
def complete(self, task_id):
    """簡化：直接標記完成，不再檢查 review score"""
    # 移除 avg_review_score < 60 打回邏輯
    # 直接設 status=COMPLETED, completed_at=now
```

### 1.6 保留 `submit_for_review()` (line 226) — 不改，仍用於送 critique

### 1.7 修改 `recover_stale_tasks()` (line 374)

```python
# 新增 CRITIQUE 超時回收:
# stale CRITIQUE (> 5 min): 強制完成（使用原 result）
```

---

## Phase 2: Orchestrator 流程重構

**文件**: `core/orchestrator.py`

### 2.1 Planner 拆解時標記複雜度 (修改 _extract_and_create_subtasks, line 179)

```python
def _extract_and_create_subtasks(board, planner_output, parent_id):
    # 現有: 解析 TASK: 行
    # 新增: 解析 COMPLEXITY: simple|normal|complex (從 planner output)
    # 預設規則:
    #   含 "review"/"audit"/"verify"/"analyze" → complex
    #   含 "fix"/"update"/"change" → normal
    #   含 "list"/"show"/"get" → simple
```

### 2.2 Planner 不再 auto-complete (修改 line 380-387)

```python
# 舊: planner auto-completes 自己
# 新: planner 完成拆解後進入 "waiting" 狀態
#     記錄 parent_task_id → subtask_ids 的映射
#     等所有 subtasks completed → 觸發收口
```

### 2.3 新增 Planner 收口函數

```python
async def _planner_close_out(agent, board, parent_task_id, config):
    """Planner 收口：合成所有子任務結果為最終輸出"""
    results = board.collect_results(parent_task_id)
    prompt = f"你之前拆解了任務。以下是各子任務的執行結果：\n\n{results}\n\n"
             f"請合成為一個完整、連貫的最終答案，直接面向用戶。"
    messages = [{"role": "system", "content": agent.cfg.role},
                {"role": "user", "content": prompt}]
    final = await agent.llm.chat(messages, agent.cfg.model)
    board.complete(parent_task_id)
    # 更新 parent task 的 result 為合成結果
```

### 2.4 Executor 完成後的路由邏輯 (重寫 line 389-414)

```python
# 舊: 一律 submit_for_review → 發 mailbox 給 reviewer
# 新:
is_simple = task.complexity == "simple"
if is_simple:
    board.complete(task.task_id)  # 跳過 review
    logger.info("simple task %s auto-completed", task.task_id)
else:
    board.submit_for_review(task.task_id, result)
    # 發 critique_request (不是 review_request)
    for r_id in reviewers:
        if r_id != agent.cfg.agent_id:
            agent.send_mail(r_id,
                _json_critique_request(task, result),
                msg_type="critique_request")
```

### 2.5 重寫 review handler → critique handler (替換 line 118-175)

```python
async def _handle_critique_request(agent, board, mail, sched):
    """智庫模式：不打分，給結構化 critique"""
    payload = json.loads(mail["content"])
    task_id, description, result = payload["task_id"], payload["description"], payload["result"]

    prompt = (
        f"Review the following task output.\n\n"
        f"## Task\n{description}\n\n"
        f"## Output\n{result}\n\n"
        f"Decide: is this ready to deliver?\n"
        f'If YES: {{"passed": true, "comment": "brief praise"}}\n'
        f'If NO: {{"passed": false, "suggestions": ["fix1", "fix2"], "comment": "why"}}\n'
        f"Max 3 suggestions, each must be specific and actionable."
    )
    raw = await agent.llm.chat([
        {"role": "system", "content": agent.cfg.role},
        {"role": "user", "content": prompt}
    ], agent.cfg.model)

    critique = json.loads(raw)
    passed = critique.get("passed", True)
    suggestions = critique.get("suggestions", [])
    comment = critique.get("comment", "")

    board.add_critique(task_id, agent.cfg.agent_id, passed, suggestions, comment)
    await sched.on_critique(agent.cfg.agent_id, passed)

    if not passed:
        logger.info("critique REJECTED task %s with %d suggestions", task_id, len(suggestions))
    else:
        logger.info("critique APPROVED task %s", task_id)
```

### 2.6 Executor 處理 CRITIQUE 修正 (在 _agent_loop claim 邏輯中新增)

```python
# 在主循環 claim_next 之前，先檢查 CRITIQUE 任務
critique_task = board.claim_critique(agent_id)
if critique_task:
    suggestions = critique_task.critique.get("suggestions", [])
    fix_prompt = (
        f"你之前提交了以下結果:\n{critique_task.result}\n\n"
        f"智庫給了修正建議:\n"
        + "\n".join(f"- {s}" for s in suggestions) +
        f"\n\n請針對以上建議修正輸出，只修改需要改的部分。"
    )
    result = await agent.run_with_prompt(fix_prompt, bus)

    # 修正後: 如果已經是第 1 輪 critique → 直接完成（不再送 review）
    if critique_task.critique_round >= 1:
        board.complete(critique_task.task_id)  # 強制完成
    else:
        board.submit_for_review(critique_task.task_id, result)  # 可再送一次
```

### 2.7 Planner 監控子任務 + 觸發收口 (在 Planner 的 _agent_loop 中)

```python
# Planner 每次循環額外檢查:
# 1. 找到自己創建的 parent tasks
# 2. 如果所有 subtasks 都 completed → 呼叫 _planner_close_out()
# 這讓 Planner 持續「值班」直到所有工作完成
```

### 2.8 mailbox 消息類型更新

```python
# 舊: msg_type="review_request"  → _handle_review_request()
# 新: msg_type="critique_request" → _handle_critique_request()
# 保留舊類型作為 fallback 以防相容性問題
```

---

## Phase 3: Agent Config + Skills 更新

**文件**: `config/agents.yaml`, `skills/`

### 3.1 Reviewer 角色 prompt 更新 (agents.yaml line 101-105)

```yaml
- id: reviewer
  role: >
    Quality advisor. Review task outputs and provide structured feedback.
    If output is ready to ship: {"passed": true, "comment": "..."}
    If needs revision: {"passed": false,
      "suggestions": ["specific fix 1", "specific fix 2"],
      "comment": "..."}
    Be specific with actionable fix recommendations. Max 3 suggestions.
```

### 3.2 Planner 角色 prompt 新增收口職責 (agents.yaml line 54-58)

```yaml
- id: planner
  role: >
    Strategic planner. Decompose user requests into subtasks.
    Write TASK: per line for each subtask. Do not implement yourself.
    For each task, add COMPLEXITY: simple|normal|complex.
    After all subtasks complete, synthesize a final unified answer.
```

### 3.3 更新 skills/review.md

```markdown
## Quality Advisor Guidelines
- Decision: PASS or NEEDS REVISION (不用數字分數)
- If PASS: briefly explain what was done well
- If NEEDS REVISION:
  - List specific, actionable suggestions (max 3)
  - Each suggestion = a concrete fix, not vague criticism
  - Prioritize by importance
- Always respond JSON:
  - {"passed": true, "comment": "..."}
  - {"passed": false, "suggestions": ["...", "..."], "comment": "..."}
```

### 3.4 skills/planning.md 追加收口指令

```markdown
## Closing Out Tasks
When all subtasks are completed, synthesize a final answer:
- Combine outputs, resolve contradictions
- Present as one unified user-facing response
- Remove internal task references
```

### 3.5 skills/coding.md 追加修正指令

```markdown
## Handling Review Feedback
When you receive critique suggestions:
- Address EACH suggestion specifically
- Only modify parts that need fixing (don't rewrite everything)
- Explain what you changed
```

---

## Phase 4: 聲譽系統適配

**文件**: `reputation/scheduler.py`, `reputation/peer_review.py`

### 4.1 scheduler.py — 新增 on_critique() (替代 on_review)

```python
async def on_critique(self, reviewer_id, passed):
    """智庫提交了 critique"""
    # 更新 reviewer 的 review_accuracy:
    #   合理的 critique (有具體 suggestions) → 85
    #   總是 pass → 60 (可能太寬鬆)
    #   總是 reject → 65 (可能太嚴格)

async def on_critique_result(self, agent_id, passed_first_time, had_revision):
    """Executor 的任務被 critique 後的結果"""
    # passed first time → output_quality = 90
    # passed after revision → output_quality = 70
    # forced complete after max rounds → output_quality = 50
```

### 4.2 peer_review.py — 簡化 anti-cheating

```python
# 移除: mutual_inflation (單 reviewer 無意義)
# 移除: consensus_deviation (單 reviewer 無意義)
# 保留: extreme_bias → 改為 always_pass_bias (>80% pass rate → 警告)
# 新增: suggestion_quality → 如果 suggestions 總是空/重複 → 降權
```

---

## Phase 5: 前端 Dashboard 更新

**文件**: `core/dashboard.html`

### 5.1 Header Bar Agent Chips

```
Reviewer chip: icon 🔍 → 🧠, 名稱 "Reviewer" → "Advisor"
狀態: "reviewing" → "advising"
```

### 5.2 updateWorkflow() 適配新狀態 (~line 1778)

```javascript
// 新增: task.status === 'critique' → executor chip 高亮 "fixing"
// 修改: task.status === 'review' → advisor chip active (不是 reviewer)
```

### 5.3 diffAndRoute() 新增 critique 狀態 dispatch 消息

```javascript
// 新增處理:
// review → "🧠 Advisor reviewing..."
// critique → "📝 Revision needed: 2 suggestions"  (帶 suggestions 預覽)
// critique 修正完成 → "✓ Revised and resubmitted"
// planner 收口 → "📋 Planner synthesizing final answer..."
// simple 任務跳過 → "⚡ Simple task auto-completed"
```

### 5.4 renderChatMsgHtml() — 新增 critique 展示 (~line 1201)

```javascript
// assistant bubble 中:
// 舊: score/100 badge
// 新: ✓ Approved / ⚠ Needs revision badge
// suggestions 列表顯示 (如果有)
// "1st attempt" / "Revised" 標記
```

### 5.5 Welcome 文案 (~line 567-571)

```
舊: "planned, executed, and reviewed"
新: "planned, executed, and quality-checked"
```

### 5.6 Chat live status 適配

```javascript
// reviewer working → "🧠 Advisor analyzing..."
// executor 在 critique 後 → "⚙️ Executor fixing..."
```

---

## Phase 6: 測試更新

**文件**: `tests/test_task_board.py`, `tests/test_p2_p3.py`

### 6.1 test_task_board.py 新增

- `test_critique_flow`: submit → critique(not passed) → claim_critique → fix → complete
- `test_simple_task_skip_review`: simple complexity → 直接完成
- `test_critique_max_rounds`: 超過 1 次 critique → 強制完成
- `test_critique_passed`: critique passed → 直接 completed
- `test_recover_stale_critique`: CRITIQUE 超時 → 強制完成

### 6.2 修改現有測試

- `test_submit_review_complete`: 適配 critique 結構
- peer review anti-cheating tests: 適配新邏輯

---

## 實施順序

| 階段 | 內容 | 依賴 | 預估改動 |
|------|------|------|---------|
| Phase 1 | TaskBoard 新狀態+方法 | 無 | ~80 行 |
| Phase 3 | Config/Skills prompts | 無 | ~40 行 |
| Phase 2 | Orchestrator 核心重構 | Phase 1 | ~150 行 |
| Phase 4 | Reputation 適配 | Phase 2 | ~50 行 |
| Phase 5 | Dashboard 前端 | Phase 2 | ~80 行 |
| Phase 6 | Tests | Phase 1-4 | ~120 行 |

**總計**: ~520 行改動（新增+修改）

Phase 1+3 可以先做，不破壞現有流程（新狀態和 prompts 是增量的）。
Phase 2 是核心斷裂點，需要和 Phase 4-6 一起完成。
