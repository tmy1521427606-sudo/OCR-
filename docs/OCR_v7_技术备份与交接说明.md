# OCR v7 技术备份与交接说明

## 1. 用途和备份范围

本文件说明当前 `OCR_v7_full` 的完整业务链路、数据库关联规则、接口、运行步骤、输出物、故障定位方式和源码入口。它用于交接、排错和后续二次开发。

完整可运行源码以当前 Git 仓库为准；本文件不重复复制全部源码，以免文档与代码产生两个不一致版本。每一项逻辑均给出对应源码文件和主要函数，可直接定位阅读。

不纳入版本库和本文档的内容包括：PostgreSQL 密码、DashScope Key、图片原文、OCR 原始结果、SQLite 运行状态、`runs/` 运行产物和 `.venv/` 环境目录。

## 2. 软件目标

输入是按商品分目录存放的商品图片。系统为每个商品：

1. 调用内网 PaddleOCR-VL 识别图片文字；
2. 同时从 PostgreSQL 查询商品主数据、月度价格和属性；
3. 将 OCR 文本和数据库属性交给 Qwen，提取模板规定的结构化字段；
4. 对关键字段、价格来源、剂量冲突和图片混入进行校验；
5. 输出 Excel、CSV、商品 JSON、实时结果页、正式报告和性能统计。

一个商品目录的名称就是 `platform_goods_id`。业务身份始终按 `(platform, product_id)` 保存，避免不同平台出现同名 ID 时误合并。

## 3. 总体流程

```text
选择批次目录和商品
        |
        v
构建 manifest：平台、商品 ID、图片路径、图片 SHA256
        |
        +---------------------- 并行前置阶段 ----------------------+
        |                                                         |
        v                                                         v
PostgreSQL 商品主表/价格/属性查询                         PaddleOCR-VL 图片 OCR
        |                                                         |
        +-------------------------+-------------------------------+
                                  |
                                  v
                    每个商品的已完成 OCR 可先进入 Qwen
                                  |
                                  v
              Qwen 结构化提取，必要时补充带来源 URL 的搜索
                                  |
                                  v
                 校验、生成商品 JSON、CSV、Excel、HTML 报告
```

前置阶段采用两个线程并行：数据库慢时，OCR 仍会完成并进入缓存；图片 OCR 失败时，其他成功图片仍可用于该商品的 Qwen 提取，失败图片会进入待复核清单。

## 4. 图片输入和 OCR

### 输入目录

```text
<平台>\
  <批次目录>\              <- 前端选择此目录
    <platform_goods_id>\
      01.jpg
      02.jpg
```

支持 `jpg/jpeg/png/webp/bmp/tif/tiff`，并按自然排序读取。一次 GUI 运行可选择 1 到 100 个商品；批量作业模块用于更大的任务。

### PaddleOCR-VL 调用

- 默认接口：`http://192.168.1.115:8870/v1/ocr`
- 预检：约 3 秒的端口/接口可达性检查；不可达时不持续等待，而是将图片标记待复核。
- 请求方式：每批最多 8 张图片；GUI 可设置批次并行 1 或 2。
- 成功缓存键：图片 SHA256 + Paddle 模型版本；同图同版本恢复运行时不会重复发送。
- 单张图片失败：保存错误码与失败原因，后续图片继续处理；成功图片仍进入 Qwen。

主要源码：

| 文件 | 主要函数/类 | 职责 |
|---|---|---|
| `paddle_ocr.py` | `verify_paddle_available`、`build_payload`、`post_batch` | `/v1/ocr` 的预检、请求、响应 ID 校验 |
| `demo.py` | `run_paddle_ocr_stage`、`paddle_result_to_ocr_result` | 批量调度、缓存、进度事件、待复核结果 |
| `demo.py` | `find_images`、`build_manifest` | 图片扫描和输入清单 |

## 5. PostgreSQL 数据库连接和关联

### 连接配置

前端在 `直接用图片测试.py` 中收集连接信息，密码使用 Windows DPAPI 加密保存到本机 `.state/`；运行时仅通过子进程环境变量交给 `demo.py`。

