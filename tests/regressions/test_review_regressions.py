"""审查回归测试（优化方案第 13 节回归组；用例源自 2026-10-10 独立审查证据）。

这些用例在 0.1.0 版本上全部失败（17 项），用于钉住修复后的正确行为：
- 合格候选中按用户选择的省钱偏好排序
- 重复数量、拆分与合并餐盘行的等价性
- 可选轮次、必选多份、合法轮次匹配（回溯而非贪心）
- 门店/履约上下文变化时报价不可复用
- 需求版本不匹配的路径不得进入当前推荐
- 未确认条件阻塞推荐；金额类型严格校验
- 系统构造的基准不得冒充“用户原购买计划”
"""

from __future__ import annotations

import pytest

from mcworth.adapters.pricing import parse_quote
from mcworth.cli import _load_snapshot
from mcworth.domain.types import (
    AtomRequirement,
    ComboChoice,
    ComboConfig,
    ComboRound,
    EvidenceRef,
    EvidenceSource,
    FoodAtom,
    MealInfo,
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
from mcworth.evidence.report import render_result_card
from mcworth.solver.exact_plate import (
    SolveResult,
    SolveStatus,
    build_combo_paths,
    build_singles_path,
    solve_exact_plate,
)


def intent(**changes):
    data = dict(session_id="review", revision=1, budget_fen=4000,
                required=[AtomRequirement(atom_code="1440", name="Burger", quantity=1),
                          AtomRequirement(atom_code="4810", name="Fries", quantity=1)])
    data.update(changes)
    return MealIntent(**data)


def evidence():
    return EvidenceRef(source=EvidenceSource.SYNTHETIC, tool="synthetic-test",
                       captured_at="2026-10-10T00:00:00+08:00")


def quote(fen):
    return VerifiedQuote(total_fen=fen, product_price_fen=fen,
                         original_total_fen=fen, evidence=evidence())


def menu_for(*codes):
    return MenuSnapshot(store_code="synthetic-store", order_type=1, be_type=1,
                        meals={c: MealInfo(code=c, name=c, current_price_fen=1000,
                                          original_price_fen=1000) for c in codes},
                        evidence=evidence())


def combo_for(round_specs):
    return ComboConfig(code="SET", name="Synthetic set", evidence=evidence(), rounds=[
        ComboRound(round_id=i, name=f"round {i}", min_quantity=lo, max_quantity=hi,
                   choices=[ComboChoice(code=c, name=c, diff_price_fen=0,
                                        is_default=j == 0) for j, c in enumerate(codes)])
        for i, (lo, hi, codes) in enumerate(round_specs, 1)
    ])


# ---------------------------------------------------------------- A1 排序与偏好


def test_choose_cheapest_when_user_allows_extra_food_and_both_fit_budget():
    menu, combos, quotes = _load_snapshot()
    result = solve_exact_plate(intent(extras_policy="allow"), menu, combos, quotes)
    assert result.main is not None
    assert result.main.quote.total_fen == 3350, "Authorized cheaper option lost to 3600 exact plate"


def test_budget_3300_marks_neither_path_within_budget():
    menu, combos, quotes = _load_snapshot()
    result = solve_exact_plate(intent(budget_fen=3300, extras_policy="allow"), menu, combos, quotes)
    assert result.main is None
    assert all(
        p.quote is None or p.quote.total_fen > 3300 for p in result.paths if p.quote
    ), "3600/3350 都超出 3300，不得被标记为预算内"


def test_strict_plate_preference_prefers_same_plate_over_cheaper_extra():
    menu, combos, quotes = _load_snapshot()
    result = solve_exact_plate(
        intent(extras_policy="allow", preference="strict_plate"), menu, combos, quotes
    )
    assert result.main is not None
    assert result.main.classification == "same_plate"
    assert result.main.quote is not None and result.main.quote.total_fen == 3600


# ---------------------------------------------------------------- A2 餐盘规范化


def test_duplicate_food_across_rounds_is_aggregated_before_classification():
    m = menu_for("A", "SET")
    i = intent(required=[AtomRequirement(atom_code="A", name="A", quantity=2)])
    c = combo_for([(1, 1, ["A"]), (1, 1, ["A"])])
    paths = build_combo_paths(i, m, c, "default")
    assert sum(a.quantity for a in paths[0].resulting_plate if a.code == "A") == 2
    result = solve_exact_plate(i, m, [c], {paths[0].cart_hash: quote(1000)})
    assert result.main is not None, "Two delivered A items should meet quantity=2"


def test_repeated_requirements_do_not_accept_only_one_delivered_item():
    requirements = [AtomRequirement(atom_code="A", name="A", quantity=1) for _ in range(2)]
    i = intent(required=requirements)
    target = [FoodAtom(code="A", name="A", quantity=1) for _ in range(2)]
    p = PathSolution(intent_revision=1, offer=[OfferItem(product_code="A", quantity=1)],
                     resulting_plate=[FoodAtom(code="A", name="A", quantity=1)],
                     classification="same_plate", quote=quote(1000))
    result = SolveResult(i, target, [p], {})
    assert result.main is None, "Repeated user requirements must sum to two, not overwrite"


def test_same_plate_hash_is_invariant_to_splitting_identical_lines():
    split = [FoodAtom(code="A", name="A", quantity=1) for _ in range(2)]
    merged = [FoodAtom(code="A", name="A", quantity=2)]
    assert plate_hash(split) == plate_hash(merged)


def test_plate_hash_distinguishes_quantity():
    a1 = FoodAtom(code="A", name="A", quantity=1)
    a2 = FoodAtom(code="A", name="A", quantity=2)
    assert plate_hash([a1]) != plate_hash([a2])


def test_plate_hash_distinguishes_spec():
    plain = FoodAtom(code="A", name="A", quantity=1)
    no_sauce = FoodAtom(code="A", name="A", quantity=1, spec=("ingredient:no-sauce",))
    assert plate_hash([plain]) != plate_hash([no_sauce])


def test_customized_request_is_preserved_or_explicitly_rejected():
    m = menu_for("A")
    i = intent(required=[AtomRequirement(atom_code="A", name="A", quantity=1,
                                         spec=("ingredient:no-sauce",))])
    try:
        p = build_singles_path(i, m)
    except ValueError:
        return
    assert p.resulting_plate[0].spec == i.required[0].spec
    assert p.offer[0].modifications, "A customized request cannot become an ordinary purchase"


# ---------------------------------------------------------------- A6 轮次结构


def test_optional_round_can_be_omitted_for_exact_plate():
    m = menu_for("A", "D", "SET")
    i = intent(required=[AtomRequirement(atom_code="A", name="A", quantity=1)])
    c = combo_for([(1, 1, ["A"]), (0, 1, ["D"])])
    paths = build_combo_paths(i, m, c, "default")
    assert any(sum(a.quantity for a in p.resulting_plate) == 1 for p in paths), \
        "Optional drink should not be inserted into every purchase path"


def test_mandatory_quantity_is_satisfied_or_combo_is_explicitly_rejected():
    m = menu_for("A", "SET")
    i = intent(required=[AtomRequirement(atom_code="A", name="A", quantity=2)])
    c = combo_for([(2, 2, ["A"])])
    paths = build_combo_paths(i, m, c, "default")
    assert not paths or all(sum(x.quantity for x in p.offer[0].rounds[0].items) == 2 for p in paths)


def test_round_assignment_finds_existing_matching():
    m = menu_for("A", "B", "SET")
    i = intent(required=[AtomRequirement(atom_code=c, name=c, quantity=1) for c in ["A", "B"]])
    c = combo_for([(1, 1, ["A", "B"]), (1, 1, ["A"])])
    paths = build_combo_paths(i, m, c, "default")
    assert paths, "Feasible assignment is round1=B, round2=A"


def test_combo_plus_singles_covers_partial_demand():
    """套餐只覆盖部分需求时，剩余需求应走单品补购（套餐＋单品路径）。"""
    m = menu_for("A", "B", "SET")
    i = intent(required=[AtomRequirement(atom_code=c, name=c, quantity=1) for c in ["A", "B"]])
    c = combo_for([(1, 1, ["A"])])
    paths = build_combo_paths(i, m, c, "default")
    partial = [p for p in paths if any(o.product_code == "B" for o in p.offer)]
    assert partial, "B 不在套餐轮次内，应出现套餐＋单品补购路径"


# ---------------------------------------------------------------- A3 未决条件与版本


def test_unresolved_hard_condition_blocks_verified_recommendation():
    menu, combos, quotes = _load_snapshot()
    result = solve_exact_plate(intent(unresolved=["Required size not confirmed"]), menu, combos, quotes)
    assert result.main is None
    assert result.status == SolveStatus.DATA_INCOMPLETE


def test_stale_path_revision_cannot_be_promoted_for_new_intent():
    i = intent(revision=2, required=[AtomRequirement(atom_code="A", name="A", quantity=1)])
    target = [FoodAtom(code="A", name="A", quantity=1)]
    p = PathSolution(intent_revision=1, offer=[OfferItem(product_code="A", quantity=1)],
                     resulting_plate=target, classification="same_plate", quote=quote(1000))
    result = SolveResult(i, target, [p], {})
    assert result.main is None, "revision=1 result must not be current for revision=2"


def test_unsupported_mode_is_not_silently_executed():
    menu, combos, quotes = _load_snapshot()
    result = solve_exact_plate(intent(mode="budget"), menu, combos, quotes)
    assert result.main is None
    assert result.status == SolveStatus.DATA_INCOMPLETE
    assert any("尚未支持" in b for b in result.blockers)


def test_locked_item_cannot_be_substituted_via_ambiguity():
    i = intent(
        required=[AtomRequirement(atom_code="1440", name="Burger", quantity=1, locked=True)],
        substitutions_allowed=["Burger"],
    )
    from mcworth.solver.exact_plate import validate_intent

    assert validate_intent(i), "锁定项出现在替换许可中必须被入口校验拦下"


# ---------------------------------------------------------------- A4 报价上下文


def test_same_cart_historical_quote_cannot_validate_different_store_context():
    menu, combos, quotes = _load_snapshot()
    changed = menu.model_copy(update={"store_code": "different-store", "order_type": 2})
    result = solve_exact_plate(intent(), changed, combos, quotes)
    assert result.main is None, "A quote with no verified current context cannot price this new context"


# ---------------------------------------------------------------- A5 金额类型


@pytest.mark.parametrize("bad_price", [True, 12.9, -1])
def test_quote_rejects_non_integer_or_negative_fen(bad_price):
    with pytest.raises((ValueError, TypeError)):
        parse_quote({"success": True, "data": {"price": bad_price}}, evidence())


def test_unknown_delivery_fee_is_not_labeled_zero():
    q = parse_quote({"success": True, "data": {"price": 1000}}, evidence())
    assert q.delivery_fen is None, "Missing fee field is unknown, not proof of zero"
    assert q.discount_fen is None
    assert q.packing_fen is None


# ---------------------------------------------------------------- A7 基准措辞


def test_counterfactual_singles_is_not_called_users_original_purchase():
    menu, combos, quotes = _load_snapshot()
    result = solve_exact_plate(intent(extras_policy="allow"), menu, combos, quotes)
    card = render_result_card(result)
    assert "原购买计划" not in card and "原计划（单品直购）" not in card, \
        "Food requirements alone do not establish the user's original purchase structure"


# ---------------------------------------------------------------- 摘要一致性


def test_cart_hash_ignores_order_of_equivalent_rounds():
    rounds = [OfferRound(round_id=str(i), items=[OfferRoundItem(code=c, quantity=1)])
              for i, c in [(1, "A"), (2, "B")]]
    a = [OfferItem(product_code="SET", quantity=1, rounds=rounds)]
    b = [OfferItem(product_code="SET", quantity=1, rounds=list(reversed(rounds)))]
    assert cart_hash(a) == cart_hash(b)
