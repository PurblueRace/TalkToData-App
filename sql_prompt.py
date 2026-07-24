"""Compact, schema-grounded prompt builder for Korean natural-language SQL."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


_PREFERRED_TABLE_ORDER = (
    "회계전표",
    "계정마스터",
    "거래처마스터",
    "부서마스터",
    "사원마스터",
    "프로젝트마스터",
    "제품마스터",
    "원재료마스터",
    "BOM마스터",
    "재공품마스터",
)

_RELATIONSHIPS = (
    ("회계전표", "계정코드", "계정마스터", "계정코드"),
    ("회계전표", "거래처코드", "거래처마스터", "거래처코드"),
    ("회계전표", "부서코드", "부서마스터", "부서코드"),
    ("회계전표", "프로젝트코드", "프로젝트마스터", "프로젝트코드"),
    ("회계전표", "제품코드", "제품마스터", "제품코드"),
    ("회계전표", "원재료코드", "원재료마스터", "원재료코드"),
    ("회계전표", "재공품코드", "재공품마스터", "재공품코드"),
    ("BOM마스터", "제품코드", "제품마스터", "제품코드"),
    ("BOM마스터", "원재료코드", "원재료마스터", "원재료코드"),
    ("BOM마스터", "대체원재료코드", "원재료마스터", "원재료코드"),
    ("재공품마스터", "제품코드", "제품마스터", "제품코드"),
    ("사원마스터", "부서코드", "부서마스터", "부서코드"),
    ("부서마스터", "상위부서코드", "부서마스터", "부서코드"),
    ("프로젝트마스터", "담당부서코드", "부서마스터", "부서코드"),
    ("원재료마스터", "공급업체코드", "거래처마스터", "거래처코드"),
)

_SYSTEM_RULES = """
너는 한국어 자연어를 데이터 조회용 SQL로 변환하는 컴파일러다.

