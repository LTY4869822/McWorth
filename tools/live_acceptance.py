"""McWorth 真实 MCP 在线只读验收（总方案 15.4 / F02 的 live 链路验证）。

流程：initialize 握手 → query-meals 在线采菜单 → plan → calculate-price
逐候选实时核价 → ingest-quotes（source_mode=live）→ report。

安全边界：
- 只读工具（query-meals / calculate-price），不调用任何下单接口；
- Token 从本机 mcp.json 读取，只进请求头，不写入任何输出文件、日志或打印；
- 输出文件只含官方响应原文与 McWorth 结果，均不携带凭证。

用法：
    python tools/live_acceptance.py [--store 1450713] [--out docs/live-acceptance]

退出码：0 = 验收通过；非 0 = 有失败项。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

MCP_CONFIG = Path.home() / ".workbuddy" / "mcp.json"
DEFAULT_STORE = "1450713"


def _load_endpoint() -> tuple[str, str]:
    """从本机 MCP 配置读 URL 与 Token（Token 只进内存，不打印）。"""
    cfg = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    server = cfg["mcpServers"]["mcd-mcp"]
    auth = server.get("headers", {}).get("Authorization", "")
    token = auth.split(" ", 1)[-1] if auth else ""
    if not token:
        raise SystemExit("mcp.json 中 mcd-mcp 未配置 Token")
    return server["url"], token


class McpClient:
    """最小 streamablehttp 客户端：initialize → tools/call。"""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id: str | None = None
        self._next_id = 0

    def _post(self, payload: dict) -> dict:
        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        r = httpx.post(self.url, json=payload, headers=headers, timeout=60.0)
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        r.raise_for_status()
        body = r.text
        if body.startswith("event:") or "\ndata:" in body or body.startswith("data:"):
            # SSE 帧：取最后一条 data 行的 JSON
            for line in reversed(body.splitlines()):
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise RuntimeError(f"SSE 响应无 data 行: {body[:200]}")
        return r.json()

    def initialize(self) -> dict:
        self._next_id += 1
        resp = self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mcworth-acceptance", "version": "0.2.1"},
            },
        })
        # notifications 无需响应，但服务端要求发送
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        return resp.get("result", {})

    def call_tool(self, name: str, arguments: dict) -> dict:
        """调用工具并返回解析后的工具输出 JSON。

        服务端返回的是面向 LLM 的说明文本 + 内嵌的原始 JSON（非独立字段），
        用括号配平提取第一个完整 JSON 对象。
        """
        self._next_id += 1
        resp = self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if "error" in resp:
            raise RuntimeError(f"{name} 调用失败: {resp['error']}")
        result = resp.get("result", {})
        content = result.get("content") or []
        if not content:
            raise RuntimeError(f"{name} 返回空内容")
        text = content[0].get("text", "")
        return _extract_json(text, name)


def _extract_json(text: str, tool_name: str) -> dict:
    """从说明文本中提取真实的 API 响应 JSON。

    说明部分可能含占位大括号，而原始 JSON 总在文本末尾——
    从最后一个 `}` 反向做括号配平找开始位置。
    """
    end = text.rfind("}")
    if end < 0:
        raise RuntimeError(f"{tool_name} 输出中未找到 JSON: {text[:200]}")
    depth = 0
    in_str = False
    for i in range(end, -1, -1):
        ch = text[i]
        # 反向扫描时引号状态无法可靠判断转义，但 JSON 字符串中
        # 不会出现裸 `}`/`{` 与括号计数歧义的关键场景：引号内的
        # 花括号在正向时已被字符串态保护；反向近似按裸字符计数，
        # 若失败则回退正向全扫描。
        if ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                candidate = text[i:end + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    break
    # 回退：正向扫描所有配平块，取最后一个能完整解析的
    best: dict | None = None
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            break
        d = 0
        in_s = False
        esc = False
        for j in range(start, len(text)):
            c = text[j]
            if in_s:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_s = False
            elif c == '"':
                in_s = True
            elif c == "{":
                d += 1
            elif c == "}":
                d -= 1
                if d == 0:
                    try:
                        best = json.loads(text[start:j + 1])
                    except json.JSONDecodeError:
                        pass
                    i = j + 1
                    break
        else:
            break
    if best is None:
        raise RuntimeError(f"{tool_name} 输出中未能提取 JSON: {text[:200]}")
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--out", default="docs/live-acceptance")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    url, token = _load_endpoint()
    client = McpClient(url, token)
    info = client.initialize()
    print(f"[1/5] MCP 握手成功：server={info.get('serverInfo', {}).get('name')} "
          f"protocol={info.get('protocolVersion')}")

    # ---- 在线采菜单 ----
    meals_raw = client.call_tool("query-meals", {"beType": 1, "orderType": 1, "storeCode": args.store})
    ok = meals_raw.get("success") is True
    print(f"[2/5] query-meals：success={ok}（门店 {args.store}）")
    if not ok:
        failures.append("query-meals 未返回 success=true")
    menu_doc = {
        "_meta": {
            "tool": "query-meals", "store_code": args.store, "order_type": 1, "be_type": 1,
            "trace_id": meals_raw.get("traceId"), "captured_at": now_iso, "completeness": "full",
            "source": "live-acceptance（在线实时采集）",
        },
        "response": meals_raw,
    }
    (out_dir / "menu_live.json").write_text(
        json.dumps(menu_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- 快照套餐结构（选配结构一天内不变，回放可接受；核价必须 live）----
    combo_code = "9900005456"  # 麦辣鸡腿汉堡三件套
    combo_raw = client.call_tool("query-meal-detail", {
        "beType": 1, "code": combo_code, "orderType": 1, "storeCode": args.store,
    })
    ok = combo_raw.get("success") is True
    print(f"[3/5] query-meal-detail：success={ok}（套餐 {combo_code}）")
    if not ok:
        failures.append("query-meal-detail 未返回 success=true")
    combo_doc = {
        "_meta": {
            "tool": "query-meal-detail", "trace_id": combo_raw.get("traceId"),
            "captured_at": now_iso, "source": "live-acceptance（在线实时采集）",
        },
        "response": combo_raw,
    }
    (out_dir / f"combo_{combo_code}_live.json").write_text(
        json.dumps(combo_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- plan（内存执行，直接用 cmd 层函数）----
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from mcworth.adapters.combo import parse_combo_detail
    from mcworth.adapters.menu import parse_menu, snapshot_evidence
    from mcworth.cli import _load_combos, _load_menu
    from mcworth.domain.types import MealIntent
    from mcworth.solver import solve_exact_plate
    from mcworth.solver.exact_plate import validate_intent

    menu = _load_menu(menu_doc)
    combos = [parse_combo_detail(combo_doc["response"], snapshot_evidence(
        combo_doc["_meta"]["trace_id"], combo_doc["_meta"]["captured_at"]))]
    intent = MealIntent(
        session_id="live-acceptance", revision=1, mode="exact_plate",
        budget_fen=4000,
        required=[
            {"atom_code": "1440", "name": "麦辣鸡腿汉堡", "quantity": 1, "locked": True},
            {"atom_code": "4810", "name": "中薯条", "quantity": 1, "locked": True},
        ],
        extras_policy="allow",
        preference="save_money",
        allowed_extras=[{"code": "3050", "max_quantity": 1}],
    )
    problems = validate_intent(intent)
    result = solve_exact_plate(intent, menu, combos, quotes={})
    print(f"[4/5] plan：候选 {len(result.paths)} 个，合格待核价 "
          f"{len(result.eligible) + (1 if result.baseline else 0)} 个，阻塞项 {len(problems)}")

    # ---- 逐候选 live 核价 ----
    from mcworth.adapters.official import to_official_payload

    need: dict[str, object] = {}
    for p in result.eligible + ([result.baseline] if result.baseline else []):
        need.setdefault(p.cart_hash, p)
    live_responses = []
    for ch, p in need.items():
        payload = to_official_payload(p.offer, menu.store_code, menu.order_type, menu.be_type)
        price_raw = client.call_tool("calculate-price", payload)
        ok = price_raw.get("success") is True
        print(f"      calculate-price {p.label or p.combo_code}: success={ok} "
              f"price={price_raw.get('data', {}).get('price', '?')}")
        if not ok:
            failures.append(f"calculate-price（{ch[:24]}）未返回 success=true")
        live_responses.append({
            "request_id": f"qr-{len(live_responses)}",
            "session_id": intent.session_id,
            "intent_revision": intent.revision,
            "context_key": menu.context_key,
            "cart_hash": ch,
            "source_mode": "live",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": price_raw.get("traceId"),
            "official_payload": payload,
            "response": price_raw,
        })
    (out_dir / "responses_live.json").write_text(
        json.dumps(live_responses, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- ingest-quotes + report（走正式 CLI 命令，验证完整契约）----
    import tempfile
    from mcworth.cli import main as cli_main

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        plan_doc = {
            "schema_version": "1",
            "intent": intent.model_dump(mode="json"),
            "context": {
                "store_code": menu.store_code, "order_type": menu.order_type,
                "be_type": menu.be_type, "context_key": menu.context_key,
            },
            "quote_requests": [
                {k: r[k] for k in ("request_id", "session_id", "intent_revision", "cart_hash", "official_payload")}
                for r in live_responses
            ],
        }
        p_plan = td_path / "plan.json"
        p_plan.write_text(json.dumps(plan_doc, ensure_ascii=False), encoding="utf-8")
        p_resp = td_path / "responses.json"
        p_resp.write_text(
            json.dumps([{k: v for k, v in r.items() if k != "official_payload"} for r in live_responses],
                       ensure_ascii=False, encoding=None) if False else
            json.dumps([{k: v for k, v in r.items() if k != "official_payload"} for r in live_responses],
                       ensure_ascii=False),
            encoding="utf-8",
        )
        p_quotes = td_path / "quotes.json"
        rc1 = cli_main(["ingest-quotes", "--plan", str(p_plan), "--responses", str(p_resp), "-o", str(p_quotes)])
        quotes_doc = json.loads(p_quotes.read_text(encoding="utf-8"))
        print(f"[5/5] ingest-quotes：登记 {len(quotes_doc['records'])} 条，拒绝 {len(quotes_doc['errors'])} 条")
        if rc1 != 0 or not quotes_doc["records"]:
            failures.append("ingest-quotes 未通过或零记录")
        p_menu = out_dir / "menu_live.json"
        p_combo = out_dir / f"combo_{combo_code}_live.json"
        # combos 封装格式适配 CLI
        combos_doc = {"combos": [json.loads(p_combo.read_text(encoding="utf-8"))]}
        p_combos = td_path / "combos.json"
        p_combos.write_text(json.dumps(combos_doc, ensure_ascii=False), encoding="utf-8")
        p_intent = td_path / "intent.json"
        p_intent.write_text(json.dumps(intent.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")
        p_summary = out_dir / "summary_live.json"
        rc2 = cli_main([
            "report", "--intent", str(p_intent), "--menu", str(p_menu),
            "--combos", str(p_combos), "--quotes", str(p_quotes), "--json", str(p_summary),
        ])
        summary = json.loads(p_summary.read_text(encoding="utf-8"))
        if rc2 != 0 or summary.get("main") is None:
            failures.append(f"report 未产出主推荐（status={summary.get('status')}）")
        if summary.get("source_mode") != "live":
            failures.append(f"source_mode 应为 live，实际 {summary.get('source_mode')!r}")

    print("")
    print("═══ 验收结论 ═══")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"结果：{len(failures)} 项失败 → 证据在 {out_dir}/")
        return 1
    main_total = summary["main"]["total_fen"]
    print(f"  ✓ live 全链路通过：主推荐 ¥{main_total / 100:.2f}，"
          f"source_mode=live，基准 ¥{(summary.get('baseline_total_fen') or 0) / 100:.2f}")
    print(f"  证据落盘 {out_dir}/（menu/combo/responses/summary，均无凭证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
