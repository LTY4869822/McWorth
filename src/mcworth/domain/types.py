"""核心域类型（对应开发方案第 9 节数据模型，2026-10-10 优化方案 A2/A3/A4 修订）。

所有金额字段以整数分存储；时间为带时区 ISO 8601 字符串。

三类摘要严格分离（方案 9.3），且都以规范化结构为单一真源：
- plate_hash：食物组成/规格/数量（聚合后）—— 判断是否相同餐盘。
- cart_hash：购买路径/套餐选择（轮次与子项按标识排序）—— 报价去重。
- decision_hash：上下文+需求版本+购物车+报价 —— 执行一致性。
哈希仅是本地一致性标识，不是官方签名或防篡改认证。
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 证据


class EvidenceSource(str, Enum):
    OFFICIAL = "official"      # 麦当劳 MCP 实时返回
    SNAPSHOT = "snapshot"     # 官方返回的实测快照（历史留存）
    USER = "user"             # 用户输入
    DERIVED = "derived"       # 本地推导（需标注推导方式）
    SYNTHETIC = "synthetic"   # 合成演示数据，非官方


class EvidenceRef(BaseModel):
    """一条数据的来源引用。金额/餐盘/授权等关键数据必须携带。"""

    source: EvidenceSource
    tool: Optional[str] = None          # 官方工具名
    captured_at: Optional[str] = None    # ISO 8601
    trace_id: Optional[str] = None       # 官方 traceId
    note: Optional[str] = None

    def label(self) -> str:
        if self.source == EvidenceSource.OFFICIAL:
            return f"官方实时({self.tool or '?'} · {self.captured_at or '?'})"
        if self.source == EvidenceSource.SNAPSHOT:
            return f"官方实测快照({self.captured_at or '?'} · trace {self.trace_id or '?'})"
        if self.source == EvidenceSource.SYNTHETIC:
            return "合成数据（演示用，非官方）"
        return f"{self.source.value}" + (f" · {self.note}" if self.note else "")


# ---------------------------------------------------------------- 餐盘


class ModificationValue(BaseModel):
    """一个特调项在购物车中的取值（calculate-price 的 modification.values[]）。"""

    code: str
    key: str          # selectedKey（选中）或 unselectedKey（未选中）
    quantity: int = 1


class FoodAtom(BaseModel):
    """最小食物单元：门店作用域商品编码 + 规格/定制 + 数量。

    名称不是唯一标识（同名不同规格/定制是不同原子）。
    """

    code: str
    name: str
    quantity: int = Field(ge=1)
    spec: tuple[str, ...] = ()   # 规格签名：定制项 "code:key" 的有序去重列表

    def canonical(self) -> dict[str, Any]:
        return {"code": self.code, "spec": list(self.spec), "quantity": self.quantity}


def normalize_plate(atoms: Iterable[FoodAtom]) -> list[FoodAtom]:
    """餐盘规范化（优化方案 A2）：按（商品标识 + 规格）聚合，累计数量，确定性排序。

    供分类、摘要、差额比较与展示共同使用：
    - A×1 + A×1 与 A×2 规范化后完全一致；
    - 同名不同规格 / 不同定制是不同键，不能合并；
    - 输出按 (code, spec) 排序，保证摘要稳定。
    """
    agg: dict[tuple[str, tuple[str, ...]], FoodAtom] = {}
    for a in atoms:
        key = (a.code, tuple(a.spec))
        if key in agg:
            agg[key].quantity += a.quantity
        else:
            agg[key] = FoodAtom(
                code=a.code, name=a.name, quantity=a.quantity, spec=tuple(a.spec)
            )
    return [agg[k] for k in sorted(agg, key=lambda t: (t[0], t[1]))]


# ---------------------------------------------------------------- 购买


class OfferRoundItem(BaseModel):
    """套餐某一轮中选中的商品。"""

    code: str
    quantity: int = Field(ge=1)
    modifications: list[ModificationValue] = Field(default_factory=list)


class OfferRound(BaseModel):
    round_id: str
    items: list[OfferRoundItem]


class OfferItem(BaseModel):
    """购物车行：单品或套餐（含轮次选择）。"""

    product_code: str
    quantity: int = Field(ge=1)
    rounds: list[OfferRound] = Field(default_factory=list)
    modifications: list[ModificationValue] = Field(default_factory=list)

    def canonical(self) -> dict[str, Any]:
        """规范化输出：轮次按 round_id、子项按 code 排序。

        轮次 id 与商品标识都是语义键，排序不改变请求含义；
        官方协议中 round 是字符串、query-meal-detail 的 id 是整数，此处统一字符串化。
        """
        return {
            "product_code": self.product_code,
            "quantity": self.quantity,
            "rounds": [
                {
                    "round": r.round_id,
                    "items": [
                        {
                            "code": i.code,
                            "quantity": i.quantity,
                            "modifications": [
                                {"code": m.code, "key": m.key, "quantity": m.quantity}
                                for m in sorted(i.modifications, key=lambda m: (m.code, m.key))
                            ],
                        }
                        for i in sorted(r.items, key=lambda i: (i.code, i.quantity))
                    ],
                }
                for r in sorted(self.rounds, key=lambda r: r.round_id)
            ],
            "modifications": [
                {"code": m.code, "key": m.key, "quantity": m.quantity}
                for m in sorted(self.modifications, key=lambda m: (m.code, m.key))
            ],
        }

    @classmethod
    def from_canonical(cls, data: dict[str, Any]) -> "OfferItem":
        """从 canonical 结构还原 OfferItem（canonical 用官方参数名 round）。"""
        rounds = [
            OfferRound(
                round_id=str(r["round"]),
                items=[
                    OfferRoundItem(
                        code=i["code"],
                        quantity=i.get("quantity", 1),
                        modifications=[
                            ModificationValue(**m) for m in i.get("modifications", [])
                        ],
                    )
                    for i in r.get("items", [])
                ],
            )
            for r in data.get("rounds", [])
        ]
        return cls(
            product_code=data["product_code"],
            quantity=data.get("quantity", 1),
            rounds=rounds,
            modifications=[ModificationValue(**m) for m in data.get("modifications", [])],
        )


# ---------------------------------------------------------------- 意图


class AtomRequirement(BaseModel):
    """需求中的一条食物要求。"""

    atom_code: str
    name: str
    quantity: int = Field(ge=1)
    locked: bool = True
    spec: tuple[str, ...] = ()


class AllowedExtra(BaseModel):
    """用户明确授权的额外食物（优化方案 A1：把「允许增量」细化为品类和数量上限）。"""

    code: str
    max_quantity: int = Field(ge=1)


class MealIntent(BaseModel):
    """一次配餐需求（版本化）。语言模型生成候选意图后必须经代码校验。"""

    schema_version: str = "1.1"
    session_id: str
    revision: int = Field(ge=1)
    mode: Literal["exact_plate", "budget"] = "exact_plate"
    budget_fen: Optional[int] = None
    required: list[AtomRequirement] = Field(default_factory=list)
    extras_policy: Literal["disallow", "allow"] = "disallow"
    # 推荐偏好（优化方案 A1）：合格候选内部的排序依据。
    # strict_plate：先保餐盘一致，再比价格；
    # save_money ：先比实际支出，再比餐盘偏离（默认）；
    # upgrade   ：与 save_money 同策略，语义上用户已同意升级（本版本行为一致，见 SKILL.md）。
    preference: Literal["strict_plate", "save_money", "upgrade"] = "save_money"
    # 细化的增量授权：为空表示 extras_policy=allow 时全量允许（兼容简单授权）。
    allowed_extras: list[AllowedExtra] = Field(default_factory=list)
    substitutions_allowed: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    source: EvidenceSource = EvidenceSource.USER


# ---------------------------------------------------------------- 报价（A4：上下文绑定）


class QuoteItem(BaseModel):
    product_code: str
    product_name: str
    quantity: int
    original_subtotal_fen: Optional[int] = None   # 缺失 = 未知
    subtotal_fen: Optional[int] = None            # 缺失 = 未知


class VerifiedQuote(BaseModel):
    """官方整单核价结果。总额取官方返回，缺项不猜补（A5：缺失=未知，不是 0）。"""

    total_fen: int                        # data.price（非负整数分，强制校验）
    product_price_fen: Optional[int] = None
    original_total_fen: Optional[int] = None
    discount_fen: Optional[int] = None    # 缺失 = 未知
    delivery_fen: Optional[int] = None    # 缺失 = 未知，绝不冒充 0
    packing_fen: Optional[int] = None
    tableware_fen: Optional[int] = None
    items: list[QuoteItem] = Field(default_factory=list)
    evidence: EvidenceRef
    # 上下文绑定（A4）：报价适用的门店/履约/账户作用域。
    # 空字符串 = 未声明上下文（仅合成/测试输入可留空）；
    # 非空时必须与求解上下文严格一致，否则报价不可用。
    context_key: str = ""

    @property
    def quote_status(self) -> str:
        """从证据来源派生运行口径：合成输入不能被标成官方实时。"""
        if self.evidence.source == EvidenceSource.OFFICIAL:
            return "official"
        if self.evidence.source == EvidenceSource.SYNTHETIC:
            return "synthetic"
        return "snapshot"


def context_key_for(store_code: str, order_type: int, be_type: int) -> str:
    """构造报价上下文键。账户作用域当前不含（官方 calculate-price 不接收账户参数，
    权益状态差异由 live 模式的实时核价天然覆盖）。"""
    return f"store={store_code};order_type={order_type};be_type={be_type}"


class QuoteRequest(BaseModel):
    """一条待执行的官方核价任务（优化方案 A6 拟议内部字段，非官方参数）。"""

    request_id: str
    session_id: str
    intent_revision: int
    context_key: str
    cart_hash: str
    official_payload: dict[str, Any]   # calculate-price 请求参数（宿主直接透传）


class QuoteRecord(BaseModel):
    """一条已完成上下文校验的报价记录（优化方案 A6 拟议内部字段）。"""

    request_id: str
    context_key: str
    cart_hash: str
    captured_at: Optional[str] = None
    # live：本次会话实时官方返回；historical_replay：官方快照回放；synthetic：合成
    source_mode: Literal["live", "historical_replay", "synthetic"]
    total_fen: int
    known_fee_components: dict[str, Optional[int]] = Field(default_factory=dict)
    evidence: EvidenceRef
    # 完整报价（供 report 复用；结构化回放场景可省略）
    quote: Optional[VerifiedQuote] = None


# ---------------------------------------------------------------- 摘要哈希（方案 9.3）


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(obj: Any, namespace: str) -> str:
    return f"{namespace}:sha256:" + hashlib.sha256(
        _stable_json(obj).encode("utf-8")
    ).hexdigest()[:16]


def plate_hash(atoms: list[FoodAtom]) -> str:
    """食物组成/规格/数量的摘要。相同餐盘 ⟺ 相同 plate_hash。

    先规范化（聚合同键数量），因此 A×1+A×1 与 A×2 的摘要一致。
    """
    return _sha256([a.canonical() for a in normalize_plate(atoms)], "plate")


def cart_hash(offer: list[OfferItem]) -> str:
    """购买路径摘要。用于报价去重：同餐盘不同买法保持不同 cart_hash。

    canonical 已对轮次/子项做语义无序集合的确定性排序，
    轮次排列顺序不同但选择一致的购物车得到相同摘要。
    """
    return _sha256([o.canonical() for o in sorted(offer, key=lambda o: o.product_code)], "cart")


def decision_hash(
    context_key: str, intent: MealIntent, cart: str, quote_total_fen: Optional[int]
) -> str:
    """执行一致性摘要：上下文 + 需求版本 + 购物车 + 报价总额。"""
    payload = {
        "context": context_key,
        "session": intent.session_id,
        "revision": intent.revision,
        "cart": cart,
        "quote_total_fen": quote_total_fen,
    }
    return _sha256(payload, "decision")


# ---------------------------------------------------------------- 快照与菜单


class WithOrderRef(BaseModel):
    """随单购资格：部分优惠价必须搭配随单购卡才能享受。"""

    membership_code: str
    spec_id: str


class MealInfo(BaseModel):
    """菜单中的一条餐品（来自 query-meals）。"""

    code: str
    name: str
    current_price_fen: int
    original_price_fen: int
    discount_type: Optional[str] = None
    with_order: Optional[WithOrderRef] = None


class MenuSnapshot(BaseModel):
    """门店+履约上下文下的菜单快照。有缓存不代表价格仍然有效。"""

    store_code: str
    order_type: int
    be_type: int
    meals: dict[str, MealInfo]
    categories: list[tuple[str, list[str]]] = Field(default_factory=list)
    evidence: EvidenceRef
    completeness: Literal["full", "partial"] = "full"

    @property
    def context_key(self) -> str:
        return context_key_for(self.store_code, self.order_type, self.be_type)


class ComboChoice(BaseModel):
    code: str
    name: str
    diff_price_fen: int          # 相对套餐默认项的带符号差价（分）
    is_default: bool = False
    support_modify: bool = False


class ComboRound(BaseModel):
    round_id: int
    name: str
    min_quantity: int
    max_quantity: int
    choices: list[ComboChoice]


class ComboConfig(BaseModel):
    """一个套餐的选配结构（来自 query-meal-detail）。"""

    code: str
    name: str
    rounds: list[ComboRound]
    evidence: EvidenceRef


# ---------------------------------------------------------------- 求解结果


PathKind = Literal["same_plate", "extras", "substitution"]

BaselineOrigin = Literal["user_provided", "constructed_singles"]


class PathSolution(BaseModel):
    """一条购买路径及其对目标餐盘的偏离分类。"""

    intent_revision: int
    offer: list[OfferItem]
    resulting_plate: list[FoodAtom]
    classification: PathKind
    extra_atoms: list[FoodAtom] = Field(default_factory=list)
    missing_atoms: list[str] = Field(default_factory=list)
    reference_total_fen: Optional[int] = None   # 本地参考价（菜单+差价推导）
    quote: Optional[VerifiedQuote] = None      # 官方核价（存在才有效）
    combo_code: Optional[str] = None
    label: str = ""

    @property
    def plate_hash(self) -> str:
        return plate_hash(self.resulting_plate)

    @property
    def cart_hash(self) -> str:
        return cart_hash(self.offer)

    @property
    def effective_total_fen(self) -> Optional[int]:
        """有效总额只认官方核价；参考价不得冒充实付（A1 验收）。"""
        return self.quote.total_fen if self.quote else None
