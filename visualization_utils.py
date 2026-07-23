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


def format_compact_value(value: object, column: object = "") -> str:
    if value is None or pd.isna(value):
        return "-"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    kind = unit_kind(column)
    suffix = unit_label(column)
    absolute = abs(number)

    if kind == "percent":
        return f"{number:,.1f}%"
    if kind == "currency":
        if absolute >= 100_000_000:
            return f"{number / 100_000_000:,.1f}억원"
        if absolute >= 10_000:
            return f"{number / 10_000:,.0f}만원"
        return f"{number:,.0f}원"
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
