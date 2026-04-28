# 微信 Windows Native Bridge / Hook 静默发送方案只读调研

调研时间：2026-04-27  
约束执行情况：仅做公开资料/API/README/PyPI/GitHub 元数据核验；未注入、未运行 Hook、未启动微信客户端、未读取密钥。

## 1. 结论摘要

| 方案 | 静默发送可行性 | 兼容/版本 | Python 集成 | 主要风险 | 推荐度 |
|---|---:|---|---|---|---|
| **WeChatFerry / wcferry** (`lich0821/WeChatFerry`, PyPI `wcferry`) | 高：提供 `send_text`/`send_image`/`send_file`/`send_xml` 等能力 | README 显示版本号按微信大/小版本适配；当前 PyPI 为 `39.5.2.0`，README 更新记录含适配 `3.9.12.51`、`3.9.12.17`、`3.9.11.25`、`3.9.10.x` | 最成熟：`pip install --upgrade wcferry`；有 Python 文档和机器人示例 | 仍属 DLL/Hook/注入类，账号风控、微信版本强绑定、杀软误报、崩溃风险 | **首选落地路径** |
| **wxhelper** (`ttttupup/wxhelper`) | 高：README 功能预览含发送文本、@文本、图片、文件、转发、公众号/小程序等 | 主 README 明确支持 `3.8.0.41`、`3.8.1.26`、`3.9.0.28`、`3.9.2.23`、`3.9.2.26`、`3.9.5.81`；分支还可见 dev 到 `3.9.11.25` | 可通过注入后 HTTP 接口/Postman/Python client 调用；Python 不是主 SDK 形态 | 注入工具和 DLL 版本匹配要求高；README 写明 Win11 简测、其他环境不保证；接口随分支差异 | 备选/研究型 |
| **wechatferry/wechatferry**（新组织仓） | 中高：README/API 显示与 WeChatFerry 生态相关，仓库活跃 | 需进一步确认其与 `lich0821/WeChatFerry` 的迁移/重构关系及版本稳定性 | 多语言/TS 结构，Python 落地不如 PyPI `wcferry` 直接 | 新仓/生态迁移风险，文档与旧版关系需核实 | 观察/二选一前需 PoC |
| **WeChatRobot / BotFlow** (`lich0821/WeChatRobot` 后迁移/停止维护提示) | 取决于底层 WeChatFerry | 机器人层，不是底层 Hook | 可作为业务层参考，但原 `WeChatRobot` README 显示“因不可抗因素，项目停止维护”，GitHub 重定向到 BotFlow | 维护状态变化，业务框架复杂度高 | 只参考实现 |
| **Wechaty + puppet-wcferry** | 发送能力由 puppet/WeChatFerry 提供 | Node/Wechaty 生态；`mrrhq/wechaty-puppet-wcferry` README 写“已迁移到👇这个仓库”，原仓 star 少 | Python 不直接，需 Node 服务桥接 | 多一层框架，迁移/维护不确定 | 不建议作为 Python 首选 |

## 2. 核验来源与关键事实

### 2.1 WeChatFerry / wcferry

核验对象：

- GitHub：`lich0821/WeChatFerry`
- PyPI：`wcferry`
- GitHub 元数据：约 6.5k stars，MIT License，2026-04-27 仍有更新记录。
- PyPI 元数据：`wcferry`，summary 为“一个玩微信的工具”，版本 `39.5.2.0`，主页指向 `https://github.com/lich0821/WeChatFerry`。

公开 README 关键点：

- Python 快速开始：`pip install --upgrade wcferry`
- 功能清单包括：
  - 获取登录二维码
  - 查询登录状态/登录用户
  - 发送文本、图片、文件、XML
  - 接收消息
  - 群/联系人相关能力
- Python 客户端代码中可见接口名：
  - `send_text(self, msg: str, receiver: str, aters: str = "") -> int`
  - `send_image(self, path: str, receiver: str) -> int`
  - `send_file(self, path: str, receiver: str) -> int`
  - `send_xml(...)`
