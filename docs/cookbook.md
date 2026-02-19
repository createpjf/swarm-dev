# Swarm Cookbook

Three practical scenarios to get productive with Swarm.

---

## 1. Code Review Pipeline

Run a structured code review with planning, implementation analysis, and quality checks.

```bash
swarm workflow run code_review --input "Review the authentication module in core/auth.py for security vulnerabilities"
```

**What happens:**
1. **Planner** analyzes the code and creates a review plan
2. **Executor** performs the detailed review
3. **Advisor** scores the review quality

**Customize it:** Copy `workflows/code_review.yaml` → `workflows/my_review.yaml`, edit steps.

---

## 2. Research Report

Generate a structured research report with outline, research, synthesis, and quality check.

```bash
swarm workflow run research_report --input "Compare serverless frameworks: AWS Lambda vs Cloudflare Workers vs Vercel Edge Functions"
```

**What happens:**
1. **Planner** creates a research outline with specific questions
2. **Executor** conducts research for each question
3. **Executor** synthesizes findings into a report
4. **Advisor** reviews for accuracy and completeness (approval gate)

---

## 3. Custom Agent

Create a specialized agent for your needs.

```bash
# Create from template
swarm agents create security-auditor --template debugger

# Or interactive (choose role, provider, model)
swarm agents create security-auditor
```

This generates:
- Config entry in `config/agents.yaml`
- Skill override: `skills/agent_overrides/security-auditor.md`
- Cognition profile: `docs/security-auditor/cognition.md`

**Customize the agent:**

1. Edit `skills/agent_overrides/security-auditor.md` — add domain-specific instructions
2. Edit `docs/security-auditor/cognition.md` — define how the agent thinks
3. Skills are hot-reloaded on the next task cycle

**Use it in a workflow:**

```yaml
# workflows/security_audit.yaml
name: Security Audit
steps:
  - id: scan
    agent: security-auditor
    prompt: "Audit this code for OWASP Top 10 vulnerabilities: {{task}}"
  - id: report
    agent: executor
    prompt: "Write a security report based on: {{scan.result}}"
    depends_on: [scan]
```

```bash
swarm workflow run security_audit --input "Review auth.py and session.py"
```
