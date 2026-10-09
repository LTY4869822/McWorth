"""domain：内部协议类型。所有官方字段经 adapters 层映射后进入这里。"""

from .money import MoneyError, assert_fen, fen_to_yuan, yuan_str_to_fen
from .types import (
    AtomRequirement,
    ComboChoice,
    ComboConfig,
    ComboRound,
    EvidenceRef,
    EvidenceSource,
    FoodAtom,
    MealInfo,
    MealIntent,
    MenuSnapshot,
    ModificationValue,
    OfferItem,
    OfferRound,
    OfferRoundItem,
    PathSolution,
    QuoteItem,
    VerifiedQuote,
    WithOrderRef,
    cart_hash,
    decision_hash,
    plate_hash,
)

__all__ = [
    "MoneyError", "assert_fen", "fen_to_yuan", "yuan_str_to_fen",
    "AtomRequirement", "ComboChoice", "ComboConfig", "ComboRound",
    "EvidenceRef", "EvidenceSource", "FoodAtom", "MealInfo", "MealIntent",
    "MenuSnapshot", "ModificationValue", "OfferItem", "OfferRound",
    "OfferRoundItem", "PathSolution", "QuoteItem", "VerifiedQuote",
    "WithOrderRef", "cart_hash", "decision_hash", "plate_hash",
]
