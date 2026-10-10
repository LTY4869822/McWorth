"""固定餐盘求解（方案 11 节；2026-10-10 优化方案 A1/A2/A3/A6 修订）。

外层：用户指定的餐盘（exact_plate 模式下唯一）。
内层：为该餐盘枚举合法购买组合——
  1) 全单品直购（系统构造的对照基准，不是"用户原购买计划"，A7）
  2) 套餐覆盖：受控回溯把需求食物分配进套餐轮次；
     未分配满 minQuantity 的轮按默认/最低差价补足；min=0 的可选轮可整轮跳过；
     剩余需求由单品补购（套餐＋单品的组合路径）
  3) 负差价可选轮（min=0 且最低差价 < 0）在 cheapest 模式下额外生成
     "多拿一份还更便宜"的候选（仅 extras_policy=allow 时才可能合格）

求解入口独立校验（A3）：模式受支持、需求非空、预算合法、
无未决问题；revision 不匹配的路径不得进入当前推荐。

分类不依赖构造时的假设，而是对每条路径的"结果餐盘"（规范化聚合后）
与目标餐盘做严格比对（A2）：
  same_plate   ：食物原子（标识+规格+定制）与数量完全一致
  extras       ：目标原子全部在，另有增量（如套餐强制的饮料）
  substitution ：目标原子有缺失

合格性（用户允许的条件）先于排序（A1）：
  硬条件满足 → 有效官方核价 → 实际总额 → 偏好排序 → 增量与复杂度。

本地参考价（菜单价+差价推导）只用于排序候选与展示，
不能替代官方核价作为预算满足证明（方案 11.3/11.4）。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from ..adapters.combo import cheapest_fill, default_fill
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
    normalize_plate,
    plate_hash,
)

# 回溯枚举的候选数量上限：超限时截断并保留搜索说明，不声称绝对无解。
MAX_ASSIGNMENTS = 256


class SolveStatus(str, Enum):
    FEASIBLE_VERIFIED = "FEASIBLE_VERIFIED"    # 至少一个已核价可执行候选
    SEARCH_EXHAUSTED = "SEARCH_EXHAUSTED"      # 候选枚举完，未找到满足者
    DATA_INCOMPLETE = "DATA_INCOMPLETE"        # 商品/规格信息不足或需求未确认
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"  # 官方服务不可用


class SolveResult:
    def __init__(
        self,
        intent: MealIntent,
        target_atoms: list[FoodAtom],
        paths: list[PathSolution],
        quotes: dict[str, VerifiedQuote],
        context_key: str = "",
    ) -> None:
        self.intent = intent
        self.context_key = context_key
        self.target_atoms = normalize_plate(target_atoms)
        self.target_plate_hash = plate_hash(self.target_atoms)
        self.blockers: list[str] = []
        # A3：旧版本需求构造的路径不得进入当前推荐集合
        self.paths = [p for p in paths if p.intent_revision == intent.revision]
        for p in self.paths:
            if p.quote is None:
                q = quotes.get(p.cart_hash)
                if q is not None and self._quote_usable(q):
                    p.quote = q
        self._classify()
        self._filter()
        self._rank()

    # ---------------------------------------------------------- 报价适用性（A4）

    def _quote_usable(self, q: VerifiedQuote) -> bool:
        """报价必须与当前上下文一致才能参与推荐。

        - 空 context_key：未声明上下文，仅限合成/测试输入（live 与回放都会填写）；
        - 非空：必须与求解上下文严格相等，否则视为无报价。
        """
        return q.context_key == "" or q.context_key == self.context_key

    # ---------------------------------------------------------- 分类（A2）

    def _classify(self) -> None:
        want = {(a.code, a.spec): a.quantity for a in self.target_atoms}
        # 需求按（标识+规格）聚合：两条 A×1 的需求合计是 A×2（A2）
        need: dict[tuple[str, tuple[str, ...]], int] = {}
        for req in self.intent.required:
            key = (req.atom_code, tuple(req.spec))
            need[key] = need.get(key, 0) + req.quantity
        for p in self.paths:
            got: dict[tuple[str, tuple[str, ...]], int] = {}
            for a in normalize_plate(p.resulting_plate):
                got[(a.code, a.spec)] = got.get((a.code, a.spec), 0) + a.quantity
            extras: list[FoodAtom] = []
            for (code, spec), qty in got.items():
                surplus = qty - want.get((code, spec), 0)
                if surplus > 0:
                    name = next(
                        (a.name for a in p.resulting_plate if a.code == code), code
                    )
                    extras.append(
                        FoodAtom(code=code, name=name, quantity=surplus, spec=spec)
                    )
            missing = [
                next(r.name for r in self.intent.required
                     if (r.atom_code, tuple(r.spec)) == key)
                for key, qty in need.items()
                if got.get(key, 0) < qty
            ]
            if not missing and not extras:
                p.classification = "same_plate"
                p.extra_atoms = []
            elif not missing:
                p.classification = "extras"
                p.extra_atoms = extras
            else:
                p.classification = "substitution"
                p.missing_atoms = missing

    # ---------------------------------------------------------- 合格性（A1）

    def _filter(self) -> None:
        allowed_sub = set(self.intent.substitutions_allowed)
        allowed_extras: dict[str, int] = {
            e.code: e.max_quantity for e in self.intent.allowed_extras
        }
        eligible: list[PathSolution] = []
        for p in self.paths:
            if p.classification == "same_plate":
                eligible.append(p)
            elif p.classification == "extras":
                if self.intent.extras_policy != "allow":
                    continue
                if allowed_extras:
                    # 细化授权：每个额外原子必须在授权清单内且不超数量上限
                    if not all(
                        a.code in allowed_extras and a.quantity <= allowed_extras[a.code]
                        for a in p.extra_atoms
                    ):
                        continue
                eligible.append(p)
            elif p.classification == "substitution":
                if set(p.missing_atoms) <= allowed_sub:
                    eligible.append(p)
        self.eligible = eligible
        self.quoted_eligible = [p for p in eligible if p.quote is not None]
        if self.intent.budget_fen is not None:
            self.budget_eligible = [
                p for p in self.quoted_eligible
                if p.quote is not None and p.quote.total_fen <= self.intent.budget_fen
            ]
        else:
            self.budget_eligible = self.quoted_eligible

    # ---------------------------------------------------------- 排序（A1）

    def _rank(self) -> None:
        def price_key(p: PathSolution):
            total = p.effective_total_fen
            return (total is None, total if total is not None else 1 << 40)

        def plate_key(p: PathSolution):
            return (p.classification != "same_plate", len(p.extra_atoms))

        # 省钱优先：先实际支出，再餐盘偏离；严格餐盘反之。
        if self.intent.preference == "strict_plate":
            key = lambda p: plate_key(p) + price_key(p)  # noqa: E731
        else:
            key = lambda p: price_key(p) + plate_key(p)  # noqa: E731

        self.budget_eligible.sort(key=key)
        self.main: Optional[PathSolution] = self.budget_eligible[0] if self.budget_eligible else None
        # 对照基准：系统构造的全单品直购（A7：不是"用户原购买计划"）。
        # 报价在 __init__ 挂载时已经过上下文适用性校验。
        self.baseline: Optional[PathSolution] = next(
            (p for p in self.paths if p.label == "baseline" and p.quote is not None),
            None,
        )
        self.baseline_origin: Optional[str] = (
            "constructed_singles" if self.baseline is not None else None
        )
        if self.main is not None:
            self.status = SolveStatus.FEASIBLE_VERIFIED
        elif self.eligible and not self.quoted_eligible:
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


# ---------------------------------------------------------------- 求解入口


def validate_intent(intent: MealIntent) -> list[str]:
    """A3：求解前的独立校验。返回阻塞项列表；非空则不得产出推荐。"""
    problems: list[str] = []
    if intent.mode != "exact_plate":
        problems.append(f"模式 {intent.mode} 尚未支持，本版本仅支持 exact_plate（固定餐盘）")
    if intent.unresolved:
        problems.append("存在未确认的硬条件：" + "；".join(intent.unresolved))
    if not intent.required:
        problems.append("需求为空：目标餐盘尚未确认")
    if intent.budget_fen is not None and intent.budget_fen < 0:
        problems.append("预算不能为负数")
    # 替换许可按需求名称匹配（与 _filter 的 missing_atoms 口径一致）
    locked_names = [r.name for r in intent.required if r.locked]
    if locked_names and set(locked_names) & set(intent.substitutions_allowed):
        problems.append("锁定项出现在替换许可中，授权自相矛盾")
    return problems


def _empty_result(intent: MealIntent, problems: list[str]) -> SolveResult:
    result = SolveResult(intent, [], [], {}, context_key="")
    result.blockers = problems
    result.status = SolveStatus.DATA_INCOMPLETE
    result.main = None
    result.baseline = None
    result.baseline_origin = None
    return result


def solve_exact_plate(
    intent: MealIntent,
    menu: MenuSnapshot,
    combos: list[ComboConfig],
    quotes: dict[str, VerifiedQuote],
) -> SolveResult:
    """固定餐盘求解入口。quotes 以 cart_hash 为键（已核价候选）。

    quotes 中的报价必须与 menu 的上下文（门店/履约）一致才会被采用（A4）。
    """
    problems = validate_intent(intent)
    if problems:
        return _empty_result(intent, problems)
    target = [
        FoodAtom(code=req.atom_code, name=req.name, quantity=req.quantity, spec=tuple(req.spec))
        for req in intent.required
    ]
    try:
        paths: list[PathSolution] = [build_singles_path(intent, menu)]
    except ValueError as exc:
        # 例如：带定制的需求在单品直购中无法保持（本版本明确不支持定制严格比价）
        return _empty_result(intent, [str(exc)])
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
    return SolveResult(intent, target, unique, quotes, context_key=menu.context_key)


# ---------------------------------------------------------------- 路径构造


def _atom_for(menu: MenuSnapshot, code: str, quantity: int, fallback_name: str = "") -> FoodAtom:
    """按商品编码取原子；菜单映射缺失时（套餐子项常不在 meals map 中）用详情名兜底。"""
    info = menu.meals.get(code)
    name = info.name if info else (fallback_name or code)
    return FoodAtom(code=code, name=name, quantity=quantity)


def build_singles_path(intent: MealIntent, menu: MenuSnapshot) -> PathSolution:
    """全单品直购路径：系统构造的对照基准（A7：不代表用户原购买计划）。

    带定制（spec）的需求无法在单品直购中保持——官方单品定制核价
    未经逐项验证，本版本明确拒绝而不是默默删除定制后继续计算。
    """
    offer: list[OfferItem] = []
    plate: list[FoodAtom] = []
    for req in intent.required:
        if req.spec:
            raise ValueError(
                f"需求「{req.name}」带定制 {list(req.spec)}："
                "单品定制核价未经官方验证，本版本不支持该定制的严格比价"
            )
        offer.append(OfferItem(product_code=req.atom_code, quantity=req.quantity))
        plate.append(_atom_for(menu, req.atom_code, req.quantity))
    reference: Optional[int] = 0
    for req in intent.required:
        info = menu.meals.get(req.atom_code)
        if info is None:
            reference = None
            break
        reference += info.current_price_fen * req.quantity
    return PathSolution(
        intent_revision=intent.revision,
        offer=offer,
        resulting_plate=plate,
        classification="same_plate",   # 由 SolveResult 重新分类
        reference_total_fen=reference,
        label="baseline",
    )


# ---------------------------------------------------------------- 套餐覆盖（A6：回溯）


def _round_selections(rnd: ComboRound, remaining: dict[str, int]) -> list[dict[str, int]]:
    """枚举某一轮从「需求商品 ∩ 该轮可选」中可选取的多重集。

    每个多重集总量不超过 min(max_quantity, 剩余需求)，可为空集 {}。
    只考虑需求商品：非需求的填充统一交给 fill 策略处理，
    因此可选集大小受需求品类数限制（通常 ≤5），不会组合爆炸。
    """
    available = [c.code for c in rnd.choices if remaining.get(c.code, 0) > 0]
    if not available:
        return [{}]
    outs: list[dict[str, int]] = []

    def rec(idx: int, cur: dict[str, int], total: int) -> None:
        if idx == len(available):
            outs.append(dict(cur))
            return
        code = available[idx]
        room = min(rnd.max_quantity - total, remaining[code])
        # 数量降序枚举：优先把需求放进套餐（纯套餐路径先于混合路径出现）
        for take in range(room, -1, -1):
            if take:
                cur[code] = take
            rec(idx + 1, cur, total + take)
            if take:
                del cur[code]

    rec(0, {}, 0)
    return outs


def _enumerate_assignments(
    rounds: list[ComboRound], demand: dict[str, int]
) -> list[tuple[dict[str, int], list[tuple[ComboRound, dict[str, int]]]]]:
    """受控回溯：枚举把需求商品分配进各轮的全部方式。

    返回 (剩余需求, [(轮次, 分配)]) 列表；剩余需求交给单品补购。
    """
    results: list[tuple[dict[str, int], list[tuple[ComboRound, dict[str, int]]]]] = []

    def bt(i: int, remaining: dict[str, int], acc: list[tuple[ComboRound, dict[str, int]]]) -> None:
        if len(results) >= MAX_ASSIGNMENTS:
            return
        if i == len(rounds):
            results.append((dict(remaining), list(acc)))
            return
        rnd = rounds[i]
        for sel in _round_selections(rnd, remaining):
            rem2 = {c: q - sel.get(c, 0) for c, q in remaining.items()}
            rem2 = {c: q for c, q in rem2.items() if q > 0}
            acc.append((rnd, sel))
            bt(i + 1, rem2, acc)
            acc.pop()

    bt(0, dict(demand), [])
    return results


def _fill_choice(rnd: ComboRound, fill: str):
    return default_fill(rnd) if fill == "default" else cheapest_fill(rnd)


def _materialize(
    intent: MealIntent,
    menu: MenuSnapshot,
    combo: ComboConfig,
    base_price: int,
    placed: list[tuple[ComboRound, dict[str, int]]],
    remaining: dict[str, int],
    fill: str,
    *,
    keep_free_negative_rounds: bool = False,
) -> Optional[PathSolution]:
    """把一个分配方案物化为购买路径。

    - 分配数不足 min_quantity 的轮用 fill 策略补足；
    - min=0 且无分配的轮默认整轮跳过（exact_plate 的精确路径）；
      keep_free_negative_rounds=True 时改为填入负差价项（cheapest 模式的
      "多拿一份还更便宜"候选，仅在用户允许增量时可能合格）；
    - 剩余需求由单品补购（套餐＋单品路径）。
    """
    rounds_out: list[OfferRound] = []
    plate_rows: list[FoodAtom] = []
    reference: Optional[int] = base_price

    for rnd, sel in placed:
        choice_map = {c.code: c for c in rnd.choices}
        items: list[OfferRoundItem] = []
        n = sum(sel.values())
        for code, qty in sorted(sel.items()):
            items.append(OfferRoundItem(code=code, quantity=qty))
            plate_rows.append(
                _atom_for(menu, code, qty, fallback_name=choice_map[code].name)
            )
            reference += choice_map[code].diff_price_fen * qty
        if n == 0 and rnd.min_quantity == 0:
            if keep_free_negative_rounds:
                fc = cheapest_fill(rnd)
                if fc.diff_price_fen < 0:
                    items.append(OfferRoundItem(code=fc.code, quantity=1))
                    plate_rows.append(
                        _atom_for(menu, fc.code, 1, fallback_name=fc.name)
                    )
                    reference += fc.diff_price_fen
                else:
                    continue
            else:
                continue
        elif n < rnd.min_quantity:
            fc = _fill_choice(rnd, fill)
            need = rnd.min_quantity - n
            merged = False
            for it in items:
                if it.code == fc.code:
                    it.quantity += need
                    merged = True
                    break
            if not merged:
                items.append(OfferRoundItem(code=fc.code, quantity=need))
            plate_rows.append(_atom_for(menu, fc.code, need, fallback_name=fc.name))
            reference += fc.diff_price_fen * need
        if items:
            rounds_out.append(OfferRound(round_id=str(rnd.round_id), items=items))

    offer: list[OfferItem] = [OfferItem(product_code=combo.code, quantity=1, rounds=rounds_out)]
    for code, qty in sorted(remaining.items()):
        offer.append(OfferItem(product_code=code, quantity=qty))
        plate_rows.append(_atom_for(menu, code, qty))
        info = menu.meals.get(code)
        if info is None:
            reference = None
        elif reference is not None:
            reference += info.current_price_fen * qty

    return PathSolution(
        intent_revision=intent.revision,
        offer=offer,
        resulting_plate=plate_rows,
        classification="same_plate",   # 由 SolveResult 重新分类
        reference_total_fen=reference,
        combo_code=combo.code,
        label=f"combo-{fill}",
    )


def build_combo_paths(
    intent: MealIntent, menu: MenuSnapshot, combo: ComboConfig, fill: str
) -> list[PathSolution]:
    """套餐覆盖路径（含套餐＋单品混合）。fill = "default" | "cheapest"。"""
    base_info = menu.meals.get(combo.code)
    if base_info is None:
        return []
    # 需求中可进套餐的部分（带定制的不进：轮次内定制核价未经验证）
    demand: dict[str, int] = {}
    for req in intent.required:
        if req.spec:
            continue
        demand[req.atom_code] = demand.get(req.atom_code, 0) + req.quantity
    if not demand:
        return []

    paths: list[PathSolution] = []
    for remaining, placed in _enumerate_assignments(combo.rounds, demand):
        total_placed = sum(q for _, sel in placed for q in sel.values())
        if total_placed == 0:
            continue  # 套餐内没有任何需求商品的路径没有比较价值
        path = _materialize(intent, menu, combo, base_info.current_price_fen, placed, remaining, fill)
        if path is not None:
            paths.append(path)
        if fill == "cheapest":
            # 负差价可选轮的"占便宜"变体（多拿一份还更便宜）
            variant = _materialize(
                intent, menu, combo, base_info.current_price_fen, placed, remaining, fill,
                keep_free_negative_rounds=True,
            )
            if variant is not None and variant.cart_hash != (path.cart_hash if path else None):
                paths.append(variant)
    return paths
