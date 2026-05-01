# WeChat Automation Local

A local-first WeChat automation toolkit for building personal or team chat assistants on desktop WeChat.

The project focuses on practical automation workflows: target management, keyword-triggered replies, local and online knowledge-base integration, pluggable response engines, and foreground message sending. It is designed to run on your own computer and keep project data under your control.

> This project is intended for personal productivity, team workflow automation, and research use. Use it only with accounts, conversations, and data that you are authorized to access.

## Highlights

- **Local-first automation** — Run the bot on a desktop machine without requiring a hosted backend.
- **Target management** — Discover, enable, disable, and organize chat targets from a CLI.
- **Keyword triggers** — Configure per-target trigger words to avoid unintended replies.
- **Wiki / knowledge base** — Attach knowledge bases to targets so replies can use relevant context.
- **Online knowledge hooks** — Connect external or online knowledge-base services through the wiki hook interface, while keeping the bot workflow unchanged.
- **Pluggable reply engines** — Use templates, local logic, LLM providers, or command-based agents.
- **Foreground sending** — Send replies through the visible WeChat client with fallback-friendly automation.
- **Windows accessibility mode support** — Includes notes for enabling Windows Narrator to make supported WeChat UIA trees visible for automation experiments.

## Use Cases

- Personal FAQ assistant for frequently asked questions.
- Group chat helper that responds only when mentioned by trigger words.
- Team knowledge-base bot backed by local Markdown notes or online documentation.
- Desktop automation research around WeChat UI workflows.
- LLM agent bridge for controlled, target-specific conversations.

## Environment

| Item | Requirement |
| --- | --- |
| OS | Windows 10/11 recommended |
| Python | Python 3.10+ |
| Client | Desktop WeChat |
| Runtime | Local machine; no hosted service required |

Linux may be usable for some non-UI utilities, but the main desktop automation workflow is designed for Windows.

## Project Structure

```text
wechat-automation-local/
├── <runtime-dir>/              # Main runtime and CLI tools
│   ├── manage_targets.py        # Target, trigger, wiki and daemon management
│   ├── wechat_bot_monitor.py    # Message monitor and reply loop
│   ├── fast_refresh_targets.py  # Lightweight target refresh helper
│   ├── wiki_dry_run.py          # Knowledge retrieval / reply dry run
│   └── ...
├── README.md
├── README_CN.md
└── LICENSE
```

## Installation

```bash
git clone https://github.com/lewis-liu-web/wechat-automation-local.git
cd wechat-automation-local
pip install -e <runtime-dir>
```

> Replace `<runtime-dir>` with the project runtime directory in this repository.

If you prefer not to install the package in editable mode, you can also run the CLI scripts directly from the runtime directory.

## Quick Start

```bash
cd <runtime-dir>

# 1. Initialize local configuration
python manage_targets.py init

# 2. Discover available chat targets
python manage_targets.py scan

# 3. Enable a target
python manage_targets.py on "Group Name"

# 4. Add a trigger word
python manage_targets.py trigger "Group Name" add "hello bot"

# 5. Start the monitor
python manage_targets.py start
```

Run a safe dry run before enabling actual sending:

```bash
python wechat_bot_monitor.py --once --dry-run --sync-on-start
```

## Knowledge Base / Wiki

The wiki layer lets each target use its own context. A target can be bound to a local knowledge base, an online knowledge base, or a custom hook.

### Local knowledge base

```bash
# Create a local wiki alias backed by a directory
python manage_targets.py kb-local product-docs ./docs/product

# Import Markdown files into the wiki
python manage_targets.py kb-import product-docs ./docs/product

# Bind the wiki to a target
python manage_targets.py kb "Group Name" product-docs

# Inspect the wiki
python manage_targets.py kb-info product-docs
```

### Online knowledge-base hook

The wiki interface also supports online knowledge bases through hook-style integrations. Use this when your source of truth lives in a remote documentation service, internal knowledge platform, search API, or another agent service.

Typical workflow:

1. Create or register a wiki alias for the online source.
2. Configure the hook endpoint / command in the wiki configuration.
3. Bind the wiki alias to one or more targets.
4. Test retrieval with `wiki_dry_run.py` before enabling automatic replies.

