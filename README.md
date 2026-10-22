# tokenmon — AI 编程工具 Token 用量统计分析

把多个 AI 编程工具的 token 用量汇总到一个本地数据库，按**日 / 周 / 月**和
**工具、模型、项目、工具+模型**等维度统计：每个模型用了多少 token、缓存命中多少、
交互（提问）次数多少。

## 支持的工具

| key | 工具 | 数据来源 |
|---|---|---|
| `codex` | OpenAI Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl`（含 `account_*` 子 home） |
| `claudecode` | Claude Code | `~/.claude/projects/**/*.jsonl` |
| `zcode` | ZCode CLI | `~/.zcode/cli/db/db.sqlite` 的 `model_usage` 表 |
| `opencode` | opencode | `~/.local/share/opencode/opencode.db`（Windows: `%APPDATA%\opencode`） |
| `deepseek-harness` | DeepSeek Harness | `~/.dsh/sessions/*/session-*/session.v4.jsonl[.zstd]` |
| `qwencode` | QwenCode CLI | `~/.qwen/usage/token-usage-*.jsonl` + `usage_record.jsonl` |
| `workbuddy` | WorkBuddy | `~/.workbuddy/workbuddy.db` 的 `session_usage`（仅上下文占用量） |

## 快速开始

```bash
# 1. 建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 看看各工具的数据目录是否被识别
python tokenmon.py tools

# 3. 采集一次（可反复运行，按 record_key 幂等去重）
python tokenmon.py collect

# 4. 命令行报表
python tokenmon.py report --period day   --span 14
python tokenmon.py report --period week  --span 4
python tokenmon.py report --period month --span 6

# 5. Web 界面
python tokenmon.py serve --port 8770
# 打开 http://127.0.0.1:8770/
```

## 统计口径

| 指标 | 含义 |
|---|---|
| `input_tokens` | 非缓存的输入 token |
| `output_tokens` | 输出 token |
| `reasoning_tokens` | 推理 / thinking token |
| `cache_read_tokens` | **缓存命中**（从缓存读取）的 token |
| `cache_write_tokens` | 写入缓存的 token（cache creation） |
| `total_tokens` | 上游返回的 total；未提供时按 `input+output+reasoning+cache_read+cache_write` 计算 |
| `interactions` | 交互次数 = 去重后的用户提问轮次 |
| `llm_calls` | 模型请求次数 |
| `tool_calls` | 工具调用次数 |

### 余额 / 额度 / 花费

除 token 用量外，还会采集各工具的账户余额、额度窗口和已花费金额：

| 工具 | 类型 | 数据来源 |
|---|---|---|
| `deepseek-harness` | 余额 | `~/.dsh/.dshw-usage.json` 的 credit book（含每日开盘/收盘余额，单位为 1e8 缩放） |
| `codex` | 额度 + 余额 | 会话日志 `token_count` 事件的 `rate_limits`：primary（5 小时窗）、secondary（周窗）已用百分比、重置时间、套餐类型、credits 余额 |
| `claudecode` | 已花费 | 会话日志 `cost-state` 记录：会话累计 USD 成本及按模型拆分 |
| `opencode` | 已花费 | `message` 表中非零的 `cost` 字段 |

> 余额按账户取**最新一次**观测（余额是可快照量，不能跨天累加）；
> 已花费金额在所选周期内**求和**（花费是可累加量）。

Web 界面新增「账户余额与额度」和「已花费金额」两张表，并提供
`GET /api/accounts` 与 `GET /api/export-accounts.csv`。

> **交互次数按轮次去重**：一个轮次可能产生多条用量记录（多次模型调用），
> 统计时按 `(tool, session, turn)` 去重，所以一条记录带 `is_interaction=1`
> 不会把交互数翻倍。

> **Codex 只统计 `token_usage_record`**：`token_count` 事件里的
> `total_token_usage` 是线程累计值，且不带 `turn_id`，无法归属模型，
> 直接使用会重复计数，因此被忽略。

## 跨平台

- 数据目录按平台解析：macOS 用 `~/Library/Application Support/`，
  Windows 用 `%APPDATA%` / `%LOCALAPPDATA%`，Linux 用 `~/.config` 与
  `~/.local/share`。
- 数据库默认位置：macOS `~/Library/Application Support/tokenmon/tokenmon.db`，
  Windows `%APPDATA%\tokenmon\tokenmon.db`，
  Linux `~/.local/share/tokenmon/tokenmon.db`。
- 每个工具都可用环境变量覆盖数据目录：
  `TOKENMON_CODEX_HOME`、`TOKENMON_CLAUDE_HOME`、`TOKENMON_ZCODE_HOME`、
  `TOKENMON_OPENCODE_HOME`、`TOKENMON_DSH_HOME`、
  `TOKENMON_ANTIGRAVITY_HOME`、`TOKENMON_QWENCODE_HOME`、
  `TOKENMON_CURSOR_HOME`；数据库用 `TOKENMON_DB`。
- `--source-dir DIR` 可在采集时指定任意目录（测试、导出的数据包）。

## 部署

### 本机常驻（macOS / Linux）

```bash
cd /Users/ou/project/pc_project/tokenmon
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python tokenmon.py collect          # 先采一次
./.venv/bin/python tokenmon.py serve            # 默认 127.0.0.1:8770
```

如需开机自启，用 launchd（macOS）或 systemd user unit（Linux）托管同一条
`serve` 命令即可，无需额外代码。

### 远端 Linux（systemd user service）

当前生产实例部署在 `zazaji@10.10.10.75:~`，端口 **8770**。

```bash
# 1. 同步代码
rsync -av --exclude .venv --exclude .git --exclude __pycache__ \
  ./ zazaji@10.10.10.75:~/projects/tokenmon/

