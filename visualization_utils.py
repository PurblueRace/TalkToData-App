"""Data-role inference helpers for TalkToData visualizations."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Mapping

import pandas as pd


_IDENTIFIER_HINTS = (
    "코드",
    "번호",
    "순번",
    "사업자번호",
    "전화번호",
    "순위",
    "rank",
    "lot",
    " id",
    "id_",
    "_id",
    "index",
    "idx",
)
_TIME_HINTS = (
    "일자",
    "날짜",
    "년월",
    "연월",
    "기준월",
    "회계월",
    "생산월",
    "입고월",
    "출고월",
    "주차",
    "분기",
    "연도",
    "기간",
    "date",
    "month",
    "week",
    "quarter",
    "period",
)
_CATEGORY_HINTS = ("명", "이름", "구분", "유형", "상태", "카테고리", "부서", "거래처", "제품", "계정", "프로젝트", "지역")
_CURRENCY_HINTS = ("금액", "매출", "매입", "비용", "원가", "이익", "손익", "예산", "단가", "가격", "잔액", "차변", "대변", "급여", "연봉")
_PERCENT_HINTS = ("비율", "율", "률", "퍼센트", "percent", "percentage", "%")
_COUNT_HINTS = ("건수", "횟수", "수량", "재고", "인원", "직원수", "사원수", "count", "quantity")
_DURATION_HINTS = ("시간", "hour", "소요")
_EXPLICIT_CURRENCY_HINTS = (
    "금액",
    "매출액",
    "매입액",
    "비용",
    "원가",
    "이익",
    "손익",
    "예산",
    "단가",
    "가격",
    "잔액",
    "차변",
    "대변",
    "급여",
    "연봉",
)

VISUALIZATION_TYPE_LABELS = {
    "auto": "자동 추천",
    "bar": "막대 차트",
    "line": "선 차트",
    "donut": "도넛 차트",
    "scatter": "산점도",
    "distribution": "분포 차트",
}

MISSING_CATEGORY_LABEL = "미지정"

_NON_ADDITIVE_VISUAL_HINTS = (
    "가격",
    "단가",
    "평균",
    "비율",
    "진행률",
    "마진율",
    "점수",
    "표준원가",
    "잔액",
    "현재재고",
    "안전재고",
)
_ADDITIVE_VISUAL_HINTS = (
    "금액",
    "매출",
    "매입",
    "비용",
    "원가",
    "수익",
    "판관비",
    "예산",
    "이익",
    "수량",
    "건수",
    "급여",
    "연봉",
    "count",
    "quantity",
    "amount",
)
_DISCRETE_UNIT_LABELS = {
    "개",
    "건",
    "명",
    "ea",
    "pcs",
    "piece",
    "pieces",
    "case",
    "cases",
    "set",
    "sets",
}


@dataclass
class VisualProfile:
    frame: pd.DataFrame
    time_columns: list[str]
    measure_columns: list[str]
    category_columns: list[str]


def _normalized(value: object) -> str:
    return re.sub(r"\s+", "", str(value).strip().lower())


def is_identifier_column(column: object) -> bool:
    name = f" {_normalized(column)} "
    return any(hint in name for hint in _IDENTIFIER_HINTS)


def is_time_column(column: object) -> bool:
    name = _normalized(column)
    return name in {"월", "년", "year"} or any(hint in name for hint in _TIME_HINTS)


def unit_kind(column: object) -> str:
    name = _normalized(column)
    if any(hint in name for hint in _PERCENT_HINTS):
        return "percent"
    if any(hint in name for hint in _EXPLICIT_CURRENCY_HINTS):
        return "currency"
    if any(hint in name for hint in _COUNT_HINTS):
        return "count"
    if any(hint in name for hint in _DURATION_HINTS):
        return "duration"
    if any(hint in name for hint in _CURRENCY_HINTS):
        return "currency"
    return "number"


def unit_label(column: object) -> str:
    kind = unit_kind(column)
    if kind == "currency":
        return "원"
    if kind == "percent":
        return "%"
    if kind == "duration":
        return "시간"
    if kind == "count":
        name = _normalized(column)
        if any(hint in name for hint in ("인원", "직원수", "사원수")):
            return "명"
        if any(hint in name for hint in ("건수", "횟수", "count")):
            return "건"
        return "개"
    return ""


def format_compact_value(
    value: object,
    column: object = "",
    unit_override: str | None = None,
) -> str:
    if value is None or pd.isna(value):
        return "-"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    kind = unit_kind(column)
    suffix = unit_label(column) if unit_override is None else str(unit_override)
    absolute = abs(number)

    if kind == "percent":
        return f"{number:,.1f}%"
    if kind == "currency":
        if absolute >= 100_000_000:
            return f"{number / 100_000_000:,.1f}억원"
        if absolute >= 10_000:
            return f"{number / 10_000:,.0f}만원"
        return f"{number:,.0f}원"
    if kind == "count" and _normalized(suffix) not in _DISCRETE_UNIT_LABELS:
        formatted = f"{number:,.3f}".rstrip("0").rstrip(".")
        return f"{formatted}{suffix}"
    if kind in {"count", "duration"}:
        return f"{number:,.0f}{suffix}"
    if number.is_integer():
        return f"{number:,.0f}"
    return f"{number:,.1f}"


def _numeric_like(series: pd.Series) -> pd.Series | None:
    if pd.api.types.is_bool_dtype(series):
        return None
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")

    text = series.astype("string").str.replace(",", "", regex=False).str.replace("%", "", regex=False)
    converted = pd.to_numeric(text, errors="coerce")
    populated = int(series.notna().sum())
    if populated and int(converted.notna().sum()) / populated >= 0.85:
        return converted
    return None


def _relevance_score(column: str, question: str, hints: Iterable[str]) -> int:
    column_name = _normalized(column)
    question_text = _normalized(question)
    score = 0
    if column_name and column_name in question_text:
        score += 100
    if any(hint in column_name and hint in question_text for hint in hints):
        score += 30
    return score


def profile_dataframe(df: pd.DataFrame, question: str = "") -> VisualProfile:
    frame = df.copy()
    time_columns: list[str] = []
    measure_columns: list[str] = []
    category_columns: list[str] = []

    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]) or is_time_column(column):
            time_columns.append(str(column))

    for column in frame.columns:
        name = str(column)
        if name in time_columns or is_identifier_column(name):
            continue
        converted = _numeric_like(frame[column])
        if converted is not None and converted.notna().any():
            frame[column] = converted
            measure_columns.append(name)

    for column in frame.columns:
        name = str(column)
        if name in time_columns or name in measure_columns or is_identifier_column(name):
            continue
        values = frame[column].dropna()
        if values.empty:
            continue
        unique_count = int(values.nunique())
        has_category_hint = any(hint in _normalized(name) for hint in _CATEGORY_HINTS)
        if unique_count <= 50 or has_category_hint:
            category_columns.append(name)

    measure_hints = (*_CURRENCY_HINTS, *_PERCENT_HINTS, *_COUNT_HINTS, *_DURATION_HINTS)
    measure_columns.sort(
        key=lambda column: (
            -_relevance_score(column, question, measure_hints),
            list(df.columns).index(column),
        )
    )
    category_columns.sort(
        key=lambda column: (
            -_relevance_score(column, question, _CATEGORY_HINTS),
            -sum(hint in _normalized(column) for hint in _CATEGORY_HINTS),
            list(df.columns).index(column),
        )
    )
    time_columns.sort(
        key=lambda column: (
            -_relevance_score(column, question, _TIME_HINTS),
            list(df.columns).index(column),
        )
    )

    return VisualProfile(frame, time_columns, measure_columns, category_columns)


def is_additive_visual_metric(column: object) -> bool:
    """Return whether summing a metric produces a meaningful composition total."""
    name = _normalized(column)
    if any(hint in name for hint in _NON_ADDITIVE_VISUAL_HINTS):
        return False
    return any(hint in name for hint in _ADDITIVE_VISUAL_HINTS)


def visual_metric_aggregation(column: object) -> str:
    """Choose a safe aggregation for totals, rates, prices, and snapshots."""
    name = _normalized(column)
    if any(keyword in name for keyword in ("현재", "누적", "잔액", "재고")):
        return "last"
    if unit_kind(column) == "percent" or not is_additive_visual_metric(column):
        return "mean"
    return "sum"


def has_mixed_unit_values(profile: VisualProfile) -> bool:
    for column in profile.frame.columns:
        if _normalized(column) not in {"단위", "측정단위", "unit"}:
            continue
        if profile.frame[column].dropna().astype(str).nunique() > 1:
            return True
    return False


def profile_unit_label(profile: VisualProfile, column: object) -> str:
    """Use a table's explicit single unit for quantity and inventory measures."""
    if unit_kind(column) != "count" or unit_label(column) != "개":
        return unit_label(column)

    for unit_column in profile.frame.columns:
        if _normalized(unit_column) not in {"단위", "측정단위", "unit"}:
            continue
        values = (
            profile.frame[unit_column]
            .dropna()
            .astype(str)
            .str.strip()
        )
        values = values[values.ne("")].drop_duplicates()
        if len(values) == 1:
            return values.iloc[0]
    return unit_label(column)