```bash
# Example shape; exact fields depend on your hook implementation
python manage_targets.py kb-add company-wiki online
python manage_targets.py kb "Group Name" company-wiki
python wiki_dry_run.py --target "Group Name" --query "How do I request access?"
```

The bot does not need to know whether the context comes from local Markdown files or an online service. The reply engine receives retrieved context through the same wiki abstraction.

## Reply Engine

Each target can use its own reply engine. Common patterns:

- **Template / rules** — deterministic replies for simple automation.
- **LLM provider** — model-generated replies with target-specific context.
- **Command bridge** — call an external script or agent process and pass a structured JSON payload.

Example target configuration:

```json
{
  "targets": {
    "Group Name": {
      "listen": true,
      "triggers": ["hello bot"],
      "wiki": "product-docs",
      "reply_engine": {
        "provider": "command",
        "cmd": ["python", "genericagent_command_bridge.py"],
        "input_format": "json",
        "timeout": 120
      }
    }
  }
}
```

## CLI Reference

### `manage_targets.py`

| Command | Aliases | Description |
| --- | --- | --- |
| `init` | `cfg` | Initialize local configuration. |
| `scan` | `discover` | Discover chat targets and add new candidates. |
| `ls` | `list` | List configured targets and status. |
| `on` | `enable` | Enable a target by display name. |
| `off` | `disable` | Disable a target. |
| `re` | `reenable` | Re-enable a target. |
| `trigger` | `triggers`, `kw`, `keyword` | Add, remove, or list trigger words. |
| `kb-list` | `kbs`, `wiki-list` | List available knowledge bases. |
| `kb-add` | `wiki-add` | Register a knowledge-base alias or hook. |
| `kb-local` | `wiki-local` | Create a local directory-backed knowledge base. |
| `kb-import` | `wiki-import` | Import files into a local knowledge base. |
| `kb-open` | `wiki-open` | Open the local knowledge-base directory. |
| `kb-info` | `wiki-info` | Show knowledge-base details and statistics. |
| `kb` | `bind-wiki` | Bind a knowledge base to a target. |
| `refresh` | `rf` | Refresh target metadata and message state. |
| `start` | — | Start the monitor daemon. |
| `stop` | — | Stop the monitor daemon. |
| `restart` | — | Restart the monitor daemon. |
| `status` | — | Show monitor status. |

### `wechat_bot_monitor.py`

```bash
python wechat_bot_monitor.py --interval 3 --sync-on-start
```

Useful options:

| Option | Description |
| --- | --- |
| `--interval <seconds>` | Polling interval. |
| `--once` | Run one cycle and exit. |
| `--dry-run` | Generate decisions without sending messages. |
| `--sync-on-start` | Refresh target state before monitoring. |
| `--no-fast-refresh` | Disable lightweight refresh in each cycle. |

### `fast_refresh_targets.py`

```bash
python fast_refresh_targets.py --json
```

Refreshes target state quickly and optionally emits machine-readable output.

### `wiki_dry_run.py`

```bash
python wiki_dry_run.py --target "Group Name" --query "test question" --llm
```

Tests wiki retrieval and optional LLM response generation without sending messages.

## Windows Narrator / UIA Notes

Some desktop WeChat builds do not expose a useful UI Automation tree by default. For UI automation experiments, enabling **Windows Narrator** may cause WeChat to publish more accessibility information, making controls visible to UIA inspection tools.

Suggested workflow:

1. Open WeChat and keep the target window visible.
2. Press `Win + Ctrl + Enter` to turn on Windows Narrator.
3. Re-scan the WeChat window with your UIA inspection or automation tool.
4. If the UIA tree becomes available, run the UIA sender experiment on a test chat first.
5. Press `Win + Ctrl + Enter` again to turn Narrator off when finished.

This is an environment-dependent behavior, not a guaranteed API contract. Keep OCR / foreground input fallback enabled for production use.

## Safety and Privacy

- Do not commit conversation content, screenshots, logs, tokens, or local runtime files.
- Use the bot only for conversations and data you are authorized to automate.
- Keep trigger words narrow for group chats to avoid accidental replies.
- Test with `--dry-run` and a dedicated test target before enabling real sending.
- Review external knowledge-base hooks before sending sensitive context to third-party services.

## License

[MIT](./LICENSE) © 2025 lewis-liu-web

## 中文文档

See [README_CN.md](./README_CN.md).
