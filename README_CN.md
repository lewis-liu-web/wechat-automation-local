# WeChat Automation Local

一个本地优先的微信自动化工具包，用于在桌面微信上构建个人或团队聊天助手。

项目关注可落地的自动化工作流：目标管理、关键词触发回复、本地与线上知识库接入、可插拔回复引擎，以及前台消息发送。它运行在你自己的电脑上，尽量让项目数据留在本地并可控。

> 本项目适用于个人效率、团队流程自动化和研究用途。请仅在你有权访问和自动化的账号、会话与数据范围内使用。

## 主要特性

- **本地优先自动化**：在桌面电脑运行，不强依赖托管后端。
- **目标管理**：通过 CLI 发现、启用、停用和管理聊天目标。
- **关键词触发**：按目标配置触发词，避免机器人误回复。
- **Wiki / 知识库**：为不同目标绑定不同知识库，让回复带上相关上下文。
- **线上知识库 Hook**：通过 wiki hook 接口关联外部或线上知识库，保持机器人主流程不变。
- **可插拔回复引擎**：支持模板、本地逻辑、LLM 提供商或命令行 agent。
- **前台发送**：通过可见微信客户端发送消息，并保留可降级的自动化路径。
- **Windows 辅助功能支持**：提供开启 Windows 讲述人模式的说明，用于让部分微信版本暴露 UIA 树，便于自动化实验。

## 适用场景

- 个人 FAQ 助手，自动回答高频问题。
- 群聊助手，仅在命中特定触发词时回复。
- 团队知识库机器人，对接本地 Markdown 文档或线上文档系统。
- 微信桌面端 UI 自动化研究。
- 面向指定会话的 LLM agent 桥接。

## 运行环境

| 项目 | 要求 |
| --- | --- |
| 操作系统 | 推荐 Windows 10/11 |
| Python | Python 3.10+ |
| 客户端 | 桌面版微信 |
| 运行方式 | 本地电脑运行，无需托管服务 |

Linux 可用于部分非 UI 工具，但主要桌面自动化流程面向 Windows 设计。

## 项目结构

```text
wechat-automation-local/
├── <runtime-dir>/              # 主要运行代码与 CLI 工具
│   ├── manage_targets.py        # 目标、触发词、知识库和守护进程管理
│   ├── wechat_bot_monitor.py    # 消息监听与回复循环
│   ├── fast_refresh_targets.py  # 轻量刷新工具
│   ├── wiki_dry_run.py          # 知识检索 / 回复 dry run
│   └── ...
├── README.md
├── README_CN.md
└── LICENSE
```

## 安装

```bash
git clone https://github.com/lewis-liu-web/wechat-automation-local.git
cd wechat-automation-local
pip install -e <runtime-dir>
```

> 将 `<runtime-dir>` 替换为本仓库中的项目运行目录。

如果不想使用 editable 安装，也可以直接进入运行目录执行各个 CLI 脚本。

## 快速开始

```bash
cd <runtime-dir>

# 1. 初始化本地配置
python manage_targets.py init

# 2. 扫描可用聊天目标
python manage_targets.py scan

# 3. 启用目标
python manage_targets.py on "群组名称"

# 4. 添加触发词
python manage_targets.py trigger "群组名称" add "你好机器人"

# 5. 启动监听
python manage_targets.py start
```

正式启用发送前，建议先 dry run：

```bash
python wechat_bot_monitor.py --once --dry-run --sync-on-start
```

## 知识库 / Wiki

Wiki 层让每个目标都能使用自己的上下文。一个目标可以绑定本地知识库、线上知识库，或自定义 hook。

### 本地知识库

```bash
# 创建一个基于本地目录的 wiki alias
python manage_targets.py kb-local product-docs ./docs/product

# 导入 Markdown 文件
python manage_targets.py kb-import product-docs ./docs/product

# 绑定到目标
python manage_targets.py kb "群组名称" product-docs

# 查看知识库信息
python manage_targets.py kb-info product-docs
```

### 线上知识库 Hook

