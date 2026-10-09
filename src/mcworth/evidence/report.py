"""价格证据与结果卡（方案 12 节）。

四种数字分别记账，不混淆：
- 已核价同餐盘差额：基准总额 − 优化总额（餐盘完全一致才有资格叫“省”）
- 方案差额：不同餐盘/履约的总额差
- 实际现金支出：官方确认支付的订单金额
- 已核价方案累计差额：多次配餐同口径之和

没有基准不报“为你省了多少”；报价不可用不冒充实付。
"""

from __future__ import annotations

from typing import Optional

from ..domain.money import fen_to_yuan
from ..domain.types import PathSolution, VerifiedQuote
from ..solver.exact_plate import SolveResult, SolveStatus


def same_plate_delta_fen(baseline: PathSolution, candidate: PathSolution) -> Optional[int]:
    """已核价同餐盘差额。餐盘不一致时返回 None（那是方案差额，不是节省）。"""
    if baseline.quote is None or candidate.quote is None:
        return None
    if baseline.plate_hash != candidate.plate_hash:
        return None
    return baseline.quote.total_fen - candidate.quote.total_fen


def render_result_card(result: SolveResult) -> str:
    """渲染可复核结果卡（方案 12.3 的结构）。"""
    lines: list[str] = []
    intent = result.intent
    keep = "、".join(
        f"{req.name}×{req.quantity}" for req in intent.required
    )
    lines.append(f"保留：{keep}")
    lines.append(f"目标餐盘摘要：{result.target_plate_hash}")
    lines.append("")

    for p in result.paths:
        if p.quote is None:
            continue
        tag = {
            "same_plate": "相同餐盘",
            "extras": "餐盘有增量",
            "substitution": "更换了餐品",
        }[p.classification]
        detail = f"{p.label or p.combo_code or '路径'} · {tag} · 官方核价 ¥{fen_to_yuan(p.quote.total_fen)}"
        if p.classification == "extras" and p.extra_atoms:
            names = "、".join(f"{a.name}×{a.quantity}" for a in p.extra_atoms)
            detail += f"（多出：{names}）"
        if p.classification == "substitution" and p.missing_atoms:
            detail += f"（缺少：{'、'.join(p.missing_atoms)}）"
        lines.append(detail)

    lines.append("")

    main = result.main
    if main is None:
        lines.append(f"求解状态：{result.status.value}")
        if result.status == SolveStatus.SEARCH_EXHAUSTED and intent.budget_fen is not None:
            sp = result.same_plate_min_fen
            lines.append(f"在本轮核价范围内，没有同时满足这些要求的方案（预算 ¥{fen_to_yuan(intent.budget_fen)}）。")
            if sp is not None and sp > intent.budget_fen:
                lines.append(
                    f"保留当前餐盘的最低核价为 ¥{fen_to_yuan(sp)}；"
                    f"你可以把预算调整到 ¥{fen_to_yuan(sp)}，或允许更换餐品后重新查看。"
                )
            return "\n".join(lines)
        return "\n".join(lines)

    quote = main.quote
    assert quote is not None
    lines.append(f"求解状态：{result.status.value}（主推荐）")
    lines.append("")
    lines.append("── 主方案 ──────────────────────────")
    if main.classification == "same_plate":
        lines.append("调整：仅购买组合变化，餐品与规格未变化")
    else:
        lines.append(f"调整：{main.classification}（增量/替换见上）")
    lines.append(f"官方整单核价：¥{fen_to_yuan(quote.total_fen)}")
    lines.append(f"报价来源：{quote.evidence.label()}")

    base = result.baseline
    if base is not None and base.quote is not None:
        delta = same_plate_delta_fen(base, main)
        lines.append("")
        lines.append("── 与原购买计划比较 ────────────────")
        lines.append(f"原计划（单品直购）核价：¥{fen_to_yuan(base.quote.total_fen)}")
        if delta is not None and delta > 0:
            lines.append(f"与原购买组合相比：低 ¥{fen_to_yuan(delta)}（相同餐盘差额）")
        elif delta is not None:
            lines.append("与原购买组合相比：没有更便宜的相同餐盘买法。")
        else:
            diff = base.quote.total_fen - quote.total_fen
            mark = "低" if diff > 0 else ("高" if diff < 0 else "持平")
            lines.append(
                f"与原购买组合相比：{mark} ¥{fen_to_yuan(abs(diff))}"
                "（注意：餐盘不完全一致，这是方案差额，不是无代价节省）"
            )

    lines.append("")
    lines.append("── 范围与复核 ──────────────────────")
    lines.append("范围：当前门店 / 当前履约 / 当前账户可用权益")
    if quote.evidence.captured_at:
        lines.append(f"核价时间：{quote.evidence.captured_at}")
    if quote.evidence.trace_id:
        lines.append(f"traceId：{quote.evidence.trace_id}")
    lines.append(f"搜索说明：已比较 {len(result.paths)} 个购买组合（其中 {len(result.quoted_eligible)} 个经官方核价）")
    return "\n".join(lines)
