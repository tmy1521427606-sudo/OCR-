# PaddleOCR-VL 商品识别 v7

这是独立的 v7 目录。每次处理 **1 到 100 个商品目录**：图片批量发送到内网 PaddleOCR-VL `/v1/ocr`，同时批量读取 PostgreSQL，再由 DashScope Qwen 生成结构化字段，最后输出 Excel、CSV、商品 JSON 和 HTML 状态报告。

v3 对 `代工厂`、`代工厂地址`、`生产许可证`、`产地` 严格按以下顺序取值：

```text
OCR 中明确写出的内容 → 数据库属性同义字段 → 带提供方 URL 的联网搜索
```

Qwen 提取不得推断这四项；联网搜索没有提供方 URL 时结果会被拒绝。`热门话题`只来自带 URL 的联网搜索。

## 目录约定

图片按下面的目录层级存放：

```text
<平台>\
  <批次>\                 ← 运行时选择这个目录
    <product_id_1>\
      01.jpg
      02.jpg
    <product_id_2>\
      01.png
    ...一次可选择 1 到 100 个商品目录
```

`product_id` 目录名只能包含字母、数字、下划线或短横线。支持 `jpg/jpeg/png/webp/bmp/tif/tiff`，子目录中的图片也会递归读取，并按文件名自然排序。实际商品身份是 `(platform, product_id)`；程序会建议批次目录的上一级名称，但必须由你确认或通过 `-Platform` 明确传入。

## 首次运行

双击 **`启动OCR_Demo.cmd`**。它会打开 v5 单页面，记住上次使用的目录、商品选择、连接配置与 Windows 加密保存的凭据，无需每次重新输入。页面中可直接点击“实时结果”、“正式报告”和“打开结果目录”。启动器会：

1. 在本目录创建 Python 3.12 的 `.venv`；
2. 安装 `psycopg[binary]>=3.2,<4`、`openpyxl>=3.1,<4` 和 `Pillow>=10,<13`；
3. 使用纯 Python 生成 Excel，不需要 Node.js 或 Codex 运行时；
4. 选择批次目录后勾选 1 到 100 个商品；测试100个商品时可点击“全选前100个”；
5. 设置 Paddle 批次并行（1 或 2）和运行方式；
6. 展示即将发送的平台、商品、图片数量及 PaddleOCR/PostgreSQL/阿里目标，确认后才开始真实调用。

如果双击运行失败，窗口会保留，便于查看错误。脚本不会自动删除已有 `.venv`，也不会把明文密钥写入配置和日志。

也可以从 PowerShell 指定目录：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run_demo.ps1 -Root "D:\商品OCR\京东\2026-08-28" -Platform jd
```

## 三种运行方式

真实运行（会访问内网 PaddleOCR、阿里和 PostgreSQL；按 PostgreSQL 网络要求开启市北 VPN）：

```powershell
.\run_demo.ps1 -Root "D:\商品OCR\京东\2026-08-28" -Platform jd
```

如果开启市北 VPN 后 PaddleOCR `192.168.1.115:8870` 不可达，先双击 `修复Paddle路由.cmd` 并在 Windows 的管理员确认窗口选择“是”。它只添加一条临时主机路由，随后会显示端口测试；必须看到 `TcpTestSucceeded : True` 再启动 OCR。重启电脑后该路由自动失效。

约 1 万商品建议先运行“批量首轮”。在前端勾选对应复选框，或使用下面的命令；它会完全跳过联网搜索，只保留 OCR、数据库、结构化提取结果，并在每个商品 JSON 的 `missing_enrichment_fields` 中列出待补全字段：

```powershell
.\run_demo.ps1 -Root "D:\商品OCR\京东\2026-08-28" -Platform jd -BulkFirstPass
```

模拟运行（自动生成 5 个本地测试商品，不外发任何数据）：

```powershell
.\run_demo.ps1 -Mock -Platform jd
```

也可给 `-Mock` 加 `-Root`，用你自己的 1 到 100 个目录验证目录和输出效果，但图片仍不会外发。

自检（不需要批次目录、密钥或数据库）：

```powershell
.\run_demo.ps1 -SelfTest
```

如果当前 PowerShell 不允许执行脚本，在以上命令前使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`，或直接双击 CMD 启动器。

## 真实模式环境变量

非密钥参数可以设置为当前 PowerShell 进程的环境变量；密钥变量不设置时，程序会用隐藏输入读取。不要创建 `.env`，也不要把值写进本目录文件。

| 环境变量 | 是否必需 | 用途 |
|---|---:|---|
| `PADDLE_OCR_API_URL` | 否 | PaddleOCR `/v1/ocr` 完整地址，默认 `http://192.168.1.115:8870/v1/ocr` |
| `PADDLE_OCR_MODEL_VERSION` | 否 | 模型部署版本，默认 `PaddleOCR-VL-1.6`；会参与缓存键 |
| `DASHSCOPE_API_KEY` | 是 | 已轮换的阿里 DashScope/Qwen Key |
| `OCR_DEMO_PLATFORM` | 否 | 数据库平台标识；也可使用 `-Platform` |
| `POSTGRES_HOST` | 是 | PostgreSQL 主机名 |
| `POSTGRES_PORT` | 否 | 端口，默认 `5432` |
| `POSTGRES_DATABASE` | 是 | 数据库名 |
| `POSTGRES_USER` | 是 | 只读服务账号 |
| `POSTGRES_PASSWORD` | 是 | PostgreSQL 密码 |
| `POSTGRES_SCHEMA` | 否 | 表所在 schema，当前库为 `workdb` |
| `POSTGRES_SSLMODE` | 否 | `verify-full`（默认）、`verify-ca`、`require` 或 `disable` |