Wiki 接口也支持通过 hook 关联线上知识库。当你的知识源位于远程文档服务、内部知识平台、搜索 API 或另一个 agent 服务时，可以使用这种方式。

典型流程：

1. 为线上知识源创建或注册一个 wiki alias。
2. 在 wiki 配置中设置 hook endpoint / command。
3. 将该 wiki alias 绑定到一个或多个目标。
4. 用 `wiki_dry_run.py` 测试检索效果，再启用自动回复。

```bash
# 示例形态；具体字段取决于你的 hook 实现
python manage_targets.py kb-add company-wiki online
python manage_targets.py kb "群组名称" company-wiki
python wiki_dry_run.py --target "群组名称" --query "如何申请权限？"
```

机器人主流程不需要关心上下文来自本地 Markdown 还是线上服务；回复引擎通过统一 wiki 抽象接收检索结果。

## 回复引擎

每个目标可以配置自己的回复引擎。常见模式：

- **模板 / 规则**：适合确定性自动回复。
- **LLM 提供商**：结合目标上下文生成回复。
- **命令桥接**：调用外部脚本或 agent 进程，以结构化 JSON 传递输入。

目标配置示例：

```json
{
  "targets": {
    "群组名称": {
      "listen": true,
      "triggers": ["你好机器人"],
      "wiki": "product-docs",
      "reply_engine": {
        "provider": "command",
        "cmd": ["hermes", "chat", "-q", "{prompt}"],
        "input_format": "plain",
        "timeout": 120
      }
    }
  }
}
```

### 响应模式：客服 vs 平衡

回复引擎支持两种响应模式，由 `target-mode` 设置：`customer_service`（客服）和 `group_assistant`（群助手 / 平衡）。代码层面对 `customer_service` 做了特化处理，其它取值一律归一化为 `group_assistant`；配置中并不存在第三种"free mode"。

| 维度 | `customer_service`（客服） | `group_assistant`（群助手 / 平衡） |
| --- | --- | --- |
| 模式归一化 | 唯一特殊分支，按客服逻辑处理 | 其余任意值都归一化为此模式 |
| 模式指令文本 | 强调先确认需求；知识库能覆盖就直接答，知识库没覆盖时再追问 | 短而克制，只回答用户明确问到的那部分 |
| 路由与策略 | `reply_decision.decide` 在没有触发词或明确追问时返回 `ask_clarification` | 同样条件下保持沉默，不主动开口 |
| 会话 / 上下文窗口 | `timeout=120s`，`max_turns=5`，上下文 `time_window=120s`，`max_messages=40`，`sender_recent_limit=6` | `timeout=60s`，`max_turns=3`，上下文 `time_window=90s`，`max_messages=30`，`sender_recent_limit=5` |
| 立即反馈 | `_agent_ack` 会发送"正在处理中，请稍等。" | 不发送立即反馈消息 |
| 知识库兜底 | `generate_reply` 在客服模式无 `scene_hits` 时提前返回 `kb_clarification` 模板 | 没有命中时继续走 agent，由 agent 自己决定是否回复 |

> `_agent_ack` 的注释里曾提到"free mode"，但代码中并不存在该模式，实际行为以 `customer_service` 为准。

