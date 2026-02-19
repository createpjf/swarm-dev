"""
core/i18n.py
Lightweight internationalization support.
Usage:
    from core.i18n import t
    t("task.completed")  # returns localized string

Locale detection: SWARM_LANG env var → system locale → "en"
"""

from __future__ import annotations
import os
import locale

# ── Locale detection ─────────────────────────────────────────────────────────

def _detect_locale() -> str:
    """Detect locale from env or system. Returns 'en' or 'zh'."""
    lang = os.environ.get("SWARM_LANG", "")
    if lang:
        return "zh" if lang.startswith("zh") else "en"
    try:
        sys_locale = locale.getdefaultlocale()[0] or ""
        if sys_locale.startswith("zh"):
            return "zh"
    except Exception:
        pass
    return "en"


_locale = _detect_locale()

# ── Translation tables ───────────────────────────────────────────────────────

_STRINGS = {
    # ── Task statuses ────────────────────────────────────────────────────
    "status.pending":     {"en": "Waiting…",       "zh": "等待中…"},
    "status.working":     {"en": "Working…",       "zh": "处理中…"},
    "status.review":      {"en": "Reviewing…",     "zh": "审查中…"},
    "status.done":        {"en": "Done",           "zh": "完成"},
    "status.failed":      {"en": "Failed",         "zh": "失败"},
    "status.cancelled":   {"en": "Cancelled",      "zh": "已取消"},
    "status.paused":      {"en": "Paused",         "zh": "已暂停"},

    # ── Summary (live status) ────────────────────────────────────────────
    "summary.done":       {"en": "Done",           "zh": "完成"},
    "summary.finished":   {"en": "Finished",       "zh": "结束"},
    "summary.working":    {"en": "working",        "zh": "进行中"},
    "summary.failed":     {"en": "failed",         "zh": "失败"},
    "summary.cancelled":  {"en": "cancelled",      "zh": "已取消"},
    "summary.elapsed":    {"en": "elapsed",        "zh": "耗时"},

    # ── Errors ───────────────────────────────────────────────────────────
    "error.api_key":      {"en": "Invalid API Key (401)",    "zh": "API Key 无效 (401)"},
    "error.api_key_expired": {"en": "API Key invalid or expired", "zh": "API Key 无效或过期"},
    "error.forbidden":    {"en": "Forbidden (403)",          "zh": "权限不足 (403)"},
    "error.rate_limit":   {"en": "Rate limited (429)",       "zh": "请求过多 (429)"},
    "error.timeout":      {"en": "Request timed out",        "zh": "请求超时"},
    "error.connect":      {"en": "Cannot connect to API",    "zh": "无法连接 API"},
    "error.exec_failed":  {"en": "Execution failed",         "zh": "执行失败"},

    # ── Commands ─────────────────────────────────────────────────────────
    "cmd.cleared":        {"en": "Cleared",                  "zh": "已清除"},
    "cmd.cancelled":      {"en": "Cancelled",                "zh": "已取消"},
    "cmd.bye":            {"en": "Bye!",                     "zh": "再见！"},
    "cmd.no_tasks":       {"en": "No tasks yet.",            "zh": "暂无任务。"},
    "cmd.no_active":      {"en": "No active tasks to cancel.", "zh": "没有可取消的任务。"},
    "cmd.force_cleared":  {"en": "Force cleared.",           "zh": "已强制清除。"},
    "cmd.unknown_cmd":    {"en": "Unknown command: {cmd}",   "zh": "未知命令：{cmd}"},
    "cmd.active_exist":   {"en": "Active tasks exist. Force clear?",
                           "zh": "有进行中的任务，确认清除？"},
    "cmd.no_result":      {"en": "No result returned.",      "zh": "未返回结果。"},

    # ── Cancel / Clear ───────────────────────────────────────────────────
    "cancel.select":      {"en": "Select tasks to cancel:",  "zh": "选择要取消的任务："},
    "cancel.done":        {"en": "Cancelled {n} task(s).",   "zh": "已取消 {n} 个任务。"},
    "cancel.none_selected": {"en": "No tasks selected.",     "zh": "未选择任何任务。"},
    "clear.select":       {"en": "What to clear:",           "zh": "选择要清除的内容："},
    "clear.tasks":        {"en": "Task history",             "zh": "任务历史"},
    "clear.context":      {"en": "Context bus",              "zh": "上下文总线"},
    "clear.mailboxes":    {"en": "Agent mailboxes",          "zh": "代理邮箱"},
    "clear.usage":        {"en": "Usage statistics",         "zh": "使用统计"},

    # ── Config ───────────────────────────────────────────────────────────
    "config.no_backups":  {"en": "No config backups yet.",   "zh": "暂无配置备份。"},
    "config.select_ver":  {"en": "Select version to restore:", "zh": "选择要恢复的版本："},
    "config.rolled_back": {"en": "Config restored.",         "zh": "配置已恢复。"},
    "config.rollback_fail": {"en": "Rollback failed — no backups found.",
                             "zh": "回滚失败 — 未找到备份。"},

    # ── Budget ───────────────────────────────────────────────────────────
    "budget.not_set":     {"en": "Budget: not configured",   "zh": "预算：未配置"},
    "budget.warning":     {"en": "Budget warning",           "zh": "预算预警"},
    "budget.exceeded":    {"en": "Budget exceeded",          "zh": "预算已超出"},

    # ── Doctor ───────────────────────────────────────────────────────────
    "doctor.title":       {"en": "Swarm Doctor",             "zh": "Swarm 系统诊断"},
    "doctor.all_ok":      {"en": "All checks passed",        "zh": "全部检查通过"},
    "doctor.some_fail":   {"en": "{ok}/{total} checks passed", "zh": "{ok}/{total} 项检查通过"},
    "doctor.fix_prompt":  {"en": "Auto-install missing optional packages?",
                           "zh": "自动安装缺失的可选依赖？"},
    "doctor.installing":  {"en": "Installing {pkg}…",        "zh": "正在安装 {pkg}…"},
    "doctor.installed":   {"en": "Installed {pkg}",          "zh": "已安装 {pkg}"},
    "doctor.install_fail": {"en": "Failed to install {pkg}", "zh": "安装 {pkg} 失败"},

    # ── Gateway ─────────────────────────────────────────────────────────
    "gw.title":           {"en": "Gateway Status",           "zh": "网关状态"},
    "gw.running":         {"en": "Running",                  "zh": "运行中"},
    "gw.stopped":         {"en": "Stopped",                  "zh": "已停止"},
    "gw.starting":        {"en": "Starting gateway…",        "zh": "正在启动网关…"},
    "gw.started":         {"en": "Gateway started",          "zh": "网关已启动"},
    "gw.stopping":        {"en": "Stopping gateway…",        "zh": "正在停止网关…"},
    "gw.not_running":     {"en": "Gateway is not running",   "zh": "网关未运行"},
    "gw.port_in_use":     {"en": "Port {port} in use",       "zh": "端口 {port} 被占用"},
    "gw.killing_port":    {"en": "Killing process on port {port}…",
                           "zh": "正在终止端口 {port} 上的进程…"},
    "gw.url":             {"en": "URL",                      "zh": "地址"},
    "gw.token":           {"en": "Token",                    "zh": "令牌"},
    "gw.uptime":          {"en": "Uptime",                   "zh": "运行时间"},
    "gw.agents_online":   {"en": "{n} agents online",        "zh": "{n} 个代理在线"},
    "gw.no_agents":       {"en": "No agents online",         "zh": "无在线代理"},
    "gw.tasks":           {"en": "{n} tasks",                "zh": "{n} 个任务"},
    "gw.probe_ok":        {"en": "Health probe OK",          "zh": "健康探测正常"},
    "gw.probe_fail":      {"en": "Health probe failed",      "zh": "健康探测失败"},
    "gw.daemon_installed": {"en": "Daemon installed",        "zh": "守护进程已安装"},
    "gw.daemon_removed":  {"en": "Daemon removed",           "zh": "守护进程已移除"},
    "gw.daemon_running":  {"en": "Service: running",         "zh": "服务：运行中"},
    "gw.daemon_stopped":  {"en": "Service: stopped",         "zh": "服务：已停止"},
    "gw.daemon_not_inst": {"en": "Service: not installed",   "zh": "服务：未安装"},
    "gw.endpoints":       {"en": "Endpoints",                "zh": "端点"},
    "gw.restarting":      {"en": "Restarting gateway…",      "zh": "正在重启网关…"},

    # ── Help panel ───────────────────────────────────────────────────────
    "help.status":        {"en": "task board",               "zh": "任务面板"},
    "help.scores":        {"en": "reputation scores",        "zh": "信誉评分"},
    "help.usage":         {"en": "token usage & cost",       "zh": "Token 用量与花费"},
    "help.budget":        {"en": "spending limits",          "zh": "预算限制"},
    "help.cancel":        {"en": "cancel tasks",             "zh": "取消任务"},
    "help.workflows":     {"en": "list workflows",           "zh": "工作流列表"},
    "help.config":        {"en": "show agent config",        "zh": "查看代理配置"},
    "help.config_hist":   {"en": "config backup history",    "zh": "配置备份历史"},
    "help.config_roll":   {"en": "restore previous config",  "zh": "恢复历史配置"},
    "help.configure":     {"en": "re-run setup wizard",      "zh": "重新配置"},
    "help.gateway":       {"en": "gateway status & control", "zh": "网关状态与控制"},
    "help.chain":         {"en": "on-chain status",          "zh": "链上状态"},
    "help.doctor":        {"en": "system health check",      "zh": "系统健康检查"},
    "help.clear":         {"en": "clear history",            "zh": "清除历史"},
    "help.exit":          {"en": "quit",                     "zh": "退出"},
}


def t(key: str, **kwargs) -> str:
    """Translate a key to the current locale."""
    entry = _STRINGS.get(key)
    if not entry:
        return key
    text = entry.get(_locale, entry.get("en", key))
    if kwargs:
        text = text.format(**kwargs)
    return text


def set_locale(lang: str):
    """Override locale at runtime."""
    global _locale
    _locale = "zh" if lang.startswith("zh") else "en"


def get_locale() -> str:
    """Get current locale."""
    return _locale
