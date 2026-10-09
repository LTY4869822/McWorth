"""solver：餐盘约束、购买路径枚举、候选多样性。"""

from .exact_plate import (
    SolveResult,
    SolveStatus,
    build_combo_paths,
    build_singles_path,
    solve_exact_plate,
)

__all__ = [
    "SolveResult", "SolveStatus", "build_combo_paths", "build_singles_path",
    "solve_exact_plate",
]
