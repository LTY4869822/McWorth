"""v0.2.2 金额自洽回归（来源：2026-10-16 实战自测发现 D1）。

背景：一次性身份四元组校验只回答"这条报价属于哪个请求"，不回答
"这条报价自己是否自相矛盾"。实测中，把响应里的 `price` 改成 1 分
（商品价与明细仍是 4800 分）能被接受，并在 report 中作为主推荐输出
"官方整单核价 ¥0.01"——直接违反不变量 ③。

本组用同一份 fixture 检查：篡改必被拒、正常必通过（防止校验收紧误伤）。
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcworth.adapters.pricing import parse_quote, quote_consistency_problems
from mcworth.cli import main, _load_resource
from mcworth.domain.types import EvidenceRef, EvidenceSource


def _put(root: Path, name: str, obj) -> str:
    p = root / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _get(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        source=EvidenceSource.OFFICIAL,
        tool="calculate-price",
        captured_at=datetime.now(timezone.utc).isoformat(),
        trace_id="amount-consistency-trace",
    )


def _setup_plan(root: Path) -> dict:
    intent = dict(
        session_id="amount-consistency-suite", revision=1, budget_fen=4000,
        required=[
            dict(atom_code="1440", name="麦辣鸡腿汉堡", quantity=1),
            dict(atom_code="4810", name="中薯条", quantity=1),
        ],
    )
    files = dict(
        intent=_put(root, "intent.json", intent),
        menu=_put(root, "menu.json", _load_resource("menu_store_1450713.json")),
        plan=str(root / "plan.json"),
        responses=str(root / "responses.json"),
        quotes=str(root / "quotes.json"),
        summary=str(root / "summary.json"),
    )
    assert main(["plan", "--intent", files["intent"], "--menu", files["menu"],
                 "-o", files["plan"]]) == 0
    return files


def _response_for(files: dict) -> dict:
    plan = _get(files["plan"])
    req = plan["quote_requests"][0]
    return dict(
        request_id=req["request_id"],
        session_id=plan["intent"]["session_id"],
        intent_revision=plan["intent"]["revision"],
        context_key=plan["context"]["context_key"],
        cart_hash=req["cart_hash"],
        source_mode="live",
        captured_at=datetime.now(timezone.utc).isoformat(),
        trace_id="amount-consistency-trace",
        response=deepcopy(_load_resource("quote_single_1440_4810.json")["response"]),
    )


def _ingest(files: dict, response: dict):
    Path(files["responses"]).write_text(json.dumps([response]), encoding="utf-8")
    status = main(["ingest-quotes", "--plan", files["plan"],
                   "--responses", files["responses"], "-o", files["quotes"]])
    return status, _get(files["quotes"])


# ---------------------------------------------------------------- 控制组


def test_control_intact_quote_still_accepted(tmp_path):
    """校验不得误伤正常报价（fixture 走真实解析路径）。"""
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    quote = parse_quote(resp["response"], _evidence())
    assert quote_consistency_problems(quote) == []
    status, data = _ingest(files, resp)
    assert status == 0 and len(data["records"]) == 1


# ---------------------------------------------------------------- D1 金额自洽


def test_tampered_total_is_rejected(tmp_path):
    """实战发现：仅把 price 改成 1 分，明细与商品价不变 → 必须拒绝。"""
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    resp["response"]["data"]["price"] = 1
    status, data = _ingest(files, resp)
    assert status != 0 and not data["records"], "自相矛盾的金额不得进入推荐"
    assert any("金额自相矛盾" in e for e in data["errors"])


def test_tampered_item_subtotal_is_rejected(tmp_path):
    """明细被改小、商品价不变 → 明细合计与商品价不一致，必须拒绝。"""
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    for it in resp["response"]["data"]["productList"]:
        it["subtotal"] = 1
    status, data = _ingest(files, resp)
    assert status != 0 and not data["records"]
    assert any("不一致" in e for e in data["errors"])


def test_total_below_discount_floor_is_rejected(tmp_path):
    """商品价与优惠都在，实付低于「商品价 - 优惠」的下限 → 必须拒绝。"""
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    data = resp["response"]["data"]
    data["productPrice"] = 5000
    data["discount"] = 1000          # 下限 4000
    data["price"] = 500              # 远低于下限
    for it in data["productList"]:
        it["subtotal"] = 2500        # 明细合计对齐商品价 5000
    status, data_out = _ingest(files, resp)
    assert status != 0 and not data_out["records"]
    assert any("下限" in e for e in data_out["errors"])


def test_consistent_quote_with_delivery_fee_is_accepted(tmp_path):
    """全部费用组件齐备且公式成立时，必须通过（含配送费的场景）。"""
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    data = resp["response"]["data"]
    data["productPrice"] = 3600
    data["discount"] = 500
    data["deliveryPrice"] = 900
    data["packingPrice"] = 100
    data["tablewarePrice"] = 0
    data["price"] = 3600 - 500 + 900 + 100 + 0
    for it in data["productList"]:
        it["subtotal"] = 1800        # 明细合计 3600 = 商品价
    quote = parse_quote(resp["response"], _evidence())
    assert quote_consistency_problems(quote) == []
    status, out = _ingest(files, resp)
    assert status == 0 and len(out["records"]) == 1


def test_missing_product_price_skips_check_without_guessing(tmp_path):
    """商品价缺失（None=未知）时不做臆断：既不放行假金额，也不无端拒绝。

    此时仍由身份四元组与商品清单对账把关；本用例确认校验函数不抛异常、
    不把"未知"当成 0。
    """
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    data = resp["response"]["data"]
    data.pop("productPrice", None)
    quote = parse_quote(resp["response"], _evidence())
    assert quote.product_price_fen is None
    assert quote_consistency_problems(quote) == []


def test_report_never_shows_tampered_price(tmp_path):
    """端到端：篡改金额后 report 不得把 ¥0.01 当成主推荐。"""
    files = _setup_plan(tmp_path)
    resp = _response_for(files)
    resp["response"]["data"]["price"] = 1
    status, data = _ingest(files, resp)
    assert not data["records"]
    main(["report", "--intent", files["intent"], "--menu", files["menu"],
          "--quotes", files["quotes"], "--json", files["summary"]])
    summary = _get(files["summary"])
    assert summary["main"] is None, "被拒报价不得支撑任何主推荐"
