"""McWorth 命令行入口（2026-10-10 优化方案 B1/B2 修订）。

子命令（标准只读工作流）：
  plan          需求 + 菜单/套餐响应 → 规范化需求、候选清单、待核价请求
  ingest-quotes 核价响应（宿主调用官方 MCP 后留存）→ 上下文校验后的报价记录
  report        需求 + 菜单/套餐 + 报价 → 方案卡（差额、限制与未解决项）
  demo          用实测快照（真实 MCP 响应留存）跑固定餐盘求解演示
  doctor        环境自检：依赖、包资源、金额规则（不打印任何凭证）

工作流（宿主 Agent 按此协议调用，无需临时编写 Python）：
  1. 宿主调用官方 query-meals / query-meal-detail，把响应存成
     {"_meta": {...}, "response": {...}} 结构的 JSON 文件；
  2. mcworth plan --intent intent.json --menu menu.json [--combos combos.json] -o plan.json
  3. 宿主按 plan.json 的 quote_requests[].official_payload 逐个调用官方
     calculate-price，把响应连同 cart_hash 存成 responses.json；
  4. mcworth ingest-quotes --plan plan.json --responses responses.json -o quotes.json
  5. mcworth report --intent intent.json --menu menu.json --quotes quotes.json

演示数据内置于包资源（mcworth/data），wheel 安装后即可独立运行。
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .adapters.combo import parse_combo_detail
from .adapters.menu import parse_menu, snapshot_evidence
from .adapters.official import to_official_payload
from .adapters.pricing import parse_quote, snapshot_quote_evidence
from .domain.money import fen_to_yuan
from .domain.types import (
    AtomRequirement,
    ComboConfig,
    EvidenceRef,
    EvidenceSource,
    MealIntent,
    MenuSnapshot,
    OfferItem,
    PathSolution,
    QuoteRecord,
    QuoteRequest,
    VerifiedQuote,
    cart_hash,
)
from .evidence import render_result_card
from .solver import solve_exact_plate
from .solver.exact_plate import validate_intent

DATA_PACKAGE = "mcworth.data"


# ---------------------------------------------------------------- 资源与文件


def _load_resource(name: str) -> dict:
    ref = resources.files(DATA_PACKAGE).joinpath(name)
    return json.loads(ref.read_text(encoding="utf-8"))


def _read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _load_menu(payload: dict) -> MenuSnapshot:
    meta = payload["_meta"]
    return parse_menu(
        payload["response"],
        snapshot_evidence(meta["trace_id"], meta["captured_at"]),
        store_code=str(meta["store_code"]),
        order_type=int(meta["order_type"]),
        be_type=int(meta["be_type"]),
        completeness=meta.get("completeness", "full"),
    )


def _load_combos(payload: dict) -> list[ComboConfig]:
    combos: list[ComboConfig] = []
    for entry in payload.get("combos") or []:
        meta = entry["_meta"]
        combos.append(
            parse_combo_detail(entry["response"], snapshot_evidence(meta["trace_id"], meta["captured_at"]))
        )
    return combos


def _load_snapshot() -> tuple[MenuSnapshot, list[ComboConfig], dict[str, VerifiedQuote]]:
    """演示快照（包资源）。报价绑定采集时的上下文。"""
    menu_raw = _load_resource("menu_store_1450713.json")
    menu = _load_menu(menu_raw)
    ctx = menu.context_key

    combos = _load_combos(
        {"combos": [_load_resource("combo_9900005456.json")]}
    )
    quotes: dict[str, VerifiedQuote] = {}
    for fname in ("quote_single_1440_4810.json", "quote_combo_9900005456.json"):
        raw = _load_resource(fname)
        m = raw["_meta"]
        quote = parse_quote(raw["response"], snapshot_quote_evidence(m["trace_id"], m["captured_at"]))
        quote.context_key = ctx
        offer = [OfferItem.from_canonical(o) for o in m["offer"]]
        quotes[cart_hash(offer)] = quote
    return menu, combos, quotes


# ---------------------------------------------------------------- plan


def cmd_plan(args: argparse.Namespace) -> int:
    intent = MealIntent.model_validate(_read_json(Path(args.intent)))
    menu = _load_menu(_read_json(Path(args.menu)))
    combos = _load_combos(_read_json(Path(args.combos))) if args.combos else []

    problems = validate_intent(intent)
    result = solve_exact_plate(intent, menu, combos, quotes={})

    candidates = []
    for p in result.paths:
        candidates.append(
            {
                "label": p.label or p.combo_code or "path",
                "cart_hash": p.cart_hash,
                "classification": p.classification,
                "extra_atoms": [
                    {"code": a.code, "name": a.name, "quantity": a.quantity}
                    for a in p.extra_atoms
                ],
                "missing_atoms": p.missing_atoms,
                "reference_total_fen": p.reference_total_fen,
                "offer": [o.canonical() for o in p.offer],
            }
        )

    # 只为「分类合格」或「对照基准」的路径请求核价：控制核价预算
    need_quote: dict[str, PathSolution] = {}
    for p in result.eligible + ([result.baseline] if result.baseline else []):
        if p.cart_hash not in need_quote:
            need_quote[p.cart_hash] = p
    quote_requests = [
        {
            "request_id": f"qr-{i}",
            "session_id": intent.session_id,
            "intent_revision": intent.revision,
            "cart_hash": h,
            "official_payload": to_official_payload(
                p.offer, menu.store_code, menu.order_type, menu.be_type
            ),
        }
        for i, (h, p) in enumerate(need_quote.items())
    ]

    plan = {
        "schema_version": "1",
        "generated_by": f"mcworth {__version__}",
        "intent": intent.model_dump(mode="json"),
        "context": {
            "store_code": menu.store_code,
            "order_type": menu.order_type,
            "be_type": menu.be_type,
            "context_key": menu.context_key,
        },
        "blockers": problems or result.blockers,
        "target_plate_hash": result.target_plate_hash,
        "candidates": candidates,
        "quote_requests": quote_requests,
        "notes": [
            "official_payload 直接作为官方 calculate-price 的请求参数使用。",
            "reference_total_fen 是本地参考价，仅用于排序候选，不作为报价。",
            "只对分类合格与基准路径请求核价；增量路径需 extras_policy=allow 才会入单。",
        ],
    }
    if args.output:
        _write_json(Path(args.output), plan)
        print(f"已生成 {args.output}：{len(candidates)} 个候选，{len(quote_requests)} 个待核价请求")
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- ingest-quotes


def cmd_ingest_quotes(args: argparse.Namespace) -> int:
    plan = _read_json(Path(args.plan))
    responses = _read_json(Path(args.responses))
    ctx = plan["context"]["context_key"]
    known = {q["cart_hash"]: q for q in plan.get("quote_requests") or []}

    mode_source = {
        "live": EvidenceSource.OFFICIAL,
        "historical_replay": EvidenceSource.SNAPSHOT,
        "synthetic": EvidenceSource.SYNTHETIC,
    }
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, item in enumerate(responses):
        ch = item.get("cart_hash")
        if ch not in known:
            errors.append(f"响应 #{idx} 的 cart_hash 不在 plan 的待核价清单中，已跳过")
            continue
        req = known[ch]
        mode = item.get("source_mode", "live")
        if mode not in mode_source:
            errors.append(f"响应 #{idx} 的 source_mode 非法: {mode!r}，已跳过")
            continue
        evidence = EvidenceRef(
            source=mode_source[mode],
            tool="calculate-price",
            captured_at=item.get("captured_at"),
            trace_id=item.get("trace_id"),
        )
        try:
            quote = parse_quote(item["response"], evidence)
        except ValueError as exc:
            errors.append(f"响应 #{idx} 解析失败：{exc}")
            continue
        quote.context_key = ctx
        records.append(
            QuoteRecord(
                request_id=req.get("request_id", f"qr-{idx}"),
                context_key=ctx,
                cart_hash=ch,
                captured_at=item.get("captured_at"),
                source_mode=mode,  # type: ignore[arg-type]
                total_fen=quote.total_fen,
                known_fee_components={
                    "deliveryPrice": quote.delivery_fen,
                    "packingPrice": quote.packing_fen,
                    "tablewarePrice": quote.tableware_fen,
                    "discount": quote.discount_fen,
                },
                evidence=evidence,
                quote=quote,
            ).model_dump(mode="json")
        )
    out = {
        "schema_version": "1",
        "context_key": ctx,
        "records": records,
        "errors": errors,
        "notes": [
            "live 报价仅代表采集时刻的官方核价，下单前请以最终订单确认为准。",
            "缺失的费用组件为 null（未知），不代表为 0。",
        ],
    }
    if args.output:
        _write_json(Path(args.output), out)
        print(f"已登记 {len(records)} 条报价（{len(errors)} 个问题）→ {args.output}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


# ---------------------------------------------------------------- report


def cmd_report(args: argparse.Namespace) -> int:
    intent = MealIntent.model_validate(_read_json(Path(args.intent)))
    menu = _load_menu(_read_json(Path(args.menu)))
    combos = _load_combos(_read_json(Path(args.combos))) if args.combos else []
    quotes_raw = _read_json(Path(args.quotes))

    quotes: dict[str, VerifiedQuote] = {}
    for r in quotes_raw.get("records") or []:
        rec = QuoteRecord.model_validate(r)
        if rec.quote is None:
            continue
        quote = VerifiedQuote.model_validate(rec.quote)
        quote.context_key = rec.context_key
        quotes[rec.cart_hash] = quote

    result = solve_exact_plate(intent, menu, combos, quotes)
    print(render_result_card(result))
    if args.json:
        summary = {
            "status": result.status.value,
            "blockers": result.blockers,
            "main": None
            if result.main is None
            else {
                "label": result.main.label,
                "cart_hash": result.main.cart_hash,
                "classification": result.main.classification,
                "total_fen": result.main.quote.total_fen if result.main.quote else None,
                "offer": [o.canonical() for o in result.main.offer],
            },
            "baseline_total_fen": result.baseline.quote.total_fen if result.baseline and result.baseline.quote else None,
            "baseline_origin": result.baseline_origin,
            "paths_compared": len(result.paths),
            "paths_quoted_eligible": len(result.quoted_eligible),
        }
        _write_json(Path(args.json), summary)
        print(f"\n（JSON 摘要已写入 {args.json}）")
    return 0


# ---------------------------------------------------------------- demo


def cmd_demo(args: argparse.Namespace) -> int:
    menu, combos, qmap = _load_snapshot()

    print("═" * 56)
    print("麦麦如意餐 McWorth · 固定餐盘求解演示")
    print("═" * 56)
    print(f"数据来源：麦当劳官方 MCP 实测快照（{menu.evidence.captured_at}）")
    print(f"门店：{menu.store_code}（上海黄浦华旭国际大厦餐厅）· 到店自取")
    print("")

    # 场景（方案 1.2 的真实版本）：
    # “35 元以内，我就想吃麦辣鸡腿汉堡和中薯，我自己带水。”
    intent = MealIntent(
        session_id="demo-session-01",
        revision=1,
        mode="exact_plate",
        budget_fen=3500,
        required=[
            AtomRequirement(atom_code="1440", name="麦辣鸡腿汉堡", quantity=1, locked=True),
            AtomRequirement(atom_code="4810", name="中薯条", quantity=1, locked=True),
        ],
        extras_policy=args.extras_policy,   # 默认不允许多出饮料：不因便宜塞进套餐
        substitutions_allowed=[],            # 锁定主餐与小食
    )
    print(f"用户需求：预算 ¥{fen_to_yuan(intent.budget_fen)}，保留：", end="")
    print("、".join(f"{r.name}×{r.quantity}" for r in intent.required))
    pol = "不允许" if intent.extras_policy == "disallow" else "已明确授权允许"
    print(f"（增量策略：{pol}多出饮料；替换策略：不允许换餐）")
    print("")

    result = solve_exact_plate(intent, menu, combos, quotes=qmap)
    print(render_result_card(result))
    print("")
    if intent.extras_policy == "disallow":
        print("── 换一种授权会怎样 ──────────────────")
        print("若明确允许“套餐附赠一杯饮料中杯”，实付 ¥33.50 即可满足 ¥35 预算。")
        print("这属于“方案差额”而非“无代价节省”，因此默认不替你做决定：")
        print("$ mcworth demo --extras-policy allow")
    return 0


# ---------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"mcworth {__version__} · 环境自检")
    print(f"python: {sys.version.split()[0]}")
    ok = True
    try:
        import pydantic

        print(f"pydantic: {pydantic.VERSION}")
    except ImportError:
        print("pydantic: 未安装（核心域类型依赖）")
        return 1
    for name in (
        "menu_store_1450713.json",
        "combo_9900005456.json",
        "quote_single_1440_4810.json",
        "quote_combo_9900005456.json",
    ):
        try:
            _load_resource(name)
            print(f"  [OK] 包资源 {name}")
        except (FileNotFoundError, ModuleNotFoundError):
            print(f"  [缺失] 包资源 {name}")
            ok = False
    from .domain.money import yuan_str_to_fen

    for s, expect in (("22.5", 2250), ("+ ¥1.5", 150), ("- ¥0.5", -50), ("¥13", 1300)):
        got = yuan_str_to_fen(s)
        mark = "OK" if got == expect else "FAIL"
        ok = ok and got == expect
        print(f"  [{mark}] 金额 {s!r} -> {got} 分（期望 {expect}）")
    print("诊断只返回能力结果，不打印凭证。")
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcworth", description="麦麦如意餐：保留想吃的，计算更省的买法"
    )
    parser.add_argument("--version", action="version", version=f"mcworth {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="需求 + 菜单/套餐 → 候选与待核价请求")
    p_plan.add_argument("--intent", required=True, help="需求 JSON（MealIntent 结构）")
    p_plan.add_argument("--menu", required=True, help="菜单响应 JSON（{_meta, response}）")
    p_plan.add_argument("--combos", help="套餐响应清单 JSON（{combos: [{_meta, response}]}）")
    p_plan.add_argument("-o", "--output", help="输出 plan.json 路径（缺省打印到 stdout）")

    p_ing = sub.add_parser("ingest-quotes", help="官方核价响应 → 上下文校验后的报价记录")
    p_ing.add_argument("--plan", required=True, help="plan.json")
    p_ing.add_argument("--responses", required=True, help="核价响应清单 JSON")
    p_ing.add_argument("-o", "--output", help="输出 quotes.json 路径（缺省打印到 stdout）")

    p_rep = sub.add_parser("report", help="需求 + 菜单/套餐 + 报价 → 方案卡")
    p_rep.add_argument("--intent", required=True)
    p_rep.add_argument("--menu", required=True)
    p_rep.add_argument("--combos", help="可选：套餐响应清单 JSON")
    p_rep.add_argument("--quotes", required=True, help="quotes.json（ingest-quotes 输出）")
    p_rep.add_argument("--json", help="可选：同时输出 JSON 摘要")

    p_demo = sub.add_parser("demo", help="实测快照求解演示")
    p_demo.add_argument(
        "--extras-policy",
        choices=("disallow", "allow"),
        default="disallow",
        help="是否明确授权接受“套餐附带的增量食物”（默认 disallow）",
    )
    sub.add_parser("doctor", help="环境自检")

    args = parser.parse_args(argv)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "ingest-quotes":
        return cmd_ingest_quotes(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "demo":
        return cmd_demo(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
