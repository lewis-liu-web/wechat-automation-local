# CUA 独立窗口发送模式设计

## 背景

Qt WeChat 主窗口的左侧会话列表对后台 UIA Invoke / PostMessage 点击不响应，导致 CUA 主窗口模式切换聊天不可靠。

把目标群聊拆成独立窗口后，目标选择问题从"微信内部切换会话"降级为"Windows 层面选择 HWND"；输入与发送仍可复用 CUA 的后台 ValuePattern + 回车。

## 方案

新增 `send_mode` 选项 `cua_separate_window` / `cua_window`。

### 1. 窗口定位

- 通过 `cua-driver list_windows` 枚举微信进程窗口
- 匹配 `target.name` / `username` / `remark` / `alias` / `send_aliases`
- 支持 `target` 或 `cfg` 里的 `cua_window_title` / `separate_window_title` / `window_title`
- 不校验窗口大小/坐标：`cua-driver` CLI 对 Qt 独立窗口常报 183×26 占位 bounds，但同一 `window_id` 可被 `get_window_state` 正常读取

### 2. 输入与发送

  1. `get_window_state` 读取 AX 树
  2. 查找 `Edit "输入" [id=chat_input_field]`
  3. 若找到：直接用 UIA `ValuePattern.SetValue` 写入文本，再后台 `press_key return`
  4. 若未找到：回退到坐标点击输入区 + `type_text` + `return`
  5. **焦点还原**：发送前保存当前前台窗口，`SetValue` 会短暂激活微信窗口，发送后把焦点还原到原前台窗口；最小化状态也能发送，无需先 restore

### 3. 与主窗口 CUA 模式的区别

| 步骤 | `cua` 主窗口模式 | `cua_separate_window` |
|------|------------------|-----------------------|
| 找窗口 | 找 `title='微信'` | 按目标群名匹配独立窗口 |
| 切换聊天 | UIA 列表点击 / 搜索 | 不需要 |
| 输入 | 坐标点击 + `type_text` | `set_value` 优先 |
| 发送 | 点发送按钮或回车 | 后台回车 |
 | 焦点 | 可能前台化 | 发送瞬间可能激活，但会还原 |

## 配置

```json
{
  "send_mode": "cua_separate_window",
  "send_confirm_timeout": 15.0
}
```

每个目标群聊需要有一个独立聊天窗口。窗口可以手动"在独立窗口中打开"并保持不关闭，也可以在发送时由 `_cua_pop_out_chat` 自动从主窗口拆分出来。

## 自动拆分窗口 (`_cua_pop_out_chat`)

当 `_cua_find_separate_chat_window` 找不到已有独立窗口时，`send_reply_cua_separate_window` 会尝试自动拆分：

1. 前台化微信主窗口（发送前后会保存/还原原来的前台窗口和鼠标位置）。
2. 在搜索框中输入目标别名，点击下拉结果里带 `search_item_<显示名>` 的条目；该 `<显示名>` 通常是用户设置的备注名。
3. 点击搜索结果后，目标群聊会自动跳到左侧会话列表顶部并被高亮；**不要清空搜索框**（`Esc` 会最小化主窗口，点击搜索框会打开覆盖列表的下拉）。
4. 直接对高亮的 `session_item_<显示名>` 进行真实鼠标双击，触发 WeChat 的"独立窗口显示"手势；只有真实鼠标双击可靠，后台 UIA/PostMessage 双击无效。
5. 通过对比双击前后出现的 WeChat 窗口集合来定位新弹出的独立窗口，而不是依赖窗口标题与原群名匹配，因此能正确处理"备注名 vs 原群名"不一致的情况。

### 备注名与原群名

微信允许给群聊设置备注名。设置后：

- 左侧会话列表显示备注名，独立窗口标题通常也跟随备注名。
- 自动拆分使用 `search_item_<备注名>` 定位会话，不依赖原群名。
- 新窗口通过"前后窗口集合差"检测，不依赖标题匹配，因此 `target.name` 写原群名或备注名均可发送。
- 若已有独立窗口标题与 `target.name` 不匹配，可在目标配置里填写 `cua_window_title` 覆盖。

## 目标字段

```json
{
  "name": "bot群聊测试",
  "cua_window_title": "测试群"
}
```

`cua_window_title` 用于独立窗口标题与 `name` 不一致时的精确匹配。

## 已知限制

- 自动拆分需要主窗口处于可交互状态；若主窗口崩溃/空白，拆分也会失败。
- 多显示器/最小化场景尚未长测。
- DB 确认仍可能因解密同步延迟而不稳定。
- `SetValue` 仍会让目标窗口短暂获得焦点，我们已用保存/还原抵消，但不会完全消除系统级可见闪烁。

## 验证

- 单元测试：`wechat-decrypt/tests/test_cua_sender.py` 21 passed
- 实机 E2E：对 `bot群聊测试` 独立窗口后台发送成功，消息进入群聊，焦点被还原
- 自动拆分 E2E：
  - `target.name='family'`（备注名）无已有独立窗口时自动拆分并发送成功
  - `target.name='家人们'`（原群名）无已有独立窗口时也能自动拆分并发送成功

## 下一步（按优先级）

1. **生产长测**：让 monitor 在 `cua_separate_window` 下运行一段时间，统计 `confirm_timeout` 率和误发率。
2. **默认切换**：`cua_separate_window` 已是默认 `send_mode`；继续观察自动拆分在生产环境中的稳定性。
3. **服务持久化**：为 `control_api`、monitor、Hermes 异步循环添加 Windows 服务/Task Scheduler 持久化。