- README 版本规则说明：
  - `w.x.y.z`
  - `w` 是微信大版本号，如 `37`/`38`/`39`
  - `x` 是适配的微信小版本号
  - `y` 是 WeChatFerry 版本
  - `z` 是客户端版本
- 更新记录核验到：
  - `v39.5.0`：适配 `3.9.12.51`
  - `v39.4.0`：适配 `3.9.12.17`
  - 还出现 `3.9.11.25`、`3.9.10.27`、`3.9.10.19` 等适配记录。

判断：

- 对“Windows 微信静默发送”的可落地性最高，特别是 Python 使用场景。
- 集成层面最直接：Python 进程通过 `wcferry` SDK 与本地微信/注入模块通信。
- 适合做本地单用户自动化桥接，但不适合作为对外高并发、多账号商业服务的无风控方案。

### 2.2 wxhelper

核验对象：

- GitHub：`ttttupup/wxhelper`
- GitHub 元数据：约 3k stars，README 描述为 Hook WeChat / 微信逆向。

公开 README 关键点：

- 架构：`wxhelper.dll` 注入后启动 HTTP 服务，client/Postman/Python 调用接口。
- 支持版本（README 明确列出）：
  - `3.8.0.41`
  - `3.8.1.26`
  - `3.9.0.28`
  - `3.9.2.23`
  - `3.9.2.26`
  - `3.9.5.81`
- 分支列表还可见：
  - `dev-3.9.7.29`
  - `dev-3.9.8.25`
  - `dev-3.9.9.43`
  - `dev-3.9.10.19`
  - `dev-3.9.11.19`
  - `dev-3.9.11.25`
- 功能预览包括：
  - 检查是否登录
  - 获取登录微信信息
  - 发送文本
  - 发送 @ 文本
  - 发送图片
  - 发送文件
  - hook/取消 hook 消息、图片、语音、日志
  - 联系人、群成员、数据库查询、转发消息、确认收款、OCR、朋友圈、撤回、公众号、小程序等。
- README 注意事项：
  - 需先安装对应版本微信，分支名代表微信版本。
  - 使用注入工具注入 `wxhelper.dll` 后，可通过 Postman 调接口。
  - 可用 `python/clent.py` 简单测试。
  - 个别接口某些版本没有实现。
  - Win11 环境简单测试，其他环境无法保证。
  - 旧版本 bug 可能只在新版本修复，旧版本不维护。

判断：

- 功能覆盖非常广，HTTP 接口也便于语言无关集成。
- 但 Python 侧不是主线 SDK，更多是“注入 DLL + 本地 HTTP API”模式。
- 版本、注入成功率和环境要求比 WeChatFerry 更需要工程兜底。
- 适合研究、备用、需要特定接口时评估，不建议作为首选稳定生产依赖。

### 2.3 wechatferry/wechatferry 新组织仓

核验对象：

- GitHub：`wechatferry/wechatferry`
- GitHub 元数据：约 2k stars，MIT License，2026-04-27 活跃。
- 仓库结构显示 `packages/core/proto/wcf.proto`、TS client 等，可能是 WeChatFerry 生态的新仓/重构。

判断：

- 活跃度高，但与 `lich0821/WeChatFerry`、PyPI `wcferry` 的继承关系和当前生产可用性需进一步确认。
- 如果后续做 PoC，应优先确认：
  1. 是否支持当前目标微信版本；
  2. 是否有稳定 Python client；
  3. 是否仍采用注入/Hook；
  4. 是否有版本迁移说明。
- 在当前 Python 落地目标下，不优先于 PyPI `wcferry`。

### 2.4 机器人/框架层项目

#### WeChatRobot / BotFlow

- `lich0821/WeChatRobot` GitHub API 返回重定向/迁移到 `lich0821/BotFlow`。
- 原 README 仅显示：“因不可抗因素，项目停止维护。”
- 可作为 WeChatFerry 上层机器人设计参考，但不应作为底层发送桥的核心依赖。