| 环境变量 | 用途 |
|---|---|
| `POSTGRES_HOST`、`POSTGRES_PORT` | PostgreSQL 主机和端口，端口默认 `5432` |
| `POSTGRES_DATABASE` | 数据库名 |
| `POSTGRES_USER`、`POSTGRES_PASSWORD` | 只读服务账号和密码 |
| `POSTGRES_SCHEMA` | Schema，当前为 `workdb` |
| `POSTGRES_SSLMODE` | 默认 `verify-full`；目标服务器不支持 SSL 时应明确设为 `disable`，不能由程序猜测 |

连接参数组装在 `demo.py` 的 `postgres_connection_info` 和 `real_config`；实际查询入口是 `redshift_backend.py` 的 `enrich_products`。

### 已确认的表与键

| 查询对象 | 实体含义 | 连接键 | 在流程中的作用 |
|---|---|---|---|
| `workdb.d_platform_goods` | 去重后的商品主表 | `platform_goods_id` | 取 `platform_goods_key`、商品名、店铺、品牌和主表价格 |
| `workdb.mv_com_goods_statistics_monthly_v2_internal_ssv4` | SSV4 月度结果/价格对象 | `platform_goods_id` | 取月度价格、月度商品名和可能的 `platform_goods_key` |
| `workdb.d_platform_goods_attributes` | SSV4 属性表 | `platform_goods_key` | 取属性名和值；必须先从主表或月度对象取得 key |
| `workdb.new_infinitus_attribute` | 无限极属性表 | `item_id`，等同商品 ID 概念 | 作为属性补充来源；不覆盖已有 SSV4 同名属性 |

`platform_goods_id`、`product_id`、`item_id` 在本业务中表示同一官方平台商品 ID 概念；图片文件夹名就是该 ID。`platform_goods_key` 是内部关联键，不能从图片目录直接获得。

### 查询顺序和合并规则

```text
输入 product_id 列表
  -> d_platform_goods        WHERE platform_goods_id = ANY(...)
  -> 月度 SSV4 对象           WHERE platform_goods_id = ANY(...)
  -> 收集上述结果的 platform_goods_key
  -> d_platform_goods_attributes
                              WHERE platform_goods_key = ANY(...)
  -> new_infinitus_attribute WHERE item_id = ANY(...)
  -> 组装每个 (platform, product_id) 的数据库快照
```

1. 主表和月度对象都按 `platform_goods_id` 查询，以主表/可验证月度结果确定内部 key。
2. SSV4 属性按内部 key 查询；无限极属性按原始商品 ID 查询。
3. 相同属性名优先 SSV4 属性；无限极只补充 SSV4 不存在的字段，并保留来源表信息。
4. 价格优先使用可唯一确定的月度价格；月度价格不可用时才使用主表价格。若键、平台或价格存在歧义，会写入校验问题，不静默猜测。
5. 如果数据库对象没有平台列，结果会标注 `PLATFORM_UNVERIFIED`，因为此时只能按商品 ID 匹配。

### 超时和索引背景

月度对象和 SSV4 属性表当前单次查询超时均为 300 秒。真实运行曾出现月度对象 300 秒超时；索引应由数据库管理员在实际底层表或物化视图上维护。

