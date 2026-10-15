"""McWorth 实战验证脚本（v0.2.1）

与 live_acceptance.py 的区别：本脚本是「以真实用户身份跑一次新场景」，
走的是**正式 CLI 命令**（plan / ingest-quotes / report 三个子命令 + 文件交接），
而不是直接调用内部函数。用来回答一个问题：这个 skill 在真实场景下到底有没有用。

用法：
    python tools/practice_scenario.py --store 1421057 --want "1120x1,4810x1" --budget 40
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_acceptance import McpClient, _load_endpoint  # noqa: E402

PYEXE = sys.executable
REPO = Path(__file__).resolve().parent.parent


def cli(*args: str) -> str:
    """调用正式 CLI（与用户手动敲的命令完全一致）。"""
    proc = subprocess.run(
        [PYEXE, "-m", "mcworth.cli", *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )
    if proc.returncode != 0:
        print(f"!! CLI 失败: mcworth.cli {' '.join(args)}", file=sys.stderr)
        print(proc.stdout[-2000:], file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(1)
    return proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, help="门店编码")
    ap.add_argument("--want", required=True, help='想要餐品，如 "1120x1,4810x1"（code x 数量）')
    ap.add_argument("--budget", type=float, required=True, help="预算（元）")
    ap.add_argument("--extras-policy", default="disallow", choices=["disallow", "allow"])
    ap.add_argument("--extra-combos", default="", help="额外强制纳入的套餐 code，逗号分隔")
    ap.add_argument("--unlock", default="", help="允许替换的餐品名（同时解锁），逗号分隔")
    ap.add_argument("--out", default="docs/practice")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    want = {}
    for part in args.want.split(","):
        code, _, qty = part.strip().partition("x")
        want[code.strip()] = int(qty or 1)

    url, token = _load_endpoint()
    client = McpClient(url, token)
    info = client.initialize()
    print(f"[1/6] MCP 握手成功：{info.get('serverInfo', {}).get('name')}")

    # ---- ① 实时菜单 ----
    meals = client.call_tool("query-meals", {"beType": 1, "orderType": 1, "storeCode": args.store})
    assert meals.get("success") is True, "query-meals 失败"
    menu_doc = {"_meta": {"tool": "query-meals", "store_code": args.store, "order_type": 1,
                          "be_type": 1, "trace_id": meals.get("traceId"), "captured_at": now_iso,
                          "completeness": "full", "source": "practice（在线实时采集）"},
                "response": meals}
    (out / "menu.json").write_text(json.dumps(menu_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    all_meals = meals["data"]["meals"]
    print(f"[2/6] 实时菜单：{len(all_meals)} 个商品，traceId={meals.get('traceId','')[:12]}…")
    for code in want:
        m = all_meals.get(code)
        if not m:
            raise SystemExit(f"菜单里没有商品 {code}")
        print(f"      想要：{m['name']}（{code}）¥{m['currentPrice']} × {want[code]}")

    # ---- ② 找相关套餐（优先名字包含目标餐品名；不足再按"套餐"后缀补）----
    want_names = [all_meals[c]["name"] for c in want]
    primary, fallback = [], []
    for code, m in all_meals.items():
        name = m["name"]
        if code in want:
            continue
        if any(wn in name for wn in want_names):
            primary.append(code)
        elif name.endswith(("套餐", "三件套", "四件套")):
            fallback.append(code)
    combo_codes = (primary + fallback)[:4]  # 控制核价次数
    forced = [c.strip() for c in args.extra_combos.split(",") if c.strip()]
    for c in forced:
        if c in all_meals and c not in combo_codes:
            combo_codes.append(c)
    print(f"[3/6] 候选套餐 {len(combo_codes)} 个：{[all_meals[c]['name'] for c in combo_codes]}")

    combos = []
    for code in combo_codes:
        raw = client.call_tool("query-meal-detail",
                               {"beType": 1, "code": code, "orderType": 1, "storeCode": args.store})
        if raw.get("success") is True:
            combos.append({"_meta": {"tool": "query-meal-detail", "trace_id": raw.get("traceId"),
                                     "captured_at": now_iso, "source": "practice（在线实时采集）"},
                           "response": raw})
    (out / "combos.json").write_text(json.dumps({"combos": combos}, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"      成功取得套餐结构 {len(combos)} 个")

    # ---- ③ 写 intent.json（用户意图，裸 JSON）----
    from mcworth.domain.types import MealIntent

    unlock = {n.strip() for n in args.unlock.split(",") if n.strip()}
    intent = MealIntent(
        session_id="practice-20261016", revision=1, mode="exact_plate",
        budget_fen=int(round(args.budget * 100)),
        required=[{"atom_code": c, "name": all_meals[c]["name"],
                   "quantity": want[c], "locked": all_meals[c]["name"] not in unlock}
                  for c in want],
        extras_policy=args.extras_policy, preference="save_money",
        substitutions_allowed=sorted(unlock),
    )
    p_intent = out / "intent.json"
    p_intent.write_text(json.dumps(intent.model_dump(mode="json"), ensure_ascii=False),
                        encoding="utf-8")

    # ---- ④ 真实 CLI: plan ----
    plan = json.loads(cli("plan", "--intent", str(p_intent), "--menu", str(out / "menu.json"),
                          "--combos", str(out / "combos.json")))
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    reqs = plan["quote_requests"]
    plan_context_key = plan["context"]["context_key"]
    print(f"[4/6] plan：候选 {len(plan.get('paths', plan.get('candidates', [])))} 条，"
          f"待核价请求 {len(reqs)} 条")

    # ---- ⑤ 逐条真调官方核价，写 responses.json ----
    responses = []
    for r in reqs:
        raw = client.call_tool("calculate-price", r["official_payload"])
        ok = raw.get("success") is True
        print(f"      calculate-price {r['request_id']}: success={ok} "
              f"price={raw.get('data', {}).get('price', '?')}分")
        responses.append({
            "request_id": r["request_id"], "session_id": r["session_id"],
            "intent_revision": r["intent_revision"], "context_key": plan_context_key,
            "cart_hash": r["cart_hash"], "source_mode": "live",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": raw.get("traceId"), "response": raw,
        })
    (out / "responses.json").write_text(json.dumps(responses, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    # ---- ⑥ 真实 CLI: ingest-quotes + report ----
    quotes = json.loads(cli("ingest-quotes", "--plan", str(out / "plan.json"),
                            "--responses", str(out / "responses.json")))
    (out / "quotes.json").write_text(json.dumps(quotes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[5/6] ingest-quotes：接受 {len(quotes.get('records', []))} 条，"
          f"拒绝 {len(quotes.get('errors', []))} 条")

    card = cli("report", "--intent", str(p_intent), "--menu", str(out / "menu.json"),
               "--combos", str(out / "combos.json"), "--quotes", str(out / "quotes.json"),
               "--json", str(out / "summary.json"))
    (out / "report_card.txt").write_text(card, encoding="utf-8")
    print("[6/6] report 输出：\n" + "─" * 60)
    print(card)
    print("─" * 60)
    print(f"证据已落盘 {out}/（intent/menu/combos/plan/responses/quotes/report_card/summary）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
