"""价格证据与结果卡（方案 12 节；2026-10-10 优化方案 A7 修订）。

四种数字分别记账，不混淆：
- 已核价同餐盘差额：基准总额 − 优化总额（餐盘完全一致才有资格叫"省"）
- 方案差额：不同餐盘/履约的总额差
- 实际现金支出：官方确认支付的订单金额
- 已核价方案累计差额：多次配餐同口径之和

基准措辞（A7）：用户只描述餐品时没有提供原买法——
系统构造的全单品直购只能叫"对照基准（单品分别购买）"，
不能叫"您的原购买计划"；没有有效基准时不显示任何"省了多少"。
"""

from __future__ import annotations

from typing import Optional

from ..domain.money import fen_to_yuan
from ..domain.types import PathSolution
from ..solver.exact_plate import SolveResult, SolveStatus

_BASELINE_LABELS = {
    "user_provided": "与您的原购买方案相比",
    "constructed_singles": "与单品分别购买相比",
}

# v0.2.1（F02）：运行口径贯穿展示——回放/合成数据不得伪装成实时推荐。
_MODE_ANNOUNCE = {
    "live": None,
    "historical_replay": "⚠ 历史回放模式：以下金额来自官方快照回放，非当前实时核价",
    "synthetic": "⚠ 合成演示模式：以下金额为演示数据，非官方实时核价",
    "mixed": "⚠ 混合来源：结果同时包含实时与回放/演示报价，请以报价来源为准",
    "none": None,
}

_MODE_PRICE_LABEL = {
    "live": "官方整单核价",
    "historical_replay": "回放核价（历史快照）",
    "synthetic": "演示核价（合成数据）",
    "mixed": "核价（来源混合）",
    "none": "官方整单核价",
}


def same_plate_delta_fen(baseline: PathSolution, candidate: PathSolution) -> Optional[int]:
    """已核价同餐盘差额。餐盘不一致时返回 None（那是方案差额，不是节省）。"""
    if baseline.quote is None or candidate.quote is None:
        return None
    if baseline.plate_hash != candidate.plate_hash:
        return None
    return baseline.quote.total_fen - candidate.quote.total_fen


# v0.2.2（A8）：展示收敛。SKILL.md 第 4 节要求"最多展示三个有意义的选择"，
# 而实测中同一诉求一次列出了 7–9 行（含 ¥80 的明显无关组合、以及多行
# 实质完全相同的重复项）。这里按"同餐盘 → 增量 → 替换"分组，
# 组内先按复合轮廓去重、再按价格升序取前若干，主推荐必在展示之列。
DISPLAY_LIMIT = 3

_CLASS_ORDER = {"same_plate": 0, "extras": 1, "substitution": 2}

_CLASS_TAG = {
    "same_plate": "相同餐盘",
    "extras": "餐盘有增量",
    "substitution": "更换了餐品",
}


def _substance_key(p: PathSolution):
    """复合轮廓：同分类 + 同增量/缺失 + 同金额，视为实质重复的同一选择。"""
    return (
        p.classification,
        tuple(sorted((a.code, a.quantity) for a in p.extra_atoms)),
        tuple(sorted(p.missing_atoms)),
        p.quote.total_fen if p.quote else None,
    )


def select_display_paths(
    result: SolveResult, limit: int = DISPLAY_LIMIT
) -> tuple[list[PathSolution], int]:
    """选出要展示的已核价组合，返回 (展示列表, 被省略的数量)。

    省略不等于隐瞒：调用方必须把省略数量如实告知用户。
    """
    quoted = [p for p in result.paths if p.quote is not None]
    seen: set = set()
    ordered: list[PathSolution] = []
    for p in sorted(
        quoted, key=lambda p: (_CLASS_ORDER.get(p.classification, 9), p.quote.total_fen)
    ):
        key = _substance_key(p)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(p)

    chosen = ordered[:limit]
    main = result.main
    if main is not None and main.quote is not None and main not in chosen:
        # 主推荐必在展示之列：替换掉当前最贵的一条
        chosen = chosen[: max(limit - 1, 0)] + [main]
        chosen.sort(key=lambda p: (_CLASS_ORDER.get(p.classification, 9), p.quote.total_fen))
    # 省略数如实统计：既包含实质重复项，也包含同类更贵项
    hidden = len(quoted) - len(chosen)
    return chosen, hidden


