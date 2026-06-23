# WeChat Automation Local

A local-first WeChat automation toolkit for building personal or team chat assistants on top of the desktop WeChat client. It runs entirely on your own machine, keeps data local, and is designed for automation research, personal productivity, and small-team workflows.

> This project is intended for accounts, conversations, and data you have permission to access and automate. Do not use it on other people's accounts or groups without explicit consent.

## What it does

WeChat Automation Local connects to the decrypted local WeChat database, listens for incoming messages in selected chats, and can reply automatically based on triggers, knowledge bases, and pluggable agent providers. The entire pipeline — receive, decide, retrieve context, generate a response, and send — runs locally.

- **Receive**: Incremental polling of the decrypted local WeChat database (`wechat-decrypt/wechat_bot_monitor.py`). This is faster and more complete than UI Automation polling, especially for multi-target monitoring and historical context.
- **Decide**: Per-target triggers, default triggers, category filters, and reply mode selection.
- **Retrieve**: Bind local Markdown wikis, online knowledge bases, or custom hooks to individual targets.
- **Generate**: Template/rule replies, LLM providers, or local agent workers (Hermes, echo test provider).
- **Send**: Foreground WeChat automation with OCR, search, clipboard/Enter, and physical click fallback paths.

## Key features

- **Local-only by default** — runs on your desktop; no required hosted backend.
- **Target management** — discover, enable, disable, and configure monitored chats through the CLI or Streamlit console.
- **Trigger-based replies** — avoid accidental replies by requiring specific keywords per target.
- **Knowledge bases / Wiki** — bind different local or online knowledge sources to different targets.
- **Online KB hooks** — connect remote documentation, search APIs, or other agent services without changing the bot core.
- **Pluggable reply engine** — templates, rules, LLM providers, or command-line agents.
- **Agent job queue** — asynchronously dispatch, reconcile, and send agent-generated replies.
- **Hermes / local agent pool** — register local agent instances, put them on/off duty, and inspect pool health.
- **CUA / UIA sender backends** — multiple sending strategies including background UIA automation.
- **Streamlit control console** — overview, target management, triggers, KB diagnostics, and agent job debugging in one UI.

## Requirements

| Item | Recommendation |
| --- | --- |
| OS | Windows 10/11 (primary target) |
| Python | Python 3.10+ |
| Client | Desktop WeChat |
| Runtime | Local machine; no hosted service required |

Linux can be used for some non-UI tools, but the main desktop automation flows are designed for Windows.

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/lewis-liu-web/wechat-automation-local.git
cd wechat-automation-local
pip install -e .
```

This installs the `wechat-auto` package entry point.

### 2. Initialize configuration

```bash
wechat-auto init
```

This creates `wechat-decrypt/config.json`. Edit `db_dir` to point to your WeChat `db_storage` directory if it is not auto-detected.

### 3. Extract database keys

```bash
wechat-auto key
```

Keys are stored in a local JSON file and never printed in full.

### 4. Decrypt databases

```bash
wechat-auto decrypt-status --json   # check status without decrypting
wechat-auto setup                   # init + key + decrypt
```

### 5. Discover and enable targets

```bash
wechat-auto scan --include-contacts
wechat-auto ls
wechat-auto on "<chat-name>" --wiki <kb-alias>
```

### 6. Add triggers

```bash
wechat-auto trigger-add "<chat-name>" "#ask" --json
wechat-auto trigger-default-replace "@assistant" --json
```

### 7. Create or import a knowledge base

```bash
wechat-auto kb-local product-docs --description "Product documentation"
wechat-auto kb-import product-docs ./docs/product
wechat-auto kb "<chat-name>" product-docs --replace
```

### 8. Start monitoring

```bash
wechat-auto start
```

Use `--dry-run` first to see decisions without sending:

```bash
python wechat-decrypt/wechat_bot_monitor.py --once --dry-run --sync-on-start
```

### 9. Inspect and manage the agent pool

```bash
wechat-auto agent profiles --json
wechat-auto agent pool --json
wechat-auto agent on hermes-default --json
wechat-auto agent off hermes-default --json
```

## CLI reference

`wechat-auto` is the unified package entry point. A few common commands:

```bash
wechat-auto decrypt-status --json
wechat-auto target-show "<chat-name>" --json
wechat-auto target-mode "<chat-name>" customer_service --json
wechat-auto kb-search <kb-id> "<query>" --limit 5 --json
wechat-auto agent health --provider echo --json
```

Deletion commands require `--yes`:

```bash
wechat-auto target-delete "<chat-name>" --yes
wechat-auto kb-delete <kb-id> --yes
```

Run `wechat-auto <command> --help` for full options.

## Project layout

```text
wechat-automation-local/
├── wechat-decrypt/              # Main runtime, decryptor, monitor, sender, tests
│   ├── wechat_bot_monitor.py    # Message listener and reply loop
│   ├── manage_targets.py        # Local CLI for targets, triggers, KBs, keys, decrypt
│   ├── control_api.py           # HTTP control surface
│   ├── control_client.py        # Shared HTTP client for CLI and UI
│   ├── wechat-control-ui/       # Streamlit console
│   │   ├── app.py
│   │   └── api.py               # Compatibility shim -> control_client
│   ├── target_registry.py       # Config/registry helpers
│   ├── reply_engine.py          # Reply decision and generation
│   └── tests/
├── wechat_auto/                 # Packaged CLI entry point
│   └── cli.py
├── docs/                        # Notes, status snapshots, design decisions
├── openspec/                    # Specs and approved change records
└── README.md
```

## Running tests

```bash
cd wechat-decrypt
python -m pytest -q
```

From the repo root:

```bash
python -m pytest wechat-decrypt/tests/ -q
```

## Safety rules

- Do not commit secrets, keys, decrypted databases, private chat exports, screenshots, logs, or runtime artifacts.
- Do not read or move key/secret files unless explicitly required.
- Do not change active bot business logic without explicit confirmation.
- Prefer isolated probes before integrating changes into the active bot path.
- Use `--dry-run` and dedicated test targets before enabling real sending.
- Confirm that external knowledge-base hooks do not send sensitive context to untrusted services.

## License

[MIT](./LICENSE) © 2025 lewis-liu-web
