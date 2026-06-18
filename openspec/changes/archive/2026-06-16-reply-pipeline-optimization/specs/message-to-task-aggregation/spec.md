## 修改说明

本 spec 不新增聚合层，仅调整 `wechat-decrypt/message_aggregator.py` 现有行为。`message_aggregator.py` 已提供同 chat+sender 的 debounce 窗口、跨消息类型聚合和 `flush_due()` 定时 flush。本次修改聚焦三个在生产测试中发现的行为不清晰/缺失点：图片+描述分离消息是否合并、触发器跨消息命中、flush 边界条件。

## MODIFIED Requirements

### Requirement: 图片消息 + 后续描述文字应进入同一 turn
当窗口期内先收到图片、后收到描述性文字时，二者应合并为同一个 `AggregatedTurn`。`has_image_task_description()` 的判定应明确支持"图片 + 同窗口后续任意非空文字（在 trigger/session 状态下）"视为有效任务描述。

#### Scenario: 图片后紧跟文字描述
- **WHEN** 用户在 5 秒内先发送气表截图，再发送 "显示什么"
- **THEN** `ingest_event` 最终返回的 `AggregatedTurn` 包含该图片和文字，`has_image_task_description()` 返回 true

#### Scenario: 图片后只有表情或无意义文字
- **WHEN** 用户发送图片后只发了一个表情或 "嗯"
- **THEN** `has_image_task_description()` 返回 false，按 image-only 处理，等待补充描述或超时 flush

### Requirement: 触发器在第 2/3 条消息命中时，前面未命中的图片应一起提交
如果窗口内第一条消息没有命中触发器，但后续消息命中触发器（如先发送图片，再 @bot 或触发关键词），则整个 turn（含前面未触发消息）应被标记为 `trigger_matched=true`，允许进入 agent 处理。

#### Scenario: 先发图片再 @bot
- **WHEN** 用户先发图片，2 秒后发送 "@飞扬的跟屁虫 这个怎么处理"
- **THEN** `AggregatedTurn.trigger_matched` 为 true，图片与文字一起进入 deep_agent 任务

#### Scenario: 先发普通消息再触发关键词
- **WHEN** 用户先发 "对了"，3 秒后发送 "@飞扬的跟屁虫 套餐怎么设置"
- **THEN** 两条消息合并为一个 turn 并触发 agent 回复

### Requirement: flush 边界条件应清晰可配置
image-only turn 等待描述的最长等待时间、窗口内最大消息数、收到有效描述后是否立即 flush，应通过配置显式控制，并在 `message_aggregator.py` / `wechat_bot_monitor.py` 中统一执行。

#### Scenario: image-only 最长等待
- **WHEN** 用户只发了图片，10 秒内没有补充文字
- **THEN** `flush_due()` 关闭该窗口，`wechat_bot_monitor.py` 发送缺失描述引导语

#### Scenario: 收到描述后立即 flush
- **WHEN** image-only 窗口已等待 3 秒，用户发送 "帮我看看"
- **THEN** 该文字与图片合并后，立即关闭窗口提交任务（或最多再等一个极短 debounce，如 0.5s）

#### Scenario: 达到最大消息数上限
- **WHEN** 配置 `max_aggregated_messages=8`，窗口内已累积 8 条消息
- **THEN** 即使窗口未满 12 秒，也立即关闭并提交聚合任务
