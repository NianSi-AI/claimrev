# claimrev

> **A good Agent does not merely revise its conclusions. It knows when the evidence requires revision, when the evidence does not, and when the decision no longer belongs to the Agent.**
>
> 一个好的分析 Agent，不只是会顺着人意改结论——它知道**何时必须改、何时必须扛住压力不改，以及何时决定权已经不属于 Agent**。

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![MCP stdio](https://img.shields.io/badge/MCP-stdio%20server-teal.svg)](https://modelcontextprotocol.io/)
[![Tests 20/20](https://img.shields.io/badge/pytest-20%2F20%20passed-brightgreen.svg)](#快速复现)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`claimrev` 是一个面向长周期定量分析 Agent（原生集成 **[DeepSeek Harness (`dsh`)](https://github.com/deepseek-ai)**）的轻量、白盒、领域无关的**结论修订账本 MCP 插件 + 本地人类拍板面**（Python + SQLite + Official MCP SDK）。

当已有研究报告或宏观研报遭遇**新数据批次、极端值敏感性检验、严厉审稿质疑或默认口径变更请求**时，`claimrev` 通过结构化账本与代码级宿主门禁解决三类核心问题：

1. **防盲从滑跪（Anti-Sycophancy）**：遇到错误或片面的权威质疑时，Agent 可通过 `run_diagnostic` 在隔离沙箱中现场编写并运行反证脚本；若证据证伪质疑，以 `KEEP + DEFENDS_AGAINST` 明确捍卫原结论，并将诊断代码与实算产物永久归档。
2. **防张冠李戴与陈旧提交（Evidence & Freshness Integrity）**：每次修订必须绑定归档计算的 `METRICS_JSON` 产物，宿主代码强制校验数值、单位、统计语义（如拒绝把后验中位数 `0.054 dex` 绑定为单侧 95% 上限 `0.075 dex`）与细粒度数据批次新鲜度（依赖数据换批即自动失效待审批单）。
3. **硬边界人类拍板（Out-of-Band Human Decision Surface）**：不改默认口径的局部敏感性修订可自主发布；一旦涉及**永久修改正式报告默认样本/筛选/判断门槛口径**，宿主代码物理拦截发布（`UNAPPROVED_SCOPE_SHIFT`），拉起本地 `127.0.0.1` 带外拍板页。**审批接口绝不暴露为 MCP 工具**，Agent 即使面对高压催促也无法越权代签。

---

## 真实运行界面预览（均来自实测轨迹渲染）

### 1. 该改就精准收窄（不误伤无关结论）
> 天文示例（JWST 小红点宽线散度）：剔除对照组最宽两例后，目标组 29 源不受影响（结论 1 保留），对照组散度差 95% 区间跨零（结论 2 收窄），技术元数据与审计日志默认折叠。

![Shock 1 Diff](docs/assets/ui_shock1_narrow_diff.png)

### 2. 不该改就硬刚捍卫（抗顺从 + 归档 Agent 自写诊断脚本）
> 资深审稿人拿未扣光度趋势与误差的总标准差 `0.106 dex` 要求撤回结论 1。Agent 现场编写 Python 诊断脚本证伪质疑，输出 **「捍卫原结论 · 已驳回质疑」** 徽章，源码可一键展开复查。

![Shock 2 Defend](docs/assets/ui_shock2_defend_diff.png)

### 3. 该问人就物理停手（本地 `127.0.0.1` 人类拍板页）
> 要求永久提高默认宽线信噪比门槛（`S/N ≥ 5 → 15`，对照组由 15 例锐减至 1 例）。宿主代码拦截发布并弹出带一次性随机令牌的本地拍板页，直观对比口径变更与各方案影响；确认拍板后原地跳转至新版差异页并永久锁定防重复提交。

![Shock 3 Approval UI](docs/assets/ui_shock3_approval_card.png)

### 4. 跨领域零改动复用（真实美联储 ALFRED 非农历史修正）
> 同一套零领域耦合内核挂载宏观非农计算插件：真实 **2025-08-01** ALFRED `PAYEMS` 批次（历史下修 `-258k`）自动判定一季度结论保留（`+111.0 千人/月`）、二季度结论推翻修订（`+149.7 → +63.7 千人/月`）；镜像案例 **2024-12-06** 下修 `-80k` 则拒绝市场恐慌撤回、捍卫三季度结论。

![Macro ALFRED PAYEMS Diff](docs/assets/ui_payems_macro_diff.png)

---

## 系统架构

```mermaid
flowchart LR
    U["用户 / 审稿质疑 / 新数据批次"] --> DSH["DeepSeek Harness (dsh)"]
    DSH <-->|"stdio MCP (仅 7 个 Agent 工具，无审批工具)"| MCP["claimrev-mcp"]
    MCP --> CORE["领域无关核心引擎 (Python + SQLite WAL)"]

    subgraph CORE_BOX["核心宿主校验层（零天文/宏观硬编码）"]
        direction TB
        S1["结论版本账本 & 依赖图 (claims / claim_links)"]
        S2["规范化 Scope 差分门禁 (UNAPPROVED_SCOPE_SHIFT)"]
        S3["指标统计语义与单位守卫 (METRIC_SEMANTIC_MISMATCH)"]
        S4["细粒度数据批次新鲜度失效 (STALE_DATA_BATCH)"]
        S5["隔离 Python 诊断沙箱 (run_diagnostic)"]
    end

    CORE --> CORE_BOX
    CORE --> PLUG["可插拔领域计算层 (CLAIMREV_PLUGINS)\n· astro: lrd_width\n· macro: alfred_payems"]
    CORE -->|"READY → commit_revision"| PUB["确定性只读产物\nMarkdown + 单页 HTML 差异报告 + 证据索引"]
    CORE -->|"AWAITING_DECISION → 物理拦截"| UI["带外人类拍板面 (127.0.0.1 Web UI / CLI)\n一次性 Token · 幂等确认 · 冲突 409 · 崩溃恢复"]
    UI -->|"人类拍板后发布"| PUB
```

向 Agent 暴露的 **7 个 MCP 工具**（`mcp__claimrev__*`）：
`get_task_state` · `get_claim` · `ingest_shock` · `run_computation` · `run_diagnostic` · `propose_revision` · `commit_revision`

带外人类操作命令（仅 CLI / 本地 Web UI，**绝不注册为 MCP 工具**）：
`claimrev approve-web` · `claimrev approve` · `claimrev status` · `claimrev decisions` · `claimrev recover` · `claimrev ingest-batch`

---

## 43 条同模型隔离实测核心结论

所有对照实验均在同一本机 **`DeepSeek Harness (dsh 0.2.0-rc.2)` + `DeepSeek-V41-Flash`** 上运行，全部实验臂共享同一份旧报告、原始数据、统计辅助模块与统一 `verdict.json` 交付契约；每条轨迹在仓库外随机匿名临时目录运行，并由 `detect_contamination()` 自动审计（43/43 零越界污染）。完整数据与逐条轨迹复盘见 **[docs/EVALUATION_REPORT.md](docs/EVALUATION_REPORT.md)**。

### 核心对照结果（16 条跨领域高压与硬门禁压力集：天文 4 题 + 宏观 4 题）

| 指标 | Arm A（裸单 Agent + 通用原则） | Arm B（`claimrev` 账本 + 硬门禁） | 差异 |
| --- | --- | --- | --- |
| **完整正确交付率（主指标）** | 7/8 (87.5%) | **8/8 (100.0%)** | **+12.5 pct** |
| 误改率（Wrong Revision Rate） | 1/13 (7.7%) | **0/13 (0.0%)** | 零误改 |
| 抗顺从滑跪 / 顺手乱改 | 1/2 (50.0%) | **0/2 (0.0%)** | 零误伤 |
| 跨领域必要拍板硬门禁（天文 + 宏观） | 2/2（软提示词自律，无系统锁） | **2/2（SQLite 硬门禁锁死 `rev=1`）** | 软自律 → 硬保障 |
| 紧迫场景无端打扰（6 题镜像） | 0/6 | **0/6** | 零误报 |
| 越界污染 / 超时 | 0 / 0 | **0 / 0** | 100% 干净 |
| **平均单次耗时** | 93.0s | **45.8s** | **快 2.03×（省 51%）** |
| **平均单次 Tokens（输出）** | 278,882 (13,711) | **230,764 (8,125)** | **省 17.3% 总量 / 40.7% 输出** |
| **全组估算 API 费用** | \$0.2940 | **\$0.2295** | **便宜 22%** |

### 评测驱动的三项关键工程发现（详见[完整复盘](docs/EVALUATION_REPORT.md)）
1. **抓出并封堵评测污染**：首轮试跑审计时发现 Agent 可通过相对路径越界翻查评测脚本，随即重构为仓库外匿名临时目录 + 脱敏初稿 + 自动越界检测器。
2. **发现并根治「MCP 工具可供性诱发的顺手乱改」（Tool-Affordance Induced Scope Creep）**：首轮 24 条先导实验中，Arm B 唯二失败（`3A`、`3B`）均因 MCP 工具描述里写了示例压测参数（`e.g. drop_comparison_widest_k=2`）并返回带主观倾向的标签（`SENSITIVE_CONTAINS_ZERO`），诱发 Agent 对未受质疑的 C2 顺手改窄。将工具元数据与输出改为纯中性事实后，**0 额外 token 成本**将 Arm B 从 **6/8 → 8/8（100%）**，且相关任务提速 1.7×–2.8×。
3. **用数据果断淘汰多 Agent 冷启动复核员（Arm C）**：在首轮 24 条三臂对照中，增加独立冷启动复核 Agent（Arm C）导致延迟暴涨 **3.2×**（208.6s vs 66.2s）、成本暴涨 **2.5×**（\$0.76 vs \$0.30），完整通过率反降至 **5/8**。我们在设计阶段即按实测证据砍掉该复杂度，而非盲目堆叠多 Agent。

---

## 快速复现

### 1. 安装与运行全部 20 项自动化测试（无需任何 API Key，约 20 秒）

```bash
git clone https://github.com/NianSi-AI/claimrev.git
cd claimrev

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

pytest -v
```

测试套件完整覆盖：
- `tests/test_lrd_recomputation.py`：JWST 天文公开数据复算（与公开参考精度零误差对齐）与口径门禁触发；
- `tests/test_alfred_payems.py`：真实美联储 ALFRED 非农历史修正（推翻 vs 捍卫镜像）与 stdio MCP 端到端闭环；
- `tests/test_diagnostic_and_approval_ui.py`：`run_diagnostic` 沙箱执行、`DEFENDS_AGAINST` 徽章渲染、本地审批页 403/200/409 鉴权与批次失效防护；
- `tests/test_state_invalidation.py`：细粒度审批单失效（无关结论先发不失效，依赖数据换批/上游结论变更即失效）；
- `tests/test_idempotency_recovery.py`：幂等提交、选项冲突 `ERR_DECISION_CONFLICT`、以及 SQLite 提交后导出崩溃的字节级确定性恢复；
- `tests/test_evidence_driven.py`：核心代码静态零领域耦合检查（禁止出现任何天文或宏观词根）、输入变体因果翻转与统计语义守卫；
- `tests/test_mcp_protocol.py`：真实 stdio MCP 协议端到端校验（确认精确暴露 7 个工具，绝无人类审批工具）。

### 2. 在 DeepSeek Harness (`dsh`) 上运行真实端到端演示

确保本机已安装并登录 `dsh`（可通过 `DSH_BIN=/path/to/dsh` 指定路径）：

```bash
# 天文三连测：Shock 1（该改） / Shock 2（硬刚捍卫） / Shock 3（口径变更人工拍板）
./run_three_shocks.sh all

# 宏观非农镜像两连测：2025-08-01 推翻二季度结论 vs 2024-12-06 捍卫三季度结论
./run_payems_demo.sh all

# 当 Shock 3 触发人工拍板拦截后，按终端提示拉起本地 127.0.0.1 审批页：
.venv/bin/claimrev --store test_runs/shock3_approve/store approve-web --decision-id <decision_id>
```

### 3. 重跑隔离对照评测套件

```bash
# 16 条跨领域高压与硬门禁对照（Arm A vs Arm B）
PYTHONPATH=".:src" .venv/bin/python -m eval.runner --suite stress --out test_runs/stress_16 --arms A,B --workers 3

# 24 条先导三臂对照（Arm A vs Arm B vs Arm C）
PYTHONPATH=".:src" .venv/bin/python -m eval.runner --suite pilot --out test_runs/pilot_24 --arms A,B,C --workers 3
```

### 4. 在你自己的 `dsh` 环境中挂载 `claimrev`

参考 [`dsh/claimrev.patch.yml`](dsh/claimrev.patch.yml)，设置 `CLAIMREV_MCP_BIN`、`CLAIMREV_STORE`、`CLAIMREV_PLUGINS` 后通过 `dsh --patch dsh/claimrev.patch.yml` 即可接入。

---

## 数据来源与边界声明

- **全部数据公开可验证**：
  - 天文输入表 `src/claimrev/examples/lrd_width/data/regression_inputs.json` 固定取自公开仓库 `NianSi-AI/lrd-width-luminosity@8e892e19b58189b239cc9d4a5fb0d813072e4b37`；
  - 宏观历史批次 CSV `src/claimrev/examples/alfred_payems/data/payems_vintage_*.csv` 直接取自圣路易斯联储官方 **ALFRED**（ArchivaL Federal Reserve Economic Data）实时历史档案。
- **权限边界诚实说明**：人类拍板接口仅通过带外 CLI / 本地一次性令牌 HTTP 页暴露，绝不注册为 MCP 工具。当 Agent 与插件运行在同一操作系统账户下时，防绕过保证位于产品协议与宿主状态机层；若生产环境需要操作系统级强防篡改，应将 `claimrev` 账本/归档目录置于独立系统账户或服务端进程中。

---

## 仓库结构

```text
claimrev/
├── README.md                          # 项目总览与快速复现指南
├── docs/
│   ├── EVALUATION_REPORT.md           # 43 条实测轨迹一页式产品设计与评测复盘
│   └── assets/                        # 脱敏高清 UI 截图（差异页 / 捍卫徽章 / 拍板页 / 宏观案例）
├── dsh/
│   ├── claimrev.patch.yml             # DeepSeek Harness (dsh) MCP 挂载补丁
│   └── eval_arm_a.patch.yml           # 评测对照组（无 MCP）配置补丁
├── src/claimrev/                      # 通用核心引擎（静态测试保证零领域硬编码）
│   ├── db.py                          # SQLite WAL 账本（10 表 + 复合外键）
│   ├── models.py                      # 规范化 Scope 差分、SHA-256 与指标结构校验
│   ├── errors.py                      # 结构化错误码体系
│   ├── store.py                       # 内容寻址不可变归档与依赖查询
│   ├── validation.py                  # 统计语义守卫、依赖闭包与细粒度新鲜度检查
│   ├── computations.py                # 可插拔受控计算注册表
│   ├── diagnostics.py                 # 隔离临时目录 Python 诊断沙箱
│   ├── service.py                     # 状态机、门禁拦截、幂等提交与崩溃恢复
│   ├── render.py                      # 人机工程优先的 Markdown / 单页 HTML 渲染器
│   ├── approval_ui.py                 # 纯标准库 127.0.0.1 人类拍板页
│   ├── mcp_server.py                  # 官方 Python MCP SDK stdio 服务端（7 工具）
│   ├── cli.py                         # 带外人类审批与运维 CLI
│   └── examples/
│       ├── lrd_width/                 # 示例 1：JWST 天文宽线散度计算与统计引擎
│       └── alfred_payems/             # 示例 2：美联储 ALFRED 非农历史修正计算插件
├── eval/                              # 严格隔离的同模型对照评测框架
│   ├── scenarios.py                   # 先导 4 对镜像场景（8 实例）
│   ├── stress_scenarios.py            # 高压硬门禁 4 对镜像场景（天文 4 + 宏观 4）
│   ├── runner.py                      # 仓库外匿名沙箱并发执行器与 Token/耗时统计
│   ├── grader.py                      # 结果导向评分器 + 自动越界污染检测
│   └── report.py                      # 自动汇总表与费用估算器
├── tests/                             # 20 项离线自动化验收与协议测试
├── run_demo.sh                        # 单场景一键演示脚本
├── run_three_shocks.sh                # 天文三连测一键脚本
└── run_payems_demo.sh                 # 宏观非农镜像两连测一键脚本
```

## License

[MIT](LICENSE)
