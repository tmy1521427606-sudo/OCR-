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
6. 点击“测试 OCR 服务”。它只检测当前 Paddle `/v1/ocr` 地址的连通性，不发送图片；显示“服务可达”后才能开始正式任务。修改 OCR 地址后需重新测试；
7. 展示即将发送的平台、商品、图片数量及 PaddleOCR/PostgreSQL/阿里目标，确认后才开始真实调用。

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

## Linux 服务器后台运行（守护进程 + 邮件报警）

Windows 上的 `直接用图片测试.py` 是 Tkinter 界面 + DPAPI 加密凭据，只能在桌面跑。Linux 服务器上没有 tty，因此这条链路改用 `ocr_daemon.py`：**完全非交互**，只靠环境变量配置，跑完按结果发邮件报警。

### 目录约定

```text
/data/ocr/inbox/            ← 收件根目录（OCR_INPUT_ROOT）
  2026-09-18/               ← 一个批次目录
    100005996353\
      01.jpg
      02.jpg
    100006731994\
      01.jpg
  2026-09-19/               ← 再来一个批次就被自动发现
    ...
```

收件根目录下的**每个子目录**算一个批次。批次内商品数超过 100 时会自动切成多个批次块依次跑。

### 一键部署

**全新机器（还没装任何依赖，含 Ubuntu minimized 精简版）**：

minimized 精简版通常连 `git` 都没有，而引导脚本在仓库里、拉仓库又需要 `git`，
所以第一次上机先手工补一下（Python 依赖一并装了，脚本之后会自动跳过已装项）：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip ca-certificates curl

git clone https://github.com/tmy1521427606-sudo/OCR-.git
cd OCR- && git checkout linux-daemon-email-alert
```

然后用引导脚本，它会做连通性预检再自动调用下面的 `install-linux.sh`：

```bash
sudo bash deploy/bootstrap-fresh-ubuntu.sh --check-only                 # 只做连通性预检，不碰系统
sudo bash deploy/bootstrap-fresh-ubuntu.sh                             # 全新机器一步到位
sudo bash deploy/bootstrap-fresh-ubuntu.sh --app-dir /opt/ocr-v7 --user ubuntu
```

预检会检查内网 OCR、PostgreSQL、GitHub、DashScope、163 SMTP 是否可达 —— 只报告不阻断，
但这些不通的话批次跑不起来，建议先把网络搞清楚。

**服务器访问不了 GitHub 时（内网很常见）**：在开发机上打一个不含 `.git` 的源码快照，
再用 Xshell 的 Xftp / 拖拽上传 / `scp` 传过去，省得 clone 卡死：

```bash
# 开发机（Windows 也能用）—— 注意 -c core.autocrlf=false，原因见下方「常见报错」
git -c core.autocrlf=false archive --format=tar.gz -o ocr-v7-latest.tar.gz HEAD