示例只设置非密钥项：

```powershell
$env:PADDLE_OCR_API_URL = "http://192.168.1.115:8870/v1/ocr"
$env:POSTGRES_HOST = "<postgres-host>"
$env:POSTGRES_DATABASE = "<database>"
$env:POSTGRES_USER = "<readonly-user>"
$env:POSTGRES_SCHEMA = "workdb"
.\run_demo.ps1 -Root "D:\商品OCR\京东\2026-08-28" -Platform jd
```

若公司要求通过环境变量传入密钥，只在本次终端进程设置，运行结束后清除：

```powershell
Remove-Item Env:DASHSCOPE_API_KEY, Env:POSTGRES_PASSWORD -ErrorAction SilentlyContinue
```

## 输出与断点恢复

每次运行会生成独立的运行目录，终端会打印实际路径，其中包括：

```text
result.xlsx
  Result             业务结果
  Audit              节点状态、重试、错误和价格来源
products\<product_id>.json
实时结果.html
report.html
```

运行期间可先打开 `实时结果.html`；它每 3 秒刷新。单张图片超过 30 秒上限时，会标记为 `OCR_TIMEOUT_REVIEW`，该图不再重试，其余图片继续以原并发处理。商品会使用已完成图片进入 Qwen 提取并导出；报告和 Audit 会列出待复核图片。正常结束后会自动打开 `report.html` 和运行目录。若某次运行处于待复核状态，再次选择相同批次会恢复未完成节点；需要强制获取新的数据库快照时使用 `-NewRun`。同一运行每次恢复都会生成独立的 `attempt_id`；HTML 报告只显示当前尝试，Audit 保留全部尝试历史。

## 性能记录与优化验证

Redshift 批量查询和本地 OCR 会并行启动；即使数据库查询失败，已完成的 OCR 产物仍会保留。每个运行目录都会生成 `performance.json`，并在 `report.html` 的“性能摘要”中显示总耗时、Redshift/OCR/Qwen 阶段耗时、OCR P50/P95、成功吞吐、缓存命中和并发峰值。

使用 `-NewRun` 对同一批次重复运行时，报告会自动对比相同批次指纹、相同 OCR 提供方和模型版本的上一次数据。调整 OCR 并发前，请至少分别测试并发 1、2、4，并比较 P50/P95 和成功图片吞吐；只有同批次、同模型的多次结果一致改善，才可认定优化有效。

真实调用开始前会通过无代理的 `/v1/models` 预检本地 Qwen-VL；连不上就立即停止，不会启动整批 OCR。每张图片只请求一次，最长等待 30 秒；超时即停止等待并标记待复核，不进行后台补偿。若连续 3 张前台图片都是网络错误，会自动停止余下图片调度并提示检查市北 VPN、内网和服务端。Qwen 提取与搜索也会各自最多尝试 5 次。部分图片 OCR 失败时，程序会使用成功图片继续生成待复核结果；再次运行相同批次只会重新请求未缓存成功的图片。

当日服量中出现多个独立的“每日…”用量时，结果会标记 `DOSAGE_CONFLICT`；当 Qwen 备注提示竞品、非本商品或品牌混淆图片时，会标记 `IMAGE_PRODUCT_MISMATCH`。两类标记都会使商品进入待复核，避免直接使用受污染的营销字段。

单图的模型推理时间不会因并发而缩短；并发只会在本地 vLLM 有 GPU 余量时增加整体吞吐。要对比真实优化，用同一批5个商依次测 3、4、6 并发：每次勾选“测速：强制重新 OCR”和“强制创建新运行”，结束后比较 `performance.json` 的 OCR 耗时、P50/P95 和成功图片/分钟。如果 4 路吞吐没提升或 P95/错误上升，就退回 3；6 路也按同样标准判断。正式批量不勾测速模式，以免重复发送已缓存图片。

如果数据库对象没有平台字段，结果会记录 `PLATFORM_UNVERIFIED`。这表示平台只来自本地选择，数据库实际仅按商品 ID 匹配；跨平台运行前必须确认 ID 全局唯一或补充平台映射。

联网搜索字段只在 DashScope 响应包含提供方返回的来源 URL 时进入结果。没有可核验来源时，搜索字段留空并记录警告，但不阻止商品进入 `Result`，避免把模型生成内容当作事实。普通模式会在 OCR 提取完成后判断缺口；批量首轮则主动跳过全部搜索，且不会把主动跳过记成失败。

本地 SQLite 状态库及 OCR/Qwen 原始产物用于缓存、断点恢复和追溯。不要在运行期间移动这些文件；需要改字段或提示词时，更新模板版本后可复用原始 OCR，避免重复付费。原始产物默认保留 90 天。

## 运行前安全确认

- **之前截图和 KNIME 脚本中出现过的阿里 Key 已经泄露。真实运行前必须先在阿里控制台吊销并轮换，旧密钥不得继续使用。**
- 关闭市北 VPN 后再连接内网 Qwen-VL；程序会绕过系统 HTTP 代理，但不会自动操作 VPN。
- 仅选择允许发送给内网 Qwen-VL 和阿里的商品图片；确认页内容无误后再继续。
- Redshift 使用专用只读账号，不使用个人账号或写权限账号。
- 不在命令行参数、文件名、模板、截图、聊天或日志中粘贴密钥。
- 保持系统证书和 TLS 校验开启；不要恢复 KNIME 旧脚本中的 `CERT_NONE` 设置。
