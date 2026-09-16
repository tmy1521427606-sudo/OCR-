# OCR v7 运行日志与排障面板设计

## 目标

让一次运行中的 OCR、PostgreSQL、Qwen、校验和导出过程可实时查看、可筛选、可在失败后追溯。用户无需从终端长文本中猜测卡点；每条失败都应显示阶段、商品/图片、耗时、错误码及建议动作。

本期不改变 OCR、PostgreSQL 或 Qwen 的调用方式、并发策略和业务结果字段。它只补齐可观测性，不用 AI 子 agent 参与运行。

## 现状与边界

现有 `demo.py` 已将结构化事件写进 `state.sqlite3.events`，并有 `实时结果.html`、`report.html` 与 Tkinter 的终端文本转发。但 SQLite 事件不在单次运行目录中、GUI 不支持结构化筛选，运行中的报告也不能完整看到数据库等待和完整错误信息。

第一期仅调整：

- `demo.py`：统一生成和持久化运行事件、阶段统计以及 HTML 运行面板。
- `直接用图片测试.py`：显示摘要、筛选后的详细日志和错误详情，并可打开运行面板。
- 自动化测试：验证事件落盘、错误脱敏、统计和页面内容。

不引入消息队列、独立服务、后台守护进程或多 agent。

## 数据与事件模型

每次 `StateStore.event(...)` 继续写入 SQLite；同一事件另追加到本次运行目录的 `events.jsonl`。每行是一个 JSON 对象，字段来源于现有事件模型：

- `created_at`、`run_id`、`attempt_id`
- `stage`（`postgresql`、`ocr`、`qwen`、`validation`、`export`）和 `status`
- `severity`、`error_code`、安全的 `message`
- `product_id`、`image_name`（有则写）、`duration_ms`、`attempts`、`cached`
- 可公开的 `source_table`、`artifact_ref`

日志不得写入 PostgreSQL 密码、DashScope Key、完整连接串、请求 Authorization 头或图片 Base64。异常信息始终经现有 `safe_message` 清理。

事件文件使用追加写入与写锁；每条事件先进入 SQLite，再进入 JSONL。JSONL 写入失败不得中断 OCR/数据库/Qwen 流程，但会额外产生一条 `observability` 警告事件。

## 前端体验

运行目录增加 `运行面板.html`，每 3 秒刷新，运行结束后保留。它展示：

1. 顶部摘要：运行状态、已经运行的时长、当前尝试、各阶段状态、成功/待复核/失败图片和商品数量。
2. 阶段耗时：PostgreSQL 总耗时与当前查询表、OCR 完成进度及 P50/P95、Qwen 与导出耗时。
3. 错误摘要：按错误码和阶段聚合，显示最近报错及建议动作。
4. 事件列表：时间、阶段、状态、商品、图片、耗时、重试次数、消息。支持浏览器端按阶段、商品 ID 和“仅错误”筛选，并可展开完整安全错误详情。

Tkinter 主页面保留原来的终端滚动文本，并增加：

- 状态栏摘要：当前阶段、总运行时长、OCR 成功/待复核、最新数据库表及已经等待的秒数。
- “运行面板”按钮：首个事件产生后可打开 `运行面板.html`。
- “查看实时日志”窗口：使用已接收的结构化事件渲染，提供阶段下拉、商品 ID 输入框、仅错误开关和详情区；不会只依赖终端字符串正则。

`实时结果.html` 继续专注展示已完成商品的业务输出；`report.html` 继续作为结束后的正式结果报告，并增加指向 `运行面板.html` 与 `events.jsonl` 的链接。

## 错误建议

页面内置有限映射，不把原始异常直接当建议：

- `PADDLE_OCR_UNREACHABLE`：检查 VPN 路由和 `192.168.1.115:8870` 端口。
- `statement timeout` / `POSTGRES_*`：显示具体表、已等待时间，建议检查索引和执行计划。
- `PermissionError` / `EXPORT_*`：关闭 Excel/资源管理器中占用的结果文件，然后恢复运行。
- OCR/Qwen 网络错误：显示重试次数与下一次重试时间；达到上限时标记待复核。

没有匹配项时仅显示“查看错误详情和同阶段事件”，不编造诊断。

## 运行结构

本期仍是一条确定性流水线：

```text
调度器 → PostgreSQL 查询（与 OCR 并行）
       → Paddle OCR
       → Qwen 提取
       → 校验与导出
       → HTML / Excel / CSV
```

模块通过结构化事件汇报状态，而不是通过 AI agent 互相对话。未来若扩展到 10,000 商品，可将同样的事件契约交给独立 OCR worker、数据库 worker 和导出 worker；是否拆进程取决于吞吐和机器资源的实测，而不是当前界面需求。

## 验收标准

1. 模拟运行期间，运行目录存在持续增长的 `events.jsonl` 和自动刷新的 `运行面板.html`。
2. 同一商品的 OCR 成功、待复核、数据库查询开始/完成/失败、Qwen、导出均能在面板按时间显示。
3. PostgreSQL 月度表超时页面能显示对象名、约 300 秒耗时、错误码/详情和“索引或执行计划”建议。
4. `result.csv` 被 Excel 占用时，页面显示导出错误详情；OCR/Qwen 已完成事件仍保留。
5. 页面和 JSONL 中不出现 API Key、密码、Authorization 或完整数据库 URL。
6. 现有单元测试通过，并新增覆盖事件落盘、过滤/错误建议、HTML 摘要的测试。

