# -*- coding: utf-8 -*-
"""Единая база программы: схема и миграция со старых баз (фича 002)."""

from .schema import connect, ensure_schema, make_lot_key, schema_version
from .feed import BUCKETS, load_feed, load_lot
from .items import (
    load_lot_items,
    lot_items_by_key,
    mark_items_fetched,
    save_lot_items,
)
from .lots import rescore_lots, save_lots
from .reports import summary as reports_summary
from .migrate import migrate, MigrationReport

__all__ = [
    "connect",
    "ensure_schema",
    "make_lot_key",
    "schema_version",
    "migrate",
    "MigrationReport",
    "save_lots",
    "rescore_lots",
    "reports_summary",
    "load_feed",
    "load_lot",
    "BUCKETS",
    "save_lot_items",
    "load_lot_items",
    "lot_items_by_key",
    "mark_items_fetched",
]
