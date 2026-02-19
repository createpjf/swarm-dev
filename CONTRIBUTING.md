# Contributing to Swarm

## Development Setup

```bash
git clone https://github.com/createpjf/swarm-dev.git
cd swarm-dev
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Project Layout

- `main.py` — CLI entry point, all `swarm <cmd>` commands
- `core/` — Orchestrator, agents, task board, gateway, workflows
- `adapters/` — LLM providers, memory backends, chain integrations
- `reputation/` — Scoring engine, evolution, peer review
- `skills/` — Markdown skill documents (hot-reload)
- `workflows/` — YAML workflow templates
- `tests/` — Unit and integration tests

## Code Style

- Python 3.10+ with type hints
- `from __future__ import annotations` in all modules
- Use `logging.getLogger(__name__)` — no print statements in library code
- File-backed coordination: always use `FileLock` for shared state
- Keep functions focused — one responsibility per function
- No unnecessary abstractions for one-time operations

## Making Changes

1. Create a branch: `git checkout -b feature/my-change`
2. Make focused, minimal changes
3. Run tests: `pytest tests/`
4. Run health check: `swarm doctor`
5. Submit a PR with a clear description

## Adding a New LLM Adapter

1. Create `adapters/llm/my_provider.py`
2. Implement `async def chat(messages, model) -> str`
3. Implement `async def chat_stream(messages, model)` (async generator yielding chunks)
4. Register in `core/onboard.py` `PROVIDERS` dict
5. Add to resilience wrapper if needed

## Adding a New Workflow Template

1. Create `workflows/my_workflow.yaml`
2. Define steps with `id`, `agent`, `prompt`, and `depends_on`
3. Use `{{task}}` for input and `{{step_id.result}}` for step results
4. Test: `swarm workflow run my_workflow --input "test task"`

## Testing

```bash
pytest tests/                    # all tests
pytest tests/test_task_board.py  # specific module
swarm doctor                     # integration health check
```
