---
name: mcworth
description: 根据用户想吃的餐品、份量和预算，比较购买组合，使用麦当劳 MCP 核价并给出可复核的购买建议。
description_zh: 保留想吃的餐品，计算更省的买法（固定餐盘模式；预算配餐尚未实现）。
description_en: Plan a satisfying meal within budget and compare verified purchase options using McDonald's MCP.
version: 0.2.2
author: McWorth contributors
---

# 麦麦如意餐 McWorth

保住你想吃的，找到更省的买法。

## 适用与不适用

**适用**：用户要按预算选餐、比较同一餐盘的购买组合、调整当前方案。

**不适用**：
- 不需要比较价格的普通点餐——直接点即可，不要用流程拖慢用户。
- 任何未经用户明确授权的下单动作。本版本**不自动下单**。
- 涉及健康建议、营养评分的判断——本产品不使用此类推断。

## 核心原则（违反即终止，不要"尽量"）

1. **预算是支出上限，不是必须花光的目标。**
2. **用户锁定的餐品、规格、排除项不得被暗中放宽。**用户说就要这个堡，不能用"更划算"的 B 替换。
3. **进入推荐的金额必须来自官方整单核价。**本地估算只用于排序候选，**永远不得**展示为价格。
4. **省钱说明必须对应可比基准。**餐盘一旦不一致（换了餐、换了规格、多给了东西），只能说"方案差额"，禁止说"省了多少"。
5. **没有基准就不报节省额。报价不可用就如实说未核价，不要用估算冒充。**

## 执行步骤

### 1. 判断模式

- **固定餐盘**：用户已说清要吃什么（"我想吃 A 堡和中薯"）→ 保持餐盘不变，只换购买组合。
- **预算配餐**：用户只给了预算和大致偏好 → 尚未实现，如实告知并退回固定餐盘模式。
- **方案变更**：用户在已有结果上修改 → 递增 `revision`，旧版本报价不得进入新版本推荐。

#### intent.json 关键字段（写错这些，授权语义就错了）

| 字段 | 取值 | 说明 |
|---|---|---|
| `mode` | `"exact_plate"` | 本版本只支持固定餐盘；`"budget"` 尚未实现 |
| `budget_fen` | 整数分或 `null` | **支出上限**，不是必须花光的目标；`null` 表示不比价上限 |
| `extras_policy` | `"disallow"`（默认）/ `"allow"` | 是否接受"餐盘有增量"的方案。注意取值是 `disallow`，**不是 `deny`** |
| `allowed_extras` | `[{"code": "3050", "max_quantity": 1}]` | 细化授权：只允许这些增量且不超数量。留空且 `extras_policy=allow` 表示全量允许 |
| `substitutions_allowed` | `["薯条"]` | 允许被替换掉的餐品名。**该餐品的 `locked` 必须为 false**，否则视为授权自相矛盾（校验会阻塞） |
| `required[].locked` | `true` / `false` | `true` = 用户锁定此项，任何情况下不得替换 |
| `preference` | `"save_money"`（默认）/ `"strict_plate"` / `"upgrade"` | 合格候选内部的排序依据 |

⚠️ 默认姿态是**保守**：不接受加量、不允许替换。只有用户明确表达（"可以多一杯饮料""换成 XX 也行"）才能放宽，
且放宽后要在结果卡里如实标注这是"方案差额"而非"无代价节省"。

### 2. 追问规则

只追问**影响可行性或执行正确性**的缺失项：

| 缺失 | 是否追问 |
|---|---|
| 门店 / 取餐方式（到店、外送） | 必须确认，影响全部价格 |
| 餐品规格（大薯 or 中薯） | 若影响比价则追问，否则用默认值并说明 |
| 品牌偏好、口味喜好 | 不追问，交给推荐环节 |
| 是否已确认要下单 | 每个订单一次，已有有效授权不重复索取 |

**不要重复询问用户已经回答过的信息。**

### 3. 只读链路（宿主编排协议）

#### 3.0 先决定"抓哪些套餐"——这一步直接决定结论质量

