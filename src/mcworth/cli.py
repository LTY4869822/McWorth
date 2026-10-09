"""McWorth 命令行入口。

子命令：
  demo    用实测快照（真实 MCP 响应留存）跑固定餐盘求解演示
  doctor  环境自检：依赖、快照、金额规则（不打印任何凭证）

演示模式的数据来源是官方 MCP 的实测快照（含 traceId 与采集时间），
不是实时调用；实时模式由宿主 Agent 调用官方工具后将结构化响应
交给本引擎（方案 5.1 方式 A）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .adapters.combo import parse_combo_detail
from .adapters.menu import parse_menu, snapshot_evidence
from .adapters.pricing import parse_quote, snapshot_quote_evidence
from .domain.money import fen_to_yuan
from .domain.types import (
    AtomRequirement,
    MealIntent,
    MenuSnapshot,
    OfferItem,
    VerifiedQuote,
    cart_hash,
)
from .evidence import render_result_card
from .solver import solve_exact_plate

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"

STORE_CODE = "1450713"
ORDER_TYPE = 1
BE_TYPE = 1


def _load(name: str) -> dict:
    path = FIXTURES / name
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_snapshot() -> tuple[MenuSnapshot, list[ComboConfig], dict[str, VerifiedQuote]]:
    menu_raw = _load("menu_store_1450713.json")
    meta = menu_raw["_meta"]
    menu = parse_menu(
        menu_raw["response"],
        snapshot_evidence(meta["trace_id"], meta["captured_at"]),
        store_code=meta["store_code"],
        order_type=meta["order_type"],
        be_type=meta["be_type"],
        completeness="partial",
    )
    combos: list[ComboConfig] = []
    for name, fname in [("麦辣鸡腿汉堡三件套", "combo_9900005456.json")]:
        raw = _load(fname)
        m = raw["_meta"]
        combos.append(
            parse_combo_detail(raw["response"], snapshot_evidence(m["trace_id"], m["captured_at"]))
        )
    quotes: dict[str, VerifiedQuote] = {}
    for fname in ("quote_single_1440_4810.json", "quote_combo_9900005456.json"):
        raw = _load(fname)
        m = raw["_meta"]
        quote = parse_quote(raw["response"], snapshot_quote_evidence(m["trace_id"], m["captured_at"]))
        # 以 cart_hash 为键登记：由 fixture 记录的购物车结构换算，与求解器口径完全一致
        offer = [OfferItem.from_canonical(o) for o in m["offer"]]
        quotes[cart_hash(offer)] = quote
    return menu, combos, quotes


def cmd_demo(args: argparse.Namespace) -> int:
    menu, combos, qmap = _load_snapshot()

    print("═" * 56)
    print("麦麦如意餐 McWorth · 固定餐盘求解演示")
    print("═" * 56)
    print(f"数据来源：麦当劳官方 MCP 实测快照（{menu.evidence.captured_at}）")
    print(f"门店：{STORE_CODE}（上海黄浦华旭国际大厦餐厅）· 到店自取")
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

    # 快照模式：报价以 cart_hash 精确对账（实时模式由宿主核价后同样按 cart_hash 传入）
    result = solve_exact_plate(intent, menu, combos, quotes=qmap)

    print(render_result_card(result))
    print("")
    if intent.extras_policy == "disallow":
        print("── 换一种授权会怎样 ──────────────────")
        print("若明确允许“套餐附赠一杯饮料中杯”，实付 ¥33.50 即可满足 ¥35 预算。")
        print("这属于“方案差额”而非“无代价节省”，因此默认不替你做决定：")
        print("$ mcworth demo --extras-policy allow")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"mcworth {__version__} · 环境自检")
    print(f"python: {sys.version.split()[0]}")
    try:
        import pydantic

        print(f"pydantic: {pydantic.VERSION}")
    except ImportError:
        print("pydantic: 未安装（可选，核心域类型依赖）")
        return 1
    checks = {
        "menu_store_1450713.json": FIXTURES / "menu_store_1450713.json",
        "combo_9900005456.json": FIXTURES / "combo_9900005456.json",
        "quote_single_1440_4810.json": FIXTURES / "quote_single_1440_4810.json",
        "quote_combo_9900005456.json": FIXTURES / "quote_combo_9900005456.json",
    }
    ok = True
    for name, path in checks.items():
        present = path.exists()
        ok = ok and present
        print(f"  [{'OK' if present else '缺失'}] {name}")
    from .domain.money import yuan_str_to_fen

    for s, expect in (("22.5", 2250), ("+ ¥1.5", 150), ("- ¥0.5", -50), ("¥13", 1300)):
        got = yuan_str_to_fen(s)
        mark = "OK" if got == expect else "FAIL"
        ok = ok and got == expect
        print(f"  [{mark}] 金额 {s!r} -> {got} 分（期望 {expect}）")
    print("诊断只返回能力结果，不打印凭证。")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcworth", description="麦麦如意餐：保留想吃的，计算更省的买法")
    parser.add_argument("--version", action="version", version=f"mcworth {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    p_demo = sub.add_parser("demo", help="实测快照求解演示")
    p_demo.add_argument(
        "--extras-policy",
        choices=("disallow", "allow"),
        default="disallow",
        help="是否明确授权接受“套餐附带的增量食物”（默认 disallow）",
    )
    sub.add_parser("doctor", help="环境自检")
    args = parser.parse_args(argv)
    if args.command == "demo":
        return cmd_demo(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
