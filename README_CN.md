# WeChat Automation Local

本地微信自动化与数据库工具工作区。

本仓库用于本地微信自动化研究与实现。当前生产代码主要位于 `wechat-decrypt/`。

## 当前架构

- **接收路径**：解密后的本地微信数据库增量轮询。
  - 主监听器：`wechat-decrypt/wechat_bot_monitor.py`
  - 快速刷新辅助：`wechat-decrypt/fast_refresh_targets.py`
  - 这是当前推荐的监听方式；相比 UIA 轮询，它在多目标后台监听和历史上下文获取上更快、更完整。
- **发送路径**：前台微信自动化发送。
  - 当前发送器优先尝试可见会话 OCR。
  - 失败后依次回退到微信搜索，以及剪贴板 / Enter / 点击等物理发送方式。
  - 适用场景下，发送后会使用数据库进行确认。
- **配置**：
  - 私有机器人目标配置：`wechat-decrypt/wechat_bot_targets.json`
  - 运行时路径和密钥均为本地私有信息，不应提交到仓库。

## 回复引擎 Provider

`wechat-decrypt/reply_engine.py` 支持多种回复 provider，包括用于接入外部 Agent 的 **command provider**。

### Command provider 快速参考

在目标配置中添加 command provider（`wechat-decrypt/wechat_bot_targets.json`）：

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

触发条件匹配后，回复引擎会启动该命令，把 JSON payload 通过 **stdin** 写入（UTF-8），并从 **stdout** 读取回复。

**Payload 字段**（当 `input_format` 为 `json` 时）：

- `prompt` — 组装后的 prompt
- `raw_text` — 原始收到的消息文本
- `clean_text` — 移除触发词后的清洗消息文本
- `wiki_hits` — 检索到的本地知识库文档列表
- `context_messages` — 最近会话上下文
- `target` — 目标名称及相关配置
- `retrieval_debug` — 检索耗时 / 调试信息

**stdout 回复格式：**

1. **纯文本** — 原始文本会直接作为回复内容。
2. **JSON** — `{"reply": "你的回复内容", ...}`

桥接层允许最终 JSON 输出前存在非 JSON 日志行。

**Windows 编码说明：**

如果外部 Agent 是 Python 脚本，并且回复中出现中文乱码，可以强制调用端（最好桥接脚本也一起）使用 UTF-8：

```bat
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

最小示例桥接脚本位于 `wechat-decrypt/genericagent_command_bridge.py`。

## 仓库结构

- `wechat-decrypt/` — 主实现，包括数据库解密工具、MCP server、机器人监听器、发送器和测试。
- `docs/` — 项目笔记、状态快照和设计决策。
- `pywechat_probe/`、`plan_*`、`source_read/` — 研究和探测材料。
- `DELIVERY_*.md`、`wechat_hook_*`、`run_wechat_hook_pipeline_check.py` — 交付说明和 hook/key pipeline 辅助材料。

生成的运行时产物、解密数据库、日志、截图、聊天导出、密钥和本地目标配置都应保持未跟踪状态，不应提交。

## 开发快速开始

```bash
cd wechat-decrypt
python -m pytest
```

解密器 / MCP 工具需要本地微信运行时数据和已提取的密钥。详细解密器用法见 `wechat-decrypt/README.md`。

## 安全规则

- 不要提交 secrets、密钥、解密后的数据库、私有聊天导出、截图、日志或运行时产物。
- 除非明确需要，不要读取或移动 key/secret 文件。
- 未经明确确认，不要修改正在使用的机器人业务逻辑。
- 集成到活跃机器人路径前，优先做隔离探测。
- 代码修改后运行相关测试。

## 当前优化方向

接收侧应继续保持数据库增量轮询。未来发送侧优化应先作为隔离层构建：

1. 探测微信 UIA / 会话列表 / 当前聊天 / 输入框能力。
2. 只针对测试群构建隔离 UIA 发送器。
3. 验证后再集成发送优先级：UIA 当前聊天 -> UIA 会话列表 -> OCR 可见列表 -> 搜索 -> 物理回退。
4. LLM / 模板 / 规则延迟优化，应与接收 / 发送通道改造分开处理。