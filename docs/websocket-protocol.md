# WebSocket Protocol

Orion uses one WebSocket connection at `/ws` for real-time browser/server communication. The token is not passed in the URL. The first WebSocket message must authenticate the connection.

```json
{ "type": "auth", "token": "<jwt>" }
```

The server replies with:

```json
{ "type": "auth_ok" }
```

or:

```json
{ "type": "auth_fail" }
```

## REST Authentication Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/__auth_status` | GET | Returns `{ "needs_setup": bool }`. |
| `/api/setup` | POST | First-time password setup. Body: `{ "password": "..." }` -> `{ "token": "..." }`. |
| `/api/login` | POST | Login. Body: `{ "password": "..." }` -> `{ "token": "..." }`. |
| `/api/verify` | POST | Verify token. Body: `{ "token": "..." }` -> `{ "valid": bool }`. |

## Message Format

Every WebSocket message is a JSON object with a `type` field.

```json
{ "type": "message_type", "...": "payload" }
```

## Client To Server

### Session Management

| Type | Payload | Description |
|------|---------|-------------|
| `get_sessions` | none | Request session list. |
| `create_session` | none | Create a new session. |
| `delete_session` | `{ "session_id": string }` | Delete a session and cancel its active task. |
| `fork_session` | `{ "session_id": string, "message_id": string, "title"?: string }` | Fork a new session at a frontend message. |
| `get_messages` | `{ "session_id": string }` | Load frontend messages for a session. |
| `update_session_title` | `{ "session_id": string, "title": string }` | Rename a session. |

### Chat

| Type | Payload | Description |
|------|---------|-------------|
| `send_message` | `{ "session_id": string, "content": string }` | Send user text and start an AI task. |
| `cancel` | `{ "session_id": string }` | Cancel the active task for a session. |
| `confirm_tools` | `{ "session_id": string, "confirmed": string[], "skipped": string[] }` | Continue after dangerous tool confirmation. |

### Settings

| Type | Payload | Description |
|------|---------|-------------|
| `get_config` | none | Request masked current config. |
| `save_config` | `{ "config": object }` | Save config and reinitialize runtime components. |
| `test_llm` | none | Test the configured LLM provider. |
| `test_axon` | none | Test Axon MCP connectivity. |
| `restart_axon` | none | Restart the managed Axon subprocess. Fails if Axon is external. |

### File Browser

| Type | Payload | Description |
|------|---------|-------------|
| `list_files` | `{ "path"?: string }` | List a directory. Empty path uses effective working directory. |
| `read_file_content` | `{ "path": string }` | Read a file for the editor. |
| `save_file_content` | `{ "path": string, "content": string }` | Save editor content to a file. |

## Server To Client

### Session Events

| Type | Payload | Description |
|------|---------|-------------|
| `session_list` | `{ "sessions": Session[] }` | Response to `get_sessions`. |
| `session_created` | `{ "session": Session }` | Broadcast when a session is created. |
| `session_forked` | `{ "session": Session }` | Broadcast when a fork session is created. |
| `session_deleted` | `{ "session_id": string }` | Broadcast when a session is deleted. |
| `session_messages` | `{ "session_id": string, "messages": Message[], "pending_options"?: string[], "is_running"?: true }` | Response to `get_messages`. |
| `session_title_updated` | `{ "session_id": string, "title": string }` | Broadcast title update. |
| `tokens_update` | `{ "session_id": string }` | Token counters changed; clients may refresh session state. |

### AI Stream Events

Typical sequence:

```text
message_start
  thinking_delta*       optional
  message_delta*        streaming text
  tool_start/tool_end*  tool calls
  compress_start/end*   context compression
  model_info*           model selected
message_end
done | ask | pending_confirm | error
```

| Type | Payload | Description |
|------|---------|-------------|
| `message_start` | `{ "session_id": string, "message_id": string, "resume"?: true }` | Assistant message begins. `resume` means append to an existing message after confirmation. |
| `thinking_delta` | `{ "session_id": string, "content": string }` | Streaming reasoning/thinking text for models that emit it. |
| `message_delta` | `{ "session_id": string, "content": string }` | Streaming assistant text. |
| `tool_start` | `{ "session_id": string, "tool_name": string, "tool_id"?: string, "params": object }` | Tool execution started. |
| `tool_end` | `{ "session_id": string, "tool_name": string, "tool_id"?: string, "success": bool, "result": string, "duration": int }` | Tool execution completed. |
| `compress_start` | `{ "session_id": string, "seg_id": string, "archived": int, "archived_tokens": int, "prompt_tokens": int }` | Context compression started. |
| `compress_end` | `{ "session_id": string, "success": bool, "title": string, "file": string, "archived_tokens": int, "error": string }` | Context compression finished. |
| `model_info` | `{ "session_id": string, "model": string }` | Current model name. |
| `message_end` | `{ "session_id": string, "message_id": string, "content": string, "tokens": int, "prompt_tokens": int, "completion_tokens": int, "session_total_tokens": int }` | Assistant message completed and persisted. |
| `pending_confirm` | `{ "session_id": string, "message_id": string, "tools": ToolConfirm[] }` | Dangerous tools need user confirmation. |
| `ask` | `{ "session_id": string, "question": string, "options"?: string[] }` | The assistant asks the user a question. |
| `done` | `{ "session_id": string }` | Processing finished normally. |
| `error` | `{ "session_id"?: string, "message": string }` | Error message. |

### Settings Events

| Type | Payload | Description |
|------|---------|-------------|
| `config_data` | `{ "config": object }` | Masked config plus `effective_cwd`. |
| `config_saved` | `{ "config": object, "message": string }` | Config was saved and runtime components were reinitialized. |
| `test_result` | `{ "target": "llm" | "axon", "success": bool, "message": string }` | Result from LLM/Axon test or Axon restart. |

### File Browser Events

| Type | Payload | Description |
|------|---------|-------------|
| `file_list` | `{ "path": string, "entries": object[], "error"?: string }` | Directory listing. |
| `file_content` | `{ "path": string, "content"?: string, "encoding"?: string, "size"?: int, "error"?: string }` | File content for editor. |
| `file_saved` | `{ "path": string, "success": bool, "error"?: string }` | Save result. |
| `fs_changed` | `{ "paths": string[] }` | Debounced filesystem change notification. |

## Stored Message Shape

Frontend messages use a `segments` list to preserve ordering inside one assistant response.

```json
{
  "id": "ai_a1b2c3d4",
  "role": "assistant",
  "tokens": 1200,
  "prompt_tokens": 900,
  "completion_tokens": 300,
  "segments": [
    { "type": "thinking", "content": "..." },
    { "type": "text", "content": "I will inspect the file." },
    { "type": "tool", "id": "tool_abc123", "name": "read_file", "params": {"path": "src/main.py"}, "status": "success", "result": "...", "duration": 45 },
    { "type": "compress", "id": "cmp_ab12cd", "status": "success", "archived": 20, "archived_tokens": 8000, "file": ".orion/20260519-151815.md" },
    { "type": "text", "content": "Done." }
  ]
}
```

`messages[]` is optimized for display. `context[]` in the same session file stores the full AI context used by the engine, including system handoff notes and tool-call messages.