已建议的索引方向：

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_d_platform_goods_platform_goods_id
ON workdb.d_platform_goods (platform_goods_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_d_platform_goods_attributes_platform_goods_key
ON workdb.d_platform_goods_attributes (platform_goods_key);
```

月度对象只有在其为普通表或物化视图时才可直接按 `platform_goods_id` 建索引；若是普通视图，必须查视图定义并在底层表建索引。索引完成后应执行对应对象的 `ANALYZE`。

主要源码：

| 文件 | 主要函数/常量 | 职责 |
|---|---|---|
| `redshift_backend.py` | `OBJECTS`、`REQUIRED_COLUMNS` | 数据库对象名、必需字段和 schema 校验 |
| `redshift_backend.py` | `_resolve_relations`、`_build_lookup_query` | 发现实际 Schema/列并构造小范围参数化查询 |
| `redshift_backend.py` | `enrich_products` | 按上方顺序查询并输出日志耗时 |
| `redshift_backend.py` | `resolve_mock_rows`、`_merge_attributes` | 主表/月度/属性/无限极数据的归并和优先级 |
| `demo.py` | `run_database_stage` | 运行缓存、数据库快照和事件写入 |

## 6. Qwen 提取、补充和校验

OCR 文本以商品为单位汇总，数据库属性会作为上下文提供给 Qwen。模板定义在 `template-v2.json`，决定字段、类型、可空性以及输出列。

关键事实字段（代工厂、代工厂地址、生产许可证、产地）的取值顺序是：

```text
OCR 图片明确文字
  -> 数据库属性同义字段
  -> 带提供方 URL 的联网搜索
```

Qwen 不得推断这些字段；搜索没有可核验 URL 时结果不会写入最终事实字段。批量首轮模式跳过联网搜索，保留核心 OCR/数据库/结构化结果，并标出待补全字段。

校验包括：模板字段校验、缺失信息、价格来源、多个“每日”剂量冲突（`DOSAGE_CONFLICT`）、图片疑似不是该商品（`IMAGE_PRODUCT_MISMATCH`）等。任何待复核问题不会丢弃已识别数据，而是将商品状态置为 `review`。

主要源码：

| 文件 | 主要函数 | 职责 |
|---|---|---|
| `demo.py` | `run_qwen_one`、`run_qwen_stage` | Qwen 提取、搜索、重试和缓存 |
| `demo.py` | `database_enrichment`、`resolve_enrichment` | OCR、数据库属性、搜索来源的事实字段优先级 |
| `demo.py` | `assemble_product` | 汇总单商品最终结构化记录 |
| `demo.py` | `has_daily_dosage_conflict`、`has_competitor_image_note` | 业务风险校验 |
| `template-v2.json` | 字段定义 | Excel/CSV/JSON 共同的字段模板 |

## 7. 输出、缓存和恢复

每次运行在 `runs/<run_id>/` 下输出：

| 文件 | 内容 |
|---|---|
| `result.xlsx` | 最终 Excel；包含 `Result` 业务结果和 `Audit` 审计信息 |
| `result.csv` | 最终业务结果 CSV |
| `products/<product_id>.json` | 单商品完整结构化结果、来源和校验问题 |
| `待复核图片.json` | 未成功 OCR 的图片清单及错误 |
| `实时结果.html` | 已完成商品的阶段性业务输出，运行中自动刷新 |
| `report.html` | 最终正式报告：成功/待复核、错误和性能摘要 |
| `performance.json` | 阶段耗时、OCR P50/P95、吞吐、缓存和与上次对比 |

状态库位于 `.state/state.sqlite3`，保存运行状态、事件和缓存原始产物。恢复相同批次时：

- 已成功 OCR 的图片读取缓存，不重复请求；
- 已完成数据库快照可读取当次缓存；
- 每次恢复生成新的 `attempt_id`，用于区分尝试；
- 运行发生错误时，已完成 OCR/Qwen 产物仍保留；
- 若 `result.csv` 或 `result.xlsx` 被 Excel 占用，会出现 `PermissionError`，应关闭占用文件后恢复运行。

主要源码：`StateStore`、`execute_pipeline`、`write_partial_product`、`write_report`、`export_csv_file` 和 `export_workbook_file`，均位于 `demo.py`。

## 8. 前端、启动和批量工具

### GUI 前端

双击 `启动OCR_Demo.cmd`，它调用 `run_demo.ps1 -Gui`，再启动 `直接用图片测试.py` 的 Tkinter 页面。该页面可以：

- 选择批次目录、勾选商品和选择前 100 个；
- 配置 Paddle OCR 地址、PostgreSQL 连接和模型版本；
- 加密保存凭据；
- 选择批量首轮、强制新运行、强制 OCR 测速及 Paddle 并行数；
- 显示终端实时日志，打开实时结果、正式报告和输出目录。

### 命令行入口

```powershell
# 正式运行
.\run_demo.ps1 -Root "D:\商品OCR\京东\批次" -Platform jd

# 批量首轮，跳过联网搜索
.\run_demo.ps1 -Root "D:\商品OCR\京东\批次" -Platform jd -BulkFirstPass

# 使用模拟数据自检工作流
.\run_demo.ps1 -Mock -Platform jd

# 代码与逻辑自检
.\run_demo.ps1 -SelfTest
```

若市北 VPN 导致 Paddle 内网服务不可达，先以管理员身份运行 `修复Paddle路由.cmd`；仅在端口检测显示 `TcpTestSucceeded : True` 后再启动应用。

### 大批量工具

- `创建批量任务.py`：生成批量任务。
- `batch_jobs.py`：SQLite 批量任务状态与任务记录。
- `batch_worker.py`：串行执行批次、生成待复核重试清单。
- `retry_review.py`：从待复核图片和原商品结果中形成失败图重试输入，并合并缺失字段。

## 9. 常见问题定位

| 现象或日志 | 首先检查 | 说明 |
|---|---|---|
| `Paddle OCR 服务不可达` | VPN 路由、`192.168.1.115:8870` 端口 | 服务不通时应快速标记待复核，不要等待整批超时 |
| 月度对象或属性表 `statement timeout` | 查询耗时日志、索引、视图底层执行计划 | 这是 PostgreSQL 查询问题，不是 OCR 慢 |
| `server does not support SSL` | `POSTGRES_SSLMODE` | 该服务端不支持 SSL 时需使用 `disable`；生产环境不要随意降低 TLS 要求 |
| `function getdate() does not exist` | SQL 兼容性 | PostgreSQL 使用 `CURRENT_TIMESTAMP` / `now()`，不是 Redshift 的 `GETDATE()` |
| CSV/Excel `Permission denied` | Excel、资源管理器预览、其他程序 | 关闭占用文件，恢复同一运行即可复用缓存 |
| 商品进入 `review` | `待复核图片.json`、商品 JSON、`report.html` | 代表部分图片或字段需要人工复核，不代表成功结果被丢弃 |

## 10. 代码文件总索引

| 文件 | 作用 |
|---|---|
| `demo.py` | 主编排器：输入、OCR、数据库、Qwen、校验、导出、恢复和报告 |
| `paddle_ocr.py` | PaddleOCR-VL `/v1/ocr` HTTP 适配器 |
| `redshift_backend.py` | PostgreSQL/SSV4/无限极表的模式发现、查询、价格和属性合并 |
| `直接用图片测试.py` | Windows Tkinter 单页前端和加密配置存储 |
| `run_demo.ps1`、`启动OCR_Demo.cmd` | Python 环境准备和 GUI 启动 |
| `fix_paddle_route.ps1`、`修复Paddle路由.cmd` | 临时添加 Paddle 服务器主机路由并检测端口 |
| `template-v2.json` | 结构化字段定义和结果列模板 |
| `export_workbook.mjs` | Excel 导出辅助实现 |
| `batch_jobs.py`、`batch_worker.py` | 大批次任务状态和串行执行 |
| `retry_review.py` | 失败图片复核/重试的清单和结果合并 |
| `test_*.py` | OCR、数据库、前端、性能、恢复和导出的自动化测试 |

## 11. Git 备份和恢复

项目已初始化为 Git 仓库，并推送到 GitHub。代码、测试、模板、README 和本文档应提交；`.state/`、`.venv/`、`runs/`、日志、压缩包和运行结果已由 `.gitignore` 排除。

日常版本操作：修改后在 GitHub Desktop 查看改动，填写说明，点击 **Commit to main**，再点击 **Push origin**。需要恢复旧版本时，在 GitHub Desktop 的 **History** 查看对应提交；恢复前先复制当前工作目录或新建分支，避免覆盖尚未保存的新修改。