def visual_number_format(column: object, unit_override: str | None = None) -> str:
    """Return a Plotly number format that preserves fractional physical quantities."""
    if unit_kind(column) == "percent":
        return ".1f"
    suffix = unit_label(column) if unit_override is None else str(unit_override)
    if unit_kind(column) == "count" and _normalized(suffix) not in _DISCRETE_UNIT_LABELS:
        return ",.3f"
    return ",.0f"


def available_visualization_types(df: pd.DataFrame, question: str = "") -> list[str]:
    """Return chart choices that are meaningful for the selected result table."""
    if df is None or df.empty:
        return []

    profile = profile_dataframe(df, question)
    choices = ["auto"]
    if len(profile.frame) <= 1 or not profile.measure_columns:
        return choices
    if has_mixed_unit_values(profile):
        return choices

    dimension = (
        profile.time_columns[0]
        if profile.time_columns
        else profile.category_columns[0]
        if profile.category_columns
        else None
    )
    primary = profile.measure_columns[0]
    if dimension:
        dimension_values = profile.frame[[dimension, primary]].replace(
            [float("inf"), float("-inf")], float("nan")
        )
        dimension_values = dimension_values.dropna(subset=[primary])
        if is_time_column(dimension):
            dimension_values = dimension_values.dropna(subset=[dimension])
        else:
            dimension_values[dimension] = dimension_values[dimension].fillna(
                MISSING_CATEGORY_LABEL
            )
    else:
        dimension_values = pd.DataFrame()
    if dimension and dimension_values[dimension].nunique() >= 2:
        choices.append("bar")
        if profile.time_columns:
            choices.append("line")

    if profile.category_columns:
        category = profile.category_columns[0]
        metric = profile.measure_columns[0]
        composition = profile.frame[[category, metric]].replace(
            [float("inf"), float("-inf")], float("nan")
        )
        composition = composition.dropna(subset=[metric])
        composition[category] = composition[category].fillna(MISSING_CATEGORY_LABEL)
        if not composition.empty:
            grouped = composition.groupby(category, dropna=False)[metric].sum(min_count=1)
            if (
                is_additive_visual_metric(metric)
                and visual_metric_aggregation(metric) == "sum"
                and unit_kind(metric) != "percent"
                and 2 <= len(grouped) <= 8
                and (grouped >= 0).all()
                and grouped.sum() > 0
            ):
                choices.append("donut")

    if len(profile.measure_columns) >= 2:
        pair = profile.frame[profile.measure_columns[:2]].replace(
            [float("inf"), float("-inf")], float("nan")
        ).dropna()
        if len(pair) >= 8 and all(pair[column].nunique() > 1 for column in pair.columns):
            choices.append("scatter")

    primary_values = profile.frame[profile.measure_columns[0]].replace(
        [float("inf"), float("-inf")], float("nan")
    ).dropna()
    if len(primary_values) >= 10 and primary_values.nunique() > 1:
        choices.append("distribution")
    return choices


