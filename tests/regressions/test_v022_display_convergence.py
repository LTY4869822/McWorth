"""v0.2.2 展示收敛回归（来源：2026-10-16 实战自测发现 D3）。

SKILL.md 第 4 节要求"最多展示三个有意义的选择"，实测同一诉求一次列出
7–9 行（含明显无关的 ¥80 组合，以及多行实质完全相同的重复项）。
本组锁定两条规则：实质重复必须合并；展示条数必须收敛且主推荐必在列。
"""
from __future__ import annotations

from mcworth.domain.types import (
    EvidenceRef,
    EvidenceSource,
    FoodAtom,
    OfferItem,
    PathSolution,
    VerifiedQuote,
)
from mcworth.evidence.report import DISPLAY_LIMIT, select_display_paths


def _quote(fen: int) -> VerifiedQuote:
    return VerifiedQuote(
        total_fen=fen, product_price_fen=fen, original_total_fen=fen,
        evidence=EvidenceRef(source=EvidenceSource.OFFICIAL, tool="calculate-price",
                             captured_at="2026-10-16T00:00:00+08:00", trace_id="t"),
    )


def _path(fen: int, *, kind: str = "extras", extras: tuple[str, ...] = ("饮品",),
          label: str = "combo-default") -> PathSolution:
    return PathSolution(
        intent_revision=1,
        offer=[OfferItem(product_code=f"P{fen}", quantity=1)],
        resulting_plate=[FoodAtom(code="A", name="A", quantity=1)],
        classification=kind,  # type: ignore[arg-type]
        extra_atoms=[FoodAtom(code=c, name=c, quantity=1) for c in extras],
        quote=_quote(fen),
        label=label,
    )


class _StubResult:
    """展示层只读 paths / main（不经过求解器，避免分类被重算）。"""

    def __init__(self, paths: list[PathSolution], main: PathSolution | None) -> None:
        self.paths = paths
        self.main = main


def _result(paths: list[PathSolution], main: PathSolution | None) -> _StubResult:
    return _StubResult(paths, main)


def test_substance_duplicates_are_merged():
    """两个金额、增量、缺失完全相同的路径是同一个选择，只应出现一次。"""
    a, b = _path(2890), _path(2890)
    shown, hidden = select_display_paths(_result([a, b], a))
    assert len(shown) == 1 and hidden == 1


def test_display_is_capped_and_main_is_always_included():
    paths = [_path(f) for f in (8000, 5700, 4800, 3400, 2890)]
    main = paths[-1]
    shown, hidden = select_display_paths(_result(paths, main))
    assert len(shown) <= DISPLAY_LIMIT
    assert main in shown, "主推荐不得因为收敛而被省略"
    assert hidden == len(paths) - len(shown) > 0


def test_cheapest_of_each_class_is_preferred():
    """同餐盘优于增量优于替换，组内取更省者（候选超过上限时才发生取舍）。"""
    same = _path(3700, kind="same_plate", extras=(), label="baseline")
    extra_cheap = _path(2890, kind="extras", extras=("红茶",))
    extra_mid = _path(3400, kind="extras", extras=("可乐",))
    extra_dear = _path(4800, kind="extras", extras=("雪碧",))
    shown, hidden = select_display_paths(
        _result([extra_dear, extra_mid, extra_cheap, same], extra_cheap)
    )
    assert len(shown) == DISPLAY_LIMIT and hidden == 1
    assert shown == [same, extra_cheap, extra_mid], "同餐盘优先，增量组内按价格升序"
    assert extra_dear not in shown, "被收敛掉的应是最贵且优先级最低的那条"