[계약]
- 제공된 설계도에 있는 테이블·컬럼만 사용하고 식별자를 추측하지 않는다.
- 테이블명과 컬럼명은 항상 큰따옴표로 감싼다. 문자열 값은 작은따옴표를 사용한다.
- 선언된 관계로만 JOIN하고, 서로 다른 상세 수준을 먼저 집계해 행 증폭과 중복 합계를 막는다.
- 질문을 대상, 지표, 분류 차원, 기간, 조건, 정렬, 개수로 해석해 필요한 최소 테이블만 사용한다.
- 집계하지 않은 SELECT 항목은 GROUP BY에 포함하고, 나눗셈은 NULLIF로 0을 방지한다.
- 기간 조건은 시작일 이상·다음 종료일 미만의 반개구간을 우선하고, 단일 합계는 COALESCE로 NULL을 막는다.
- 코드가 결과에 나오면 가능한 경우 같은 마스터의 명칭도 바로 옆에 출력한다.
- 임의의 LIMIT를 붙이지 않는다. 사용자가 상위·하위·최근 N개를 요구한 경우에만 정렬 후 LIMIT를 쓴다.
- SELECT 또는 읽기 전용 WITH로 시작하는 한 문장만 만든다. 데이터 변경, 시스템 객체 조회, 주석은 금지한다.
- 설명, 마크다운, 백틱 없이 실행 가능한 SQL만 출력한다.
"""

_POSTGRES_RULES = """
- 방언은 PostgreSQL이다.
- 현재 날짜가 필요하면 제공된 오늘을 DATE 리터럴로 사용하고, 없을 때만 CURRENT_DATE를 사용한다. 연도는 EXTRACT, 월 단위는 DATE_TRUNC 또는 TO_CHAR를 사용한다.
- 부분 문자열 검색은 ILIKE를 사용한다. SQLite 전용 날짜 함수는 사용하지 않는다.
- 소수 자릿수 반올림은 ROUND((식)::numeric, 자릿수)로 처리한다.
"""

_SQLITE_RULES = """
- 방언은 SQLite다.
- 현재 날짜가 필요하면 제공된 오늘을 날짜 리터럴로 사용하고, 없을 때만 date('now')를 사용한다. 연도·월은 strftime을 사용한다.
- 부분 문자열 검색은 LIKE를 사용한다. PostgreSQL 전용 함수는 사용하지 않는다.
"""

_BUSINESS_SEMANTICS = """
- 회계전표는 분개 행이다. '거래 건수'는 COUNT(DISTINCT 전표번호), '행 수'만 COUNT(*)로 계산한다.
- 회계 지표 분류는 계정명 문자열이 아니라 계정마스터의 대분류·중분류로 판단한다.
- 매출은 대분류='수익' AND 중분류='매출액'인 행의 SUM(대변금액-차변금액)이고, 총수익은 대분류='수익' 전체의 같은 순액이다.
- 비용은 계정마스터 대분류='비용'인 행의 SUM(차변금액-대변금액)이다. 매출원가·판매비와관리비·영업외비용·법인세비용은 중분류로 구분한다.
- 영업이익은 매출액-매출원가-판매비와관리비, 순손익은 수익 순액-비용 순액이다.
- 잔액은 자산·비용 계정은 차변-대변, 부채·자본·수익 계정은 대변-차변으로 계산한다.
- 손익은 지정 기간의 흐름이고 재무상태 잔액은 종료일까지 누적한다. 기간을 말하지 않으면 제공된 전체 데이터 범위를 사용한다.
- 기간별 제품 실적 마진은 회계전표의 매출액과 매출원가를 제품별로 각각 먼저 집계해 매출-매출원가로 계산한다.
- 제품 BOM 단위원재료원가는 SUM(BOM 수량*(1+손실률)*원재료 단가)다. 제품마스터 표준원가, BOM 재료원가, 회계전표 실제 매출원가는 서로 구분한다.
- 판매수량은 매출액 계정의 회계전표 행에서만 집계해 다른 분개 행과 중복하지 않는다.
- 재고·가격·예산·직원·프로젝트 상태 같은 현재 속성은 해당 마스터에서 직접 조회한다.
- '최신 데이터'는 해당 날짜 컬럼의 MAX 값을 기준으로 하고, 오늘·이번 달 같은 달력 기간은 제공된 오늘을 기준으로 한다.
- 이름 일부를 말하면 부분 일치로 찾고, 명시한 코드·이름·기간·상태 조건은 빠뜨리지 않는다.
"""


def build_sql_system_prompt(*, postgres: bool) -> str:
    """Return the stable SQL contract plus one explicit dialect block."""
    dialect_rules = _POSTGRES_RULES if postgres else _SQLITE_RULES
    return f"{_SYSTEM_RULES.strip()}\n\n[방언]\n{dialect_rules.strip()}"


def _ordered_table_names(schema: Mapping[str, Sequence[str]]) -> list[str]:
    preferred = [name for name in _PREFERRED_TABLE_ORDER if name in schema]
    remaining = sorted(name for name in schema if name not in _PREFERRED_TABLE_ORDER)
    return preferred + remaining


def build_schema_blueprint(schema: Mapping[str, Sequence[str]]) -> str:
    """Render the live schema and only relationships that actually exist."""
    normalized: dict[str, list[str]] = {}
    for raw_table, raw_columns in schema.items():
        table = str(raw_table).strip()
        columns = [str(column).strip() for column in raw_columns if str(column).strip()]
        if table and columns:
            normalized[table] = columns

    if not normalized:
        raise ValueError("SQL 설계도에 사용할 테이블이 없습니다.")

    table_lines = [
        f"{table}({','.join(normalized[table])})"
        for table in _ordered_table_names(normalized)
    ]
    relation_lines = []
    for left_table, left_column, right_table, right_column in _RELATIONSHIPS:
        if (
            left_table in normalized
            and right_table in normalized
            and left_column in normalized[left_table]
            and right_column in normalized[right_table]
        ):
            relation_lines.append(
                f"{left_table}.{left_column}={right_table}.{right_column}"
            )

    sections = ["[테이블]", *table_lines]
    if relation_lines:
        sections.extend(("[관계]", "; ".join(relation_lines)))
    return "\n".join(sections)


def build_sql_user_prompt(
    question: str,
    schema: Mapping[str, Sequence[str]],
    *,
    postgres: bool,
    data_context: Mapping[str, str] | None = None,
) -> str:
    """Attach a compact live blueprint and semantic layer to a short user question."""
    clean_question = str(question).strip()
    if not clean_question:
        raise ValueError("SQL로 변환할 질문이 없습니다.")

    dialect = "PostgreSQL" if postgres else "SQLite"
    context_lines = []
    if data_context:
        context_lines = [
            f"{key}={value}"
            for key, value in data_context.items()
            if str(key).strip() and str(value).strip()
        ]
    context_block = ""
    if context_lines:
        context_block = f"\n[데이터 범위]\n{' | '.join(context_lines)}"
    return (
        f"[데이터베이스]\n방언={dialect}{context_block}\n"
        f"{build_schema_blueprint(schema)}\n\n"
        f"[업무 의미]\n{_BUSINESS_SEMANTICS.strip()}\n\n"
        f"[사용자 질문]\n{clean_question}"
    )
