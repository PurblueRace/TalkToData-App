"""Data-role inference helpers for TalkToData visualizations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

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
