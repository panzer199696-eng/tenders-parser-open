# -*- coding: utf-8 -*-
"""Условия предоплаты: три состояния, «не знаем» не выдаётся за «нет»."""

from .detector import (
    PREPAYMENT_NO,
    PREPAYMENT_UNKNOWN,
    PREPAYMENT_YES,
    describe,
    detect,
    sort_bonus,
)

__all__ = [
    "detect",
    "describe",
    "sort_bonus",
    "PREPAYMENT_YES",
    "PREPAYMENT_NO",
    "PREPAYMENT_UNKNOWN",
]
