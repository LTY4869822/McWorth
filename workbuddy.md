# WorkBuddy 开发上下文

本文件记录「麦麦如意餐 McWorth」使用 WorkBuddy 开发的对话上下文，用于核验是否符合
[麦当劳开发者创新挑战赛](https://github.com/M-China/mcd-developer-innovation-challenge)
WorkBuddy 专项奖励条件。文件名与用途以官方活动repo要求为准。

## 项目概况

| 项 | 值 |
|---|---|
| 项目名 | 麦麦如意餐 McWorth |
| 定位 | 保住你想吃的，找到更省的买法 |
| 开发环境 | WorkBuddy + 麦当劳中国 MCP 连接器 |
| 起始日期 | 2026-10-09 |

## WorkBuddy 在本项目中的作用

1. **赛事策略分析**：读取官方活动仓库规则与排行榜，确认评分口径为 GitHub Star 数，据此确定「传播性优先」的产品方向。
2. **真实接口验证**：通过已配置的 `mcd-mcp` 连接器直接调用麦当劳官方 MCP，完成只读链路实测（`now-time-info`、`query-nearby-stores`、`query-meals`、`query-meal-detail`、`calculate-price`）。
3. **代码实现**：在其工作区内完成域模型、适配器、求解器、结果卡渲染与 CLI，并搭建测试。
4. **交付文件生成**：README、MCP_INTEGRATION.md、SKILL.md、契约测试样例、可行性报告等。

## 关键会话节点（摘要）

| 阶段 | 内容 | 产出 |
|---|---|---|
| 赛事分析 | 解读活动规则与排行榜，输出作战计划 | `M-CODE-作战计划.md` |
| MCP 连通性验证 | 实测 Token 鉴权与只读工具 | 确认连接器可用，30+ 工具可达 |
| Schema 枚举 | 读取核价与套餐详情的真实输入输出结构 | 发现 `roundList.comboItemList` 支撑严格同餐盘优化 |
| 只读闭环 | 菜单 → 套餐结构 → 官方核价全链路打通 | 发现「套餐比单买便宜 ¥2.50 但多一杯饮料」真实案例 |
| 域模型实现 | 整数分金额、三类摘要哈希分离、五条不变量 | `src/mcworth/` |
| 契约与不变量测试 | 22 项测试全部通过 | `tests/contract/`、`tests/solver/` |
| 交付文件 | P0 五件套 + Skill 包 + 可行性报告 | 见下方清单 |

## 由 WorkBuddy 生成的主要文件

```text
README.md                          项目介绍、安装、使用示例、目标用户
CONTEST_DECLARATION.md             参赛声明（使用官方原文，内容未改动）
MCP_INTEGRATION.md                 MCP 集成说明
mcp-config.example.json            配置模板（仅含环境变量占位符）
src/mcworth/                       核心引擎源码
skills/mcworth/SKILL.md            WorkBuddy Skill 包
docs/tool-schemas.md               官方工具输入输出记录
docs/w1-feasibility.md             W1 可行性报告
tests/fixtures/                    脱敏的官方实测响应快照
tests/contract/ tests/solver/      契约与不变量测试
```

## 开发过程中的技术要点（WorkBuddy 侧环境）

- **专用虚拟环境**：`~/.workbuddy/binaries/python/envs/mcworth`（Python 3.13 + pydantic 2.14 + pytest）
- **镜像源注意**：本机 pip 访问清华源返回 403，需改用腾讯云镜像 `https://mirrors.cloud.tencent.com/pypi/simple`
- **Windows 路径含空格**：运行时的 `PYTHONPATH` 必须使用 `C:/...` 风格绝对路径
- **编码**：涉及中文输出的命令需设置 `PYTHONIOENCODING=utf-8`

## 凭证处理说明

本项目**不包含任何真实凭证**。真实 Token 存放于 WorkBuddy 的 MCP 连接器配置中
（`~/.workbuddy/mcp.json`），未纳入版本控制；提交的配置模板 `mcp-config.example.json`
仅含 `${MCD_MCP_URL}` 与 `${MCD_MCP_TOKEN}` 占位符。
