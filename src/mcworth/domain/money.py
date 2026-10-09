"""金额处理：内部一律使用整数分；展示层转为元。

规则（对应开发方案 10.1）：
- 官方 calculate-price 返回的金额本身就是“分”（整数），直接使用。
- 菜单/差价等以元为单位的字符串，必须经 Decimal 定点转换，
  禁止浮点乘 100 后截断。
- 超过两位小数的金额视为数据异常，抛错而不是静默取整。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_YUAN_SYMBOLS = ("¥", "￥", "$")
_SIGN_PREFIX = ("+", "-")


class MoneyError(ValueError):
    """金额解析失败或精度非法。"""


def yuan_str_to_fen(value: str) -> int:
    """把 "22.5"、"+ ¥1.5"、"- ¥0.5" 这类字符串转为带符号的整数分。

    支持前导正负号（query-meal-detail 的 diffPrice 格式）与货币符号。
    """
    if value is None:
        raise MoneyError("金额为空")
    text = str(value).strip()
    sign = 1
    if text.startswith(_SIGN_PREFIX):
        if text[0] == "-":
            sign = -1
        text = text[1:].strip()
    for symbol in _YUAN_SYMBOLS:
        if text.startswith(symbol):
            text = text[len(symbol):].strip()
    text = text.replace(",", "")
    if not text:
        raise MoneyError(f"金额内容为空: {value!r}")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyError(f"金额格式非法: {value!r}") from exc
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise MoneyError(f"金额精度超过两位小数: {value!r}")
    return sign * int(cents.to_integral_value())


def fen_to_yuan(fen: int) -> str:
    """整数分转为展示用元字符串，保留两位小数。"""
    sign = "-" if fen < 0 else ""
    fen = abs(int(fen))
    return f"{sign}{fen // 100}.{fen % 100:02d}"


def assert_fen(value: int) -> int:
    """校验一个数值确实是整数分。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise MoneyError(f"期望整数分，得到 {type(value).__name__}: {value!r}")
    return value
