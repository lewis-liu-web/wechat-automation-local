# WeChat Automation Local

Local WeChat automation and database tooling workspace.

This repository is a working area for local WeChat automation research and implementation. The active production code currently lives in `wechat-decrypt/`.

## Current architecture

- **Receive path**: decrypted local WeChat database incremental polling.
  - Main monitor: `wechat-decrypt/wechat_bot_monitor.py`
  - Fast refresh helper: `wechat-decrypt/fast_refresh_targets.py`
  - This is the preferred listener because it is faster and more complete than UIA polling for multi-target background monitoring and historical context.
- **Send path**: foreground WeChat automation.
  - Current sender attempts visible conversation OCR first.
  - It falls back to WeChat search and then physical clipboard/Enter/click style sending.
  - Database confirmation is used after sending where applicable.
- **Configuration**:
  - Private bot target config: `wechat-decrypt/wechat_bot_targets.json`
  - Runtime paths and keys are intentionally local/private and must not be committed.

## Reply engine providers

`wechat-decrypt/reply_engine.py` supports multiple reply providers, including a new **command provider** for integrating external agents.

### Command provider quick reference

Add a command provider in your target config (`wechat-decrypt/wechat_bot_targets.json`):

```json
{
  "reply_engine": {
    "provider": "command",
    "cmd": ["python", "wechat-decrypt/genericagent_command_bridge.py"],
    "input_format": "json",
    "timeout": 120
  }
}
```

When the trigger matches, the engine spawns the command, writes a JSON payload to **stdin** (UTF-8), and reads the reply from **stdout**.

**Payload fields** (when `input_format` is `json`):

- `prompt` — the assembled prompt
- `raw_text` — the original incoming message text
- `clean_text` — trigger-removed / clean version of the message
- `wiki_hits` — list of retrieved local knowledge documents
- `context_messages` — recent conversation context
- `target` — target name and related config
- `retrieval_debug` — retrieval timing/debug info

**Stdout reply formats:**

1. **Plain text** — the raw text is used as the reply.
2. **JSON** — `{"reply": "your reply text", ...}`

The bridge tolerates non-JSON log lines before the final JSON output.

**Windows encoding note:**
If the external agent is a Python script and you see garbled Chinese in the reply, force UTF-8 by running the caller (and ideally the bridge) with:

```
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

A minimal example bridge is provided at `wechat-decrypt/genericagent_command_bridge.py`.

## Repository layout

- `wechat-decrypt/` — main implementation, database decrypt tooling, MCP server, bot monitor, sender, and tests.
- `docs/` — project notes, status snapshots, and design decisions.
- `pywechat_probe/`, `plan_*`, `source_read/` — research/probe material.
- `DELIVERY_*.md`, `wechat_hook_*`, `run_wechat_hook_pipeline_check.py` — delivery notes and hook/key pipeline helper material.

Generated runtime artifacts, decrypted databases, logs, screenshots, chat exports, keys, and local target configs should remain untracked.

## Quick start for development

```bash
cd wechat-decrypt
python -m pytest
```

The decryptor/MCP tooling requires local WeChat runtime data and extracted keys. See `wechat-decrypt/README.md` for detailed decryptor usage.

## Safety rules

- Do not commit secrets, keys, decrypted databases, private chat exports, screenshots, logs, or runtime artifacts.
- Do not read or move key/secret files unless explicitly required.
- Do not change active bot business logic without explicit confirmation.
- Prefer isolated probes before integrating changes into the active bot path.
- Run relevant tests after code changes.

## Current optimization direction

The receive side should remain database incremental polling. Future sender optimization should be built as an isolated layer first:

1. Probe WeChat UIA/session-list/current-chat/input-field capabilities.
2. Build isolated UIA sender for a test group only.
3. Integrate sender priority only after validation: UIA current chat -> UIA session list -> OCR visible list -> search -> physical fallback.
4. Keep LLM/template/rule latency work separate from receive/send transport changes.