# 服务器
mkdir -p ~/OCR- && tar xzf ocr-v7-latest.tar.gz -C ~/OCR- && cd ~/OCR-
```

快照是**扁平结构**（顶层直接就是 `demo.py`、`deploy/`…），解压后铺在目标目录里，不会再套一层。

已有 Python 和 git 的机器可以直接用：

```bash
sudo bash deploy/install-linux.sh                      # 装依赖 + 建目录 + 注册 systemd
sudo bash deploy/install-linux.sh --app-dir /opt/ocr-v7 --user ocr
sudo bash deploy/install-linux.sh --no-service         # 只装依赖
```

脚本会在 `/etc/ocr-v7/ocr-daemon.env` 生成配置模板（已存在则不覆盖）。填好后：

```bash
sudo /opt/ocr-v7/.venv/bin/python /opt/ocr-v7/email_alert.py --self-test   # 先验证邮件链路
sudo systemctl enable --now ocr-v7-daemon
journalctl -u ocr-v7-daemon -f
```

### 常见报错

**`deploy/xxx.sh: line NN: $'\r': command not found` / `: invalid option name`**

脚本带的是 CRLF 换行，bash 把每行末尾那个 `\r` 当成命令名了。Windows 上打包最容易踩
——`core.autocrlf=true` 时 `git archive` 会把 LF 转成 CRLF（不只是 checkout 会转）。
现场一刀修好即可：

```bash
cd ~/OCR-
for f in deploy/*.sh; do tr -d '\r' < "$f" > "$f.tmp" && mv "$f.tmp" "$f"; done
```

检查是否还有残留：`grep -lU $'\r' deploy/*.sh`（无输出即干净）。

仓库里已有 `.gitattributes` 强制 `deploy/*.sh`、`*.service` 等走 LF，
重新打包/克隆不会再出现这个问题。手工打包时请带上 `-c core.autocrlf=false`。

**`ERROR: Cannot install psycopg[...] and psycopg-binary==3.2.0, ... because these package versions have conflicting dependencies`**

pip 把所有候选版本都试了一遍还是没成，**这不是版本号写错了**，基本就是
「当前平台（或镜像源）没有可用的 wheel」。最常见的两种：

1. 机器是 **aarch64（ARM）**，而 pip 走的是内网镜像源，镜像里只同步了 x86_64 的 wheel；
2. 镜像源同步不全 / 版本太旧。

先看真实原因 —— **不要加 `--quiet`，它会把「The conflict is caused by:」整段原因一起吞掉**：

```bash
/opt/ocr-v7/.venv/bin/pip install 'psycopg[binary]>=3.2,<4' 2>&1 | tail -40
uname -m                                        # 看架构
/opt/ocr-v7/.venv/bin/pip config list           # 看用的是哪个源
```

三种解法，从快到慢：

```bash
# ① 换官方源试一次（镜像源缺 wheel 时最有效）
/opt/ocr-v7/.venv/bin/pip install -i https://pypi.org/simple 'psycopg[binary]>=3.2,<4'

# ② 退回纯 Python 版 psycopg + 系统 libpq5（功能一致，性能略低）
sudo apt-get install -y libpq5
/opt/ocr-v7/.venv/bin/pip install 'psycopg>=3.2,<4'

# ③ 确认真的能导入再往下走
/opt/ocr-v7/.venv/bin/python -c "from psycopg import pq; print(pq.__impl__, pq.version())"
```

`install-linux.sh` 现在会自动做 ②（`psycopg[binary]` 装不上就自动退回纯 Python 版），
并且在装依赖前先打印架构 / platform / pip 版本 —— 一旦失败，那几行基本就是答案。
依赖装完还会做一次导入校验（含 libpq 版本），校验不过直接停下来，不会带着坏环境继续。

### 手动运行

```bash
# 常驻：每 60 秒扫描一次收件目录
.venv/bin/python ocr_daemon.py --input-root /data/ocr/inbox --platform jd

# 只跑一轮就退出（适合挂 cron / systemd timer）
.venv/bin/python ocr_daemon.py --input-root /data/ocr/inbox --platform jd --once

# 只列出待处理批次，不真正运行（不需要密钥）
.venv/bin/python ocr_daemon.py --input-root /data/ocr/inbox --platform jd --dry-run --once
```

常用参数：`--interval`（轮询间隔）、`--error-backoff`（服务中断后的退避秒数）、`--max-attempts`（单批次最大尝试次数）、`--no-retry-review`（待复核批次不自动重试）、`--ocr-workers`，以及下面这些告警相关开关（都有对应的环境变量，写进 env 文件即可）：

| 参数 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--chunk-size` | `OCR_CHUNK_SIZE` | `500` | 一个批次块最多几个商品（上限 500）。500 就是「一次收 500 条、一次跑完、出一个结果文件」 |
| `--stuck-minutes` | `OCR_STUCK_MINUTES` | `30` | 连续多少分钟没有进度输出就告警 |
| `--no-stuck-alert` | — | 关 | 关掉卡住看门狗 |
| `--ocr-probe-url` | `PADDLE_OCR_API_URL` | 空 | 探活目标；默认取 OCR 地址 |
| `--ocr-probe-interval` | `OCR_PROBE_INTERVAL` | `600` | 探活间隔秒数 |
| `--ocr-probe-failures` | `OCR_PROBE_FAILURES` | `2` | 连续失败几次才告警 |
| `--ocr-probe-repeat` | `OCR_PROBE_REPEAT` | `3600` | 持续不可达时重复提醒的间隔 |
| `--no-ocr-probe` | — | 关 | 关掉探活 |
| `--maintenance-window` | `ALERT_OCR_MAINTENANCE_WINDOWS` | 空 | OCR 停机窗口，如 `14:00-18:00`；窗口内探活失败不告警 |

### 怎么看跑得怎么样

从粗到细四个地方：

**1）台账 —— 一眼看全部批次的状态**

```bash
sudo cat /var/lib/ocr-v7/state/daemon/ledger.json
```

每个批次一条记录：`status`（`pending` / `running` / `complete` / `review` / `failed` / `interrupted`）、
`attempts`、`run_id`、`success`、`review`、`finished_at`、`last_error`。

**2）实时日志**

```bash
journalctl -u ocr-v7-daemon -f                                    # 跟着看
journalctl -u ocr-v7-daemon --since "2 hours ago" | grep '\[pipeline\]'
sudo tail -f /var/lib/ocr-v7/state/daemon/ocr-daemon.log           # 日志文件
```

带 `[pipeline]` 前缀的是 demo.py 的进度（OCR 第 N/M 张、Qwen 阶段、导出阶段），
看门狗的「还活着」心跳也是从这里取的。

**3）产物目录**

```bash
ls -lt /var/lib/ocr-v7/runs/<批次名>/
```

每跑一次生成一个 `<时间戳-运行ID>/`，里面有：

| 文件 | 内容 |
|---|---|
| `result.xlsx` | 交付用的 Excel（31 列） |
| `result.csv` | 同内容 CSV |
| `report.html` | 单次运行报告，打开就能看成功率、耗时分布、告警清单 |
| `performance.json` | 性能明细：各阶段耗时、OCR P50/P95、缓存命中 |
| `待复核图片.json` | 需要人工看的图片清单 + 错误码 |
| `products/<product_id>.json` | 单个商品的完整字段与 `source_evidence`（字段到底从哪来的） |

**4）服务本身还活着吗**

```bash
sudo systemctl status ocr-v7-daemon
sudo cat /var/lib/ocr-v7/state/daemon/ocr-daemon.pid
```

### 邮件报警

配置全部通过环境变量，模板见 `deploy/ocr-daemon.env.example`。默认收件人是 **13306032298@163.com**。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ALERT_MAIL_ENABLED` | 配了账号密码即为 `1` | 总开关 |
| `ALERT_SMTP_HOST` / `ALERT_SMTP_PORT` | `smtp.163.com` / `465` | SMTP 服务与端口 |
| `ALERT_SMTP_SSL` | `1` | `1` 用 SMTP_SSL；`0` 用 STARTTLS |
| `ALERT_SMTP_USER` | 空 | 登录账号（163 填完整邮箱） |
| `ALERT_SMTP_PASSWORD` | 空 | **163 授权码，不是邮箱登录密码** |
| `ALERT_MAIL_FROM` | 同 `ALERT_SMTP_USER` | 发件人 |
| `ALERT_MAIL_TO` | `13306032298@163.com` | 收件人，多个用逗号分隔 |
| `ALERT_ON_SUCCESS` | `1` | 运行成功时是否也发汇总邮件；设 `0` 只收告警 |
| `ALERT_ATTACH_ARTIFACTS` | `1` | 附上 `performance.json` 和 `待复核图片.json`（单文件上限 2 MB） |
| `ALERT_EMPTY_PRODUCT_MIN_FIELDS` | `1` | 一个商品核心字段里非空少于几个算「没识别出来」 |
| `ALERT_OCR_MAINTENANCE_WINDOWS` | 空 | OCR 停机窗口，如 `14:00-18:00`；窗口内探活失败不告警 |

想先看看邮件长什么样（**不发信**，OCR 不在线也能跑）：

```bash
.venv/bin/python email_alert.py --preview      # 打印全部 10 种报警邮件正文
.venv/bin/python email_alert.py --self-test    # 真的发一封测试邮件
```

会触发邮件的事件：

| 主题标签 | 触发条件 | 时机 |
|---|---|---|
| `【严重故障】` | 启动时缺环境变量 / 安全确认项 | 启动即发 |
| `【运行信息】` | 服务启动、优雅停止、OCR 探活恢复 | 即时 |
| `【运行成功】` | 批次全部商品 `success`，且没有下面的异常 | 批次结束 |
| `【待复核】` | 有商品待复核，或**有商品核心字段全空**，或**批次疑似卡住** | 批次结束 / 即时 |
| `【运行失败】` | 批次抛错、**没生成 CSV**、OCR 探活连续失败、服务被强制中断 | 即时或批次结束 |

逐条对照「什么情况会收到邮件」：

| 情况 | 会不会发 | 具体行为 |
|---|---|---|
| **OCR 服务断了** | ✅ | ① 批次跑到一半断：立刻发 `【运行失败】批次运行失败（PADDLE_OCR_INTERRUPTED）`，正文注明多少秒后自动重试；② 空闲期断：探活线程连续 2 次连不上就发「内网 OCR 服务不可达」。**停机窗口内不报**（`ALERT_OCR_MAINTENANCE_WINDOWS`），窗口结束后仍未恢复才报；恢复时补发一封「已恢复」 |
| **程序被中断** | ✅ | ① 优雅停止（`systemctl stop`）：`【运行信息】后台服务已退出`；② 被第二次信号或异常打断：`【运行失败】`，并注明批次未收尾；③ 被 `kill -9` / OOM / 断电（进程本身发不出邮件）：下次启动时发「发现上次运行没有正常收尾」，列出停在 `running` 的批次 |
| **没识别出 CSV** | ✅ | 批次结束后逐个检查 `result.csv` 是否存在、行数是否够。缺文件发 `【运行失败】批次没有产出结果文件`；行数少于商品数会在汇总邮件里用 `!!` 标出 |
| **一个物品什么都没识别出来** | ✅ | 批次结束后逐行读 `result.csv`，17 个核心字段（规格/包装/规格总量/最小单位价格/是否多规格/日服量/最小·最大日服量/最小·最大日服成本/成分/人群/功能/品牌/剂型/蓝帽标识/代工厂）全空就发「有商品完全没识别出来」，列出 product_id 与排查建议 |
| **堵塞过久** | ✅ | 看门狗盯着 `demo.progress` 心跳，连续 `OCR_STUCK_MINUTES`（默认 30）分钟没有进度就发「批次疑似卡住」，**只发一次**；之后一旦有新进度会重置计时，再卡住还能再报 |

邮件正文含商品成功/待复核数量、CSV 行数、总耗时、OCR P50/P95、缓存命中数与输出目录。**报警是旁路：SMTP 连不上只写日志，不会影响 OCR 主管线。**

### 批次台账与重试语义

`.state/daemon/ledger.json` 按批次记录状态、清单指纹和尝试次数：

- 上一次 `complete` 且图片清单指纹未变 → **跳过**；
- 批次目录里新增/替换了图片（指纹变化）→ 自动重跑；
- 上一次 `review` → 在下一次扫描时自动补齐（`--no-retry-review` 可关闭）；
- 上一次 `failed` → 重试，直到 `--max-attempts` 上限后停止；
- `PADDLE_OCR_INTERRUPTED` / `VLLM_NETWORK_OUTAGE` → 记为可重试，退避 `--error-backoff` 秒后继续，不会因为 OCR 服务抖一下就永久判死。

因为底层仍是 demo.py 的 SQLite 状态库，**进程被 kill 之后再启动，已成功 OCR 的图片不会重复请求**（缓存命中会体现在邮件里）。

### 优雅退出

收到 `SIGTERM`/`SIGINT` 后先让当前批次跑完再退出，不会留下半截状态；再发一次信号则立即退出。systemd 单元里 `TimeoutStopSec=21600`，即最多等 6 小时。

### 与 Windows 版的差异

- `demo.py` 新增无人值守模式：`OCR_ASSUME_YES=1` 或 `--assume-yes`。打开后不再等待任何输入，缺少必填项直接报错退出。
- 凭据只能走环境变量；守卫进程不会替你填 `OCR_DEMO_KEYS_ROTATED`，「确认泄露的阿里 Key 已轮换」这道闸门依然要求人工显式写 `YES`。
- `直接用图片测试.py`、`run_demo.ps1`、`*.cmd`、DPAPI 加密凭据、`os.startfile` 打开报告这些只在 Windows 生效，后台服务不使用它们。

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
