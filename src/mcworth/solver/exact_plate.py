"""固定餐盘求解（方案 11 节，P0）。

外层：用户指定的餐盘（exact_plate 模式下唯一）。
内层：为该餐盘枚举合法购买组合——
  1) 全单品直购（用户的原计划，作为公平基准）
  2) 套餐覆盖：把锁定食物分配进套餐轮次，其余轮次按 默认/最低差价 填充
     （最低差价填充可能换来用户不需要的增量，须按 extras_policy 处置）

分类不依赖构造时的假设，而是对每条路径的“结果餐盘”与目标餐盘做严格比对：
  same_plate   ：食物原子完全一致
  extras       ：目标原子全部在，另有增量（如套餐强制的饮料）
  substitution ：目标原子有缺失（换了主餐/小食）

本地参考价（菜单价+差价推导）只用于排序候选与展示，
不能替代官方核价作为预算满足证明（方案 11.3/11.4）。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from ..domain.types import (
    AtomRequirement,
    ComboConfig,
    ComboRound,
    FoodAtom,
    MealIntent,
    MenuSnapshot,
    OfferItem,
    OfferRound,
    OfferRoundItem,
    PathSolution,
    VerifiedQuote,
    cart_hash,
    plate_hash,
)
from ..adapters.combo import cheapest_fill, default_fill


class SolveStatus(str, Enum):
    FEASIBLE_VERIFIED = "FEASIBLE_VERIFIED"    # 至少一个已核价可执行候选
    SEARCH_EXHAUSTED = "SEARCH_EXHAUSTED"      # 候选枚举完，未找到满足者
    DATA_INCOMPLETE = "DATA_INCOMPLETE"        # 商品/规格信息不足
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"  # 官方服务不可用


class SolveResult:
    def __init__(
        self,
        intent: MealIntent,
        target_atoms: list[FoodAtom],
        paths: list[PathSolution],
        quotes: dict[str, VerifiedQuote],
    ) -> None:
        self.intent = intent
        self.target_atoms = target_atoms
        self.target_plate_hash = plate_hash(target_atoms)
        self.paths = paths
        for p in paths:
            if p.quote is None:
                q = quotes.get(p.cart_hash)
                if q is not None:
                    p.quote = q
        self._classify()
        self._filter()
        self._rank()

    # ---------------------------------------------------------- 分类

    def _classify(self) -> None:
        for p in self.paths:
            got = {(a.code, a.spec): a.quantity for a in p.resulting_plate}
            want = {(a.code, a.spec): a.quantity for a in self.target_atoms}
            extras = [
                FoodAtom(code=a.code, name=a.name, quantity=a.quantity - want.get((a.code, a.spec), 0), spec=a.spec)
                for a in p.resulting_plate
                if a.quantity > want.get((a.code, a.spec), 0)
            ]
            missing = [
                req.name for req in self.intent.required
                if got.get((req.atom_code, tuple(req.spec)), 0) < req.quantity
            ]
            if not missing and not extras and got == want:
                p.classification = "same_plate"
            elif not missing:
                p.classification = "extras"
                p.extra_atoms = extras
            else:
                p.classification = "substitution"
                p.missing_atoms = missing

    # ---------------------------------------------------------- 过滤

    def _filter(self) -> None:
        allowed_sub = set(self.intent.substitutions_allowed)
        eligible: list[PathSolution] = []
        for p in self.paths:
            if p.classification == "same_plate":
                eligible.append(p)
            elif p.classification == "extras":
                if self.intent.extras_policy == "allow":
                    eligible.append(p)
            elif p.classification == "substitution":
                if set(p.missing_atoms) <= allowed_sub:
                    eligible.append(p)
        self.eligible = eligible
        self.quoted_eligible = [p for p in eligible if p.quote is not None]
        if self.intent.budget_fen is not None:
            self.budget_eligible = [
                p for p in self.quoted_eligible if p.quote.total_fen <= self.intent.budget_fen  # type: ignore[union-attr]
            ]
        else:
            self.budget_eligible = self.quoted_eligible

    # ---------------------------------------------------------- 排序

    def _rank(self) -> None:
        def key(p: PathSolution):
            return (
                p.classification != "same_plate",          # 同餐盘优先
                p.effective_total_fen is None,              # 已核价优先
                p.effective_total_fen if p.effective_total_fen is not None else 1 << 40,
                len(p.extra_atoms),                        # 增量少的优先
            )

        self.budget_eligible.sort(key=key)
        self.main: Optional[PathSolution] = self.budget_eligible[0] if self.budget_eligible else None
        # 公平基准：用户原计划（全单品直购）的已核价路径
        self.baseline: Optional[PathSolution] = next(
            (p for p in self.paths if p.label == "baseline" and p.quote is not None), None
        )
        if self.main is not None:
            self.status = SolveStatus.FEASIBLE_VERIFIED
        elif any(p.quote is None for p in self.eligible) and not self.quoted_eligible and self.eligible:
            self.status = SolveStatus.UPSTREAM_UNAVAILABLE
        elif self.paths:
            self.status = SolveStatus.SEARCH_EXHAUSTED
        else:
            self.status = SolveStatus.DATA_INCOMPLETE

    # ---------------------------------------------------------- 解释

    @property
    def same_plate_min_fen(self) -> Optional[int]:
        vals = [p.quote.total_fen for p in self.quoted_eligible if p.classification == "same_plate"]
        return min(vals) if vals else None


# ---------------------------------------------------------------- 路径构造


def _atom_for(menu: MenuSnapshot, code: str, quantity: int, fallback_name: str = "") -> FoodAtom:
    """按商品编码取原子；菜单映射缺失时（套餐子项常不在 meals map 中）用详情名兜底。"""
    info = menu.meals.get(code)
    name = info.name if info else (fallback_name or code)
    return FoodAtom(code=code, name=name, quantity=quantity)


def build_singles_path(intent: MealIntent, menu: MenuSnapshot) -> PathSolution:
    """用户原计划：每个锁定食物按单品直购。作为公平基准参与核价。"""
    offer: list[OfferItem] = []
    plate: list[FoodAtom] = []
    for req in intent.required:
        offer.append(OfferItem(product_code=req.atom_code, quantity=req.quantity))
        plate.append(_atom_for(menu, req.atom_code, req.quantity))
    reference = sum(
        menu.meals[req.atom_code].current_price_fen * req.quantity
        for req in intent.required
        if req.atom_code in menu.meals
    ) or None
    return PathSolution(
        intent_revision=intent.revision,
        offer=offer,
        resulting_plate=plate,
        classification="same_plate",
        reference_total_fen=reference,
        label="baseline",
    )


def _assign_rounds(
    combo: ComboConfig, required: list[AtomRequirement]
) -> Optional[dict[int, str]]:
    """把锁定食物分配到套餐轮次（每轮恰好选 1 个的简化情形）。

    返回 {round_id: choice_code}；无法全部安放则返回 None。
    """
    used: set[int] = set()
    assign: dict[int, str] = {}
    for req in required:
        placed = False
        for rnd in combo.rounds:
            if rnd.round_id in used:
                continue
            if any(c.code == req.atom_code for c in rnd.choices):
                assign[rnd.round_id] = req.atom_code
                used.add(rnd.round_id)
                placed = True
                break
        if not placed:
            return None
    return assign


def build_combo_paths(
    intent: MealIntent, menu: MenuSnapshot, combo: ComboConfig, fill: str
) -> list[PathSolution]:
    """套餐覆盖路径。fill = "default" | "cheapest" 决定空轮次的填充策略。"""
    assign = _assign_rounds(combo, intent.required)
    if assign is None:
        return []
    base_price = menu.meals[combo.code].current_price_fen if combo.code in menu.meals else None
    if base_price is None:
        return []

    rounds_out: list[OfferRound] = []
    plate: list[FoodAtom] = []
    reference = base_price
    for rnd in combo.rounds:
        if rnd.round_id in assign:
            code = assign[rnd.round_id]
            choice = next(c for c in rnd.choices if c.code == code)
        else:
            choice = default_fill(rnd) if fill == "default" else cheapest_fill(rnd)
            if fill == "cheapest":
                # 默认项也参与最低差价比较（cheapest_fill 已全局取最小）
                pass
        rounds_out.append(
            OfferRound(round_id=str(rnd.round_id), items=[OfferRoundItem(code=choice.code, quantity=1)])
        )
        plate.append(_atom_for(menu, choice.code, 1, fallback_name=choice.name))
        reference += choice.diff_price_fen
    offer = [OfferItem(product_code=combo.code, quantity=1, rounds=rounds_out)]
    return [
        PathSolution(
            intent_revision=intent.revision,
            offer=offer,
            resulting_plate=plate,
            classification="same_plate",  # 由 SolveResult 重新分类
            reference_total_fen=reference,
            combo_code=combo.code,
            label=f"combo-{fill}",
        )
    ]


def solve_exact_plate(
    intent: MealIntent,
    menu: MenuSnapshot,
    combos: list[ComboConfig],
    quotes: dict[str, VerifiedQuote],
) -> SolveResult:
    """固定餐盘求解入口。quotes 以 cart_hash 为键（已核价候选）。"""
    target = [
        FoodAtom(code=req.atom_code, name=req.name, quantity=req.quantity, spec=tuple(req.spec))
        for req in intent.required
    ]
    paths = [build_singles_path(intent, menu)]
    for combo in combos:
        paths.extend(build_combo_paths(intent, menu, combo, "default"))
        paths.extend(build_combo_paths(intent, menu, combo, "cheapest"))
    # 同 cart_hash 去重（保留首个）
    seen: set[str] = set()
    unique: list[PathSolution] = []
    for p in paths:
        h = p.cart_hash
        if h not in seen:
            seen.add(h)
            unique.append(p)
    return SolveResult(intent, target, unique, quotes)
