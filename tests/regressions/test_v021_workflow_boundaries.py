"""v0.2.1 工作流边界回归（收编自独立审查反例，2026-10-10）。

覆盖总方案 F01/F02/F03 的验收：报价身份、运行模式、时效与需求版本。
所有调用均为本地只读；把 fixture 标为 live 只测试校验逻辑，
不代表测试真的发起了 MCP 调用。
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcworth.adapters.combo import parse_combo_detail
from mcworth.cli import main, _load_resource
from mcworth.domain.types import (
    AllowedExtra,
    AtomRequirement,
    ComboChoice,
    ComboConfig,
    ComboRound,
    EvidenceRef,
    EvidenceSource,
    MealInfo,
    MealIntent,
    MenuSnapshot,
)
from mcworth.solver.exact_plate import build_combo_paths


def _put(root: Path, name: str, obj) -> str:
    p = root / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _get(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _setup_plan(root: Path, store: str = "1450713") -> dict:
    intent = dict(
        session_id="regression-suite", revision=1, budget_fen=4000,
        required=[
            dict(atom_code="1440", name="麦辣鸡腿汉堡", quantity=1),
            dict(atom_code="4810", name="中薯条", quantity=1),
        ],
    )
    menu = _load_resource("menu_store_1450713.json")
    menu["_meta"]["store_code"] = store
    files = dict(
        intent=_put(root, "intent.json", intent),
        menu=_put(root, "menu.json", menu),
        plan=str(root / "plan.json"),
        responses=str(root / "responses.json"),
        quotes=str(root / "quotes.json"),
        summary=str(root / "summary.json"),
    )
    assert main(["plan", "--intent", files["intent"], "--menu", files["menu"], "-o", files["plan"]]) == 0
    return files


def _response_for(files: dict, mode: str = "live") -> dict:
    plan = _get(files["plan"])
    req = plan["quote_requests"][0]
    raw = _load_resource("quote_single_1440_4810.json")
    return dict(
        request_id=req["request_id"],
        session_id=plan["intent"]["session_id"],
        intent_revision=plan["intent"]["revision"],
        context_key=plan["context"]["context_key"],
        cart_hash=req["cart_hash"],
        source_mode=mode,
        captured_at=datetime.now(timezone.utc).isoformat(),
        trace_id="regression-trace",
        response=deepcopy(raw["response"]),
    )


def _ingest(files: dict, response: dict):
    Path(files["responses"]).write_text(json.dumps([response]), encoding="utf-8")
    status = main(["ingest-quotes", "--plan", files["plan"], "--responses", files["responses"], "-o", files["quotes"]])
    return status, _get(files["quotes"])


def _report(files: dict, intent_override: dict | None = None):
    if intent_override is not None:
        Path(files["intent"]).write_text(json.dumps(intent_override), encoding="utf-8")
    status = main([
        "report", "--intent", files["intent"], "--menu", files["menu"],
        "--quotes", files["quotes"], "--json", files["summary"],
    ])
    return status, _get(files["summary"])


# ---------------------------------------------------------------- 控制组


def test_control_complete_matching_workflow_runs(tmp_path):
    files = _setup_plan(tmp_path)
    status, data = _ingest(files, _response_for(files))
    assert status == 0 and len(data["records"]) == 1
    _, summary = _report(files)
    assert summary["main"]["total_fen"] == 3600
    assert summary["source_mode"] == "live"


# ---------------------------------------------------------------- F01 报价身份


def test_foreign_context_response_is_rejected(tmp_path):
    files = _setup_plan(tmp_path, store="new-store")
    resp = _response_for(files)
    resp["context_key"] = "store=1450713;order_type=1;be_type=1"
    status, data = _ingest(files, resp)
    assert status != 0 and not data["records"], "错门店上下文必须被拒绝，不能被改写为计划上下文"


def test_response_request_identity_must_match_the_task(tmp_path):
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    resp.update(request_id="different-request", session_id="other-session", intent_revision=99)
    status, data = _ingest(files, resp)
    assert status != 0 and not data["records"], "cart_hash 匹配不能证明响应属于本任务"


def test_response_with_different_product_lines_is_rejected(tmp_path):
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    resp["response"]["data"]["productList"] = [
        dict(productCode="OTHER_PRODUCT", productName="Other", quantity=1, subtotal=100)
    ]
    resp["response"]["data"]["price"] = 100
    status, data = _ingest(files, resp)
    assert status != 0 and not data["records"], "商品清单不符的响应不能给原购物车记账"


# ---------------------------------------------------------------- F02 模式与时效


@pytest.mark.parametrize("mode", ["historical_replay", "synthetic"])
def test_nonlive_report_carries_explicit_mode(tmp_path, mode):
    files = _setup_plan(tmp_path)
    assert _ingest(files, _response_for(files, mode=mode))[0] == 0
    _, summary = _report(files)
    assert summary["main"] is None or summary.get("source_mode") == mode, \
        "非实时输出必须带独立模式标注，不得冒充已验证的当前推荐"


def test_very_old_live_quote_requires_refresh(tmp_path):
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    resp["captured_at"] = "2020-01-01T00:00:00+00:00"
    status, _ = _ingest(files, resp)
    if status != 0:
        return
    _, summary = _report(files)
    assert summary["main"] is None, "六年前的报价不能支撑当前价格推荐"
    assert summary["rejected_quotes"], "过期拒绝必须留下解释"


def test_future_captured_at_is_not_fresh(tmp_path):
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    resp["captured_at"] = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    status, _ = _ingest(files, resp)
    if status != 0:
        return
    _, summary = _report(files)
    assert summary["main"] is None, "未来时间戳属于异常数据，不能当新鲜报价"


# ---------------------------------------------------------------- F03 需求版本


def test_new_intent_revision_requires_quote_applicability_check(tmp_path):
    files = _setup_plan(tmp_path)
    assert _ingest(files, _response_for(files))[0] == 0
    changed = _get(files["intent"])
    changed["revision"] = 2
    changed["session_id"] = "new-session"
    _, summary = _report(files, intent_override=changed)
    assert summary["main"] is None, "report 必须对旧报价做 session/revision 适用性校验"
    assert summary["rejected_quotes"], "旧版本拒绝必须留下解释"


# ---------------------------------------------------------------- F05/F06 候选生成


def _synthetic_menu(*codes):
    return MenuSnapshot(
        store_code="S", order_type=1, be_type=1,
        meals={
            c: MealInfo(code=c, name=c, current_price_fen=1000, original_price_fen=1000)
            for c in codes
        },
        evidence=EvidenceRef(source=EvidenceSource.SYNTHETIC),
    )


def test_allowed_extra_choice_is_explored_when_not_default_or_cheapest():
    m = _synthetic_menu("A", "SET")
    c = ComboConfig(
        code="SET", name="Set", evidence=EvidenceRef(source=EvidenceSource.SYNTHETIC),
        rounds=[
            ComboRound(round_id=1, name="Main", min_quantity=1, max_quantity=1,
                       choices=[ComboChoice(code="A", name="A", diff_price_fen=0)]),
            ComboRound(round_id=2, name="Drink", min_quantity=1, max_quantity=1,
                       choices=[
                           ComboChoice(code="COLA", name="Cola", diff_price_fen=0, is_default=True),
                           ComboChoice(code="WATER", name="Water", diff_price_fen=-50),
                           ComboChoice(code="TEA", name="Tea", diff_price_fen=50),
                       ]),
        ],
    )
    i = MealIntent(
        session_id="s", revision=1, extras_policy="allow",
        allowed_extras=[AllowedExtra(code="TEA", max_quantity=1)],
        required=[AtomRequirement(atom_code="A", name="A", quantity=1)],
    )
    result = build_combo_paths(i, m, c, "default")
    tea_paths = [
        p for p in result
        if any(it.code == "TEA" for o in p.offer for r in o.rounds for it in r.items)
    ]
    assert tea_paths, "合法的茶饮加购组合存在，填充策略不能只试默认与最低差价"


def test_individual_choice_maximum_is_respected():
    m = _synthetic_menu("A", "B", "SET")
    raw = {"success": True, "data": {"code": "SET", "name": "Set", "rounds": [
        {"id": 1, "minQuantity": 2, "maxQuantity": 2, "choices": [
            {"code": "A", "name": "A", "diffPrice": "0", "isDefault": 1, "maxQuantity": 1},
            {"code": "B", "name": "B", "diffPrice": "0", "isDefault": 0, "maxQuantity": 1},
        ]}]}}
    c = parse_combo_detail(raw, EvidenceRef(source=EvidenceSource.SYNTHETIC))
    i = MealIntent(
        session_id="s", revision=1, extras_policy="allow",
        required=[AtomRequirement(atom_code="A", name="A", quantity=2)],
    )
    paths = build_combo_paths(i, m, c, "default")
    assert paths, "轮容量2、A上限1 时应存在合法组合（如 A+B 套餐 + 单品补 A）"
    assert all(
        item.quantity <= 1
        for p in paths for o in p.offer if o.product_code == "SET"
        for r in o.rounds for item in r.items
    ), "轮容量=2 不允许在 A.maxQuantity=1 时把 A 选两份"
