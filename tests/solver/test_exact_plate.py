"""求解不变量测试（方案 11/12 节）。

这些用例守护的是本产品最容易被做错、也最容易误导用户的几条边界：
- 餐盘不一致时，绝不报“省了多少”（只能报方案差额）
- 增量/替换路径必须得到明确授权才能成为主推荐
- 没有官方核价的路径，一律不能当主推荐
- 用户的原购买计划必须始终作为公平基准参与比较
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcworth.adapters.combo import parse_combo_detail
from mcworth.adapters.menu import parse_menu, snapshot_evidence
from mcworth.adapters.pricing import parse_quote, snapshot_quote_evidence
from mcworth.domain.types import (
    AtomRequirement,
    MealIntent,
    OfferItem,
    VerifiedQuote,
    cart_hash,
)
from mcworth.evidence.report import same_plate_delta_fen
from mcworth.solver.exact_plate import SolveStatus, solve_exact_plate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> tuple[dict, dict]:
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return raw["_meta"], raw["response"]


def _fixtures():
    mm, mr = _load("menu_store_1450713.json")
    menu = parse_menu(
        mr,
        snapshot_evidence(mm["trace_id"], mm["captured_at"]),
        store_code=mm["store_code"],
        order_type=mm["order_type"],
        be_type=mm["be_type"],
        completeness="partial",
    )
    cm, cr = _load("combo_9900005456.json")
    combo = parse_combo_detail(cr, snapshot_evidence(cm["trace_id"], cm["captured_at"]))

    quotes: dict[str, VerifiedQuote] = {}
    for fname in ("quote_single_1440_4810.json", "quote_combo_9900005456.json"):
        qm, qr = _load(fname)
        quote = parse_quote(qr, snapshot_quote_evidence(qm["trace_id"], qm["captured_at"]))
        quotes[cart_hash([OfferItem.from_canonical(o) for o in qm["offer"]])] = quote
    return menu, [combo], quotes


MENU, COMBOS, QUOTES = _fixtures()


def _intent(budget_fen: int | None = 3500, extras: str = "disallow") -> MealIntent:
    return MealIntent(
        session_id="t",
        revision=1,
        mode="exact_plate",
        budget_fen=budget_fen,
        required=[
            AtomRequirement(atom_code="1440", name="麦辣鸡腿汉堡", quantity=1),
            AtomRequirement(atom_code="4810", name="中薯条", quantity=1),
        ],
        extras_policy=extras,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- 核价对账


def test_quotes_are_matched_to_paths_by_cart_hash():
    """快照报价必须能对上求解器构造的路径，否则后续断言都是在空跑。"""
    result = solve_exact_plate(_intent(), MENU, COMBOS, QUOTES)
    priced = [p for p in result.paths if p.quote is not None]
    assert len(priced) == 2, "基准路径与套餐路径都应挂上官方报价"
    labels = {p.label for p in priced}
    assert labels == {"baseline", "combo-default"}


# ---------------------------------------------------------------- 增量策略


def test_disallow_extras_blocks_cheaper_but_different_plate():
    """真实案例：¥33.5 套餐比 ¥36 便宜，但多一杯饮料 → 不得成为主推荐。"""
    result = solve_exact_plate(_intent(extras="disallow"), MENU, COMBOS, QUOTES)
    combo_path = next(p for p in result.paths if p.combo_code == "9900005456")
    assert combo_path.classification == "extras"
    assert combo_path.quote is not None and combo_path.quote.total_fen == 3350
    # 预算 ¥35 卡不住 ¥36 的严格餐盘 → 不产出主推荐
    assert result.main is None
    assert result.status == SolveStatus.SEARCH_EXHAUSTED


def test_allow_extras_promotes_combo_and_labels_it_as_plan_delta():
    """明确授权后才能主推；餐盘已变，比较口径必须是方案差额而非节省。"""
    result = solve_exact_plate(_intent(extras="allow"), MENU, COMBOS, QUOTES)
    assert result.main is not None
    assert result.main.classification == "extras"
    assert result.main.quote is not None and result.main.quote.total_fen == 3350
    assert result.status == SolveStatus.FEASIBLE_VERIFIED
    # 关键：餐盘不一致 → same_plate_delta 必须为 None
    assert result.baseline is not None
    assert same_plate_delta_fen(result.baseline, result.main) is None


def test_baseline_always_priced_when_quotes_present():
    """没有基准就没有资格谈“省了多少”。"""
    result = solve_exact_plate(_intent(extras="allow"), MENU, COMBOS, QUOTES)
    assert result.baseline is not None and result.baseline.quote is not None
    assert result.baseline.quote.total_fen == 3600
    assert result.baseline.plate_hash == result.target_plate_hash


# ---------------------------------------------------------------- 报价缺失


def test_unpriced_paths_never_become_main():
    """没有任何报价时不得产出主推荐，也不得假装“验价可行”。"""
    result = solve_exact_plate(_intent(), MENU, COMBOS, quotes={})
    assert result.main is None
    assert result.status == SolveStatus.UPSTREAM_UNAVAILABLE
    assert result.quoted_eligible == []


def test_partially_priced_paths_do_not_block_quoted_main():
    """只有套餐核价、基准未核价时，套餐仍可作为主推荐（但没有节省可言）。"""
    combo_key = next(
        h for h in QUOTES if QUOTES[h].items and QUOTES[h].items[0].product_code == "9900005456"
    )
    result = solve_exact_plate(_intent(extras="allow"), MENU, COMBOS, {combo_key: QUOTES[combo_key]})
    assert result.main is not None
    # 基准未核价 → 没有 baseline，因而没有任何差额可报
    assert result.baseline is None


# ---------------------------------------------------------------- 预算与去重


def test_budget_boundary_is_inclusive():
    """预算 ¥36.00 恰好等于基准核价 → 严格餐盘路径应当通过。"""
    loose = solve_exact_plate(_intent(budget_fen=3600), MENU, COMBOS, QUOTES)
    assert loose.main is not None
    assert loose.main.classification == "same_plate"
    assert loose.main.quote is not None and loose.main.quote.total_fen == 3600
    # 同餐盘下“没有更便宜的买法”，节省额度为 0 而非负数
    assert same_plate_delta_fen(loose.baseline, loose.main) == 0


def test_duplicate_cart_paths_are_collapsed():
    """相同购买组合只保留一条，避免重复核价与重复计数。"""
    result = solve_exact_plate(_intent(), MENU, COMBOS, QUOTES)
    hashes = [p.cart_hash for p in result.paths]
    assert len(hashes) == len(set(hashes))


def test_missing_quotes_do_not_crash_classification():
    """完全无菜单匹配时仍能安全降级，不抛异常。"""
    intent = _intent()
    intent.required = [AtomRequirement(atom_code="9999999", name="不存在的餐品", quantity=1)]
    result = solve_exact_plate(intent, MENU, COMBOS, QUOTES)
    assert result.status in {
        SolveStatus.SEARCH_EXHAUSTED,
        SolveStatus.DATA_INCOMPLETE,
        SolveStatus.UPSTREAM_UNAVAILABLE,
    }