### `manage_targets.py`

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `init` | `cfg` | 初始化本地配置。 |
| `scan` | `discover` | 扫描聊天目标并加入候选列表。 |
| `ls` | `list` | 列出已配置目标及状态。 |
| `on` | `enable` | 按显示名称启用目标。 |
| `off` | `disable` | 停用目标。 |
| `re` | `reenable` | 重新启用目标。 |
| `target-show` | — | 查看目标详情。 |
| `target-delete` | — | 删除目标（需 `--yes`）。 |
| `target-field` | — | 设置目标任意字段。 |
| `target-mode` | — | 设置响应模式：`group_assistant` / `customer_service`。 |
| `target-category` | — | 设置目标类别：`user` / `admin`。 |
| `trigger` | `triggers`, `kw`, `keyword` | 添加、删除或查看触发词。 |
| `trigger-default-list` | — | 查看全局默认触发词。 |
| `trigger-default-replace` | — | 替换全局默认触发词。 |
| `trigger-default-clear` | — | 清空全局默认触发词。 |
| `kb-list` | `kbs`, `wiki-list` | 列出可用知识库。 |
| `kb-add` | `wiki-add` | 注册知识库 alias 或 hook。 |
| `kb-local` | `wiki-local` | 创建本地目录型知识库。 |
| `kb-import` | `wiki-import` | 向本地知识库导入文件。 |
| `kb-open` | `wiki-open` | 打开本地知识库目录。 |
| `kb-info` | `wiki-info` | 查看知识库详情与统计。 |
| `kb-enable` | — | 启用知识库。 |
| `kb-disable` | — | 禁用知识库。 |
| `kb-delete` | — | 删除知识库（需 `--yes`）。 |
| `kb-search` | — | 检索本地知识库。 |
| `kb` | `bind-wiki` | 将同源知识库绑定到目标。 |
| `decrypt-status` | — | 查看解密状态（只读，不打印密钥）。 |
| `refresh` | `rf` | 刷新目标元数据和消息状态。 |
| `start` | — | 启动监听守护进程。 |
| `stop` | — | 停止监听守护进程。 |
| `restart` | — | 重启监听守护进程。 |
| `status` | — | 查看监听状态。 |

### `wechat-auto` 包入口

安装后可使用统一入口：

```bash
wechat-auto decrypt-status --json
wechat-auto target-show "<target>" --json
wechat-auto trigger-default-list
wechat-auto kb-search <kb_id> <query> --limit 5
wechat-auto agent profiles
wechat-auto agent on <instance_id>
wechat-auto agent off <instance_id>
```

> 使用 `wechat-auto <命令> --help` 查看完整参数；所有删除命令必须显式加 `--yes`。

### `wechat_bot_monitor.py`

```bash
python wechat_bot_monitor.py --interval 3 --sync-on-start
```

常用选项：

| 选项 | 说明 |
| --- | --- |
| `--interval <秒数>` | 轮询间隔。 |
| `--once` | 只运行一个周期后退出。 |
| `--dry-run` | 只生成决策，不发送消息。 |
| `--sync-on-start` | 监听前刷新目标状态。 |
| `--no-fast-refresh` | 禁用每轮轻量刷新。 |

### `fast_refresh_targets.py`

```bash
python fast_refresh_targets.py --json
```

快速刷新目标状态，并可输出机器可读结果。

### `wiki_dry_run.py`

```bash
python wiki_dry_run.py --target "群组名称" --query "测试问题" --llm
```

测试知识库检索和可选 LLM 生成，不会发送消息。

## Windows 讲述人 / UIA 说明

部分桌面微信版本默认不会暴露完整可用的 UI Automation 树。进行 UI 自动化实验时，开启 **Windows 讲述人** 有时会让微信发布更多辅助功能信息，从而让控件能被 UIA 检查工具看到。

建议流程：

1. 打开微信，并保持目标窗口可见。
2. 按 `Win + Ctrl + Enter` 开启 Windows 讲述人。
3. 用 UIA 检查或自动化工具重新扫描微信窗口。
4. 如果 UIA 树可见，先在测试聊天中验证 UIA 发送器。
5. 完成后再次按 `Win + Ctrl + Enter` 关闭讲述人。

这是依赖环境的行为，不是稳定 API 契约。生产使用仍建议保留 OCR / 前台输入等回退路径。

## 安全与隐私

- 不要提交会话内容、截图、日志、token 或本地运行产物。
- 仅在你有权访问和自动化的会话与数据范围内使用。
- 群聊触发词应尽量收窄，避免误回复。
- 启用真实发送前，先使用 `--dry-run` 和专用测试目标验证。
- 使用外部知识库 hook 前，确认不会把敏感上下文发送到不可信服务。

## 开源协议

[MIT](./LICENSE) © 2025 lewis-liu-web

## English Documentation

See [README.md](./README.md).