#### zhayujie/chatgpt-on-wechat / CowAgent

- 大型多渠道机器人/Agent 框架，README 显示支持微信、飞书、钉钉、企微、QQ 等。
- 重点不在 Windows 原生 Hook 静默发送，而在多渠道业务框架。
- 若只需要“本机微信静默发送桥”，引入该类框架会过重。

#### Wechaty / puppet-wcferry

- Wechaty 是成熟机器人 SDK，但个人微信 Windows Hook 需 puppet。
- `mrrhq/wechaty-puppet-wcferry` README 显示是 WeChatFerry puppet，但原仓说明“已迁移到👇这个仓库”，维护状态需继续核实。
- Python 项目若接入 Wechaty 需要 Node 服务桥接，不是最短路径。

## 3. 安全性与合规风险

这些方案的共同风险：

1. **账号风控/封禁风险**  
   Hook/注入/非官方接口可能触发微信客户端或服务端风控，尤其是：
   - 高频发送
   - 批量群发
   - 新号/低活跃号
   - 发送链接、营销内容、重复内容
   - 多账号同机/同 IP
2. **客户端稳定性风险**  
   DLL 注入、函数地址偏移、消息结构变化可能导致：
   - 微信崩溃
   - 消息发送失败
   - 版本升级后接口失效
   - 内存访问异常
3. **安全软件/EDR 风险**  
   注入工具、Hook DLL、读取数据库、注入微信进程等行为容易被杀毒/EDR 标为可疑。
4. **隐私与数据风险**  
   项目常包含联系人、群、消息、数据库解密能力。若封装为服务，必须隔离权限，避免暴露全量聊天数据。
5. **合规风险**  
   非官方自动化可能违反微信软件许可或平台规则；商业用途风险更高。

安全落地原则：

- 仅在用户本人明确授权的本机环境使用。
- 不做批量营销、骚扰、撞库、绕风控用途。
- 不保存不必要聊天内容。
- 不开放未鉴权 HTTP 接口到公网。
- 本地服务绑定 `127.0.0.1`，加 token/ACL。
- 限频、去重、人工确认高风险消息。
- 固定微信版本，禁自动升级。
- 注入模块与客户端 SDK 版本锁定。
- 单独 Windows 用户/虚拟机运行，降低对主机影响。

## 4. 推荐落地路径

### 路径 A：优先 WeChatFerry/wcferry（推荐）

适用：Python 项目需要本机 Windows 微信静默发送文本/图片/文件。

建议架构：

```text
业务系统 / Agent
   |
   | Python 调用
   v
本地 bridge 服务（FastAPI/Flask/自定义队列）
   |
   | wcferry SDK
   v
WeChatFerry 本地组件 / 已登录 Windows 微信
```

集成步骤（仅方案设计，未执行）：

1. 准备隔离 Windows 运行环境。
2. 固定受支持微信版本，例如 README 当前适配记录中的 `3.9.12.51` 或与 PyPI `wcferry==39.5.2.0` 匹配的版本。
3. 安装 Python 包：`pip install --upgrade wcferry`（实际落地时应 pin 版本，如 `wcferry==39.5.2.0`）。
4. 编写本地 bridge：
   - `/send_text`
   - `/send_image`
   - `/send_file`
   - `/health`
   - `/whoami`
5. Bridge 内做：
   - 登录状态检查
   - 消息限频
   - 接收人白名单
   - 失败重试/熔断
   - 本地 token 鉴权
   - 审计日志（不记录敏感正文或做脱敏）
6. 首次 PoC 只发给“文件传输助手”或测试小号/测试群。
7. 验证稳定后再接入业务层。

关键工程约束：

- pin 微信版本 + pin `wcferry` 版本。
- 避免微信自动升级。
- 每次微信升级必须重新验证发送、接收、图片/文件接口。
- Bridge 只监听 localhost，不暴露公网。
- 所有发送请求排队串行化，避免并发压垮微信客户端。

### 路径 B：wxhelper HTTP 模式（备选）

