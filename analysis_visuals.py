"""Privacy boundary for charts rendered beside AI management analysis."""

from __future__ import annotations

from typing import Any

import pandas as pd

from analysis_context import (
    _clip_text,
    _is_sensitive_column,
    _redact_lineage_text,
    _safe_scalar,
    _sensitive_values,
    _unique_column_names,
)


def prepare_analysis_visual_data(
    frame: pd.DataFrame,
    question: object = "",
) -> tuple[pd.DataFrame, str]:
    """Return chart-safe data using the same redaction boundary as AI context."""
    if not isinstance(frame, pd.DataFrame):
        return pd.DataFrame(), _redact_lineage_text(question, [])

    sensitive_values = _sensitive_values(frame)
    safe_positions = [
        index
        for index, column in enumerate(frame.columns)
        if not _is_sensitive_column(column)
    ]
    safe_frame = frame.iloc[:, safe_positions].copy()
    safe_frame.columns = _unique_column_names(safe_frame.columns)

    def redact_visual_value(value: Any) -> Any:
        safe_value = _safe_scalar(value, sensitive=False)
        if safe_value is None:
            return None
        return _clip_text(_redact_lineage_text(safe_value, sensitive_values))

    for index in range(safe_frame.shape[1]):
        series = safe_frame.iloc[:, index]
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            safe_frame.isetitem(
                index,
                series.map(redact_visual_value),
            )

    safe_question = _clip_text(
        _redact_lineage_text(question, sensitive_values),
        500,
    )
    return safe_frame, safe_question