def _chart_values(values: Any) -> list[Any]:
    if values is None:
        return []
    if hasattr(values, "tolist"):
        values = values.tolist()
    try:
        return list(values)
    except TypeError:
        return []


def _finite_chart_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _chart_axis_text(figure: Any, axis_name: str, field: str) -> str:
    try:
        axis = getattr(figure.layout, axis_name)
        if field == "title":
            value = axis.title.text
        else:
            value = getattr(axis, field)
    except (AttributeError, TypeError):
        return ""
    return str(value or "").strip()


def _chart_change_text(
    label: str,
    first_label: Any,
    first_value: float,
    last_label: Any,
    last_value: float,
    suffix: str,
) -> str:
    difference = last_value - first_value
    direction = "증가" if difference > 0 else "감소" if difference < 0 else "변동이 없었"
    if difference == 0:
        change = "변동이 없었습니다"
    elif first_value:
        change = f"{abs(difference / first_value) * 100:,.1f}% {direction}했습니다"
    else:
        change = f"{format_compact_value(abs(difference), label, unit_override=suffix)} {direction}했습니다"
    return (
        f"{label}은 {first_label} "
        f"{format_compact_value(first_value, label, unit_override=suffix)}에서 "
        f"{last_label} {format_compact_value(last_value, label, unit_override=suffix)}로 {change}."
    )