`plan --combos` 的套餐清单由宿主提供。**抓漏了，"无解"和"最低 ¥X"的结论就可能是错的**：
同一诉求（35 元内、双层吉士汉堡+薯条），只抓名字含目标餐品的套餐会得到"无解，最低 ¥37.00"，
把"随心配"类商品也纳入后，真实答案是"¥28.90"（用 1+1 组合把汉堡搭进去）。按此清单抓：

| 优先级 | 抓什么 | 为什么 |
|---|---|---|
| 1 | 用户想吃的单品本身 | 构成"单品分别购买"对照基准，必须有 |
| 2 | 名字含目标餐品名的套餐 / 三件套 / 四件套 | 最常见的"换套餐更省"来源 |
| 3 | **随心配 / 1+1 / 加价购 / 超值组合类商品** | 名字**不含**目标餐品名，最易漏；实测在多个门店都是最省路径 |
| 4 | 目标餐品的同类替代品（可选，需用户授权替换） | 用户允许换餐品时才抓 |

- **不要抓**与目标餐品无关的套餐：既污染结果卡，也浪费核价次数（每次 `calculate-price` 都是一次真实请求）。
- 候选套餐多时按"与目标餐品的相关度 → 菜单标价"排序后截断，并告知用户本轮覆盖了哪些套餐。
- 抓到的每个套餐都要用 `query-meal-detail` 取完整轮次结构（`rounds`），
  **`round.id` 是数字（1、2…），转 payload 时按字符串写入 `round` 字段**——宿主不要自己猜编号。

#### 3.1 三个只读命令

用 `mcworth` 的三个只读命令完成一次完整求解，各命令间通过 JSON 文件交接，**全程不发起网络调用**：

```text
① mcworth plan --intent intent.json --menu menu.json --combos combos.json -o plan.json
   输入：intent.json 为 MealIntent 结构（裸 JSON）；
        menu.json / combos.json 为宿主留存封装 {"_meta": {...}, "response": {...}}
        （_meta 是本工具的溯源封装：trace_id、captured_at、store_code 等，
         不是官方响应的一部分——不要把封装文件描述为"原始官方 JSON"）
   输出：候选购买组合列表 + quote_requests（每条含 request_id / session_id /
        intent_revision / cart_hash 与官方 official_payload）

② 宿主按 quote_requests 调用麦当劳 MCP 的 calculate-price，收集响应。
   responses.json 每条记录的最小结构：
   {
     "request_id":       "qr-0",                        // 抄自 plan 的对应请求
     "session_id":       "...",                         // 抄自 plan.intent.session_id
     "intent_revision":  1,                             // 抄自 plan.intent.revision
     "context_key":      "store=...;order_type=...;be_type=...",  // 抄自 plan.context.context_key
     "cart_hash":        "cart:sha256:...",             // 抄自 plan 的对应请求
     "source_mode":      "live",                        // live | historical_replay | synthetic
     "captured_at":      "2026-10-10T13:00:00+08:00",   // 实际调用时间
     "trace_id":         "官方返回的 traceId",
     "response":         { ...官方 calculate-price 响应原文... }
   }
   身份四元组 + cart_hash 必须由宿主从 plan 原样抄录，不得凭记忆重写。
   ⚠️ context_key **不在** quote_requests 里：它在 plan 顶层 `context.context_key`
   （quote_requests 的每条含 request_id / session_id / intent_revision / cart_hash / official_payload）。
   ⚠️ response 必须是**官方原始 JSON**，不要改写、不要"顺手修正"其中的金额字段——
   ingest-quotes 会校验金额自洽（见下），改写过的金额会被拒绝。

③ mcworth ingest-quotes --plan plan.json --responses responses.json -o quotes.json
   输入：plan.json + responses.json
   校验（任一不过即拒绝该条并计入 errors，宁可少推荐，不错账）：
        · 身份四元组与 cart_hash 必须匹配；
        · 响应商品清单与请求对账；
        · **金额自洽**：productPrice = Σ items.subtotal，
          且 total = productPrice − discount + delivery + packing + tableware
          （缺失组件视为"未知"而非 0，此时只做单侧可证伪的下限/上限检查）。
   输出：quotes.json，顶层为 {schema_version, context_key, records, errors, notes}
        ——通过校验的报价在 `records`（不是 quotes），被拒原因在 `errors`。
        每条记录已绑定 session_id / intent_revision。

④ mcworth report --intent intent.json --menu menu.json [--combos combos.json] --quotes quotes.json
   校验：报价与会话/版本不符 → 不适用；live 报价超过 6 小时新鲜度阈值 →
        不用于主推荐（阈值是本地复查策略，非官方保价承诺）。
   输出：可复核结果卡 + JSON 摘要（含 source_mode 与 rejected_quotes）。
```

