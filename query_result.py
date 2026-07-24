"""Helpers for distinguishing query errors, empty results, and real data."""

from __future__ import annotations

import pandas as pd


def is_effectively_empty_result(frame: pd.DataFrame | None) -> bool:
    """Treat zero rows and an all-NULL aggregate row as no matching data."""
    if frame is None or frame.empty:
        return True
    return bool(frame.isna().to_numpy().all())
