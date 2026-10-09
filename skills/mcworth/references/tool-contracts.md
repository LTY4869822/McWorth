# 工具契约摘要

按需读取。完整记录见仓库 `docs/tool-schemas.md`。

标注：✅ 已实测 ｜ ⚠️ 未验证，实现中不得依赖。

## 调用顺序（只读链路）

```text
query-meals({ storeCode, beType, orderType })
  → data.meals: { code: { name, currentPrice, originalPrice, discountType?, withOrder? } }
  → 价格是字符串（元），需转整数分

query-meal-detail({ code, storeCode, beType, orderType })
  → data.rounds: [{ id, name, minQuantity, maxQuantity, choices[{ code, diffPrice, isDefault }] }]
  → diffPrice 形如 "+ ¥1.5" / "- ¥0.5"，必须保留符号
  → minQuantity>0 表示该轮强制选择

calculate-price({ storeCode, beType, orderType, items[] })
  → data.price 为实付总额（整数分）；缺失即视为无效报价
  → items[].roundList[].round 是字符串（"1"），与上方的整数 id 类型不同
```

（上述三个工具均已实测 ✅）

## 三条会改变结论的实测约束

1. **`minQuantity` 强制选项**：套餐的某些轮次必须选（如三件套的饮料轮）。走套餐路径必然可能多出用户没要的东西。
2. **`diffPrice` 可为负**：套餐内有些选项比默认项便宜。所以"最低价填充"与"默认填充"是两个不同目标。
3. **`withOrder` 前置条件**：部分超低价需搭配随单购卡。菜单标价不等于实际可得价格，故估算不可替代核价。

## 购物车去重

同一购买路径用 `cart_hash` 标识（由 `product_code` + `rounds` + `modifications` 的 canonical 结构哈希而来，顺序无关）。报价以 `cart_hash` 对账，防止不同买法的报价串台。

⚠️ 未验证 / 不使用

- 优惠券相关工具调用顺序未验证 → 不参与计价
- `auto-bind-coupons` 会领取全部符合条件的优惠 → 排除
- `create-order` → 本版本不做
- `mall-create-order`（积分消费）、`query-meal-assistance`（企业团餐）、`list-nutrition-foods` → 不使用
