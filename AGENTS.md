# AGENTS.md

Guidance for AI coding agents working in this repository.

## Repository Scope

These instructions apply to the entire Orion repository.

Orion is a self-hosted AI agent with:

- `src/`: FastAPI server, WebSocket protocol, engine, config, persistence, LLM client, MCP client, Axon manager, and web assets.
- `axon/`: Axon MCP Server source included via git subtree. Do not describe it as a git submodule.
- `docs/`: public documentation that must stay aligned with current code.
- `data/`, `workspace/`, and `.orion/`: runtime state, ignored by git or created under the configured working directory.

## Source Of Truth

Use current code as the source of truth before editing docs or README content.

- WebSocket behavior: `src/server.py`.
- Config fields and environment variables: `src/config.py`.
- Tool loop, `register_tool`, compression, and memory handoff: `src/engine.py`.
- Archive files and `.orion/index.json`: `src/memory.py`.
- Fork context reconstruction: `src/store.py`.
- Tool catalog and schemas: `src/tools.py`.
- Browser behavior: `src/web/app.js`, `src/web/index.html`, `src/web/style.css`.

Do not preserve stale claims when the code has changed. In particular:

- Do not call Axon a submodule. It is included under `axon/` via git subtree.
- Do not document a `SELECT -> PARAMS -> EXEC` or `Two-Phase` tool loop. Current Orion uses native OpenAI-compatible `tool_calls` plus `register_tool` / `unregister_tool`.
- Do not document WebSocket token auth as a URL query parameter. The client connects to `/ws` and sends `{ "type": "auth", "token": "..." }` as the first message.
- Do not document `engine.max_history` or `ORION_MAX_HISTORY`. Current context behavior uses `context_window`, `compress_at`, `context_recent_n`, and `.orion` archives.

## Editing Rules

- Keep changes focused on the requested task.
- Match existing style and avoid unrelated refactors.
- Use `apply_patch` for manual edits.
- Do not rewrite generated/runtime files under `data/`, `workspace/`, or working-directory `.orion/` archives unless explicitly asked.
- Do not commit, branch, tag, or push unless explicitly asked.
- Do not revert user changes. If the worktree is dirty, inspect relevant files and work with the existing changes.

## Documentation Standards

- Public documentation should be factual and code-backed.
- Avoid hard marketing, inflated claims, and unexplained percentages.
- When README and docs mention setup, keep `config.example.json`, `docs/configuration.md`, and `docs/getting-started.md` consistent.
- When WebSocket events change, update `docs/websocket-protocol.md` in the same change.
- When engine compression/fork behavior changes, update `docs/architecture.md` and any README section that describes context or memory.
- Use Markdown links with workspace-relative paths when referencing repository files in responses.

## Validation

For Python or server changes, run focused checks when feasible:

```bash
python -m compileall src
```

For documentation-only changes, run:

```bash
git diff --check
```

Search for stale documentation terms before finishing when relevant:

```bash
rg -n "submodule|--recurse-submodules|git submodule|max_history|ORION_MAX_HISTORY|Two-Phase|Context FIFO|ws\?token|SELECT|PARAMS" README*.md docs src
```

Investigate matches before removing them. Some matches may appear in historical notes or explicit warnings.

## Local Run Commands

Install dependencies:

```bash
pip install -r requirements.txt
pip install -r axon/requirements.txt
```

Run Orion:

```bash
cd src
python main.py
```

Default URL:

```text
http://127.0.0.1:8080
```

## Safety Notes

- `config.json` may contain API keys, password hashes, and JWT secrets. Do not print or commit secrets.
- Dangerous tools include command execution, writes, deletes, and moves. Keep confirmation behavior intact unless the user asks for a deliberate change.
- Remote deployment docs must recommend HTTPS, a strong password, and private handling of `config.json`, `data/`, and `.orion/`.