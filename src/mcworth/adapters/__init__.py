"""adapters：官方 MCP schema → 内部类型。隔离 SDK/字段演进。"""

from .combo import cheapest_fill, default_fill, parse_combo_detail
from .menu import parse_menu, snapshot_evidence
from .pricing import parse_quote, snapshot_quote_evidence

__all__ = [
    "cheapest_fill", "default_fill", "parse_combo_detail",
    "parse_menu", "parse_quote", "snapshot_evidence", "snapshot_quote_evidence",
]