def render_result_card(result: SolveResult, run_mode: str = "live") -> str:
    """渲染可复核结果卡（方案 12.3 的结构）。

    v0.2.1（F02）：run_mode（live / historical_replay / synthetic / mixed）
    决定金额标签与模式声明——回放与演示不会被渲染成实时推荐。
    """
    lines: list[str] = []
    announce = _MODE_ANNOUNCE.get(run_mode)
    price_label = _MODE_PRICE_LABEL.get(run_mode, "官方整单核价")
    if announce:
        lines.append(announce)
        lines.append("")
    intent = result.intent
    keep = "、".join(f"{req.name}×{req.quantity}" for req in intent.required)
    lines.append(f"保留：{keep}")
    lines.append(f"目标餐盘摘要：{result.target_plate_hash}")
    if result.blockers:
        lines.append("")
        lines.append("未满足的前置条件：")
        for b in result.blockers:
            lines.append(f"  · {b}")
    lines.append("")

    shown, hidden = select_display_paths(result)
    for p in shown:
        if p.quote is None:
            continue
        tag = _CLASS_TAG[p.classification]
        detail = f"{p.label or p.combo_code or '路径'} · {tag} · {price_label} ¥{fen_to_yuan(p.quote.total_fen)}"
        if p.classification == "extras" and p.extra_atoms:
            names = "、".join(f"{a.name}×{a.quantity}" for a in p.extra_atoms)
            detail += f"（多出：{names}）"
        if p.classification == "substitution" and p.missing_atoms:
            detail += f"（缺少：{'、'.join(p.missing_atoms)}）"
        lines.append(detail)
    if hidden > 0:
        lines.append(
            f"（另有 {hidden} 个已官方核价组合未逐条展示：含实质重复项与同类更贵项，"
            "全部候选与核价结果可在 plan.json / quotes.json 中复核）"
        )

    lines.append("")

    main = result.main
    if main is None:
        lines.append(f"求解状态：{result.status.value}")
        if result.status == SolveStatus.SEARCH_EXHAUSTED and intent.budget_fen is not None:
            sp = result.same_plate_min_fen
            lines.append(
                f"在本轮核价范围内，没有同时满足这些要求的方案（预算 ¥{fen_to_yuan(intent.budget_fen)}）。"
            )
            if sp is not None and sp > intent.budget_fen:
                lines.append(
                    f"保留当前餐盘的最低核价为 ¥{fen_to_yuan(sp)}；"
                    f"你可以把预算调整到 ¥{fen_to_yuan(sp)}，或允许更换餐品后重新查看。"
                )
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
    lines.append(f"{price_label}：¥{fen_to_yuan(quote.total_fen)}")
    lines.append(f"报价来源：{quote.evidence.label()}")

    base = result.baseline
    if base is not None and base.quote is not None:
        delta = same_plate_delta_fen(base, main)
        lines.append("")
        header = _BASELINE_LABELS.get(result.baseline_origin or "", "对照基准")
        lines.append(f"── {header} ────────────────")
        lines.append(f"对照基准（单品分别购买）核价：¥{fen_to_yuan(base.quote.total_fen)}")
        if delta is not None and delta > 0:
            lines.append(f"与单品分别购买相比：低 ¥{fen_to_yuan(delta)}（相同餐盘差额）")
        elif delta is not None:
            lines.append("与单品分别购买相比：没有更便宜的相同餐盘买法。")
        else:
            diff = base.quote.total_fen - quote.total_fen
            mark = "低" if diff > 0 else ("高" if diff < 0 else "持平")
            lines.append(
                f"与单品分别购买相比：{mark} ¥{fen_to_yuan(abs(diff))}"
                "（注意：餐盘不完全一致，这是方案差额，不是无代价节省）"
            )

    lines.append("")
    lines.append("── 范围与复核 ──────────────────────")
    lines.append("范围：当前门店 / 当前履约 / 当前账户可用权益")
    if quote.evidence.captured_at:
        lines.append(f"核价时间：{quote.evidence.captured_at}")
    if quote.evidence.trace_id:
        lines.append(f"traceId：{quote.evidence.trace_id}")
    lines.append(
        f"搜索说明：已比较 {len(result.paths)} 个购买组合"
        f"（其中 {len(result.quoted_eligible)} 个经官方核价且合格）"
    )
    return "\n".join(lines)
