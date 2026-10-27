# -*- coding: utf-8 -*-
"""Реестр заявок: воронка работы с лотом по регламенту."""

from .registry import (
    ALLOWED_TRANSITIONS,
    STATUS_TITLES,
    Registry,
    TransitionError,
    next_step_map,
)

__all__ = [
    "Registry",
    "TransitionError",
    "STATUS_TITLES",
    "ALLOWED_TRANSITIONS",
    "next_step_map",
]