def build_management_chart_explanation(chart: Mapping[str, Any]) -> str:
    """Explain the exact values carried by the rendered Plotly figure."""
    figure = chart.get("figure")
    kind = str(chart.get("kind") or "").lower()
    note = str(chart.get("note") or "").strip()
    traces = list(getattr(figure, "data", []) or []) if figure is not None else []
    if not traces:
        return note or "표시할 수 있는 차트 값이 없습니다."

    y_suffix = _chart_axis_text(figure, "yaxis", "ticksuffix")
    x_suffix = _chart_axis_text(figure, "xaxis", "ticksuffix")

    if kind == "benchmark" and len(traces) >= 2:
        actual_trace, benchmark_trace = traces[:2]
        categories = _chart_values(getattr(actual_trace, "y", None))
        actual_values = _chart_values(getattr(actual_trace, "x", None))
        benchmark_values = _chart_values(getattr(benchmark_trace, "x", None))
        rows = []
        for category, actual, benchmark in zip(categories, actual_values, benchmark_values):
            actual_number = _finite_chart_number(actual)
            benchmark_number = _finite_chart_number(benchmark)
            if actual_number is not None and benchmark_number is not None:
                rows.append((category, actual_number, benchmark_number))
        if rows:
            shortage = "부족" in note
            alerts = [row for row in rows if row[1] < row[2]] if shortage else [row for row in rows if row[1] > row[2]]
            focus = max(rows, key=lambda row: abs(row[1] - row[2]))
            alert_label = "기준 미달" if shortage else "기준 초과"
            return (
                f"표시된 {len(rows):,}개 항목 중 {len(alerts):,}개가 {alert_label}입니다. "
                f"차이가 가장 큰 항목은 {focus[0]}이며 "
                f"{getattr(actual_trace, 'name', None) or '실제값'} "
                f"{format_compact_value(focus[1], str(getattr(actual_trace, 'name', '') or ''), unit_override=x_suffix)}, "
                f"{getattr(benchmark_trace, 'name', None) or '기준값'} "
                f"{format_compact_value(focus[2], str(getattr(benchmark_trace, 'name', '') or ''), unit_override=x_suffix)}입니다."
            )

    if kind in {"correlation", "scatter"}:
        trace = traces[0]
        if str(getattr(trace, "type", "")) == "heatmap":
            matrix = _chart_values(getattr(trace, "z", None))
            labels = _chart_values(getattr(trace, "x", None))
            strongest = None
            for row_index, row in enumerate(matrix):
                for column_index, value in enumerate(_chart_values(row)):
                    number = _finite_chart_number(value)
                    if row_index == column_index or number is None:
                        continue
                    if strongest is None or abs(number) > abs(strongest[2]):
                        strongest = (row_index, column_index, number)
            if strongest and strongest[0] < len(labels) and strongest[1] < len(labels):
                return (
                    f"가장 강한 관계는 {labels[strongest[0]]}과 {labels[strongest[1]]} 사이의 "
                    f"상관계수 {strongest[2]:.2f}입니다. 상관관계만으로 원인을 단정할 수는 없습니다."
                )
        pairs = []
        for x_value, y_value in zip(
            _chart_values(getattr(trace, "x", None)),
            _chart_values(getattr(trace, "y", None)),
        ):
            x_number = _finite_chart_number(x_value)
            y_number = _finite_chart_number(y_value)
            if x_number is not None and y_number is not None:
                pairs.append((x_number, y_number))
        if len(pairs) >= 2:
            coefficient = pd.Series([item[0] for item in pairs]).corr(
                pd.Series([item[1] for item in pairs])
            )
            if pd.notna(coefficient):
                strength = "강한" if abs(coefficient) >= 0.7 else "보통" if abs(coefficient) >= 0.4 else "약한"
                direction = "같은" if coefficient >= 0 else "반대"
                x_label = _chart_axis_text(figure, "xaxis", "title") or "가로축 지표"
                y_label = _chart_axis_text(figure, "yaxis", "title") or "세로축 지표"
                return (
                    f"{x_label}과 {y_label}의 상관계수는 {coefficient:.2f}로, "
                    f"{strength} 수준에서 {direction} 방향으로 움직였습니다. "
                    "상관관계만으로 원인을 단정할 수는 없습니다."
                )

    if kind == "distribution":
        trace = traces[0]
        values = [
            number
            for number in (
                _finite_chart_number(value)
                for value in _chart_values(getattr(trace, "x", None))
            )
            if number is not None
        ]
        if values:
            metric = (
                str(getattr(trace, "name", None) or "").strip()
                or _chart_axis_text(figure, "xaxis", "title")
                or "표시 지표"
            )
            series = pd.Series(values)
            return (
                f"{metric}의 중앙값은 {format_compact_value(series.median(), metric, unit_override=x_suffix)}이며, "
                f"최솟값 {format_compact_value(series.min(), metric, unit_override=x_suffix)}부터 "
                f"최댓값 {format_compact_value(series.max(), metric, unit_override=x_suffix)}까지 분포합니다."
            )

    if kind in {"composition", "donut"}:
        trace = traces[0]
        labels = _chart_values(getattr(trace, "labels", None))
        values = [_finite_chart_number(value) for value in _chart_values(getattr(trace, "values", None))]
        rows = [(label, value) for label, value in zip(labels, values) if value is not None]
        if rows:
            top_label, top_value = max(rows, key=lambda row: row[1])
            total = sum(value for _, value in rows)
            share = top_value / total * 100 if total else 0
            return f"{top_label}이 표시된 합계의 {share:,.1f}%로 가장 큰 비중을 차지합니다."

    change_candidates = []
    for trace in traces:
        if str(getattr(trace, "orientation", "") or "").lower() == "h":
            continue
        labels = _chart_values(getattr(trace, "x", None))
        values = _chart_values(getattr(trace, "y", None))
        pairs = []
        for label, value in zip(labels, values):
            number = _finite_chart_number(value)
            if number is not None:
                pairs.append((label, number))
        if len(pairs) >= 2:
            first_label, first_value = pairs[0]
            last_label, last_value = pairs[-1]
            relative_change = abs(last_value - first_value) / abs(first_value) if first_value else abs(last_value - first_value)
            change_candidates.append(
                (
                    relative_change,
                    str(getattr(trace, "name", None) or "표시 지표"),
                    first_label,
                    first_value,
                    last_label,
                    last_value,
                )
            )
    if change_candidates:
        _, label, first_label, first_value, last_label, last_value = max(change_candidates, key=lambda item: item[0])
        return _chart_change_text(label, first_label, first_value, last_label, last_value, y_suffix)

    horizontal_trace = next(
        (
            trace
            for trace in traces
            if str(getattr(trace, "orientation", "") or "").lower() == "h"
        ),
        None,
    )
    if horizontal_trace is not None:
        rows = []
        for category, value in zip(
            _chart_values(getattr(horizontal_trace, "y", None)),
            _chart_values(getattr(horizontal_trace, "x", None)),
        ):
            number = _finite_chart_number(value)
            if number is not None:
                rows.append((category, number))
        if rows:
            top_category, top_value = max(rows, key=lambda row: row[1])
            metric = str(getattr(horizontal_trace, "name", None) or "표시 지표")
            return (
                f"표시된 항목 중 {top_category}의 {metric}이 "
                f"{format_compact_value(top_value, metric, unit_override=x_suffix)}로 가장 큽니다."
            )

    return note or "차트에 표시된 값을 기준으로 핵심 흐름을 확인할 수 있습니다."