**关键规则**：
- 报价以 `cart_hash` 与候选对账，对不上的响应会被拒绝并计入问题数。
- `source_mode` 必须如实标注：`live`（本次实时核价）/ `historical_replay`（官方快照回放）/ `synthetic`（合成演示）。只有 `live` 且通过时效检查的报价可支撑对用户的正式推荐；回放与演示结果会在结果卡上明确标注。
- 任何候选缺少官方核价时保持"未核价"状态，**不得**掉回本地估算充当报价。

### 4. 分类与展示

每条路径按"实际到手餐盘"与目标餐盘严格比对后分类：

| 分类 | 含义 | 默认处理 |
|---|---|---|
| `same_plate` | 食物原子完全一致 | 直接进入推荐 |
| `extras` | 目标都在，另有增量（如套餐强制送的饮料） | **除非用户明确授权，否则不推荐** |
| `substitution` | 换掉了目标餐品 | 除非用户明确允许更换该项 |

最多展示**三个**有意义的选择（v0.2.2 起由工具自动收敛：按"同餐盘 → 增量 → 替换"分组，
组内合并实质重复项、按价格升序取前若干，主推荐必在展示之列；被省略的数量会如实写在卡片上）。
每个都要说清：保留什么、变了什么、官方核价多少、核价时间与 traceId。

### 5. 措辞规则

对照基准的来源必须如实交代（`baseline_origin`）：

- 用户提供了原购物车 → 才可以说"与您的原计划相比"。
- 没有原购物车、由系统构造的全单品直购 → 只能说"**与单品分别购买相比**"。
- 两者都没有 → 不展示任何比较，只报各方案核价。

| 情形 | 正确说法 | 错误说法 |
|---|---|---|
| 餐盘完全一致，金额更低 | "比单品分别购买低 ¥X（相同餐盘）" | — |
| 餐盘有变化 | "低 ¥X（方案差额，不是无代价节省）" | "为你省了 ¥X" |
| 未核价 | "该方案尚未通过官方核验" | 给出任何金额 |
| 搜索完毕无解 | "在本轮核价范围内没有同时满足这些要求的方案" | "绝对买不到" |

### 6. 无解时的正确表达

搜索完毕仍无解时，给出可操作的下一步，而不是干巴巴报错：

> "保留当前餐盘的最低核价为 ¥36.00。你可以把预算调整到 ¥36.00，或允许更换餐品后重新查看。"

不得把有限的搜索范围升级为全局最优性声明。

## 边界

- **不自动下单**：`create-order` 等任何下单操作涉及真实资金与账户权益，本版本保持只读。
- **不做**：`auto-bind-coupons` —— 它会领取符合条件的全部优惠，不是精确领券。
- **不用健康评分**：不使用未经验证的健康总分，不把热量高解释为更管饱。
- **不冒充用户意图**：没有用户明说，不得假设"原计划"；系统构造的基准必须注明"单品分别购买"。

## 参考文件

按需读取，不要一次性全读：

- `references/tool-contracts.md` — 官方工具的输入输出契约与实测结论
- `references/pricing-rules.md` — 四种金额口径的记账规则

## 来源声明

价格、套餐与优惠规则来自麦当劳中国官方 MCP Server。本技能是第三方非商业作品，与麦当劳公司无从属或背书关系。
