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
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .adapters.combo import parse_combo_detail
from .adapters.menu import parse_menu, snapshot_evidence
from .adapters.official import to_official_payload
from .adapters.pricing import (
    parse_quote,
    quote_consistency_problems,
    snapshot_quote_evidence,
)
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

    def _offer_items(payload: dict) -> set[tuple[str, int]]:
        """官方请求的顶层（商品, 数量）集合——用于与响应 productList 对账。"""
        return sorted((str(i["productCode"]), int(i["quantity"])) for i in payload.get("items", []))  # type: ignore[return-value]

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, item in enumerate(responses):
        ch = item.get("cart_hash")
        if ch not in known:
            errors.append(f"响应 #{idx} 的 cart_hash 不在 plan 的待核价清单中，已跳过")
            continue
        req = known[ch]
        # F01（v0.2.1）：响应身份必须与任务一致——cart_hash 匹配不构成身份证明。
        # request_id / session_id / intent_revision / context_key 任一不符即拒绝，
        # 防止复制错文件、异步响应串线或宿主配对错误把别的报价当成本次报价。
        identity_fields = (
            ("request_id", item.get("request_id"), req.get("request_id")),
            ("session_id", item.get("session_id"), req.get("session_id")),
            ("intent_revision", item.get("intent_revision"), req.get("intent_revision")),
            ("context_key", item.get("context_key"), ctx),
        )
        mismatched = [name for name, got, want in identity_fields if got != want]
        if mismatched:
            errors.append(
                f"响应 #{idx} 身份不符（{', '.join(mismatched)} 与计划不一致），已拒绝——"
                "cart_hash 匹配不构成身份证明"
            )
            continue
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
        # F01：响应商品行与请求对账——返回的商品清单与请求的购物车不符时，
        # 这个金额不能记在这笔购物车头上。
        resp_items = sorted((i.product_code, i.quantity) for i in quote.items)
        want_items = _offer_items(req.get("official_payload") or {})
        if resp_items and want_items and resp_items != want_items:
            errors.append(
                f"响应 #{idx} 的商品清单与请求不符（响应 {resp_items} ≠ 请求 {want_items}），已拒绝"
            )
            continue
        # A6（v0.2.2）：金额自洽校验——身份匹配只说明"属于本任务"，
        # 不说明"这条报价内部自洽"。总额/商品价/明细/费用互相矛盾时，
        # 这个金额不得进入推荐（实测：把 price 改成 1 分曾能被当主推荐 ¥0.01）。
        inconsistency = quote_consistency_problems(quote)
        if inconsistency:
            errors.append(
                f"响应 #{idx} 金额自相矛盾（{'；'.join(inconsistency)}），已拒绝——"
                "进入推荐的金额必须能由官方响应字段互相解释"
            )
            continue
        quote.context_key = ctx
        records.append(
            QuoteRecord(
                request_id=req["request_id"],
                context_key=ctx,
                cart_hash=ch,
                # F03：抄录生成该请求的任务与需求版本，report 据此判断适用性
                session_id=req.get("session_id", ""),
                intent_revision=int(req.get("intent_revision", 0)),
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
        "schema_version": "2",
        "context_key": ctx,
        "records": records,
        "errors": errors,
        "notes": [
            "live 报价仅代表采集时刻的官方核价，下单前请以最终订单确认为准。",
            "缺失的费用组件为 null（未知），不代表为 0。",
            "records 已绑定 session_id / intent_revision；需求变更后旧报价不会自动适用于新推荐。",
        ],
    }
    if args.output:
        _write_json(Path(args.output), out)
        print(f"已登记 {len(records)} 条报价（{len(errors)} 个问题）→ {args.output}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


# ---------------------------------------------------------------- report

# v0.2.1（F02）：live 报价的新鲜度阈值（小时）。这是本地复查策略，
# 不是官方价格有效期保证。到期不删除记录，只不再用于主推荐。
QUOTE_FRESHNESS_HOURS = 6


def _quote_is_fresh(captured_at: str | None) -> bool:
    """live 报价时效检查：无时间戳 / 无法解析 / 未来时间异常 / 超阈值 → 不新鲜。"""
    if not captured_at:
        return False
    try:
        dt = datetime.fromisoformat(captured_at)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if dt > now + timedelta(hours=1):   # 未来时间异常
        return False
    return (now - dt) <= timedelta(hours=QUOTE_FRESHNESS_HOURS)


def cmd_report(args: argparse.Namespace) -> int:
    intent = MealIntent.model_validate(_read_json(Path(args.intent)))
    menu = _load_menu(_read_json(Path(args.menu)))
    combos = _load_combos(_read_json(Path(args.combos))) if args.combos else []
    quotes_raw = _read_json(Path(args.quotes))

    quotes: dict[str, VerifiedQuote] = {}
    rejected: list[str] = []
    for r in quotes_raw.get("records") or []:
        rec = QuoteRecord.model_validate(r)
        if rec.quote is None:
            continue
        # F03（v0.2.1）：报价必须属于当前会话与需求版本。
        # 空值 = 旧格式记录未携带版本信息，按不适用处理，宁可重新核价。
        if rec.session_id and rec.session_id != intent.session_id:
            rejected.append(
                f"报价 {rec.cart_hash[:24]} 来自旧会话 {rec.session_id!r}（当前 {intent.session_id!r}），不适用"
            )
            continue
        if rec.intent_revision and rec.intent_revision != intent.revision:
            rejected.append(
                f"报价 {rec.cart_hash[:24]} 来自旧版本 revision={rec.intent_revision}（当前 {intent.revision}），不适用"
            )
            continue
        # F02：live 报价过时效 → 保留记录但不再支撑当前推荐
        if rec.source_mode == "live" and not _quote_is_fresh(rec.captured_at):
            rejected.append(
                f"报价 {rec.cart_hash[:24]} 已超过 {QUOTE_FRESHNESS_HOURS} 小时新鲜度阈值"
                f"（采集于 {rec.captured_at}），需重新核价后才能用于推荐"
            )
            continue
        quote = VerifiedQuote.model_validate(rec.quote)
        quote.context_key = rec.context_key
        quotes[rec.cart_hash] = quote

    result = solve_exact_plate(intent, menu, combos, quotes)
    # F02：贯穿展示与 JSON 的运行口径——不能让回放/合成数据伪装成实时推荐
    used_modes = {rec.source_mode for rec in map(QuoteRecord.model_validate, quotes_raw.get("records") or [])
                  if rec.cart_hash in quotes}
    if len(used_modes) > 1:
        run_mode = "mixed"
    elif used_modes:
        run_mode = next(iter(used_modes))
    else:
        run_mode = "none"
    print(render_result_card(result, run_mode=run_mode))
    if rejected:
        print(f"\n⚠ {len(rejected)} 条报价未通过适用性检查：")
        for msg in rejected:
            print(f"  - {msg}")
    if args.json:
        summary = {
            "status": result.status.value,
            "source_mode": run_mode,
            "freshness_hours": QUOTE_FRESHNESS_HOURS,
            "blockers": result.blockers,
            "rejected_quotes": rejected,
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
    # v0.2.1（F04）：可选能力单独检查——缺失不影响核心功能
    try:
        import playwright  # noqa: F401

        print("  [OK] 图片导出（playwright 已安装）")
    except ImportError:
        print("  [--] 图片导出未启用（可选：pip install playwright && playwright install chromium）")
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
