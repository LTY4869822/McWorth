"""核心域类型（对应开发方案第 9 节数据模型）。

所有金额字段以整数分存储；时间为带时区 ISO 8601 字符串。
三类摘要严格分离（方案 9.3）：
- plate_hash：食物组成/规格/数量/定制 —— 判断是否相同餐盘。
- cart_hash：购买路径/套餐选择 —— 报价去重。
- decision_hash：上下文+需求版本+购物车+报价 —— 执行一致性。
哈希仅是本地一致性标识，不是官方签名或防篡改认证。
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .money import yuan_str_to_fen

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
                                for m in i.modifications
                            ],
                        }
                        for i in r.items
                    ],
                }
                for r in self.rounds
            ],
            "modifications": [
                {"code": m.code, "key": m.key, "quantity": m.quantity}
                for m in self.modifications
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


class MealIntent(BaseModel):
    """一次配餐需求（版本化）。语言模型生成候选意图后必须经代码校验。"""

    schema_version: str = "1.0"
    session_id: str
    revision: int = Field(ge=1)
    mode: Literal["exact_plate", "budget"] = "exact_plate"
    budget_fen: Optional[int] = None
    required: list[AtomRequirement] = Field(default_factory=list)
    extras_policy: Literal["disallow", "allow"] = "disallow"
    substitutions_allowed: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    source: EvidenceSource = EvidenceSource.USER


# ---------------------------------------------------------------- 报价


class QuoteItem(BaseModel):
    product_code: str
    product_name: str
    quantity: int
    original_subtotal_fen: int
    subtotal_fen: int


class VerifiedQuote(BaseModel):
    """官方整单核价结果。总额取官方返回，缺项不猜补。"""

    total_fen: int                       # data.price
    product_price_fen: int
    original_total_fen: int              # data.originalPrice
    discount_fen: int = 0
    delivery_fen: int = 0
    packing_fen: int = 0
    tableware_fen: int = 0
    items: list[QuoteItem] = Field(default_factory=list)
    evidence: EvidenceRef
    quote_status: Literal["official", "snapshot"] = "official"


# ---------------------------------------------------------------- 摘要哈希（方案 9.3）


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(obj: Any, namespace: str) -> str:
    return f"{namespace}:sha256:" + hashlib.sha256(
        _stable_json(obj).encode("utf-8")
    ).hexdigest()[:16]


def plate_hash(atoms: list[FoodAtom]) -> str:
    """食物组成/规格/数量的摘要。相同餐盘 ⟺ 相同 plate_hash。"""
    return _sha256([a.canonical() for a in sorted(atoms, key=lambda a: a.code)], "plate")


def cart_hash(offer: list[OfferItem]) -> str:
    """购买路径摘要。用于报价去重：同餐盘不同买法保持不同 cart_hash。"""
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
        return self.quote.total_fen if self.quote else self.reference_total_fen
