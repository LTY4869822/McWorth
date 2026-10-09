# 麦当劳 MCP 工具 Schema 记录

本文件记录 McWorth 实际依赖的官方工具的输入输出结构，作为契约测试的参照。

**标注约定**：
- ✅ **已实测** —— 有真实调用留存，`tests/fixtures/` 下有脱敏样例。
- ⚠️ **仅见于工具描述** —— 尚未通过真实调用验证语义，实现中不得依赖。

**重要**：本文件不定义官方参数。官方协议以 [麦当劳 MCP Server 文档](https://github.com/M-China/mcd-mcp-server) 为准。

---

## `query-meals` ✅ 已实测

用途：读取指定门店与履约上下文下的菜单。

**输入**

| 参数 | 类型 | 说明 |
|---|---|---|
| `storeCode` | string | 门店编码 |
| `beType` | int | 业务类型（`1` = 到店自取场景） |
| `orderType` | int | 订单类型 |

**输出关键路径**

```text
success: bool
data.meals: { code: { name, currentPrice, originalPrice, discountType?, withOrder? } }
data.categories: [{ name, meals: [{ code, ... }] }]
```

**实测要点**

- `currentPrice` / `originalPrice` 是**字符串**（如 `"22.5"`），单位是元，需转为整数分。直接用浮点会有精度损失。
- `discountType` 存在时意味着当前价依赖某种优惠资格，**不是无条件可得的价格**。
- `withOrder`（部分工具返回为 `canWithOrder` 布尔或 `withOrder: { membershipCode, specId }` 结构）表示该价格**必须搭配随单购卡**才能享受。这是"购买组合 ≠ 菜单标价"的直接证据。
- 单次返回体量较大（实测 14 个分类、百余餐品），入库的 fixture 只做子集投影。

---

## `query-meal-detail` ✅ 已实测

用途：读取套餐的多轮选配结构。

**输入**

| 参数 | 类型 | 说明 |
|---|---|---|
| `code` | string | 商品编码（套餐） |
| `storeCode` | string | 门店编码 |
| `beType` / `orderType` | int | 同上 |

**输出关键路径**

```text
success: bool
data.code / data.name
data.rounds: [{
  id: int,
  name: string,
  minQuantity: int,
  maxQuantity: int,
  choices: [{ code, name, quantity, maxQuantity, isDefault, diffPrice, supportModify }]
}]
```

**实测要点（决定实现的关键三条）**

1. **`diffPrice` 是带符号字符串**：`"+ ¥1.5"` / `"- ¥0.5"` / `"+ ¥0"`。必须按符号解析为整数分，不能用 `abs` 或忽略负号。
2. **`minQuantity` 会强制选择**。实测三件套第 3 轮（饮料）`minQuantity=1` —— 走套餐路径必然多拿一份目标之外的食物。这条直接催生了"增量（extras）"这一独立分类。
3. **存在负差价选项**。实测第 2 轮「选择套餐内小食」6 选 1，其中「小杯玉米杯」`diffPrice = "- ¥0.5"`，**比默认的中薯条还便宜**。因此"最低价填充"与"默认填充"是两个不同的求解目标。

**实测样本**（门店 1450713，2026-10-09）

| 轮次 | 名称 | 选项数 | 备注 |
|---:|---|---:|---|
| 1 | 麦辣鸡腿汉堡 | 1 | 固定，主餐 |
| 2 | 选择套餐内小食 | 6 | 含 −¥0.5 的负差价项 |
| 3 | 选择套餐内饮料 | 26 | `minQuantity=1`，强制选择 |

⚠️ `supportModify` 与 modification 深层结构（特调项）尚未纳入当前解析器。

---

## `calculate-price` ✅ 已实测

用途：官方整单核价。本产品唯一的价格来源。

**输入**

| 参数 | 类型 | 说明 |
|---|---|---|
| `storeCode` | string | 门店编码 |
| `beType` / `orderType` | int | 履约上下文 |
| `items[]` | array | 购物车行 |

`items[]` 元素结构：

```jsonc
{
  "productCode": "9900005456",
  "quantity": 1,
  "roundList": [                       // 仅套餐需要
    {
      "round": "1",                    // 注意是字符串
      "comboItemList": [{ "code": "1440", "quantity": 1 }]
    }
  ]
}
```

**输出关键路径**（金额单位均为**整数分**）

```text
success: bool
data.price            → 实付总额（必须存在，否则视为无效报价）
data.originalPrice    → 原价总额
data.productPrice     → 商品金额
data.discount         → 优惠金额
data.deliveryPrice / packingPrice / tablewarePrice
data.productList[]    → { productCode, productName, quantity, originalSubtotal, subtotal }
data.takeWayList[]    → 取餐方式
```

**实测要点**

- **`roundList` 是严格同餐盘优化的基石**：可以精确到"第几轮选哪个商品"，否则无法表达"套餐里具体选了什么"，也就无法判断两条路径是否为同一餐盘。
- `round` 字段是**字符串**（`"1"`），与 `query-meal-detail` 返回的整数 `id` 类型不同，需要显式转换。
- 输出缺失 `price` 时必须判定为无效报价，不得用其他字段推算。

**实测对照**（门店 1450713，到店自取）

| 购物车 | 官方核价 |
|---|---:|
| 麦辣鸡腿汉堡×1 + 中薯条×1（单品） | ¥36.00 |
| 麦辣鸡腿汉堡三件套×1（堡 + 中薯 + 可乐中杯） | ¥33.50 |

---

## `available-coupons` / `query-store-coupons` / `query-my-coupons` ⚠️ 未实现

工具存在且可读取 schema，但调用顺序与券的使用范围尚未通过真实调用验证。**当前版本不参与计价。**

⚠️ `auto-bind-coupons` 会领取符合条件的**全部**优惠，不可当作只读查询或精确领单张券使用。明确排除。

---

## `create-order` ⚠️ 明确不做

涉及真实资金与账户权益。在券有效期、库存一致性、重复提交、退款路径验证完成前，本项目保持只读。

---

## 其他工具

| 工具 | 状态 | 备注 |
|---|---|---|
| `now-time-info` | ✅ 已验证可用 | 返回服务器时间与 traceId，可用于核价时间基准 |
| `query-nearby-stores` | ✅ 已验证可用 | 返回门店编码、营业时间、预约时段 |
| `mall-create-order` | ⚠️ 不使用 | 涉及积分消费 |
| `query-meal-assistance` | ⚠️ 不使用 | 面向企业团餐，普通拼餐不因此获得团餐能力 |
| `list-nutrition-foods` | ⚠️ 不使用 | 本产品不做营养评分 |
