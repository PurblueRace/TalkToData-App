"""Trusted, privacy-safe chart helpers for the management analysis report."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping
from typing import Any

import pandas as pd

from analysis_context import (
    _clip_text,
    _is_sensitive_column,
    _redact_lineage_text,
    _safe_scalar,
    _sensitive_sql_literals,
    _sensitive_values,
    _unique_column_names,
)
from visualization_utils import format_compact_value


_SCRIPT_END_RE = re.compile(r"</script", flags=re.IGNORECASE)


def _safe_visual_text(value: object, *, limit: int = 500) -> str:
    """Keep labels readable while preventing them from becoming HTML/script markup."""
    text = _clip_text(value, limit)
    return (
        text.replace("<", "＜")
        .replace(">", "＞")
        .replace("\u2028", " ")
        .replace("\u2029", " ")
    )


def prepare_analysis_visual_data(
    frame: pd.DataFrame,
    question: object = "",
    sql: object = "",
) -> tuple[pd.DataFrame, str]:
    """Return chart-safe data using the same privacy boundary as AI context."""
    if not isinstance(frame, pd.DataFrame):
        return pd.DataFrame(), _safe_visual_text(
            _redact_lineage_text(question, _sensitive_sql_literals(sql))
        )

    working = frame.copy()
    working.columns = _unique_column_names(working.columns)
    sensitive_values = [
        *_sensitive_values(working),
        *_sensitive_sql_literals(sql),
    ]
    safe_positions = [
        index
        for index, column in enumerate(working.columns)
        if not _is_sensitive_column(column)
    ]
    safe_frame = working.iloc[:, safe_positions].copy()
    safe_frame.columns = _unique_column_names(
        [_safe_visual_text(column, limit=120) for column in safe_frame.columns]
    )

    def redact_visual_value(value: Any) -> Any:
        safe_value = _safe_scalar(value, sensitive=False)
        if safe_value is None:
            return None
        return _safe_visual_text(
            _redact_lineage_text(safe_value, sensitive_values),
            limit=500,
        )

    for index in range(safe_frame.shape[1]):
        series = safe_frame.iloc[:, index]
        if (
            pd.api.types.is_object_dtype(series)
            or pd.api.types.is_string_dtype(series)
            or isinstance(series.dtype, pd.CategoricalDtype)
        ):
            safe_frame.isetitem(index, series.map(redact_visual_value))

    safe_question = _safe_visual_text(
        _redact_lineage_text(question, sensitive_values),
        limit=500,
    )
    return safe_frame.reset_index(drop=True), safe_question


def should_embed_management_chart(
    raw_content: object,
    report: Mapping[str, Any],
) -> bool:
    """Only enrich a successfully returned management report with a chart."""
    if not str(raw_content or "").strip():
        return False
    report_fragment = str(report.get("html_fragment") or "")
    return not any(
        marker in report_fragment
        for marker in (
            "AI가 분석 결과를 반환하지 않았습니다.",
            "분석 결과 형식 오류",
        )
    )


def _chart_values(values: Any) -> list[Any]:
    if values is None:
        return []
    if hasattr(values, "tolist"):
        values = values.tolist()
    try:
        return list(values)
    except TypeError:
        return []


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _axis_text(figure: Any, axis_name: str, field: str) -> str:
    try:
        axis = getattr(figure.layout, axis_name)
        value = axis.title.text if field == "title" else getattr(axis, field)
    except (AttributeError, TypeError):
        return ""
    return str(value or "").strip()


def summarize_management_chart(chart: Mapping[str, Any]) -> str:
    """Describe one grounded fact visible in the rendered chart."""
    figure = chart.get("figure")
    kind = str(chart.get("kind") or "").lower()
    note = str(chart.get("note") or "").strip()
    traces = list(getattr(figure, "data", []) or []) if figure is not None else []
    if not traces:
        return note or "차트에 표시할 값이 없습니다."

    if kind in {"composition", "donut"}:
        trace = traces[0]
        rows = [
            (label, number)
            for label, value in zip(
                _chart_values(getattr(trace, "labels", None)),
                _chart_values(getattr(trace, "values", None)),
            )
            if (number := _finite_number(value)) is not None
        ]
        if rows:
            top_label, top_value = max(rows, key=lambda row: row[1])
            total = sum(value for _, value in rows)
            share = top_value / total * 100 if total else 0
            return f"{top_label}이 표시 합계의 {share:,.1f}%로 가장 큰 비중을 차지합니다."

    if kind == "benchmark" and len(traces) >= 2:
        actual_trace, benchmark_trace = traces[:2]
        rows = []
        for category, actual, benchmark in zip(
            _chart_values(getattr(actual_trace, "y", None)),
            _chart_values(getattr(actual_trace, "x", None)),
            _chart_values(getattr(benchmark_trace, "x", None)),
        ):
            actual_number = _finite_number(actual)
            benchmark_number = _finite_number(benchmark)
            if actual_number is not None and benchmark_number is not None:
                rows.append((category, actual_number, benchmark_number))
        if rows:
            focus = max(rows, key=lambda row: abs(row[1] - row[2]))
            suffix = _axis_text(figure, "xaxis", "ticksuffix")
            actual_name = str(getattr(actual_trace, "name", None) or "실제값")
            benchmark_name = str(getattr(benchmark_trace, "name", None) or "기준값")
            return (
                f"차이가 가장 큰 항목은 {focus[0]}이며, {actual_name} "
                f"{format_compact_value(focus[1], actual_name, unit_override=suffix)}, "
                f"{benchmark_name} {format_compact_value(focus[2], benchmark_name, unit_override=suffix)}입니다."
            )

    if kind in {"correlation", "scatter"}:
        trace = traces[0]
        pairs = [
            (x_number, y_number)
            for x_value, y_value in zip(
                _chart_values(getattr(trace, "x", None)),
                _chart_values(getattr(trace, "y", None)),
            )
            if (x_number := _finite_number(x_value)) is not None
            and (y_number := _finite_number(y_value)) is not None
        ]
        if len(pairs) >= 2:
            coefficient = pd.Series([item[0] for item in pairs]).corr(
                pd.Series([item[1] for item in pairs])
            )
            if pd.notna(coefficient):
                direction = "같은" if coefficient >= 0 else "반대"
                return (
                    f"두 지표는 상관계수 {coefficient:.2f}로 {direction} 방향으로 움직였지만, "
                    "상관관계만으로 원인을 단정할 수는 없습니다."
                )

    change_candidates = []
    for trace in traces:
        if str(getattr(trace, "orientation", "") or "").lower() == "h":
            continue
        pairs = [
            (label, number)
            for label, value in zip(
                _chart_values(getattr(trace, "x", None)),
                _chart_values(getattr(trace, "y", None)),
            )
            if (number := _finite_number(value)) is not None
        ]
        if len(pairs) >= 2:
            first_label, first_value = pairs[0]
            last_label, last_value = pairs[-1]
            magnitude = (
                abs(last_value - first_value) / abs(first_value)
                if first_value
                else abs(last_value - first_value)
            )
            change_candidates.append(
                (
                    magnitude,
                    str(getattr(trace, "name", None) or "표시 지표"),
                    first_label,
                    first_value,
                    last_label,
                    last_value,
                )
            )
    if change_candidates:
        _, label, first_label, first_value, last_label, last_value = max(
            change_candidates,
            key=lambda item: item[0],
        )
        difference = last_value - first_value
        if difference == 0:
            change_text = "변동이 없었습니다"
        elif first_value:
            direction = "증가" if difference > 0 else "감소"
            change_text = f"{abs(difference / first_value) * 100:,.1f}% {direction}했습니다"
        else:
            direction = "증가" if difference > 0 else "감소"
            change_text = f"{direction}했습니다"
        suffix = _axis_text(figure, "yaxis", "ticksuffix")
        return (
            f"{label}은 {first_label} {format_compact_value(first_value, label, unit_override=suffix)}에서 "
            f"{last_label} {format_compact_value(last_value, label, unit_override=suffix)}로 {change_text}."
        )

    horizontal_trace = next(
        (
            trace
            for trace in traces
            if str(getattr(trace, "orientation", "") or "").lower() == "h"
        ),
        None,
    )
    if horizontal_trace is not None:
        rows = [
            (category, number)
            for category, value in zip(
                _chart_values(getattr(horizontal_trace, "y", None)),
                _chart_values(getattr(horizontal_trace, "x", None)),
            )
            if (number := _finite_number(value)) is not None
        ]
        if rows:
            top_category, top_value = max(rows, key=lambda row: row[1])
            metric = str(getattr(horizontal_trace, "name", None) or "표시 지표")
            suffix = _axis_text(figure, "xaxis", "ticksuffix")
            return (
                f"표시된 항목 중 {top_category}의 {metric}이 "
                f"{format_compact_value(top_value, metric, unit_override=suffix)}로 가장 큽니다."
            )

    return note or "저장된 표의 실제 값을 한눈에 비교할 수 있도록 정리했습니다."


def _script_safe_payload(value: Any) -> Any:
    """Neutralize closing-script sequences inside Plotly's data payload only."""
    if isinstance(value, str):
        return _SCRIPT_END_RE.sub(r"<\\/script", value)
    if isinstance(value, Mapping):
        return {key: _script_safe_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_script_safe_payload(item) for item in value]
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return _script_safe_payload(value.tolist())
        except (TypeError, ValueError):
            return value
    return value