适用：需要 wxhelper 特有接口，或 WeChatFerry 对目标版本不可用。

建议架构：

```text
业务系统 / Agent
   |
   | HTTP localhost
   v
wxhelper HTTP server
   |
   | wxhelper.dll 注入
   v
固定版本 Windows 微信
```

落地注意：

- 严格按分支选择对应微信版本和 DLL。
- 只使用官方 README 明确列出的接口。
- 对每个微信版本维护接口能力矩阵，因为 README 明确说个别接口在部分版本没有实现。
- 注入失败时不要自动多次尝试；需要人工检查版本、位数、权限、杀软。
- 不建议在主力微信号上先试。

### 路径 C：业务机器人框架（不推荐作为底层桥首选）

如果已经使用 Wechaty/CowAgent/BotFlow 等框架，可考虑其微信通道。但若目标只是“Windows 微信静默发送桥”，这些框架过重，并且会引入额外依赖、配置、运行时和维护风险。

## 5. 发送能力对比

| 能力 | WeChatFerry/wcferry | wxhelper | Wechaty/机器人框架 |
|---|---|---|---|
| 文本 | 有 `send_text` | 有“发送文本” | 取决于底层 channel/puppet |
| @ 群成员 | `send_text` 有 `aters` 参数迹象 | 有“发送@文本” | 框架通常支持，但底层实现决定 |
| 图片 | 有 `send_image` | 有“发送图片” | 取决于 channel |
| 文件 | 有 `send_file` | 有“发送文件” | 取决于 channel |
| XML/公众号/小程序 | WeChatFerry 有 `send_xml`；具体能力需版本验证 | wxhelper 功能预览含公众号/小程序 | 框架层未必暴露 |
| 接收消息 | 有接收消息能力 | 需 hook 消息 | 框架通常封装较好 |
| Python 直接性 | 强 | 中：HTTP/Python client 示例 | 弱/中：多为 Node 或大框架 |
| 版本锁定压力 | 高 | 很高 | 取决于底层 |

## 6. PoC 验收清单（后续执行前）

在允许执行 Hook/注入的独立测试环境中，建议按以下清单验收；当前调研阶段未执行：

1. 环境：
   - Windows 版本
   - 微信版本
   - Python 版本
   - wcferry/wxhelper 版本
   - 是否管理员权限
   - 杀软/EDR 状态
2. 登录：
   - 判断登录状态
   - 获取当前登录用户 wxid
3. 基础发送：
   - 文件传输助手发送文本
   - 测试好友发送文本
   - 测试群发送文本
   - 群 @ 指定成员
4. 媒体：
   - 图片发送
   - 文件发送
   - 中文路径文件发送
5. 异常：
   - 微信未登录
   - 接收人不存在
   - 文件不存在
   - 微信重启后恢复
   - 网络断开/恢复
6. 稳定性：
   - 低频连续 100 条
   - 间隔随机化
   - 失败率和错误码记录
7. 安全：
   - 本地端口只绑定 `127.0.0.1`
   - 接口鉴权
   - 发送白名单
   - 日志脱敏

## 7. 最终建议

1. **首选：WeChatFerry / PyPI `wcferry`。**  
   理由：Python 集成最直接，公开 README/代码显示发送文本、图片、文件、XML 能力，版本更新到 3.9.12.x 系列，维护活跃。

2. **备选：wxhelper。**  
   理由：能力广、HTTP 接口方便，但版本/注入/环境敏感度更高，README 自身也提示 Win11 简测、接口随版本差异。

3. **不建议把 Wechaty/CowAgent/BotFlow 作为“静默发送桥”的底层首选。**  
   它们更适合完整机器人/Agent 框架；如果只需要本机微信发送，直接接 `wcferry` 更短、更可控。

4. **生产化必须加隔离与限频。**  
   任何 Hook/注入方案都不能视作官方稳定 API。建议只在受控本机、低频、白名单场景下使用，并准备 GUI 自动化或人工兜底通道。