# 2. 建环境
ssh zazaji@10.10.10.75 \
  "cd ~/projects/tokenmon && python3 -m venv .venv && \
   ./.venv/bin/pip install -r requirements.txt"

# 3. 采一次数据
ssh zazaji@10.10.10.75 "cd ~/projects/tokenmon && ./.venv/bin/python tokenmon.py collect"

# 4. 安装 systemd user service（linger 已开启，注销后仍运行）
ssh zazaji@10.10.10.75 "mkdir -p ~/.config/systemd/user"
```

`~/.config/systemd/user/tokenmon.service`：

```ini
[Unit]
Description=tokenmon AI token usage dashboard
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/projects/tokenmon
ExecStart=%h/projects/tokenmon/.venv/bin/python tokenmon.py serve \
          --host 0.0.0.0 --port 8770
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
ssh zazaji@10.10.10.75 "systemctl --user daemon-reload && \
  systemctl --user enable --now tokenmon.service && \
  systemctl --user status tokenmon.service --no-pager"
```

访问 `http://10.10.10.75:8770/`。

> **端口说明**：8770 是本项目统一端口。远端 8765 已被 AgentNode 占用，
> 因此部署时显式指定 8770；本机默认值也已改为 8770。

## Web API

| 端点 | 说明 |
|---|---|
| `GET /` | 单页仪表盘 |
| `GET /api/summary` | KPI 汇总 + 按工具 |
| `GET /api/series` | 时间桶序列（日/周/月） |
| `GET /api/breakdown` | 按工具 / 模型 / 项目 / 工具+模型 |
| `GET /api/records` | 明细分页 |
| `GET /api/tools` | 已识别数据目录、可用模型与项目 |
| `GET /api/runs` | 最近采集记录 |
| `GET /api/accounts` | 最新余额 / 额度 / 周期内花费 |
| `GET /api/export-accounts.csv` | 导出余额与额度快照 |
| `GET /api/export.csv` | 导出当前周期为 CSV |
| `POST /api/collect` | 后台触发一次采集 |
| `GET /api/collect/status` | 采集状态 |

公共查询参数：`period=day|week|month`、`span=N`（回溯几个周期）、
`tz=IANA时区`、`tools=codex,zcode`、`model=模型名`。

示例：

```bash
curl "http://127.0.0.1:8770/api/summary?period=week&span=4&tz=Asia/Shanghai"
curl "http://127.0.0.1:8770/api/breakdown?period=day&dimension=model"
```

## 项目结构

```
tokenmon/
├── tokenmon.py                  # CLI 入口（collect / report / serve / tools）
├── requirements.txt
├── tokenmon_pkg/
│   ├── paths.py                 # 跨平台数据目录发现
│   ├── model.py                 # 统一 UsageRecord 与口径
│   ├── store.py                 # SQLite 幂等入库与查询
│   ├── analysis.py              # 日/周/月分桶与多维聚合
│   ├── cli.py                   # 供 Web 层复用的采集入口
│   ├── web.py                   # Flask 应用工厂
│   ├── collectors/
│   │   ├── base.py              # 公共解析助手
│   │   ├── codex.py
│   │   ├── claudecode.py
│   │   ├── zcode.py
│   │   ├── opencode.py
│   │   ├── deepseek_harness.py
│   │   └── generic.py           # antigravity / qwencode / cursor 通用扫描
│   ├── templates/index.html
│   └── static/app.css, app.js
└── tests/
    ├── conftest.py              # 合成 fixture 数据
    ├── test_paths.py
    ├── test_parsers.py
    └── test_store.py
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

测试全部使用 `tests/conftest.py` 里的合成数据，不依赖本机真实用户数据。

## 设计说明

- **幂等**：每条记录有稳定 `record_key`（工具+会话+turn+时间+token 数的哈希），
  入库用 `INSERT OR IGNORE`，重复采集不会翻倍。
- **容错**：单个文件/单行解析失败只记 warning 并跳过，不会中断整次采集；
  大文件流式逐行读取。
- **不写源数据**：所有数据库以 `mode=ro&immutable=1` 只读打开，
  绝不修改或上传任何工具的原始数据。

## 已知限制

- `antigravity` / `qwencode` / `cursor` 依赖目录内可识别的 token 字段；
  若某版本改了存储格式，需要扩展 `collectors/generic.py` 的字段别名表
  （`INPUT_KEYS` / `OUTPUT_KEYS` / `CACHE_READ_KEYS` 等）。
- 各工具的「交互次数」定义略有差异：codex 记 `task_started` 轮次，
  claude 记真实用户 prompt，zcode 记 `message` 表 user 行，
  opencode 记 user 消息，harness 记 `user/message`。