def build_management_chart_html(
    chart: Mapping[str, Any],
    *,
    source_number: int,
    question: str,
    row_count: int,
) -> str:
    """Render one server-created Plotly chart as a trusted report section."""
    import plotly.graph_objects as go
    import plotly.io as pio

    figure = chart.get("figure")
    if figure is None:
        return ""

    safe_figure = go.Figure(_script_safe_payload(figure.to_plotly_json()))
    safe_figure.update_layout(
        height=410,
        autosize=True,
        margin={"l": 42, "r": 30, "t": 86, "b": 52},
    )
    chart_fragment = pio.to_html(
        safe_figure,
        full_html=False,
        include_plotlyjs="cdn",
        default_width="100%",
        default_height="410px",
        config={
            "displayModeBar": False,
            "displaylogo": False,
            "scrollZoom": False,
            "responsive": True,
        },
    )
    safe_question = html.escape(_clip_text(question, 180))
    explanation = html.escape(summarize_management_chart(chart))
    return (
        '<section class="report-section report-chart-section" data-analysis-chart="1">'
        '<h2 class="section-heading">핵심 흐름 한눈에 보기</h2>'
        f'<p class="report-chart-source">표{int(source_number)} · {safe_question} · 원본 {int(row_count):,}행</p>'
        f'<div class="report-chart-frame">{chart_fragment}</div>'
        f'<p class="report-chart-caption"><strong>차트 해석</strong> {explanation}</p>'
        '</section>'
    )
