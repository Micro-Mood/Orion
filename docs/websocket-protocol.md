# WebSocket Protocol

Orion uses a single WebSocket connection (`/ws?token=<jwt>`) for all real-time communication between the browser and server.

## Connection

```
ws://host:port/ws?token=<jwt_token>
```

The JWT token is obtained via the REST authentication endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/__auth_status` | GET | Returns `{ needs_setup: bool }` |
| `/api/setup` | POST | First-time password setup. Body: `{ password }` → `{ token }` |
| `/api/login` | POST | Login. Body: `{ password }` → `{ token }` |
| `/api/verify` | POST | Verify token. Body: `{ token }` → `{ valid: bool }` |

## Message Format

All messages are JSON objects with a `type` field:

```json
{ "type": "message_type", ...payload }
```

## Client → Server Messages

### Session Management

| Type | Payload | Description |
|------|---------|-------------|
| `get_sessions` | — | Request session list |
| `create_session` | — | Create a new session |
| `delete_session` | `{ session_id }` | Delete a session |
| `get_messages` | `{ session_id }` | Get messages for a session |
| `update_session_title` | `{ session_id, title }` | Rename a session |

### Chat

| Type | Payload | Description |
|------|---------|-------------|
| `send_message` | `{ session_id, content }` | Send a user message to AI |
| `cancel` | `{ session_id }` | Cancel ongoing AI processing |

### Settings

| Type | Payload | Description |
|------|---------|-------------|
| `get_config` | — | Request current configuration (API key masked) |
| `save_config` | `{ config: {...} }` | Save configuration changes |
| `test_llm` | — | Test LLM API connection |
| `test_axon` | — | Test Axon MCP Server connection |
| `restart_axon` | — | Restart the Axon subprocess |

### File Browser

| Type | Payload | Description |
|------|---------|-------------|
| `list_files` | `{ path? }` | List directory contents (defaults to working dir) |
| `read_file_content` | `{ path }` | Read a file's content |

## Server → Client Messages

### Session Events

| Type | Payload | Description |
|------|---------|-------------|
| `session_list` | `{ sessions: [...] }` | Response to `get_sessions` |
| `session_created` | `{ session: {...} }` | Broadcast: new session created |
| `session_deleted` | `{ session_id }` | Broadcast: session deleted |
| `session_messages` | `{ session_id, messages: [...] }` | Messages for a session |
| `session_title_updated` | `{ session_id, title }` | Broadcast: session renamed |

### AI Response Stream

These events arrive in sequence during AI processing:

```
message_start
  ├── message_delta  (repeated, streaming text)
  ├── tool_start     (tool execution begins)
  ├── tool_end       (tool execution completes)
  ├── model_info     (which model is being used)
  └── ...            (more deltas and tools)
message_end
done | ask | error
```

| Type | Payload | Description |
|------|---------|-------------|
| `message_start` | `{ session_id, message_id }` | AI response begins |
| `message_delta` | `{ session_id, content }` | Streaming text chunk |
| `tool_start` | `{ session_id, tool_name, tool_id, params }` | Tool execution started |
| `tool_end` | `{ session_id, tool_name, tool_id, success, result, duration }` | Tool execution finished |
| `model_info` | `{ session_id, model }` | Current model name |
| `message_end` | `{ session_id, message_id, content }` | AI response complete |
| `done` | `{ session_id }` | Processing finished normally |
| `ask` | `{ session_id, question, options? }` | AI is asking the user a question |
| `error` | `{ session_id?, message }` | Error occurred |

### Settings Events

| Type | Payload | Description |
|------|---------|-------------|
| `config_data` | `{ config: {...} }` | Current configuration |
| `config_saved` | `{ config, message }` | Configuration saved successfully |
| `test_result` | `{ target, success, message }` | LLM/Axon test result |

### File System Events

| Type | Payload | Description |
|------|---------|-------------|
| `file_list` | `{ path, entries, error? }` | Directory listing |
| `file_content` | `{ path, content, encoding, size, error? }` | File content |
| `fs_changed` | `{ paths: [...] }` | Filesystem changes detected (debounced) |

## Message Storage Format

Messages are stored with a segments model:

```json
{
    "id": "ai_a1b2c3d4",
    "role": "assistant",
    "segments": [
        { "type": "text", "content": "Let me read that file..." },
        { "type": "tool", "name": "read_file", "params": {"path": "/src/main.py"}, "status": "success", "result": "...", "duration": 45 },
        { "type": "text", "content": "Here's what I found..." }
    ]
}
```

Segments preserve the chronological order of text and tool calls within a single AI response.
