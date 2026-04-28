<!-- EXECUTION PROTOCOL
1. 业务逻辑变更前必须经用户确认。
2. 每步改前读相关文件，改后做 mini 验证。
3. 不自动 commit/push/reset；最终只汇报 diff 和验证结果。
-->
# WeChat listener/sender optimization plan

需求：在 `feature/wechat-listener-sender-optimization` 分支优化微信监听发送端。

## 探索发现
- 当前接收主链路：`wechat-decrypt/wechat_bot_monitor.py` 通过 DB 增量轮询 + `fast_refresh_targets.py` 刷新，触发后调用 `reply_engine.py`。
- 当前回复生成：`reply_engine.py` spawn GenericAgent subagent，按群 wiki 检索生成。
- 当前发送：`send_mode=foreground`；`backend_only` 在 Qt 微信环境不可主用。
- 当前前台发送：OCR 可见列表命中 → 搜索 fallback → 固定坐标输入/发送 → DB 反查确认。
- 风险：前台发送会抢微信窗口；搜索/固定坐标误点会发错群；确认超时过短可能误判失败。

## 用户影响说明
- 保留 DB 监听，不改触发词、不改回复内容策略。
- 优化重点是“更稳地打开目标会话、更可观测地发送、更少误发”。
- 发送仍可能抢微信窗口；不会尝试后台静默发送作为主路径。

## 执行计划（待用户确认）
1. [ ] 拆分发送层：新增/抽出 `sender` 相关函数或模块，保留旧入口 `send_reply()` 兼容配置。
2. [ ] 增加 UIA/pywechat-style 探测层：只读探测微信窗口/会话标题/输入框/发送按钮可用性；失败不阻断现有 OCR/搜索路径。
3. [ ] 改造发送优先级：当前会话校验 → UIA 定位/切换 → OCR 可见列表 → 搜索 fallback → 物理坐标输入发送。
4. [ ] 增强安全闸：发送前校验目标会话名/用户名，发送后沿用 DB 自消息反查；失败记录原因码。
5. [ ] 增强配置：增加 `send_strategy`、`uia_probe_enabled`、`send_confirm_timeout` 默认建议，但不覆盖用户现有目标状态。
6. [ ] 增加单元/离线测试：目标匹配、策略选择、失败原因记录；避免依赖真实微信窗口。
7. [ ] 运行验证：语法检查 + pytest/离线测试 + git diff/status 检查。

## 暂不做
- 不恢复 `backend_only` 作为主发送方案。
- 不做 hook/注入/模拟器/ADB。
- 不直接删除历史探测脚本。
- 不自动 commit/push。