def time_sort_values(series: pd.Series) -> pd.Series:
    """Return sortable values for dates, Korean year-months, and quarter labels."""
    text = series.astype("string").str.strip()

    quarter = text.str.extract(
        r"(?P<year>\d{4}).*?(?:[Qq]\s*(?P<quarter_q>[1-4])|(?P<quarter_kr>[1-4])\s*분기)"
    )
    quarter_number = quarter["quarter_q"].fillna(quarter["quarter_kr"])
    quarter_values = pd.to_numeric(quarter["year"], errors="coerce") * 4 + pd.to_numeric(quarter_number, errors="coerce")
    if quarter_values.notna().mean() >= 0.6:
        return quarter_values

    korean_month = text.str.extract(r"(?P<year>\d{4})\s*년?.*?(?P<month>\d{1,2})\s*월")
    month_values = pd.to_numeric(korean_month["year"], errors="coerce") * 12 + pd.to_numeric(korean_month["month"], errors="coerce")
    if month_values.notna().mean() >= 0.6:
        return month_values

    parsed = pd.to_datetime(series, errors="coerce")
    if parsed.notna().mean() >= 0.6:
        return parsed

    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().mean() >= 0.6:
        return numeric
    return text


def is_composition_question(question: str) -> bool:
    text = _normalized(question)
    return any(keyword in text for keyword in ("비중", "구성", "점유", "비율"))


def is_correlation_question(question: str) -> bool:
    text = _normalized(question)
    return any(keyword in text for keyword in ("상관", "연관관계", "관계분석"))


def is_time_series_question(question: str) -> bool:
    text = _normalized(question)
    return any(
        keyword in text
        for keyword in (
            "추이",
            "월별",
            "일별",
            "주별",
            "주차별",
            "분기별",
            "연도별",
            "기간별",
            "시계열",
            "변화",
            "trend",
        )
    )
