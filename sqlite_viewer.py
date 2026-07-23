"""
TalkToData - AI 기반 대화형 SQL 쿼리 시스템
Wireframer/Framer 스타일 대시보드
"""

import streamlit as st
import pandas as pd
import os
import re
import json
import time
import io
import textwrap
import html
import hmac
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from pathlib import Path

from persistent_db import (
    PersistenceError,
    backend_label,
    bootstrap_metadata_from_local,
    drop_table as db_drop_table,
    get_schema as db_get_schema,
    hash_password,
    initialize_metadata,
    is_remote_database,
    list_tables as db_list_tables,
    load_remote_tables,
    ping_database,
    quote_identifier,
    read_dataframe as db_read_dataframe,
    remote_authenticate_user,
    remote_create_user,
    replace_dataframes as db_replace_dataframes,
    save_remote_tables,
    table_columns as db_table_columns,
    table_exists as db_table_exists,
    verify_password,
    write_dataframe as db_write_dataframe,
)

# 정규화 스크립트 임포트
try:
    from normalize_db import normalize_database
except ImportError:
    normalize_database = None

# Config Manager 임포트
try:
    from config_manager import (
        load_config, save_config, auto_detect_columns,
        update_required_columns, get_required_columns,
        get_managed_tables, add_managed_table, update_managed_tables,
        clear_managed_tables, get_column_keywords
    )
except ImportError:
    st.error("⚠️ config_manager.py를 찾을 수 없습니다!")
    load_config = None
    save_config = None
    auto_detect_columns = None
    update_required_columns = None
    get_required_columns = None
    get_managed_tables = None
    add_managed_table = None
    update_managed_tables = None
    clear_managed_tables = None
    get_column_keywords = None

# OpenAI API 임포트
from openai import OpenAI

# Plotly 시각화 라이브러리 임포트
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ============================================
# 🧰 OpenAI 에러 포맷팅 유틸 (400/401 원인 확인용)
# ============================================
def _format_openai_exception(e: Exception) -> str:
    """
    openai>=1.x 예외 객체(APIStatusError/BadRequestError 등)를 최대한 안전하게 사람이 읽기 좋게 문자열로 변환
    """
    status = getattr(e, "status_code", None) or getattr(e, "status", None)
    body = getattr(e, "body", None)

    if isinstance(body, dict):
        err = body.get("error") or {}
        msg = err.get("message") or str(e)
        code = err.get("code")
        typ = err.get("type")
        parts = []
        if status:
            parts.append(f"HTTP {status}")
        if code:
            parts.append(f"code={code}")
        if typ:
            parts.append(f"type={typ}")
        meta = f" ({', '.join(parts)})" if parts else ""
        return f"{msg}{meta}"

    if status:
        return f"HTTP {status} - {str(e)}"
    return str(e)

# ============================================
# 🔑 OpenAI API 키 로딩
# 우선순위: Streamlit Secrets → 환경변수(OPENAI_API_KEY) → 안내 후 중단
# (로컬 실행 시 secrets.toml이 없으면 StreamlitSecretNotFoundError가 발생할 수 있음)
# ============================================
OPENAI_API_KEY = None
try:
    # secrets 파일 자체가 없을 때도 예외가 날 수 있어 안전하게 처리
    if hasattr(st, "secrets"):
        OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY")
except Exception:
    OPENAI_API_KEY = None

if not OPENAI_API_KEY:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    st.error("⚠️ OpenAI API 키를 찾을 수 없습니다!")
    st.info("아래 둘 중 하나로 설정하세요.")
    st.markdown("""
**방법 A) 로컬에서 `.streamlit/secrets.toml` 만들기**

프로젝트 루트에 `.streamlit/secrets.toml` 파일을 만들고 아래처럼 입력:

```
OPENAI_API_KEY = "your-api-key-here"
```

**방법 B) 환경변수로 설정하기**

- PowerShell:
```
$env:OPENAI_API_KEY="your-api-key-here"
```
""")
    st.stop()

# 공백/줄바꿈 등으로 인한 인증 실패 방지
OPENAI_API_KEY = str(OPENAI_API_KEY).strip()

# (선택) 로컬 환경에서 다른 엔드포인트로 향하는 설정이 있으면 401이 날 수 있어 안내
if os.getenv("OPENAI_BASE_URL") and os.getenv("OPENAI_BASE_URL").strip():
    st.warning("ℹ️ 환경변수 `OPENAI_BASE_URL`가 설정되어 있습니다. 기본 OpenAI 엔드포인트가 아니라면 401이 발생할 수 있어요.")

# ============================================
# 🤖 OpenAI 모델 (프로젝트 전역 기본값)
# - 요구사항: 파일 내 사용하는 AI 모델을 GPT 5.2로 통일
# - 필요 시 Streamlit Secrets / 환경변수로 오버라이드 가능: OPENAI_MODEL
# ============================================
OPENAI_MODEL = None
try:
    if hasattr(st, "secrets"):
        OPENAI_MODEL = st.secrets.get("OPENAI_MODEL")
except Exception:
    OPENAI_MODEL = None

if not OPENAI_MODEL:
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")

# OpenAI 클라이언트 초기화
client = OpenAI(api_key=OPENAI_API_KEY)

# 스크립트 파일의 디렉토리를 기준으로 DB 경로 설정
SCRIPT_DIR = Path(__file__).parent.absolute()
DB_PATH = str(SCRIPT_DIR / 'accounting.db')

# System Prompt V2 (토큰 최적화 + 관계도 포함 + 원가/비용 계산 로직 정밀화)

SYSTEM_PROMPT_V2 = """

[역할]

너는 SQLite 데이터베이스용 SQL 쿼리 생성 전문가다.

한국어 자연어 질문을 받아 이 회계 스키마에 맞는 SQL 쿼리를 생성한다.

쿼리는 사람이 바로 실행할 수 있는 형태여야 하며, 불필요한 설명/백틱은 출력하지 않는다.



[테이블 / 관계]

- 회계전표: 모든 거래가 기록되는 핵심 테이블

  - 계정마스터      : ON 회계전표."계정코드"      = 계정마스터."계정코드"

  - 거래처마스터    : ON 회계전표."거래처코드"    = 거래처마스터."거래처코드"

  - 부서마스터      : ON 회계전표."부서코드"      = 부서마스터."부서코드"

  - 프로젝트마스터 : ON 회계전표."프로젝트코드" = 프로젝트마스터."프로젝트코드"

  - 원재료마스터    : ON 회계전표."원재료코드"    = 원재료마스터."원재료코드"

  - 재공품마스터    : ON 회계전표."재공품코드"    = 재공품마스터."재공품코드"

  - 제품마스터      : ON 회계전표."제품코드"      = 제품마스터."제품코드"

- BOM마스터        : 제품마스터."제품코드"      = BOM마스터."제품코드"

- BOM마스터        : 원재료마스터."원재료코드" = BOM마스터."원재료코드"

- 재공품마스터     : 제품마스터."제품코드"      = 재공품마스터."제품코드"



[핵심 규칙]

1. 컬럼명은 항상 큰따옴표로 감싼다. 예) "거래일자", "대변금액".

2. 하나의 질문에 대해 하나의 SQL 쿼리만 생성하되, CTE(WITH 절)를 적극 사용하여 가독성을 높인다.

3. SELECT 절에는 핵심 컬럼만 넣되, 집계/지표가 필요한 경우 적절한 계산 컬럼을 포함한다.

4. 매출:

   - 정의: 회계전표에서 "계정명"에 '매출'이 포함된 대변금액의 합계.

   - 쿼리: SELECT ..., SUM(j."대변금액") AS "매출" FROM 회계전표 j WHERE j."계정명" LIKE '%매출%'



5. 🔥 [비용/예산사용액 계산 절대 규칙] (가장 중요):

   - 단순히 `회계전표`의 `차변금액`을 전부 더하면 안 된다. (자산 증가, 부채 감소도 차변에 오기 때문)

   - **반드시 `계정마스터` 테이블과 JOIN** 하여, **`대분류`가 '비용'** (또는 제조원가, 판관비 등 비용 성격)인 계정만 합산해야 한다.

   - 조건 예시: `WHERE a."대분류" LIKE '%비용%' OR a."대분류" IN ('매출원가', '판매비와관리비', '제조원가', '영업외비용')`

   

6. 기간 필터:

   - 올해:     strftime('%Y', j."거래일자") = strftime('%Y','now')

   - 특정 월: strftime('%Y-%m', j."거래일자") = '2025-01' 과 같이 사용.

7. ORDER BY / LIMIT:

   - LIMIT 사용은 피하고, ORDER BY DESC와 필요한 경우 상위 N 개는 서브쿼리 또는 윈도우 함수를 사용한다.



8. ★ 코드-명칭 동반 출력 규칙 ★:

   - SELECT 절에 식별자(코드)가 포함될 경우, 반드시 해당 마스터 테이블을 JOIN하여 **'명칭' 컬럼을 바로 옆에 포함**시켜야 한다.

   - 예시: `SELECT j."프로젝트코드", p."프로젝트명", ...`



[🔥 원가 / 마진 계산 규칙]

1. 제품 단위 원가는 BOM마스터와 원재료마스터를 통해 계산한다.

2. 제품별 매출과 판매수량은 회계전표에서 집계한다.

3. 🚨 **치명적 오류 방지**:

   - 절대 `매출액 * 단위원가`를 계산하지 마시오.

   - 반드시 **`판매수량 * 단위원가`** 공식을 사용하여 매출원가를 구해야 한다.



[응답 형식]

- 항상 실행 가능한 순수 SQL 쿼리만 한 번 출력한다.

"""

POSTGRES_DIALECT_OVERRIDE = """

[최우선 데이터베이스 규칙 - PostgreSQL]

- 현재 데이터베이스는 SQLite가 아니라 PostgreSQL이다. 앞의 SQLite 문법 예시는 무시한다.
- 모든 테이블명과 컬럼명은 정확한 대소문자를 보존하도록 반드시 쌍따옴표로 감싼다.
- 연도는 EXTRACT(YEAR FROM "거래일자"), 월은 TO_CHAR("거래일자", 'YYYY-MM'),
  월 시작일은 DATE_TRUNC('month', "거래일자")를 사용한다.
- 현재 날짜는 CURRENT_DATE를 사용한다.
- strftime(), julianday(), date('now', ...), PRAGMA는 절대 사용하지 않는다.
- SELECT 또는 읽기 전용 WITH 쿼리 한 개만 생성한다. 데이터 변경 SQL은 절대 생성하지 않는다.
- 집계하지 않은 SELECT 컬럼은 모두 GROUP BY에 포함한다.
"""


def get_sql_system_prompt() -> str:
    if is_remote_database():
        postgres_prompt = SYSTEM_PROMPT_V2.replace(
            "너는 SQLite 데이터베이스용 SQL 쿼리 생성 전문가다.",
            "너는 PostgreSQL 데이터베이스용 SQL 쿼리 생성 전문가다.",
        )
        postgres_prompt = postgres_prompt.replace(
            "strftime('%Y', j.\"거래일자\") = strftime('%Y','now')",
            "EXTRACT(YEAR FROM j.\"거래일자\") = EXTRACT(YEAR FROM CURRENT_DATE)",
        )
        postgres_prompt = postgres_prompt.replace(
            "strftime('%Y-%m', j.\"거래일자\") = '2025-01'",
            "TO_CHAR(j.\"거래일자\", 'YYYY-MM') = '2025-01'",
        )
        return postgres_prompt + POSTGRES_DIALECT_OVERRIDE
    return SYSTEM_PROMPT_V2

# Few-shot 예시 (비용 집계 시 계정 분류 필터링 적용)

FEW_SHOT_EXAMPLES = """
[Few-shot 예제]

Q: 프로젝트별 예산 대비 사용액과 초과 여부를 보여줘
A: WITH ProjectCost AS (
  SELECT 
    j."프로젝트코드",
    SUM(j."차변금액") AS "사용금액"
  FROM 회계전표 j
  JOIN 계정마스터 a ON j."계정코드" = a."계정코드"
  WHERE a."대분류" LIKE '%비용%' OR a."대분류" IN ('매출원가', '판매비와관리비', '제조원가') -- 중요: 비용 계정만 필터링
  GROUP BY j."프로젝트코드"
)
SELECT 
  p."프로젝트코드",
  p."프로젝트명",
  COALESCE(c."사용금액", 0) AS "사용금액",
  p."예산",
  CASE 
    WHEN COALESCE(c."사용금액", 0) > p."예산" THEN '초과'
    ELSE '이내'
  END AS "초과여부",
  ROUND(COALESCE(c."사용금액", 0) * 100.0 / NULLIF(p."예산", 0), 2) AS "예산사용율"
FROM 프로젝트마스터 p
LEFT JOIN ProjectCost c ON p."프로젝트코드" = c."프로젝트코드"
ORDER BY "예산사용율" DESC

Q: 부서별 비용 합계
A: SELECT 
  d."부서코드",
  d."부서명", 
  COUNT(DISTINCT j."전표번호") AS "전표수",
  SUM(j."차변금액") AS "비용"
FROM 회계전표 j
LEFT JOIN 부서마스터 d ON j."부서코드" = d."부서코드"
JOIN 계정마스터 a ON j."계정코드" = a."계정코드"
WHERE a."대분류" LIKE '%비용%' -- 중요: 자산/부채 제외
GROUP BY d."부서코드", d."부서명"
ORDER BY "비용" DESC

Q: 제품별 단위원가를 보여줘
A: SELECT 
  p."제품코드", 
  p."제품명", 
  SUM(b."수량" * m."단가") AS "단위원가"
FROM "BOM마스터" b
LEFT JOIN 제품마스터 p ON b."제품코드" = p."제품코드"
LEFT JOIN 원재료마스터 m ON b."원재료코드" = m."원재료코드"
GROUP BY p."제품코드", p."제품명"

Q: 제품별 수량, 매출, 매출원가, 마진율 정리해줘
A: WITH Sales AS (
  SELECT 
    j."제품코드",
    SUM(j."수량") AS "판매수량",
    SUM(j."대변금액") AS "매출"
  FROM 회계전표 j
  WHERE j."계정명" LIKE '%매출%'
  GROUP BY j."제품코드"
),
Cost AS (
  SELECT 
    p."제품코드",
    SUM(b."수량" * m."단가") AS "단위원가"
  FROM "BOM마스터" b
  LEFT JOIN 제품마스터 p ON b."제품코드" = p."제품코드"
  LEFT JOIN 원재료마스터 m ON b."원재료코드" = m."원재료코드"
  GROUP BY p."제품코드"
)
SELECT 
  s."제품코드",
  p."제품명",
  s."판매수량",
  s."매출",
  (s."판매수량" * c."단위원가") AS "매출원가",
  (s."매출" - (s."판매수량" * c."단위원가")) AS "마진",
  ROUND((s."매출" - (s."판매수량" * c."단위원가")) * 100.0 / NULLIF(s."매출", 0), 2) AS "마진율"
FROM Sales s
LEFT JOIN Cost c ON s."제품코드" = c."제품코드"
LEFT JOIN 제품마스터 p ON s."제품코드" = p."제품코드"
ORDER BY "매출" DESC
"""

# ============================================
# 🔥 궁극의 퓨샷 저장소 (토큰 최대화 및 고급 패턴 추가)
# ============================================

FEW_SHOT_EXAMPLES_ULTIMATE = """[Few-shot 예시 - 궁극의 확장판]

# ============================================
# 카테고리 1: 기본 집계 (SUM, COUNT, AVG, MAX, MIN) - (기존 유지)
# ============================================

Q: 전체 매출 합계
A: SELECT SUM("대변금액") AS "매출합계" FROM 회계전표 WHERE "계정명" LIKE '%매출%'

Q: 전체 비용 합계
A: SELECT SUM("차변금액") AS "비용합계" FROM 회계전표

Q: 계정과목은 몇 개인가요?
A: SELECT COUNT(DISTINCT "계정코드") AS "계정수" FROM 회계전표

# ============================================
# 카테고리 2: 그룹핑 & JOIN (거래처별, 부서별, 프로젝트별) - (기존 유지)
# ============================================

Q: 거래처별 매출 합계
A: SELECT g."거래처코드", g."거래처명", SUM(j."대변금액") AS "매출합계" FROM 회계전표 j LEFT JOIN 거래처마스터 g ON j."거래처코드"=g."거래처코드" WHERE j."계정명" LIKE '%매출%' GROUP BY g."거래처코드", g."거래처명" ORDER BY "매출합계" DESC

Q: 프로젝트별 매출
A: SELECT p."프로젝트코드", p."프로젝트명", SUM(j."대변금액") AS "매출" FROM 회계전표 j LEFT JOIN 프로젝트마스터 p ON j."프로젝트코드"=p."프로젝트코드" WHERE j."계정명" LIKE '%매출%' GROUP BY p."프로젝트코드", p."프로젝트명" ORDER BY "매출" DESC

# ============================================
# 카테고리 3: 시간 기반 분석 (일/월/년별, 기간 비교) - (고급 날짜 함수 강화)
# ============================================

Q: 올해 월별 매출 추이
A: SELECT strftime('%Y-%m',"거래일자") AS "월", SUM("대변금액") AS "매출" FROM 회계전표 WHERE "계정명" LIKE '%매출%' AND strftime('%Y',"거래일자")=strftime('%Y','now') GROUP BY strftime('%Y-%m',"거래일자") ORDER BY "월"

Q: 분기별 매출 (CASE WHEN 사용)
A: SELECT CASE WHEN CAST(strftime('%m',"거래일자") AS INTEGER) BETWEEN 1 AND 3 THEN '1분기' WHEN CAST(strftime('%m',"거래일자") AS INTEGER) BETWEEN 4 AND 6 THEN '2분기' WHEN CAST(strftime('%m',"거래일자") AS INTEGER) BETWEEN 7 AND 9 THEN '3분기' ELSE '4분기' END AS "분기", SUM("대변금액") AS "매출" FROM 회계전표 WHERE "계정명" LIKE '%매출%' AND strftime('%Y',"거래일자")=strftime('%Y','now') GROUP BY "분기" ORDER BY "분기"

Q: 작년 대비 올해 매출 증가율 (WITH 절 사용)
A: WITH CurrentY AS (SELECT SUM("대변금액") AS C_SALES FROM 회계전표 WHERE "계정명" LIKE '%매출%' AND strftime('%Y',"거래일자")=strftime('%Y','now')), PreviousY AS (SELECT SUM("대변금액") AS P_SALES FROM 회계전표 WHERE "계정명" LIKE '%매출%' AND strftime('%Y',"거래일자")=strftime('%Y',date('now','-1 year'))) SELECT ROUND((C.C_SALES - P.P_SALES) * 100.0 / NULLIF(P.P_SALES, 0), 2) AS "증가율(%)" FROM CurrentY C JOIN PreviousY P

# ============================================
# 카테고리 4: 비율/비중 계산 - (기존 유지)
# ============================================

Q: 프로젝트별 매출 비중
A: SELECT p."프로젝트코드", p."프로젝트명", SUM(j."대변금액") AS "매출", ROUND(SUM(j."대변금액")*100.0/(SELECT SUM("대변금액") FROM 회계전표 WHERE "계정명" LIKE '%매출%'),2) AS "비중" FROM 회계전표 j LEFT JOIN 프로젝트마스터 p ON j."프로젝트코드"=p."프로젝트코드" WHERE j."계정명" LIKE '%매출%' GROUP BY p."프로젝트코드", p."프로젝트명" ORDER BY "매출" DESC

# ============================================
# 카테고리 5: 순위/상위N개 (ORDER BY) - (기존 유지)
# ============================================

Q: 매출 상위 5개 거래처
A: SELECT g."거래처명", SUM(j."대변금액") AS "매출" FROM 회계전표 j LEFT JOIN 거래처마스터 g ON j."거래처코드"=g."거래처코드" WHERE j."계정명" LIKE '%매출%' GROUP BY g."거래처명" ORDER BY "매출" DESC LIMIT 5

# ============================================
# 카테고리 6: 복잡한 조건 (CASE WHEN, 다중 WHERE) - (기존 유지)
# ============================================

Q: 부서별 예산 초과 여부
A: SELECT d."부서명", d."예산", SUM(j."차변금액") AS "실제비용", CASE WHEN SUM(j."차변금액")>d."예산" THEN '초과' ELSE '정상' END AS "상태" FROM 회계전표 j LEFT JOIN 부서마스터 d ON j."부서코드"=d."부서코드" GROUP BY d."부서명", d."예산"

# ============================================
# 카테고리 7: 서브쿼리 (상위N%, 평균 초과 등) - (기존 유지)
# ============================================

Q: 평균 매출 이상 거래처
A: SELECT g."거래처명", SUM(j."대변금액") AS "매출" FROM 회계전표 j LEFT JOIN 거래처마스터 g ON j."거래처코드"=g."거래처코드" WHERE j."계정명" LIKE '%매출%' GROUP BY g."거래처명" HAVING SUM(j."대변금액")>=(SELECT AVG("총매출") FROM (SELECT SUM("대변금액") AS "총매출" FROM 회계전표 WHERE "계정명" LIKE '%매출%' GROUP BY "거래처코드")) ORDER BY "매출" DESC

# ============================================
# 카테고리 8: 재고/생산 관리 (원재료, 재공품, 제품) - (기존 유지)
# ============================================

Q: 원재료 재고 부족 목록
A: SELECT "원재료코드", "원재료명", "현재재고", "안전재고" FROM 원재료마스터 WHERE "현재재고"<"안전재고" AND "사용여부"='Y'

# ============================================
# 카테고리 9: BOM/원가 계산 - (복잡한 논리 명시 강화)
# ============================================

Q: 제품별 BOM 원재료 원가 (3개 테이블 조인)
A: SELECT p."제품코드", p."제품명", SUM(b."수량"*m."단가") AS "원재료원가" FROM BOM마스터 b LEFT JOIN 제품마스터 p ON b."제품코드"=p."제품코드" LEFT JOIN 원재료마스터 m ON b."원재료코드"=m."원재료코드" GROUP BY p."제품코드", p."제품명"

Q: 원재료 구매액의 공급업체별 비중 (5% 초과만)
A: WITH SupplierCost AS (SELECT m."공급업체코드", SUM(m."단가"*m."현재재고") AS "총구매액" FROM 원재료마스터 m GROUP BY m."공급업체코드") SELECT g."거래처명", S."총구매액", ROUND(S."총구매액" * 100.0 / (SELECT SUM("총구매액") FROM SupplierCost), 2) AS "비중" FROM SupplierCost S LEFT JOIN 거래처마스터 g ON S."공급업체코드"=g."거래처코드" WHERE "비중">5.0 ORDER BY "비중" DESC

# ============================================
# 카테고리 10: 재무제표 유형 (PL, BS) - (기존 유지)
# ============================================

Q: 손익계산서 - 영업이익
A: SELECT (SELECT SUM("대변금액") FROM 회계전표 WHERE "계정명" LIKE '%매출%')-(SELECT SUM("차변금액") FROM 회계전표 WHERE "계정명" LIKE '%매출원가%' OR "계정명" LIKE '%판매관리비%') AS "영업이익"

# ============================================
# 카테고리 11: 다중 테이블 JOIN (3개 이상) - (기존 유지)
# ============================================

Q: 프로젝트별 부서별 매출 (3-way JOIN)
A: SELECT p."프로젝트명", d."부서명", SUM(j."대변금액") AS "매출" FROM 회계전표 j LEFT JOIN 프로젝트마스터 p ON j."프로젝트코드"=p."프로젝트코드" LEFT JOIN 부서마스터 d ON j."부서코드"=d."부서코드" WHERE j."계정명" LIKE '%매출%' GROUP BY p."프로젝트명", d."부서명" ORDER BY "매출" DESC

# ============================================
# 카테고리 12: NULL 처리 (COALESCE, IFNULL) - (기존 유지)
# ============================================

Q: NULL 거래처는 '미지정'으로 표시
A: SELECT COALESCE(g."거래처명",'미지정') AS "거래처명", SUM(j."대변금액") AS "매출" FROM 회계전표 j LEFT JOIN 거래처마스터 g ON j."거래처코드"=g."거래처코드" WHERE j."계정명" LIKE '%매출%' GROUP BY g."거래처명" ORDER BY "매출" DESC

# ============================================
# 카테고리 13: 문자열 처리 (LIKE, SUBSTR, LENGTH) - (기존 유지)
# ============================================

Q: 계정명에 '급여' 포함된 거래
A: SELECT "계정명", SUM("차변금액") AS "금액" FROM 회계전표 WHERE "계정명" LIKE '%급여%' GROUP BY "계정명"

# ============================================
# 카테고리 14: 날짜 계산 (고급 날짜 함수 추가)
# ============================================

Q: 지난 분기의 시작일과 종료일 (날짜 산술)
A: SELECT date('now', 'start of quarter', '-3 month') AS "지난분기시작일", date('now', 'start of quarter', '-1 day') AS "지난분기종료일"

Q: 이번 주 월요일 날짜 계산
A: SELECT date('now', 'weekday 1') AS "이번주월요일" -- SQLite의 'weekday N'은 0(일요일)부터 시작함. 1은 월요일

Q: 6개월 이상 거래 없는 거래처 (날짜 차이)
A: SELECT g."거래처명", MAX(j."거래일자") AS "최종거래일" FROM 회계전표 j LEFT JOIN 거래처마스터 g ON j."거래처코드"=g."거래처코드" GROUP BY g."거래처명" HAVING julianday('now')-julianday(MAX(j."거래일자"))>180

# ============================================
# 카테고리 15: 특수 분석 패턴 (기존 유지)
# ============================================

Q: 거래처 충성도 (거래 지속 개월수)
A: SELECT g."거래처명", COUNT(DISTINCT strftime('%Y-%m',j."거래일자")) AS "거래개월수" FROM 회계전표 j LEFT JOIN 거래처마스터 g ON j."거래처코드"=g."거래처코드" GROUP BY g."거래처명" ORDER BY "거래개월수" DESC

# ============================================
# ⭐️ 카테고리 16: 창 함수 (Window Functions) - (새로운 패턴 추가)
# ============================================

Q: 월별 매출액과 누적 매출액을 함께 계산 (SUM OVER)
A: SELECT strftime('%Y-%m', "거래일자") AS "월", SUM("대변금액") AS "월별매출", SUM(SUM("대변금액")) OVER (ORDER BY strftime('%Y-%m', "거래일자")) AS "누적매출액" FROM 회계전표 WHERE "계정명" LIKE '%매출%' AND strftime('%Y', "거래일자") = strftime('%Y', 'now') GROUP BY "월" ORDER BY "월"

Q: 부서별 비용 순위를 함께 표시 (RANK OVER)
A: SELECT d."부서명", SUM(j."차변금액") AS "총비용", RANK() OVER (ORDER BY SUM(j."차변금액") DESC) AS "비용순위" FROM 회계전표 j LEFT JOIN 부서마스터 d ON j."부서코드"=d."부서코드" GROUP BY d."부서명" ORDER BY "비용순위"

Q: 거래처별 매출 기여도 상위 3개 (WINDOW FRAME + PARTITION BY)
A: SELECT "거래처명", "월", "월별매출", RANK() OVER (PARTITION BY "월" ORDER BY "월별매출" DESC) AS "월별순위" FROM (SELECT g."거래처명", strftime('%Y-%m', j."거래일자") AS "월", SUM(j."대변금액") AS "월별매출" FROM 회계전표 j LEFT JOIN 거래처마스터 g ON j."거래처코드"=g."거래처코드" WHERE j."계정명" LIKE '%매출%' GROUP BY g."거래처명", "월") WHERE "월별순위"<=3 ORDER BY "월", "월별순위"

Q: 계정별 월별 이동 평균 비용 (AVG OVER)
A: SELECT strftime('%Y-%m', "거래일자") AS "월", "계정명", SUM("차변금액") AS "월별비용", AVG(SUM("차변금액")) OVER (PARTITION BY "계정명" ORDER BY strftime('%Y-%m', "거래일자") ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS "3개월이동평균" FROM 회계전표 GROUP BY "월", "계정명" ORDER BY "계정명", "월"

# ============================================
# 카테고리 17: 복잡한 논리 조합 (CTE 사용 명시) - (고급 논리)
# ============================================

Q: 매출 상위 10%의 거래처와 하위 10% 거래처의 평균 매출액 비교
A: WITH RankedClients AS (SELECT "거래처코드", SUM("대변금액") AS TotalSales, NTILE(10) OVER (ORDER BY SUM("대변금액") DESC) AS SalesTile FROM 회계전표 WHERE "계정명" LIKE '%매출%' GROUP BY "거래처코드") SELECT '상위 10%' AS "구분", AVG(TotalSales) AS "평균매출" FROM RankedClients WHERE SalesTile = 1 UNION ALL SELECT '하위 10%' AS "구분", AVG(TotalSales) AS "평균매출" FROM RankedClients WHERE SalesTile = 10;

Q: 전표번호의 차변/대변 불일치와 금액 차이 (복합 HAVING)
A: SELECT "전표번호", SUM("차변금액") AS "총차변", SUM("대변금액") AS "총대변", ABS(SUM("차변금액") - SUM("대변금액")) AS "차액" FROM 회계전표 GROUP BY "전표번호" HAVING "차액" > 0.01 AND SUM("차변금액") IS NOT NULL AND SUM("대변금액") IS NOT NULL ORDER BY "차액" DESC

# ============================================
# 카테고리 18: 마스터 데이터 기준 필터링 (마스터 제약 조건)
# ============================================

Q: 사용여부가 'Y'인 원재료만 필터링한 재고 현황
A: SELECT "원재료명", "현재재고", "안전재고" FROM 원재료마스터 WHERE "사용여부" = 'Y' ORDER BY "현재재고" DESC

Q: 거래처 마스터의 '지역'이 '서울'인 거래처의 총 매출
A: SELECT g."거래처명", SUM(j."대변금액") AS "매출" FROM 회계전표 j LEFT JOIN 거래처마스터 g ON j."거래처코드"=g."거래처코드" WHERE j."계정명" LIKE '%매출%' AND g."지역" = '서울' GROUP BY g."거래처명" ORDER BY "매출" DESC

"""

# System Prompt 정의 (기본 프롬프트 - 하위 호환성용, 토큰 절약을 위해 최소화)
SYSTEM_PROMPT = """너는 한국어 회계 전문 SQLite 쿼리 생성기다. 사용자 질문을 정확한 SQL로 변환해라. 오직 실행 가능한 SQL 쿼리만 출력하고 백틱, 설명, 주석은 절대 포함하지 마라. LIMIT 절을 절대 사용하지 마라. 한국어 컬럼명은 큰따옴표로 감싸라."""

# ============================================
# 🔥 최종 2500토큰 - 최대 5개 칼럼 우선순위 버전 (한화면 딱!)
# ============================================

# 하드코딩된 스키마 설명 제거됨 - JSON 기반으로만 동작

# 페이지 설정
st.set_page_config(
    page_title="TalkToData",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Pretendard 폰트 로드
st.markdown("""
    <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" />
""", unsafe_allow_html=True)

# Wireframer/Framer 스타일 CSS
st.markdown("""
    <style>
    /* 전체 스타일 초기화 */
    * {
        margin: 0;
        padding: 0;
        box-sizing: border-box;
    }
    
    /* 전체 배경 - 블랙 앤 화이트 테마 */
    .stApp {
        background: #ffffff;
        color: #000000;
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif !important;
    }
    
    /* body 배경도 흰색으로 */
    body {
        background: #ffffff !important;
    }
    
    /* Streamlit 기본 요소들 배경 흰색 */
    section[data-testid="stAppViewContainer"],
    section[data-testid="stAppViewContainer"] > div,
    .main > div {
        background: #ffffff !important;
    }
    
    /* 기본 폰트 적용 (Expander 제외) */
    body, html {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif !important;
    }
    
    /* 제목에만 폰트 적용 */
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif !important;
    }
    
    /* 사이드바 스타일 */
    .css-1d391kg {
        /* 사이드바 표시 */
    }
    
    /* 메인 콘텐츠 영역 */
    .main .block-container {
        padding: 0;
        max-width: 100%;
        background: #ffffff !important;
    }
    
    /* 메인 영역 배경 */
    .main {
        background: #ffffff !important;
    }
    
    /* 메인 콘텐츠 섹션 - 중앙 정렬 */
    .main-content {
        min-height: 100vh;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: flex-start;
        padding: 3rem 2rem 2rem 2rem;
        text-align: center;
        position: relative;
    }
    
    /* 제목 스타일 - 중앙 정렬 */
    h1 {
        text-align: center !important;
        font-size: 5rem;
        font-weight: 700;
        color: #000000;
        margin-bottom: 1rem;
        letter-spacing: -2px;
        line-height: 1.1;
    }
    
    .main-title {
        text-align: center !important;
    }
    
    /* 태그라인 */
    .tagline {
        font-size: 1.25rem;
        color: #000000;
        margin-bottom: 3rem;
        font-weight: 400;
    }
    
    /* 메인 검색 입력 필드 - 좁고 길게 */
    .main-search-container {
        max-width: 900px;
        width: 100%;
        margin: 0 auto 2rem auto;
        position: relative;
    }
    
    .main-search-input {
        width: 100%;
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
        padding: 1.2rem 1.8rem;
        font-size: 1rem;
        color: #ffffff;
        font-family: inherit;
        transition: all 0.3s ease;
    }
    
    .main-search-input::placeholder {
        color: rgba(255, 255, 255, 0.5);
    }
    
    .main-search-input:focus {
        outline: none;
        border-color: rgba(255, 255, 255, 0.3);
        background: rgba(255, 255, 255, 0.08);
    }
    
    
    /* 메시지 컨테이너 - 큰 대화창 영역 */
    .messages-container {
        width: 100%;
        min-height: 0 !important;
        max-height: 80vh;
        margin: 2rem auto;
        padding: 0 !important;
        overflow-y: auto;
        background: #ffffff;
        border: none !important;
        border-radius: 16px;
        box-shadow: none !important;
    }
    
    /* 메시지가 있을 때만 padding과 shadow 적용 */
    .messages-container:not(:empty) {
        padding: 2rem !important;
        padding-bottom: 3rem !important;
        box-shadow: 0px 4px 20px rgba(0, 0, 0, 0.1) !important;
    }
    
    .message {
        background: #ffffff;
        border: none !important;
        border-radius: 12px;
        padding: 1rem 1.5rem;
        margin-bottom: 1rem;
        color: #000000;
        width: 100%;
        box-shadow: 0px 2px 10px rgba(0, 0, 0, 0.08) !important;
    }
    
    .message.user {
        background: #f5f5f5;
        box-shadow: 0px 2px 10px rgba(0, 0, 0, 0.08) !important;
    }
    
    .sql-box {
        background: #ffffff;
        border: none !important;
        border-radius: 8px;
        padding: 1rem;
        margin-top: 1rem;
        font-family: 'Courier New', monospace;
        font-size: 0.85rem;
        color: #000000;
        overflow-x: auto;
        box-shadow: 0px 2px 10px rgba(0, 0, 0, 0.08) !important;
    }
    
    /* 데이터프레임 스타일 */
    .dataframe {
        background: #ffffff !important;
        border: none !important;
        border-radius: 12px;
        color: #000000 !important;
        margin-top: 1rem;
        box-shadow: 0px 2px 10px rgba(0, 0, 0, 0.08) !important;
    }
    
    .dataframe thead {
        background: #f5f5f5 !important;
        color: #000000 !important;
    }
    
    .dataframe tbody tr {
        border-bottom: 1px solid #000000;
    }
    
    .dataframe tbody tr:hover {
        background: #f5f5f5 !important;
    }
    
    .dataframe td {
        color: #000000 !important;
    }
    
    /* 입력창 디자인 - 정밀 타겟팅 (검색창만) */
    /* Expander 내부 input은 위에서 명시적으로 제외했으므로 여기서는 메인 검색창만 타겟팅 */
    div[data-testid="stTextInput"] input {
        height: 60px !important;
        font-size: 20px !important;
        line-height: 60px !important;
        background-color: #FFFFFF !important;
        border: none !important;
        border-radius: 30px !important;
        color: #000000 !important;
        text-align: center !important;
        padding-top: 0px !important;
        padding-bottom: 0px !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
        width: 100% !important;
        max-width: 800px !important;
        margin: 0 auto !important;
        transition: box-shadow 0.5s cubic-bezier(0.4, 0, 0.2, 1), transform 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
        box-shadow: 0px 1px 3px 0px rgba(0, 0, 0, 0.05), 0px 2px 8px 2px rgba(0, 0, 0, 0.04), 0px 4px 16px 4px rgba(0, 0, 0, 0.03) !important;
        display: flex !important;
        align-items: center !important;
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif !important;
        position: relative !important;
        z-index: 10 !important;
    }
    
    /* 호버 시 구름처럼 부드럽게 */
    div[data-testid="stTextInput"] input:hover {
        box-shadow: 0px 2px 6px 1px rgba(0, 0, 0, 0.06), 0px 4px 12px 3px rgba(0, 0, 0, 0.05), 0px 8px 24px 6px rgba(0, 0, 0, 0.04) !important;
        transform: translateY(-1px) !important;
    }
    
    /* 포커스 됐을 때 - 구름처럼 더 부드럽게 */
    div[data-testid="stTextInput"] input:focus {
        box-shadow: 0px 3px 8px 2px rgba(0, 0, 0, 0.07), 0px 6px 16px 4px rgba(0, 0, 0, 0.06), 0px 12px 32px 8px rgba(0, 0, 0, 0.05) !important;
        border: none !important;
        outline: none !important;
        background-color: #FFFFFF !important;
        z-index: 10 !important;
        transform: translateY(-2px) !important;
    }
    
    div[data-testid="stTextInput"] input::placeholder {
        color: #999999 !important;
        font-size: 16px !important;
        transition: opacity 0.5s ease-in-out !important;
        animation: placeholderFadeIn 0.5s ease-in-out !important;
    }
    
    @keyframes placeholderFadeIn {
        0% {
            opacity: 0;
            transform: translateY(5px);
        }
        100% {
            opacity: 1;
            transform: translateY(0);
        }
    }
    
    /* 입력 시 placeholder 부드럽게 사라지기 */
    div[data-testid="stTextInput"] input:focus::placeholder {
        opacity: 0.4;
        transition: opacity 0.3s ease;
    }
    
    div[data-testid="stTextInput"] {
        max-width: 800px !important;
        margin: 0 auto !important;
        display: block !important;
        width: 100% !important;
        overflow: visible !important;
        padding: 0 !important;
        position: relative !important;
        background: transparent !important;
        border: none !important;
    }
    
    div[data-testid="stTextInput"] > div {
        max-width: 800px !important;
        margin: 0 auto !important;
        display: block !important;
        overflow: visible !important;
        padding: 0.5rem !important;
        position: relative !important;
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }
    
    /* Streamlit 기본 input 컨테이너 스타일 제거 */
    div[data-testid="stTextInput"] > div > div {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        padding: 0 !important;
    }
    
    /* Streamlit 기본 input wrapper 스타일 완전 제거 */
    div[data-testid="stTextInput"] * {
        background: transparent !important;
    }
    
    div[data-testid="stTextInput"] *:not(input) {
        border: none !important;
        box-shadow: none !important;
    }
    
    /* input만 배경색 유지 */
    div[data-testid="stTextInput"] input {
        background-color: #FFFFFF !important;
    }
    /* Google-style outline for the main question input only. */
    div[data-testid="stTextInput"]:has(input[aria-label="질문"]) div[data-testid="stTextInputRootElement"] {
        height: 60px !important;
        overflow: visible !important;
        border-radius: 30px !important;
    }

    div[data-testid="stTextInput"] input[aria-label="질문"] {
        border: 1px solid #c4c7c5 !important;
        box-shadow: 0 1px 6px rgba(32, 33, 36, 0.18) !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
    }

    div[data-testid="stTextInput"] input[aria-label="질문"]:hover {
        border-color: #aeb4bb !important;
        box-shadow: 0 1px 6px rgba(32, 33, 36, 0.24) !important;
        transform: none !important;
    }

    div[data-testid="stTextInput"] input[aria-label="질문"]:focus {
        border-color: #4285f4 !important;
        box-shadow: 0 0 0 1px rgba(66, 133, 244, 0.18), 0 1px 6px rgba(32, 33, 36, 0.28) !important;
        transform: none !important;
    }
    
    /* Expander 내부 input은 검색창 스타일 적용 안 함 (검색창 CSS 뒤에 배치하여 우선순위 확보) */
    div[data-testid="stExpander"] input,
    div[data-testid="stExpander"] div[data-testid="stTextInput"] input {
        height: auto !important;
        font-size: inherit !important;
        line-height: normal !important;
        background-color: transparent !important;
        border: none !important;
        border-radius: 0 !important;
        color: inherit !important;
        text-align: left !important;
        padding: inherit !important;
        width: auto !important;
        max-width: none !important;
        margin: 0 !important;
        box-shadow: none !important;
        display: block !important;
    }
    
    .stButton>button {
        background: #ffffff !important;
        color: #000000 !important;
        border: none !important;
        border-radius: 12px !important;
        padding: 0.75rem 1.5rem !important;
        font-weight: 500 !important;
        transition: all 0.2s ease !important;
        box-shadow: 0px 2px 10px rgba(0, 0, 0, 0.08) !important;
    }
    
    .stButton>button:hover {
        background: #f5f5f5 !important;
        transform: translateY(-2px);
        box-shadow: 0px 4px 15px rgba(0, 0, 0, 0.12) !important;
    }
    
    /* 기본 텍스트 색상 (Expander 제외) */
    .main p, .main span, .main label {
        color: #000000 !important;
    }
    
    /* Expander는 기본 스타일 유지 - CSS 간섭 방지 */
    div[data-testid="stExpander"] * {
        font-family: inherit !important;
    }
    
    div[data-testid="stExpander"] .streamlit-expanderHeader {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif !important;
    }
    
    /* 헤더 제거 */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    
    /* Streamlit 접근성 관련 숨겨진 텍스트 제거 */
    .stScreenReaderOnly,
    [class*="screenReaderOnly"],
    [class*="sr-only"],
    span[style*="position: absolute"][style*="clip"],
    [aria-label*="keyboard"][aria-label*="allow"],
    [title*="keyboard"][title*="allow"] {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        height: 0 !important;
        width: 0 !important;
        overflow: hidden !important;
        position: absolute !important;
        clip: rect(0, 0, 0, 0) !important;
    }
    
    /* 애니메이션 */
    @keyframes fadeIn {
        from {
            opacity: 0;
            transform: translateY(20px);
        }
        to {
            opacity: 1;
            transform: translateY(0);
        }
    }
    
    .fade-in {
        animation: fadeIn 0.5s ease-out;
    }
    
    /* Expander 스타일 - 겹침 방지 및 CSS 간섭 차단 */
    div[data-testid="stExpander"] {
        margin-bottom: 3rem !important;
        margin-top: 1rem !important;
    }
    
    div[data-testid="stExpander"] .streamlit-expanderHeader {
        background: #ffffff !important;
        border: none !important;
        border-radius: 8px !important;
        padding: 0.75rem 1rem !important;
        margin-bottom: 0 !important;
        margin-top: 0 !important;
        color: #000000 !important;
        font-size: 1rem !important;
        font-weight: 500 !important;
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif !important;
        line-height: 1.5 !important;
        display: flex !important;
        align-items: center !important;
        box-shadow: 0px 2px 10px rgba(0, 0, 0, 0.08) !important;
        white-space: normal !important;
        word-wrap: break-word !important;
    }
    
    div[data-testid="stExpander"] .streamlit-expanderHeader:hover {
        background: #f5f5f5 !important;
        box-shadow: 0px 4px 15px rgba(0, 0, 0, 0.12) !important;
    }
    
    /* Expander 헤더 내부 텍스트 - 정밀 타겟팅 */
    div[data-testid="stExpander"] .streamlit-expanderHeader p,
    div[data-testid="stExpander"] .streamlit-expanderHeader span,
    div[data-testid="stExpander"] .streamlit-expanderHeader div {
        color: #000000 !important;
        font-size: 1rem !important;
        font-weight: 500 !important;
        line-height: 1.5 !important;
        margin: 0 !important;
        padding: 0 !important;
        white-space: normal !important;
        word-wrap: break-word !important;
    }
    
    /* Keyboard allow down 텍스트 숨기기 - Streamlit 접근성 관련 요소 */
    div[data-testid="stExpander"] .streamlit-expanderHeader [title*="keyboard"],
    div[data-testid="stExpander"] .streamlit-expanderHeader [aria-label*="keyboard"],
    div[data-testid="stExpander"] .streamlit-expanderHeader [title*="allow"],
    div[data-testid="stExpander"] .streamlit-expanderHeader [aria-label*="allow"],
    div[data-testid="stExpander"] .streamlit-expanderHeader [title*="down"],
    div[data-testid="stExpander"] .streamlit-expanderHeader [aria-label*="down"],
    /* Streamlit의 접근성 관련 숨겨진 텍스트 */
    div[data-testid="stExpander"] .streamlit-expanderHeader span[style*="position: absolute"],
    div[data-testid="stExpander"] .streamlit-expanderHeader span[style*="clip"],
    div[data-testid="stExpander"] .streamlit-expanderHeader .stScreenReaderOnly {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        height: 0 !important;
        width: 0 !important;
        overflow: hidden !important;
        position: absolute !important;
        clip: rect(0, 0, 0, 0) !important;
    }
    
    /* 🚨 Expander 아이콘 완전 제거 (Keyboard_arrow_down 텍스트 포함) */
    div[data-testid="stExpander"] .streamlit-expanderHeader [data-testid="stExpanderIcon"],
    div[data-testid="stExpander"] .streamlit-expanderHeader button[aria-label*="arrow"],
    div[data-testid="stExpander"] .streamlit-expanderHeader button[aria-label*="Keyboard"],
    div[data-testid="stExpander"] .streamlit-expanderHeader button[aria-label*="keyboard"],
    div[data-testid="stExpander"] .streamlit-expanderHeader svg,
    div[data-testid="stExpander"] .streamlit-expanderHeader [aria-label*="arrow_down"],
    div[data-testid="stExpander"] .streamlit-expanderHeader [aria-label*="Keyboard_arrow"],
    div[data-testid="stExpander"] .streamlit-expanderHeader [class*="icon"],
    div[data-testid="stExpander"] .streamlit-expanderHeader .material-icons,
    /* 버튼 요소 전체 숨기기 */
    div[data-testid="stExpander"] .streamlit-expanderHeader button {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        width: 0 !important;
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        font-size: 0 !important;
        overflow: hidden !important;
        position: absolute !important;
        clip: rect(0, 0, 0, 0) !important;
    }
    
    /* Expander 헤더 내부 구조 정리 - 텍스트 겹침 방지 */
    div[data-testid="stExpander"] .streamlit-expanderHeader > div {
        display: flex !important;
        align-items: center !important;
        gap: 0 !important;
    }
    
    /* 아이콘 영역(두 번째 div) 완전 제거 */
    div[data-testid="stExpander"] .streamlit-expanderHeader > div > div:nth-child(2),
    div[data-testid="stExpander"] .streamlit-expanderHeader > div > div:last-child:not(:first-child) {
        display: none !important;
        width: 0 !important;
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
    }
    
    /* 제목 텍스트만 표시 */
    div[data-testid="stExpander"] .streamlit-expanderHeader > div > div:first-child {
        flex: 1 !important;
        line-height: 1.5 !important;
        width: 100% !important;
    }
    
    /* "keyBoardlarrow" 같은 텍스트를 포함하는 모든 요소 숨기기 */
    div[data-testid="stExpander"] .streamlit-expanderHeader *:not(div:first-child):not(p) {
        text-indent: -9999px !important;
        font-size: 0 !important;
        line-height: 0 !important;
        overflow: hidden !important;
    }
    
    /* 🚨 최종 해결책: Expander 헤더의 버튼 영역 완전 제거 */
    div[data-testid="stExpander"] .streamlit-expanderHeader button,
    div[data-testid="stExpander"] .streamlit-expanderHeader [role="button"],
    div[data-testid="stExpander"] .streamlit-expanderHeader [tabindex="0"] {
        display: none !important;
        visibility: hidden !important;
        width: 0 !important;
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        font-size: 0 !important;
        line-height: 0 !important;
        overflow: hidden !important;
        position: absolute !important;
        left: -9999px !important;
        clip: rect(0, 0, 0, 0) !important;
    }
    
    /* Expander 헤더 내부의 모든 텍스트 노드 중 "keyboard", "arrow" 포함하는 것 숨기기 */
    div[data-testid="stExpander"] .streamlit-expanderHeader {
        position: relative !important;
    }
    
    /* 아이콘 컨테이너 전체 제거 */
    div[data-testid="stExpander"] .streamlit-expanderHeader > div:has(button),
    div[data-testid="stExpander"] .streamlit-expanderHeader > div:has([data-testid="stExpanderIcon"]),
    div[data-testid="stExpander"] .streamlit-expanderHeader > div:has(svg) {
        display: none !important;
        width: 0 !important;
        height: 0 !important;
    }
    
    div[data-testid="stExpander"] .streamlit-expanderContent {
        margin-top: 1rem !important;
        margin-bottom: 2rem !important;
        padding: 1rem !important;
        background: #ffffff !important;
    }
    
    /* Expander 내부 일반 텍스트 */
    div[data-testid="stExpander"] .streamlit-expanderContent p,
    div[data-testid="stExpander"] .streamlit-expanderContent span,
    div[data-testid="stExpander"] .streamlit-expanderContent label {
        color: #000000 !important;
    }
    
    /* ========================================== 
       사용자 메시지 스타일
       ========================================== */
    
    .message {
        margin: 1.5rem 0;
        padding: 1.5rem 2rem;
        border-radius: 2px;
        font-size: 0.95rem;
        line-height: 1.7;
        border-left: 3px solid #7f8c8d;
        background: #f8f9fa;
    }
    
    .message.user {
        border-left-color: #5d6d7e;
        background: #ecf0f1;
    }
    
    /* ========================================== 
       SQL 쿼리 박스 - 흰색 배경, 전문적인 디자인
       ========================================== */
    
    .sql-box {
        margin: 2rem 0;
        padding: 1.5rem 2rem;
        background: #ffffff;  /* ⭐ 흰색 배경 */
        color: #1e293b;  /* ⭐ 어두운 글자 */
        border-radius: 8px;
        font-family: 'Courier New', monospace;
        font-size: 0.82rem;
        line-height: 1.7;
        overflow-x: auto;
        border: 2px solid #cbd5e1;  /* ⭐ 밝은 회색 테두리 */
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);  /* ⭐ 부드러운 그림자 */
        transition: box-shadow 0.3s ease;
    }
    
    .sql-box:hover {
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
    }
    
    /* SQL 키워드 강조 */
    .sql-box code {
        color: #2563eb;  /* ⭐ 파란색 (SELECT, FROM 등) */
        font-family: 'Courier New', monospace;
        font-weight: 500;
    }
    
    /* SQL 박스 제목 */
    .sql-box strong {
        color: #1e293b;  /* ⭐ 진한 회색 */
        font-weight: 700;
        display: block;
        margin-bottom: 0.85rem;
        font-size: 0.9rem;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        padding-bottom: 0.5rem;
        border-bottom: 1px solid #e5e7eb;
    }
    
    /* ========================================== 
       테이블 스타일
       ========================================== */
    
    .dataframe {
        border: 1px solid #d1d5da !important;
        border-radius: 2px;
    }
    
    .dataframe th {
        background: #34495e !important;
        color: white !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        font-size: 0.85rem;
        letter-spacing: 0.5px;
    }
    
    .dataframe td {
        color: #495057 !important;
        font-size: 0.9rem;
    }
    
    /* ========================================== 
       반응형 디자인
       ========================================== */
    
    @media (max-width: 768px) {
        .professional-report {
            padding: 2rem 1.5rem;
            margin: 1rem;
        }
        
        .report-section {
            padding: 1.5rem;
        }
        
        .conclusion-box {
            padding: 2rem 1.5rem !important;
        }
    }
    
    /* Placeholder 애니메이션 */
    @keyframes placeholderFadeIn {
        0% {
            opacity: 0;
            transform: translateY(5px);
        }
        100% {
            opacity: 1;
            transform: translateY(0);
        }
    }
    
    /* 하단 배경 이미지 제거 - 새하얀 배경 */
    
    /* ========================================== 
       사이드바 항상 고정 및 스타일링
       ========================================== */
    
    /* ========================================== 
       사이드바 - 넓이 확대 + 실제 바다 사진
       ========================================== */
    
    /* ⭐⭐⭐ 숨기기 토글 완전 제거 */
    [data-testid="collapsedControl"] {
        display: none !important;
    }
    
    button[kind="header"] {
        display: none !important;
    }
    
    [data-testid="stSidebarCollapseButton"] {
        display: none !important;
    }
    
    section[data-testid="stSidebar"] button[kind="header"] {
        display: none !important;
    }
    
    /* ⭐ 사이드바 크기 증가 (글자 줄바뀜 방지) - 하얀 배경 */
    section[data-testid="stSidebar"] {
        display: block !important;
        visibility: visible !important;
        position: relative !important;
        min-width: 14rem !important;
        max-width: 14rem !important;
        background: #ffffff !important;
        border-right: 1px solid #e5e7eb;
        box-shadow: 2px 0 10px rgba(0, 0, 0, 0.05);
    }
    
    /* 사이드바 내용 */
    section[data-testid="stSidebar"] > div {
        position: relative;
        padding-top: 0;
        padding-bottom: 2rem;
    }
    
    /* ⭐ 사이드바 제목 스타일 */
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2 {
        font-size: 1.4rem;
        font-weight: 700;
        color: #1e293b;
        margin-bottom: 1rem;
        padding-bottom: 0.75rem;
        border-bottom: 1px solid #e5e7eb;
        text-align: center;
    }
    
    /* ⭐ 사이드바 메뉴 - 심플 텍스트 링크 스타일 */
    section[data-testid="stSidebar"] [role="radiogroup"] {
        gap: 0.2rem;
        display: flex;
        flex-direction: column;
    }
    
    /* 라디오 버튼 동그라미 완전히 숨기기 */
    section[data-testid="stSidebar"] [role="radiogroup"] input[type="radio"] {
        display: none !important;
        opacity: 0 !important;
        width: 0 !important;
        height: 0 !important;
        position: absolute !important;
        pointer-events: none !important;
    }
    
    /* 라벨 스타일 - 텍스트만 표시 */
    section[data-testid="stSidebar"] [role="radiogroup"] label {
        padding: 0.75rem 1.5rem !important;
        margin: 0 !important;
        border-radius: 0 !important;
        border: none !important;
        background: transparent !important;
        color: #475569 !important;
        font-size: 0.95rem !important;
        font-weight: 500 !important;
        transition: all 0.25s ease !important;
        cursor: pointer !important;
        box-shadow: none !important;
        text-align: left !important;
        display: block !important;
        position: relative !important;
    }
    
    /* 호버 시 빨간색 */
    section[data-testid="stSidebar"] [role="radiogroup"] label:hover {
        background: transparent !important;
        color: #dc2626 !important;
        transform: translateX(3px) !important;
        box-shadow: none !important;
    }
    
    /* 선택된 메뉴 - 진한 빨간색 */
    section[data-testid="stSidebar"] [role="radiogroup"] label[data-checked="true"] {
        background: transparent !important;
        color: #b91c1c !important;
        font-weight: 700 !important;
        box-shadow: none !important;
    }
    
    /* 선택된 메뉴에 좌측 빨간 바 추가 */
    section[data-testid="stSidebar"] [role="radiogroup"] label[data-checked="true"]::before {
        content: '';
        position: absolute;
        left: 0;
        top: 50%;
        transform: translateY(-50%);
        width: 3px;
        height: 60%;
        background: #dc2626;
        border-radius: 0 2px 2px 0;
    }
    
    /* 라디오 버튼 관련 모든 요소 숨기기 */
    section[data-testid="stSidebar"] [role="radiogroup"] label > div:first-child {
        display: none !important;
    }
    
    section[data-testid="stSidebar"] [role="radiogroup"] label span[data-testid] {
        display: none !important;
    }
    
    /* 메인 컨텐츠 조정 */
    .main .block-container {
        max-width: none;
        padding-left: 2rem;
        padding-right: 2rem;
    }
    
    /* 반응형 */
    @media (max-width: 1024px) {
        section[data-testid="stSidebar"] {
            min-width: 12rem !important;
            max-width: 12rem !important;
        }
    }
    
    @media (max-width: 768px) {
        section[data-testid="stSidebar"] {
            min-width: 11rem !important;
            max-width: 11rem !important;
        }
        
        .main .block-container {
            padding-left: 1rem;
            padding-right: 1rem;
        }
    }
    
    /* ==========================================
       Dashboard workspace tabs
       ========================================== */
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tablist"],
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] div[data-baseweb="tab-list"] {
        display: flex !important;
        width: 100% !important;
        gap: 0.25rem !important;
        padding: 0.25rem !important;
        border: 1px solid #e4e7ec !important;
        border-radius: 12px !important;
        background: #f4f5f7 !important;
        box-sizing: border-box !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tab"],
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] button[data-baseweb="tab"] {
        display: flex !important;
        flex: 1 1 0 !important;
        align-items: center !important;
        justify-content: center !important;
        min-height: 42px !important;
        margin: 0 !important;
        padding: 0.625rem 1rem !important;
        border: none !important;
        border-radius: 9px !important;
        background: transparent !important;
        color: #667085 !important;
        font-size: 0.9rem !important;
        line-height: 1.2 !important;
        font-weight: 600 !important;
        box-shadow: none !important;
        transition: background-color 0.18s ease, color 0.18s ease, box-shadow 0.18s ease !important;
        cursor: pointer !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tab"] p,
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tab"] span,
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] button[data-baseweb="tab"] p,
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] button[data-baseweb="tab"] span {
        margin: 0 !important;
        color: inherit !important;
        white-space: nowrap !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tab"]:hover,
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] button[data-baseweb="tab"]:hover {
        background: #eaecf0 !important;
        color: #344054 !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tab"]:focus-visible,
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] button[data-baseweb="tab"]:focus-visible {
        outline: 2px solid #98a2b3 !important;
        outline-offset: 2px !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tab"][aria-selected="true"],
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
        background: #ffffff !important;
        color: #101828 !important;
        font-weight: 650 !important;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.08), 0 1px 3px rgba(16, 24, 40, 0.05) !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] .react-aria-SelectionIndicator,
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [data-baseweb="tab-highlight"],
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [data-baseweb="tab-border"] {
        display: none !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tabpanel"],
    section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] div[data-baseweb="tab-panel"] {
        padding-top: 1.25rem !important;
    }

    .dashboard-empty-state {
        display: flex;
        align-items: flex-start;
        gap: 0.875rem;
        padding: 1rem 1.125rem;
        border: 1px solid #e4e7ec;
        border-radius: 14px;
        background: #fbfcfd;
        color: #344054;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }

    .dashboard-empty-state__marker {
        width: 0.625rem;
        height: 0.625rem;
        margin-top: 0.35rem;
        flex: 0 0 auto;
        border-radius: 999px;
        background: #475467;
        box-shadow: 0 0 0 4px #eef2f6;
    }

    .dashboard-empty-state__title {
        color: #1d2939;
        font-size: 0.925rem;
        line-height: 1.4;
        font-weight: 650;
    }

    .dashboard-empty-state__description {
        margin-top: 0.2rem;
        color: #667085;
        font-size: 0.875rem;
        line-height: 1.55;
    }

    .dashboard-section-header {
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        gap: 1.5rem;
        margin: 0.125rem 0 1.125rem;
    }

    .dashboard-section-header__title {
        color: #101828;
        font-size: 1.25rem;
        line-height: 1.35;
        font-weight: 700;
        letter-spacing: -0.02em;
    }

    .dashboard-section-header__description {
        max-width: 42rem;
        margin-top: 0.3rem;
        color: #667085;
        font-size: 0.875rem;
        line-height: 1.55;
    }

    .dashboard-section-header__meta {
        flex: 0 0 auto;
        padding: 0.375rem 0.625rem;
        border: 1px solid #e4e7ec;
        border-radius: 8px;
        background: #f9fafb;
        color: #475467;
        font-size: 0.78rem;
        line-height: 1;
        font-weight: 650;
        white-space: nowrap;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[class*="st-key-saved_table_card_"] {
        margin-bottom: 0.875rem;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[class*="st-key-saved_table_card_"] [data-testid="stVerticalBlockBorderWrapper"] {
        padding: 1rem 1.125rem 1.125rem !important;
        border: 1px solid #e4e7ec !important;
        border-radius: 14px !important;
        background: #ffffff !important;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04) !important;
    }

    .saved-table-card__title {
        overflow: hidden;
        color: #1d2939;
        font-size: 1rem;
        line-height: 1.45;
        font-weight: 650;
        letter-spacing: -0.01em;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .saved-table-card__meta {
        margin-top: 0.25rem;
        color: #667085;
        font-size: 0.78rem;
        line-height: 1.4;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[class*="st-key-saved_table_card_"] [data-testid="stDataFrame"] {
        overflow: hidden;
        border: 1px solid #eaecf0;
        border-radius: 10px;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[class*="st-key-delete_saved_"] button {
        min-height: 2.25rem !important;
        border: 1px solid #e4e7ec !important;
        border-radius: 9px !important;
        background: #ffffff !important;
        color: #b42318 !important;
        font-size: 0.82rem !important;
        font-weight: 600 !important;
        box-shadow: none !important;
        transform: none !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) div[class*="st-key-delete_saved_"] button:hover {
        border-color: #fda29b !important;
        background: #fff5f4 !important;
        color: #912018 !important;
        transform: none !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-ai_analysis_workspace [data-testid="stVerticalBlockBorderWrapper"] {
        padding: 1.125rem !important;
        border: 1px solid #e4e7ec !important;
        border-radius: 14px !important;
        background: #fbfcfd !important;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04) !important;
    }

    .ai-workspace__intro {
        margin-bottom: 0.25rem;
    }

    .ai-workspace__title {
        color: #1d2939;
        font-size: 0.95rem;
        line-height: 1.4;
        font-weight: 650;
    }

    .ai-workspace__description,
    .ai-workspace__selection-note {
        margin-top: 0.2rem;
        color: #667085;
        font-size: 0.8rem;
        line-height: 1.5;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-ai_analysis_prompt textarea {
        border: 1px solid #d0d5dd !important;
        border-radius: 10px !important;
        background: #ffffff !important;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04) !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-ai_analysis_prompt textarea:focus {
        border-color: #667085 !important;
        box-shadow: 0 0 0 3px rgba(71, 84, 103, 0.12) !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-ai_analysis_sources [data-baseweb="tag"] {
        border: 1px solid #d0d5dd !important;
        background: #eef2f6 !important;
        color: #344054 !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-ai_analysis_sources [data-baseweb="tag"] span,
    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-ai_analysis_sources [data-baseweb="tag"] svg {
        color: #475467 !important;
        fill: currentColor !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-run_ai_analysis button {
        min-height: 2.75rem !important;
        border: 1px solid #101828 !important;
        border-radius: 10px !important;
        background: #101828 !important;
        color: #ffffff !important;
        font-weight: 650 !important;
        box-shadow: none !important;
        transform: none !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-run_ai_analysis button:hover {
        border-color: #344054 !important;
        background: #344054 !important;
        color: #ffffff !important;
        transform: none !important;
    }

    section[data-testid="stMain"]:has(.st-key-main_search) .st-key-run_ai_analysis button:disabled {
        border-color: #d0d5dd !important;
        background: #eaecf0 !important;
        color: #98a2b3 !important;
    }

    .ai-analysis-result-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
        margin: 1.5rem 0 0.75rem;
        padding-top: 1.25rem;
        border-top: 1px solid #eaecf0;
    }

    .ai-analysis-result-header__title {
        color: #101828;
        font-size: 1rem;
        line-height: 1.4;
        font-weight: 700;
    }

    .ai-analysis-result-header__meta {
        color: #667085;
        font-size: 0.78rem;
        line-height: 1.4;
        text-align: right;
    }

    @media (max-width: 640px) {
        .dashboard-section-header,
        .ai-analysis-result-header {
            align-items: flex-start;
            flex-direction: column;
            gap: 0.625rem;
        }

        section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tablist"],
        section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] div[data-baseweb="tab-list"] {
            overflow-x: auto !important;
        }

        section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] [role="tab"],
        section[data-testid="stMain"]:has(.st-key-main_search) div[data-testid="stTabs"] button[data-baseweb="tab"] {
            flex: 1 0 auto !important;
            min-width: 7rem !important;
            padding-inline: 0.75rem !important;
        }
    }
    </style>
 """, unsafe_allow_html=True)


def check_database():
    """로컬 또는 영구 데이터베이스 연결 확인"""
    if is_remote_database() and st.session_state.get("_persistent_store_ready"):
        return True
    try:
        ping_database(DB_PATH)
        if is_remote_database():
            initialize_metadata()
            bootstrap_metadata_from_local(SCRIPT_DIR / "config.json", SCRIPT_DIR / "users")
            if not db_list_tables(DB_PATH):
                st.error("Supabase 연결은 성공했지만 데이터 테이블이 아직 없습니다.")
                st.info("먼저 `migrate_to_supabase.py`를 한 번 실행해 기존 제조업 데이터를 옮겨주세요.")
                st.stop()
            st.session_state["_persistent_store_ready"] = True
    except PersistenceError as exc:
        st.error(f"데이터 저장소 연결 오류: {exc}")
        st.stop()
    return True


def read_sql_query(sql: str, params=None) -> pd.DataFrame:
    """Backend-neutral pandas query helper."""
    return db_read_dataframe(sql, DB_PATH, params=params)


def get_db_schema() -> Dict[str, List[str]]:
    """데이터베이스 스키마 정보 가져오기"""
    return db_get_schema(DB_PATH)


def get_all_tables() -> List[str]:
    """데이터베이스의 모든 테이블 목록 가져오기"""
    try:
        return db_list_tables(DB_PATH)
    except Exception as e:
        st.error(f"테이블 목록 조회 오류: {e}")
        return []


def delete_table(table_name: str) -> bool:
    """데이터베이스에서 테이블 삭제"""
    try:
        db_drop_table(table_name, DB_PATH)
        return True
    except Exception as e:
        st.error(f"테이블 삭제 오류 ({table_name}): {e}")
        return False


def delete_managed_tables() -> int:
    """JSON에 등록된 모든 테이블을 데이터베이스에서 삭제 (전체 마이그레이션용)"""
    if get_managed_tables is None:
        return 0
    
    config = load_config()
    managed_tables = get_managed_tables(config)
    
    deleted_count = 0
    for table_name in managed_tables:
        if delete_table(table_name):
            deleted_count += 1
    
    return deleted_count


# analyze_question, build_cot_prompt, generate_final_user_prompt 함수 제거됨 (CoT 설명 제거)


def generate_sql_5col(question: str) -> str:
    """JSON 기반 SQL 생성 (하드코딩 제거)"""
    try:
        config = load_config()
        if config is None:
            st.error("❌ config.json을 로드할 수 없습니다.")
            return None
        
        managed_tables = get_managed_tables(config) if get_managed_tables else []
        if not managed_tables:
            managed_tables = get_all_tables()
        
        required_columns_dict = {}
        for table_name in managed_tables:
            required_cols = get_required_columns(table_name, config) if get_required_columns else []
            if required_cols:
                required_columns_dict[table_name] = required_cols
        
        column_keywords = get_column_keywords(config) if get_column_keywords else {}
        table_aliases = config.get("table_aliases", {}) if config else {}
        
        # ★★★ 수정: JSON 형식으로 전달 ★★★
        schema_json = json.dumps({
            "required_columns": required_columns_dict,
            "table_aliases": table_aliases,
            "column_keywords": column_keywords,
            "managed_tables": managed_tables
        }, ensure_ascii=False, indent=2)
        
        # ★★★ 수정: Few-shot 예시 추가 (전역 상수 사용) ★★★
        user_prompt = f"""[스키마]
{schema_json}

{FEW_SHOT_EXAMPLES}

[질문]

{question}

SQL:"""

        # ★★★ 디버그 로깅 추가 ★★★
        print(f"[DEBUG] User prompt length: {len(user_prompt)}")
        
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": get_sql_system_prompt()},
                {"role": "user", "content": user_prompt}
            ],
            max_completion_tokens=500
        )
        
        sql = response.choices[0].message.content.strip()
        
        # ★★★ 디버그: 원본 응답 출력 ★★★
        print(f"[DEBUG] Raw LLM response: {sql[:200]}...")
        
        # 백틱 제거 (더 강화)
        sql = re.sub(r'```sql\s*', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'```\s*', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'^sql\s*', '', sql, flags=re.IGNORECASE)  # "sql SELECT..." 형태 제거
        sql = sql.strip()
        
        # ★★★ 유효성 검증 추가 ★★★
        sql_upper = sql.upper().strip()
        if not (sql_upper.startswith('SELECT') or sql_upper.startswith('WITH')):
            print(f"[ERROR] Invalid SQL (not starting with SELECT or WITH): {sql}")
            return None
        
        return sql
    except Exception as e:
        st.error(f"SQL 생성 오류: {_format_openai_exception(e)}")
        import traceback
        print(f"[ERROR] {traceback.format_exc()}")
        return None


def generate_sql_complete(user_question: str) -> str:
    """
    STEP 1~4 통합: 완전한 프롬프트 엔지니어링
    최대 5개 칼럼 버전 사용 (토큰 절약)
    """
    try:
        # 🔥 최대 5개 칼럼 버전 사용 (2500토큰 최적화)
        return generate_sql_5col(user_question)
        
        # 기존 버전 (주석 처리 - 필요시 활성화)
        # user_prompt = generate_final_user_prompt(user_question)
        # response = client.chat.completions.create(
        #     model=OPENAI_MODEL,
        #     messages=[
        #         {"role": "system", "content": SYSTEM_PROMPT_V2},
        #         {"role": "user", "content": user_prompt}
        #     ],
        #     temperature=0.05,
        #     max_tokens=2000
        # )
        # sql = response.choices[0].message.content.strip()
        # sql = re.sub(r'^```sql\s*', '', sql, flags=re.IGNORECASE)
        # sql = re.sub(r'^```\s*', '', sql)
        # sql = re.sub(r'\s*```$', '', sql)
        # sql = sql.strip()
        # return sql
    except Exception as e:
        st.error(f"SQL 생성 오류: {_format_openai_exception(e)}")
        return None


# build_dynamic_system_prompt() 함수 전체 삭제됨 (토큰 절약 - 600+ 토큰)


def generate_sql_with_openai(user_question: str) -> str:
    """OpenAI API를 사용하여 자연어를 SQL 쿼리로 변환 (동적 프롬프트 사용)"""
    # generate_sql_complete 함수 사용 (통합 버전)
    return generate_sql_complete(user_question)


def format_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    """DataFrame의 숫자 컬럼에 천단위 쉼표 적용"""
    if df.empty:
        return df
    
    # 복사본 생성 (원본 데이터 보존)
    df_formatted = df.copy()
    
    # 숫자형 컬럼 찾기
    numeric_cols = df_formatted.select_dtypes(include=['int64', 'float64', 'int32', 'float32', 'Int64', 'Float64', 'number']).columns
    
    # 각 숫자 컬럼에 천단위 쉼표 적용
    for col in numeric_cols:
        # ID 관련 컬럼은 제외 (쉼표 없이 표시) - 한글명 포함
        if col.lower() in ['id', 'journal_id', 'line_no', 'client_id', 'account_code', 'dept_code'] or \
           col in ['전표번호', '거래처코드', '계정코드', '부서코드', '원재료코드', '프로젝트코드']:
            continue
        
        # 천단위 쉼표 적용 (소수점 처리 개선)
        def format_number(x):
            if pd.isna(x):
                return ''
            try:
                # 숫자로 변환 시도
                num = float(x)
                # 정수인 경우
                if num == int(num):
                    return f'{int(num):,}'
                # 소수점이 있는 경우
                else:
                    return f'{num:,.2f}'
            except (ValueError, TypeError):
                return str(x) if x is not None else ''
        
        df_formatted[col] = df_formatted[col].apply(format_number)
    
    return df_formatted


def execute_sql_query(sql_query: str) -> pd.DataFrame:
    """SQL 쿼리를 실행하고 DataFrame 반환"""
    try:
        return db_read_dataframe(sql_query, DB_PATH)
    except Exception as e:
        st.error(f"쿼리 실행 오류: {str(e)}")
        return pd.DataFrame()


def get_schema_context() -> str:
    """config.json의 required_columns, column_keywords, managed_tables만 사용하여 스키마 정보 반환"""
    try:
        config = load_config()
        
        # managed_tables 가져오기
        managed_tables = get_managed_tables(config) if get_managed_tables else []
        if not managed_tables:
            return "관리되는 테이블이 없습니다. config.json의 managed_tables를 확인하세요.\n\n"
        
        schema_parts = []
        
        # 각 관리 테이블에 대해 required_columns만 사용
        for table_name in managed_tables:
            required_cols = get_required_columns(table_name, config) if get_required_columns else []
            if not required_cols:
                continue
            
            schema_parts.append(f"테이블: {table_name}")
            schema_parts.append(f"필수 컬럼: {', '.join([f'\"{col}\"' for col in required_cols])}")
            schema_parts.append("")
        
        # column_keywords 정보 추가
        column_keywords = get_column_keywords(config) if get_column_keywords else {}
        if column_keywords:
            schema_parts.append("컬럼 키워드 매핑:")
            for keyword_type, keywords in column_keywords.items():
                schema_parts.append(f"  {keyword_type}: {', '.join(keywords)}")
            schema_parts.append("")
        
        return "\n".join(schema_parts) if schema_parts else "스키마 정보가 없습니다.\n\n"
    except Exception as e:
        return f"스키마 정보 오류: {e}\n\n"




def generate_insight_report(df: pd.DataFrame, user_question: str, sql_query: str) -> str:
    """OpenAI API를 사용하여 데이터 기반 인사이트 보고서 생성"""
    global client  # 전역 변수 client 접근
    
    if df.empty:
        return "조회된 데이터가 없습니다."
    
    # DataFrame을 요약 통계로 변환 (GPT에게 전달할 컨텍스트)
    data_summary = f"데이터 건수: {len(df)}건\n\n"
    data_summary += f"컬럼 목록: {', '.join(df.columns.tolist())}\n\n"
    
    # 숫자형 컬럼 통계
    numeric_cols = df.select_dtypes(include=['number']).columns
    if len(numeric_cols) > 0:
        data_summary += "숫자형 컬럼 통계:\n"
        for col in numeric_cols:
            data_summary += f"- {col}: 합계={df[col].sum():,.0f}, 평균={df[col].mean():,.0f}, 최대={df[col].max():,.0f}, 최소={df[col].min():,.0f}\n"
    
    # 상위 10개 데이터 샘플
    data_summary += f"\n데이터 샘플 (상위 10건):\n{df.head(10).to_string(index=False)}"
    
    # OpenAI API 호출하여 인사이트 보고서 생성
    try:
        analysis_prompt = f"""당신은 회계 데이터 분석 전문가입니다. 다음 데이터를 분석하여 구체적이고 실용적인 보고서를 작성하세요.

사용자 질문: {user_question}

실행된 SQL: {sql_query}

데이터 요약: {data_summary}

아래 형식으로 분석 보고서를 작성하세요. 각 섹션은 반드시 구체적인 수치를 포함해야 합니다:

📊 핵심 발견사항

• 가장 중요한 발견 2-3개를 구체적인 수치와 함께 작성
• 예: "거래처A가 전체의 35%(123,456,789원)를 차지"

📈 추세 분석

• 시간별/항목별 변화 패턴을 수치로 설명
• 증가/감소율, 전월 대비 변화 등 포함

🚨 이상 징후

• 주의가 필요한 항목이나 비정상적인 패턴
• 구체적인 거래처명이나 항목명 명시

💡 실행 가능한 제안

• 데이터 기반으로 즉시 실행할 수 있는 액션 아이템 2-3개
• 예: "거래처B와의 계약 재협상 검토 필요 (비용 30% 증가)"

🎯 핵심 결론 요약 (반드시 작성!)

⚠️ 중요: 아래 형식을 정확히 따를 것! 각 항목은 반드시 새로운 줄에 작성!

1) 첫 번째 핵심 내용 (구체적 숫자 반드시 포함)

2) 두 번째 핵심 내용 (구체적 숫자 반드시 포함)

3) 세 번째 핵심 내용 (구체적 숫자 반드시 포함)

예시:
1) 총 매출 120,000,000원 (전월 대비 +15% 증가)
2) 상위 3개 거래처가 전체의 60% 차지 (72,000,000원)
3) IT 부서 지출 21,000,000원으로 예산 초과 주의

중요 규칙:
1. 각 섹션은 반드시 이모지(📊📈🚨💡🎯)로 시작
2. 각 항목은 반드시 불릿(•)으로 시작 (단, 핵심 결론 요약은 1), 2), 3) 형식)
3. HTML 태그나 코드를 절대 포함하지 마라
4. 순수 텍스트만 사용해라
5. 모든 숫자는 천단위 쉼표를 사용하세요
6. 일반적인 문구 대신 이 데이터의 구체적인 내용만 작성하세요
7. 핵심 결론 요약의 1), 2), 3)은 반드시 새로운 줄에 작성하세요!
"""
        
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "당신은 구체적인 수치 기반 분석을 제공하는 회계 데이터 분석 전문가입니다. 일반적인 문구를 사용하지 말고, 실제 데이터의 구체적인 수치와 인사이트만 제공하세요. 핵심 결론 요약은 반드시 1), 2), 3) 형식으로 각 줄에 작성하세요."},
                {"role": "user", "content": analysis_prompt}
            ],
            max_completion_tokens=1500
        )
        
        insight_report = response.choices[0].message.content.strip()
        return insight_report
        
    except Exception as e:
        return f"인사이트 생성 오류: {_format_openai_exception(e)}\n\n기본 요약:\n데이터 {len(df)}건 조회됨."


def create_visualizations(df: pd.DataFrame, user_question: str):
    """데이터프레임 기반으로 자동 시각화 생성"""
    charts = []
    
    # ⭐⭐⭐ 전문가급 색상 팔레트 정의
    PROFESSIONAL_COLORS = [
        '#667eea',  # 보라
        '#764ba2',  # 진한 보라
        '#f093fb',  # 핑크
        '#4facfe',  # 파랑
        '#00f2fe',  # 청록
        '#43e97b',  # 녹색
        '#38f9d7',  # 민트
        '#fa709a',  # 연핑크
        '#fee140',  # 노랑
        '#30cfd0'   # 하늘색
    ]
    
    # 그라데이션 색상 생성 함수
    def get_gradient_colors(num_colors):
        """그라데이션 색상 리스트 생성"""
        if num_colors <= len(PROFESSIONAL_COLORS):
            return PROFESSIONAL_COLORS[:num_colors]
        else:
            return PROFESSIONAL_COLORS * (num_colors // len(PROFESSIONAL_COLORS) + 1)
    
    # 컬럼 타입 분류
    numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
    text_cols = df.select_dtypes(include=['object']).columns.tolist()
    date_cols = [col for col in df.columns if 'date' in col.lower() or '날짜' in col or 'transaction_date' in col]
    
    # ⭐⭐⭐ 우선순위 1: 날짜 데이터가 있으면 시계열 차트 우선
    if len(date_cols) >= 1 and len(numeric_cols) >= 1:
        df_sorted = df.sort_values(by=date_cols[0])
        
        # 1-1. 라인 차트 (추세선)
        fig_line = go.Figure()
        
        # 영역 채우기
        fig_line.add_trace(go.Scatter(
            x=df_sorted[date_cols[0]],
            y=df_sorted[numeric_cols[0]],
            mode='lines',
            line={'width': 0, 'color': 'rgba(102,126,234,0)'},
            fill='tozeroy',
            fillcolor='rgba(102,126,234,0.1)',
            showlegend=False,
            hoverinfo='skip'
        ))
        
        # 메인 라인
        fig_line.add_trace(go.Scatter(
            x=df_sorted[date_cols[0]],
            y=df_sorted[numeric_cols[0]],
            mode='lines+markers',
            name=numeric_cols[0],
            line={'width': 3, 'color': '#667eea', 'shape': 'spline', 'smoothing': 0.3},
            marker={'size': 8, 'color': '#667eea', 'line': {'color': 'white', 'width': 2}},
            hovertemplate='<b>%{x}</b><br>금액: %{y:,.0f}원<br><extra></extra>'
        ))
        
        fig_line.update_layout(
            title={'text': f'📈 시간별 {numeric_cols[0]} 추이', 'font': {'size': 24, 'color': '#1a1a1a', 'family': 'Pretendard'}, 'x': 0.5, 'xanchor': 'center'},
            xaxis={'title': date_cols[0], 'showgrid': True, 'gridcolor': 'rgba(0,0,0,0.05)'},
            yaxis={'title': '금액 (원)', 'showgrid': True, 'gridcolor': 'rgba(0,0,0,0.05)'},
            template='plotly_white',
            height=520,
            paper_bgcolor='white',
            plot_bgcolor='rgba(250,250,252,1)',
            hovermode='x unified',
            font={'family': 'Pretendard, sans-serif', 'size': 12, 'color': '#333'}
        )
        
        charts.append(('line', fig_line))
        
        # 1-2. 막대 차트 (월별 비교용) - 24개월 이하만
        if len(df_sorted) <= 24:
            fig_bar = go.Figure()
            
            fig_bar.add_trace(go.Bar(
                x=df_sorted[date_cols[0]],
                y=df_sorted[numeric_cols[0]],
                name=numeric_cols[0],
                marker={'color': '#667eea', 'opacity': 0.85},
                text=df_sorted[numeric_cols[0]].apply(lambda x: f'{x:,.0f}'),
                textposition='outside',
                hovertemplate='<b>%{x}</b><br>금액: %{y:,.0f}원<br><extra></extra>'
            ))
            
            fig_bar.update_layout(
                title={'text': f'📊 월별 {numeric_cols[0]} 비교', 'font': {'size': 24, 'color': '#1a1a1a', 'family': 'Pretendard'}, 'x': 0.5, 'xanchor': 'center'},
                xaxis={'title': date_cols[0], 'tickangle': -45},
                yaxis={'title': '금액 (원)', 'showgrid': True, 'gridcolor': 'rgba(0,0,0,0.05)'},
                template='plotly_white',
                height=520,
                paper_bgcolor='white',
                plot_bgcolor='rgba(250,250,252,1)',
                font={'family': 'Pretendard, sans-serif', 'size': 12, 'color': '#333'}
            )
            
            charts.append(('bar', fig_bar))
    
    # ⭐⭐⭐ 우선순위 2: 비교 데이터 (날짜 없을 때)
    elif len(numeric_cols) >= 2 and len(text_cols) >= 1:
        plot_df = df.nlargest(10, numeric_cols[0]) if len(df) > 10 else df
        
        fig = go.Figure()
        colors = get_gradient_colors(2)
        
        for idx, col in enumerate(numeric_cols[:2]):
            fig.add_trace(go.Bar(
                name=col,
                x=plot_df[text_cols[0]],
                y=plot_df[col],
                text=plot_df[col].apply(lambda x: f'{x:,.0f}'),
                textposition='outside',
                marker={'color': colors[idx], 'opacity': 0.85},
                hovertemplate=f'<b>%{{x}}</b><br>{col}: %{{y:,.0f}}원<br><extra></extra>'
            ))
        
        fig.update_layout(
            title={'text': f'📊 {text_cols[0]}별 {", ".join(numeric_cols[:2])} 비교', 'font': {'size': 24, 'color': '#1a1a1a', 'family': 'Pretendard'}, 'x': 0.5, 'xanchor': 'center'},
            xaxis={'title': text_cols[0], 'tickangle': -45 if len(plot_df) > 5 else 0},
            yaxis={'title': '금액 (원)', 'showgrid': True, 'gridcolor': 'rgba(0,0,0,0.05)'},
            barmode='group',
            template='plotly_white',
            height=520,
            paper_bgcolor='white',
            plot_bgcolor='rgba(250,250,252,1)',
            font={'family': 'Pretendard, sans-serif', 'size': 12, 'color': '#333'}
        )
        
        charts.append(('bar', fig))
    
    # ⭐⭐⭐ 우선순위 3: 단일 비교 데이터 (막대 차트로 표시)
    elif len(numeric_cols) >= 1 and len(text_cols) >= 1 and len(date_cols) == 0:
        plot_df = df.nlargest(10, numeric_cols[0]) if len(df) > 10 else df
        
        # 데이터가 5개 이하면 파이 차트, 많으면 막대 차트
        if len(plot_df) <= 5:
            # 파이 차트
            colors = get_gradient_colors(len(plot_df))
            
            fig = go.Figure(data=[go.Pie(
                labels=plot_df[text_cols[0]],
                values=plot_df[numeric_cols[0]],
                hole=0.45,
                textinfo='label+percent',
                textposition='outside',
                marker={'colors': colors, 'line': {'color': 'white', 'width': 3}},
                hovertemplate='<b>%{label}</b><br>금액: %{value:,.0f}원<br>비율: %{percent}<br><extra></extra>'
            )])
            
            fig.update_layout(
                title={'text': f'🥧 {text_cols[0]}별 {numeric_cols[0]} 구성 비율', 'font': {'size': 24, 'color': '#1a1a1a', 'family': 'Pretendard'}, 'x': 0.5, 'xanchor': 'center'},
                template='plotly_white',
                height=550,
                paper_bgcolor='white',
                font={'family': 'Pretendard, sans-serif', 'size': 12, 'color': '#333'}
            )
            
            charts.append(('pie', fig))
        else:
            # 막대 차트
            fig = go.Figure()
            
            fig.add_trace(go.Bar(
                x=plot_df[text_cols[0]],
                y=plot_df[numeric_cols[0]],
                marker={'color': '#667eea', 'opacity': 0.85},
                text=plot_df[numeric_cols[0]].apply(lambda x: f'{x:,.0f}'),
                textposition='outside',
                hovertemplate=f'<b>%{{x}}</b><br>금액: %{{y:,.0f}}원<br><extra></extra>'
            ))
            
            fig.update_layout(
                title={'text': f'📊 {text_cols[0]}별 {numeric_cols[0]} 비교', 'font': {'size': 24, 'color': '#1a1a1a', 'family': 'Pretendard'}, 'x': 0.5, 'xanchor': 'center'},
                xaxis={'title': text_cols[0], 'tickangle': -45},
                yaxis={'title': '금액 (원)', 'showgrid': True, 'gridcolor': 'rgba(0,0,0,0.05)'},
                template='plotly_white',
                height=520,
                paper_bgcolor='white',
                plot_bgcolor='rgba(250,250,252,1)',
                font={'family': 'Pretendard, sans-serif', 'size': 12, 'color': '#333'}
            )
            
            charts.append(('bar', fig))
    
    # 4. 숫자형 컬럼이 여러 개면 히트맵 (상관관계)
    if len(numeric_cols) >= 3:
        corr_matrix = df[numeric_cols].corr()
        
        fig = go.Figure(data=go.Heatmap(
            z=corr_matrix.values,
            x=corr_matrix.columns,
            y=corr_matrix.columns,
            colorscale='RdBu',
            zmid=0,
            text=corr_matrix.values,
            texttemplate='%{text:.2f}',
            textfont={"size": 11, 'family': 'Pretendard', 'color': '#333'},
            colorbar=dict(
                title={'text': "상관계수", 'font': {'size': 12, 'family': 'Pretendard'}},
                tickfont={'size': 11, 'family': 'Pretendard'}
            ),
            hovertemplate='<b>%{x}</b> vs <b>%{y}</b><br>' +
                         '상관계수: %{z:.2f}<br>' +
                         '<extra></extra>'
        ))
        
        fig.update_layout(
            title={
                'text': '🔥 숫자형 변수 간 상관관계 히트맵',
                'font': {'size': 24, 'color': '#1a1a1a', 'family': 'Pretendard'},
                'x': 0.5,
                'xanchor': 'center',
                'y': 0.95,
                'yanchor': 'top'
            },
            template='plotly_white',
            height=520,
            paper_bgcolor='white',
            plot_bgcolor='rgba(250,250,252,1)',
            font={'family': 'Pretendard, sans-serif', 'size': 12, 'color': '#333'},
            xaxis={
                'tickangle': -45,
                'tickfont': {'size': 11, 'color': '#666'}
            },
            yaxis={
                'tickfont': {'size': 11, 'color': '#666'}
            },
            margin={'t': 100, 'b': 80, 'l': 80, 'r': 40}
        )
        charts.append(('heatmap', fig))
    
    return charts


def format_report_to_html(raw_report: str) -> str:
    """AI 생성 텍스트 보고서를 단순 텍스트로 변환 (CSS 스타일 제거)"""
    # HTML/CSS 없이 단순 마크다운 텍스트로 반환
    return raw_report


def calculate_table_height(df: pd.DataFrame) -> int:
    """데이터프레임 행 수에 맞춰 테이블 높이 계산"""
    if df.empty:
        return 100
    
    row_count = len(df)
    
    # 데이터 행수 + 1행 여유 (최대 30행)
    display_rows = min(row_count + 1, 30)
    
    # 행당 35px + 헤더 50px
    height = (display_rows * 35) + 50
    
    # 최소 높이: 150px (헤더 + 최소 2행)
    # 최대 높이: 1100px (헤더 + 30행)
    return max(150, min(height, 1100))


# ============================================================
# 로그인 및 사용자 관리
# ============================================================
USERS_DIR = SCRIPT_DIR / "users"
USERS_DB_FILE = USERS_DIR / "users_db.json"

def init_users_directory():
    """사용자 디렉토리 초기화"""
    USERS_DIR.mkdir(exist_ok=True)

def load_users_db() -> Dict:
    """사용자 데이터베이스 로드"""
    try:
        if USERS_DB_FILE.exists():
            with open(USERS_DB_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    except Exception as e:
        return {}

def save_users_db(users_db: Dict) -> bool:
    """사용자 데이터베이스 저장"""
    try:
        with open(USERS_DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(users_db, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        return False

def authenticate_user(username: str, password: str) -> bool:
    """사용자 인증"""
    if not username or not password:
        return False
    if is_remote_database():
        try:
            return remote_authenticate_user(username, password)
        except PersistenceError as exc:
            st.error(f"로그인 저장소 오류: {exc}")
            return False

    users_db = load_users_db()
    if username in users_db:
        record = users_db[username]
        encoded = record.get("password_hash")
        if encoded:
            return verify_password(password, encoded)

        # 기존 평문 레코드는 로그인 성공 시 즉시 해시 형식으로 변환한다.
        if hmac.compare_digest(str(record.get("password", "")), password):
            record["password_hash"] = hash_password(password)
            record.pop("password", None)
            save_users_db(users_db)
            return True
    return False

def create_user(username: str, password: str) -> bool:
    """새 사용자 생성"""
    if not username or not password:
        return False
    if is_remote_database():
        try:
            return remote_create_user(username, password)
        except PersistenceError as exc:
            st.error(f"회원정보 저장소 오류: {exc}")
            return False

    users_db = load_users_db()
    if username in users_db:
        return False  # 이미 존재
    users_db[username] = {
        'password_hash': hash_password(password),
        'created_at': datetime.now().isoformat()
    }
    if save_users_db(users_db):
        # 사용자 디렉토리 생성
        (USERS_DIR / username).mkdir(exist_ok=True)
        return True
    return False

def get_user_data_path(username: str, filename: str) -> Path:
    """사용자별 데이터 파일 경로 반환"""
    return USERS_DIR / username / filename

# ============================================================
# 저장된 표 파일 관리 (사용자별)
# ============================================================
def save_tables_to_file(saved_tables: List[Dict], username: str) -> bool:
    """사용자별 저장 표를 PostgreSQL 또는 로컬 개발 파일에 저장"""
    try:
        if not username:
            return False

        # DataFrame을 CSV 문자열로 변환하여 저장
        tables_data = []
        for item in saved_tables:
            table_data = {
                "query": item.get("query", ""),
                "sql": item.get("sql", ""),
                "timestamp": item.get("timestamp", ""),
                "data_csv": item["data"].to_csv(index=False) if not item["data"].empty else ""
            }
            tables_data.append(table_data)

        if is_remote_database():
            save_remote_tables(username, tables_data)
            return True

        user_file = get_user_data_path(username, "saved_tables.json")
        user_file.parent.mkdir(parents=True, exist_ok=True)
        with open(user_file, 'w', encoding='utf-8') as f:
            json.dump(tables_data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        st.error(f"저장된 표 보관 오류: {e}")
        return False

def load_tables_from_file(username: str) -> List[Dict]:
    """사용자별 저장 표를 PostgreSQL 또는 로컬 개발 파일에서 로드"""
    try:
        if not username:
            return []
        if is_remote_database():
            tables_data = load_remote_tables(username)
        else:
            user_file = get_user_data_path(username, "saved_tables.json")
            if not user_file.exists():
                return []
            with open(user_file, 'r', encoding='utf-8') as f:
                tables_data = json.load(f)
        
        # CSV 문자열을 DataFrame으로 변환
        saved_tables = []
        for item in tables_data:
            if item.get("data_csv"):
                df = pd.read_csv(io.StringIO(item["data_csv"]))
            else:
                df = pd.DataFrame()
            
            saved_tables.append({
                "query": item.get("query", ""),
                "sql": item.get("sql", ""),
                "timestamp": item.get("timestamp", ""),
                "data": df
            })
        
        return saved_tables
    except Exception as e:
        st.error(f"저장된 표 불러오기 오류: {e}")
        return []

# ============================================================
# 로그인 페이지 렌더링
# ============================================================
def is_signup_allowed() -> bool:
    """Disable public registration by default on the deployed app."""
    if not is_remote_database():
        return True
    value = os.getenv("ALLOW_SIGNUP")
    if value is None:
        try:
            value = st.secrets.get("ALLOW_SIGNUP")
        except Exception:
            value = None
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def render_login_page():
    """로그인 페이지"""
    st.markdown("<h1 style='text-align: center; margin-bottom: 3rem;'>🔐 로그인</h1>", unsafe_allow_html=True)
    
    tab1, tab2 = st.tabs(["로그인", "회원가입"])
    
    with tab1:
        st.markdown("### 로그인")
        st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        username = st.text_input("사용자명", key="login_username")
        st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        password = st.text_input("비밀번호", type="password", key="login_password")
        st.markdown("<div style='height: 2rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        if st.button("로그인", type="primary", use_container_width=True):
            if authenticate_user(username, password):
                st.session_state['logged_in'] = True
                st.session_state['username'] = username
                # 사용자별 데이터 로드
                st.session_state.saved_tables = load_tables_from_file(username)
                st.success("✅ 로그인 성공!")
                st.rerun()
            else:
                st.error("❌ 사용자명 또는 비밀번호가 올바르지 않습니다.")
    
    with tab2:
        st.markdown("### 회원가입")
        signup_allowed = is_signup_allowed()
        if not signup_allowed:
            st.info("현재 회원가입은 비활성화되어 있습니다. 관리자에게 계정 생성을 요청해주세요.")
        st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        new_username = st.text_input("새 사용자명", key="signup_username", disabled=not signup_allowed)
        st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        new_password = st.text_input("새 비밀번호", type="password", key="signup_password", disabled=not signup_allowed)
        st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        confirm_password = st.text_input("비밀번호 확인", type="password", key="signup_confirm", disabled=not signup_allowed)
        st.markdown("<div style='height: 2rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        if st.button("회원가입", type="primary", use_container_width=True, disabled=not signup_allowed):
            if not new_username or not new_password:
                st.error("❌ 사용자명과 비밀번호를 입력해주세요.")
            elif new_password != confirm_password:
                st.error("❌ 비밀번호가 일치하지 않습니다.")
            elif create_user(new_username, new_password):
                st.success(f"✅ '{new_username}' 회원가입 완료! 로그인해주세요.")
            else:
                st.error("❌ 이미 존재하는 사용자명입니다.")

# ============================================================
# 1. [수정] 세션 초기화 (로그인 상태 추가)
# ============================================================
def init_session_state():
    """세션 상태 초기화"""
    if 'messages' not in st.session_state:
        st.session_state.messages = []
    if 'sql_history' not in st.session_state:
        st.session_state.sql_history = []
    if 'vanna' not in st.session_state:
        st.session_state.vanna = None
    # 로그인 상태 초기화
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
    if 'username' not in st.session_state:
        st.session_state.username = None
    # 저장된 표는 로그인 후 사용자별로 로드
    if 'saved_tables' not in st.session_state:
        st.session_state.saved_tables = []


# ============================================================
# 2. 저장 데이터 종합 분석 함수
# ============================================================
def generate_comprehensive_report(saved_tables: List[Dict], additional_prompt: str = "", custom_system_prompt: str = None) -> str:
    """저장된 여러 표를 종합한 경영 분석 보고서를 생성합니다."""
    global client
    
    if not saved_tables:
        return "저장된 표가 없습니다."

    # 1. 저장된 데이터들을 텍스트로 변환
    combined_data_context = ""
    for idx, item in enumerate(saved_tables):
        df = item['data']
        query = item['query']
        timestamp = item.get('timestamp', '')
        
        combined_data_context += f"\n[데이터셋 #{idx+1} | 저장일시: {timestamp}]\n"
        combined_data_context += f"- 사용자 질문: {query}\n"
        combined_data_context += f"- 데이터 요약 (상위 10행):\n{df.head(10).to_string(index=False)}\n"
        
        numeric_cols = df.select_dtypes(include=['number']).columns
        if len(numeric_cols) > 0:
             stats = [f"{c} 합계={df[c].sum():,.0f}" for c in numeric_cols]
             combined_data_context += f"- 주요 수치 통계: {', '.join(stats)}\n"
        combined_data_context += "-" * 50 + "\n"

    # 2. 시스템 프롬프트: 제공된 데이터에 근거한 간결한 HTML 보고서
    if custom_system_prompt:
        system_prompt = custom_system_prompt
    else:
        system_prompt = """
    당신은 경영진이 빠르게 판단할 수 있도록 복잡한 표를 명료하게 정리하는 데이터 분석가입니다.

    다음 원칙을 반드시 지키세요.
    1. 제공된 저장 데이터만 근거로 사용하고, 외부 검색을 했다고 주장하거나 확인되지 않은 사실을 만들지 마세요.
    2. 데이터에서 직접 확인되는 사실과 분석적 해석을 명확히 구분하세요.
    3. 핵심 결론을 먼저 제시하고 전체 분량은 약 800~1200단어로 제한하세요.
    4. 결과는 Markdown이 아닌 순수 HTML만 출력하세요.
    5. 이모지, 그라데이션, 색상 배지, 강한 그림자, 과도한 강조색을 사용하지 마세요.
    6. 흰 배경, 얇은 회색 구분선, 단일 열 구조로 읽기 쉬운 보고서를 만드세요.
    """

    # 3. 사용자 프롬프트: 구체적인 디자인 가이드라인 제공
    additional_context = f"\n\n[사용자 추가 요청사항]\n{additional_prompt}\n" if additional_prompt else ""
    
    user_prompt = f"""
    아래 [저장된 표 데이터]를 **기반(base)**으로 하여 종합 경영 인사이트 보고서를 작성해줘.
    표 데이터는 분석의 핵심 근거이며, 모든 인사이트는 이 데이터에서 도출되어야 합니다.
    
    **필수 요구사항:**
    1. 제공된 표의 수치와 관계를 우선 분석하고 중요한 변화나 이상 징후를 구체적으로 설명하세요.
    2. 확인 가능한 데이터 사실과 그 사실에서 도출한 해석을 구분하세요.
    3. 핵심 요약 3개, 데이터 근거, 주요 리스크와 기회, 실행 제안 순서로 구성하세요.
    4. 실행 제안에는 우선순위, 담당 역할, 확인할 지표를 포함하되 데이터로 뒷받침되지 않는 수치를 만들지 마세요.
    5. 전체 분량은 약 800~1200단어로 제한하고 반복 설명을 피하세요.
       
    {additional_context}
    
    [디자인 요구사항]
    1. 흰색 배경, 12px 모서리, 얇은 #e4e7ec 테두리의 단일 열 구조를 사용할 것.
    2. 제목은 단색 #101828, 본문은 #344054, 보조 문구는 #667085를 사용할 것.
    3. 각 섹션은 배경색 카드 대신 충분한 여백과 얇은 구분선으로 나눌 것.
    4. 이모지, 그라데이션, 색상 배지, 강한 그림자와 과도한 강조색을 사용하지 말 것.
    5. 모든 텍스트는 가독성을 위해 적절한 줄간격(line-height: 1.6)을 유지할 것.
    6. **절대 Markdown 코드 블록(```html)을 사용하지 말고, 순수 HTML 코드만 출력할 것.**

    [분석 대상 데이터]
    {combined_data_context}

    [보고서 구조 (HTML)]
    <div style="font-family: 'Pretendard', sans-serif; background: #ffffff; padding: 2.5rem; border-radius: 16px; box-shadow: 0 10px 40px rgba(0,0,0,0.08); border: 1px solid #e2e8f0; color: #1e293b;">
        <div style="border-bottom: 2px solid #f1f5f9; padding-bottom: 1.5rem; margin-bottom: 2rem; text-align: center;">
            <h2 style="font-size: 1.8rem; font-weight: 800; margin: 0; background: linear-gradient(135deg, #1e293b 0%, #334155 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">🚀 종합 시장 및 경영 인사이트</h2>
            <p style="color: #64748b; font-size: 0.95rem; margin-top: 0.5rem;">Data Intelligence & Strategic Report</p>
        </div>

        <!-- 1. 데이터 융합 섹션 -->
        <div style="margin-bottom: 2rem;">
            <h3 style="font-size: 1.2rem; font-weight: 700; color: #0f172a; margin-bottom: 1rem; display: flex; align-items: center;">
                <span style="background: #eff6ff; color: #3b82f6; padding: 0.2rem 0.6rem; border-radius: 6px; font-size: 0.9rem; margin-right: 0.5rem;">01</span>
                🔗 데이터 융합 및 시사점
            </h3>
            <div style="background: #f8fafc; padding: 1.5rem; border-radius: 12px; font-size: 0.95rem; color: #334155; line-height: 1.8;">
                <p style="margin: 0 0 1rem 0;">
                    저장된 여러 데이터셋을 종합 분석한 결과, 다음과 같은 핵심 인사이트를 도출할 수 있습니다. 
                    표 간의 연관성과 상호작용을 깊이 있게 분석하고, 각 수치의 의미를 상세히 설명해야 합니다.
                    핵심 수치는 <b>굵게</b> 표시하여 강조하세요.
                </p>
                <p style="margin: 0;">
                    (여기에 각 데이터셋 간의 상관관계, 인과관계, 패턴, 이상 징후 등을 상세히 분석. 
                    각 데이터 포인트의 의미와 비즈니스 임팩트를 구체적으로 설명. 
                    최소 3-4개 문단으로 구성된 깊이 있는 분석 포함)
                </p>
            </div>
        </div>

        <!-- 2. 시장 동향 섹션 -->
        <div style="margin-bottom: 2rem;">
            <h3 style="font-size: 1.2rem; font-weight: 700; color: #0f172a; margin-bottom: 1rem; display: flex; align-items: center;">
                <span style="background: #f0fdf4; color: #22c55e; padding: 0.2rem 0.6rem; border-radius: 6px; font-size: 0.9rem; margin-right: 0.5rem;">02</span>
                📰 시장 동향 (Market Context)
            </h3>
            <div style="line-height: 1.7; font-size: 0.95rem;">
                (실제 검색한 최신 업계 뉴스, 트렌드, 리포트를 기반으로 한 상세 분석 내용. 최소 3개 이상의 구체적인 시장 트렌드를 제시)
            </div>
            <div style="margin-top: 1rem; padding: 1rem; background: #f8fafc; border-radius: 8px; border-left: 4px solid #22c55e;">
                <p style="margin: 0; font-size: 0.85rem; color: #64748b; font-weight: 600;">📚 참고 자료:</p>
                <ul style="margin: 0.5rem 0 0 0; padding-left: 1.5rem; font-size: 0.85rem; color: #64748b;">
                    <li>(인용한 기사/논문/리포트 출처를 여기에 명시)</li>
                </ul>
            </div>
        </div>

        <!-- 3. 리스크 및 기회 -->
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2rem;">
            <div style="background: #fef2f2; padding: 1.5rem; border-radius: 12px; border: 1px solid #fee2e2;">
                <h4 style="color: #991b1b; margin: 0 0 1rem 0; font-size: 1rem; font-weight: 700;">🚨 핵심 리스크 (Risk)</h4>
                <div style="font-size: 0.9rem; color: #7f1d1d; line-height: 1.8;">
                    <div style="margin-bottom: 1.2rem; padding-bottom: 1rem; border-bottom: 1px solid #fecaca;">
                        <strong style="display: block; margin-bottom: 0.5rem;">리스크 1: (제목)</strong>
                        <p style="margin: 0; line-height: 1.7;">(각 리스크를 3-4줄 이상 상세히 설명. 발생 가능성, 영향도, 시기 등을 포함)</p>
                    </div>
                    <div style="margin-bottom: 1.2rem; padding-bottom: 1rem; border-bottom: 1px solid #fecaca;">
                        <strong style="display: block; margin-bottom: 0.5rem;">리스크 2: (제목)</strong>
                        <p style="margin: 0; line-height: 1.7;">(상세 설명)</p>
                    </div>
                    <div>
                        <strong style="display: block; margin-bottom: 0.5rem;">리스크 3: (제목)</strong>
                        <p style="margin: 0; line-height: 1.7;">(상세 설명)</p>
                    </div>
                </div>
            </div>
            <div style="background: #eff6ff; padding: 1.5rem; border-radius: 12px; border: 1px solid #dbeafe;">
                <h4 style="color: #1e40af; margin: 0 0 1rem 0; font-size: 1rem; font-weight: 700;">🚀 성장 기회 (Opportunity)</h4>
                <div style="font-size: 0.9rem; color: #1e3a8a; line-height: 1.8;">
                    <div style="margin-bottom: 1.2rem; padding-bottom: 1rem; border-bottom: 1px solid #bfdbfe;">
                        <strong style="display: block; margin-bottom: 0.5rem;">기회 1: (제목)</strong>
                        <p style="margin: 0; line-height: 1.7;">(각 기회를 3-4줄 이상 상세히 설명. 실현 가능성, 예상 효과, 시장 규모 등을 포함)</p>
                    </div>
                    <div style="margin-bottom: 1.2rem; padding-bottom: 1rem; border-bottom: 1px solid #bfdbfe;">
                        <strong style="display: block; margin-bottom: 0.5rem;">기회 2: (제목)</strong>
                        <p style="margin: 0; line-height: 1.7;">(상세 설명)</p>
                    </div>
                    <div>
                        <strong style="display: block; margin-bottom: 0.5rem;">기회 3: (제목)</strong>
                        <p style="margin: 0; line-height: 1.7;">(상세 설명)</p>
                    </div>
                </div>
            </div>
        </div>

        <!-- 4. 실행 전략 -->
        <div style="margin-bottom: 2rem;">
            <h3 style="font-size: 1.2rem; font-weight: 700; color: #0f172a; margin-bottom: 1rem; display: flex; align-items: center;">
                <span style="background: #fff1f2; color: #e11d48; padding: 0.2rem 0.6rem; border-radius: 6px; font-size: 0.9rem; margin-right: 0.5rem;">Action</span>
                🎯 C-Level 실행 전략
            </h3>
            <div style="background: #1e293b; color: white; padding: 2rem; border-radius: 12px; font-size: 0.95rem;">
                <div style="margin-bottom: 2rem; padding-bottom: 1.5rem; border-bottom: 1px solid rgba(255,255,255,0.2);">
                    <h4 style="color: #fbbf24; margin: 0 0 0.8rem 0; font-size: 1.05rem; font-weight: 700;">전략 1: (전략 제목)</h4>
                    <ul style="margin: 0; padding-left: 1.5rem; line-height: 2.0; list-style-type: disc;">
                        <li><strong>실행 단계:</strong> (구체적인 단계별 액션)</li>
                        <li><strong>예상 일정:</strong> (시작일, 완료 목표일)</li>
                        <li><strong>필요 자원:</strong> (예산, 인력, 시스템 등)</li>
                        <li><strong>담당 조직:</strong> (부서/팀 명시)</li>
                        <li><strong>예상 비용:</strong> (구체적 금액 또는 범위)</li>
                        <li><strong>성공 지표(KPI):</strong> (측정 가능한 지표)</li>
                        <li><strong>리스크 관리:</strong> (예상 리스크 및 대응 방안)</li>
                    </ul>
                </div>
                <div style="margin-bottom: 2rem; padding-bottom: 1.5rem; border-bottom: 1px solid rgba(255,255,255,0.2);">
                    <h4 style="color: #fbbf24; margin: 0 0 0.8rem 0; font-size: 1.05rem; font-weight: 700;">전략 2: (전략 제목)</h4>
                    <ul style="margin: 0; padding-left: 1.5rem; line-height: 2.0; list-style-type: disc;">
                        <li><strong>실행 단계:</strong> (구체적인 단계별 액션)</li>
                        <li><strong>예상 일정:</strong> (시작일, 완료 목표일)</li>
                        <li><strong>필요 자원:</strong> (예산, 인력, 시스템 등)</li>
                        <li><strong>담당 조직:</strong> (부서/팀 명시)</li>
                        <li><strong>예상 비용:</strong> (구체적 금액 또는 범위)</li>
                        <li><strong>성공 지표(KPI):</strong> (측정 가능한 지표)</li>
                        <li><strong>리스크 관리:</strong> (예상 리스크 및 대응 방안)</li>
                    </ul>
                </div>
                <div>
                    <h4 style="color: #fbbf24; margin: 0 0 0.8rem 0; font-size: 1.05rem; font-weight: 700;">전략 3: (전략 제목)</h4>
                    <ul style="margin: 0; padding-left: 1.5rem; line-height: 2.0; list-style-type: disc;">
                        <li><strong>실행 단계:</strong> (구체적인 단계별 액션)</li>
                        <li><strong>예상 일정:</strong> (시작일, 완료 목표일)</li>
                        <li><strong>필요 자원:</strong> (예산, 인력, 시스템 등)</li>
                        <li><strong>담당 조직:</strong> (부서/팀 명시)</li>
                        <li><strong>예상 비용:</strong> (구체적 금액 또는 범위)</li>
                        <li><strong>성공 지표(KPI):</strong> (측정 가능한 지표)</li>
                        <li><strong>리스크 관리:</strong> (예상 리스크 및 대응 방안)</li>
                    </ul>
                </div>
            </div>
        </div>
        
        <!-- 5. 참고문헌 섹션 -->
        <div style="margin-top: 2rem; padding: 1.5rem; background: #f8fafc; border-radius: 12px; border: 1px solid #e2e8f0;">
            <h4 style="color: #1e293b; margin: 0 0 1rem 0; font-size: 1rem; font-weight: 700;">📚 참고문헌 및 출처</h4>
            <div style="font-size: 0.9rem; color: #64748b; line-height: 1.8;">
                <p style="margin: 0 0 0.5rem 0;">본 보고서 작성 시 참고한 외부 자료 목록:</p>
                <ul style="margin: 0; padding-left: 1.5rem;">
                    <li>(기사/논문/리포트 출처 1)</li>
                    <li>(기사/논문/리포트 출처 2)</li>
                    <li>(기사/논문/리포트 출처 3)</li>
                </ul>
            </div>
        </div>
    </div>
    """

    # 레거시 예시 대신 실제 모델에는 간결하고 중립적인 최종 프롬프트만 전달
    user_prompt = f"""
    아래 저장 데이터를 바탕으로 경영진용 종합 분석 보고서를 작성하세요.

    [분석 대상 데이터]
    {combined_data_context}

    {additional_context}

    [내용 구성]
    1. 핵심 요약: 가장 중요한 결론 3개
    2. 데이터 근거: 표 사이의 관계, 주요 수치, 변화 또는 이상 징후
    3. 리스크와 기회: 데이터에서 합리적으로 도출할 수 있는 항목
    4. 실행 제안: 우선순위, 담당 역할, 확인할 지표가 포함된 구체적인 다음 단계

    [출력 규칙]
    - 외부 기사, 뉴스, 논문을 검색하거나 인용했다고 주장하지 말고 제공된 데이터만 분석할 것.
    - 확인 가능한 데이터 사실과 분석적 해석을 명확히 구분할 것.
    - 이모지, 그라데이션 제목, 번호/상태 배지, 강한 색상 패널을 모두 제거할 것.
    - 흰 배경의 단일 열 레이아웃, #e4e7ec 구분선, 10~12px 모서리를 사용할 것.
    - 리스크와 기회는 중립적인 목록과 얇은 구분선으로 표현할 것.
    - 제목과 항목명을 자연스러운 한국어로 통일하고 전체 분량은 800~1200단어로 제한할 것.
    - Markdown 코드 블록 없이 순수 HTML만 반환할 것.
    """

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_completion_tokens=8000
        )
        # HTML 렌더링을 위해 반환값 그대로 전달
        html_content = response.choices[0].message.content.strip()
        
        # 마크다운 코드 블록 제거 (```html 또는 ```로 감싸진 경우)
        if html_content.startswith('```'):
            # 코드 블록 시작과 끝 제거
            lines = html_content.split('\n')
            # 첫 줄(```html 등)과 마지막 줄(```) 제거
            if lines[0].startswith('```'):
                lines = lines[1:]
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            html_content = '\n'.join(lines)
        
        return html_content
    except Exception as e:
        safe_error = html.escape(_format_openai_exception(e))
        return (
            "<div style='padding:16px 18px;border:1px solid #e4e7ec;border-radius:12px;"
            "background:#f9fafb;color:#475467;font-family:Pretendard,sans-serif;font-size:14px;'>"
            f"분석을 완료하지 못했습니다. {safe_error}</div>"
        )


def check_table_exists(table_name: str) -> bool:
    """테이블 존재 여부 확인"""
    try:
        return db_table_exists(table_name, DB_PATH)
    except Exception:
        return False


def fetch_journal_entries():
    """전표내역 데이터 조회 (JOIN 포함)"""
    try:
        # 정규화된 테이블(전표내역)이 있는지 확인
        is_normalized = check_table_exists("전표내역")
        
        if is_normalized:
            query = """
                SELECT 
                    j.전표번호, j.거래일자, 
                    j.계정코드, a.계정명, 
                    j.거래처코드, c.거래처명, 
                    j.부서코드, d.부서명, 
                    j.프로젝트코드, p.프로젝트명,
                    j.차변금액, j.대변금액, 
                    j.헤드텍스트, j.라인텍스트, j.증빙유형
                FROM 전표내역 j
                LEFT JOIN 계정과목 a ON j.계정코드 = a.계정코드
                LEFT JOIN 거래처 c ON j.거래처코드 = c.거래처코드
                LEFT JOIN 부서 d ON j.부서코드 = d.부서코드
                LEFT JOIN 프로젝트 p ON j.프로젝트코드 = p.프로젝트코드
                ORDER BY j.거래일자 DESC, j.전표번호
            """
        else:
            # 기존 '회계전표' 테이블 조회 (백업용)
            query = "SELECT * FROM 회계전표 ORDER BY 거래일자 DESC, 전표번호"
        
        df = db_read_dataframe(query, DB_PATH)
        
        # 숫자 포맷팅 적용
        df = format_numeric_columns(df)
        
        return df
    except Exception as e:
        st.error(f"전표내역 조회 오류: {e}")
        return pd.DataFrame()


def fetch_clients():
    """거래처 데이터 조회"""
    try:
        # 정규화된 '거래처' 테이블 우선 조회
        if check_table_exists('거래처'):
            query = "SELECT * FROM 거래처 ORDER BY 거래처명"
        elif check_table_exists('clients'):
            query = "SELECT * FROM clients ORDER BY client_name"
        else:
            return pd.DataFrame()
            
        df = db_read_dataframe(query, DB_PATH)
        
        # 숫자 포맷팅 적용
        df = format_numeric_columns(df)
        
        return df
    except Exception as e:
        st.error(f"거래처 조회 오류: {e}")
        return pd.DataFrame()


def fetch_accounts():
    """계정과목 데이터 조회"""
    try:
        # 정규화된 '계정과목' 테이블 우선 조회
        if check_table_exists('계정과목'):
            query = "SELECT * FROM 계정과목 ORDER BY 계정코드"
        elif check_table_exists('accounts'):
            query = "SELECT * FROM accounts ORDER BY account_code"
        else:
            return pd.DataFrame()
            
        df = db_read_dataframe(query, DB_PATH)
        
        # 숫자 포맷팅 적용
        df = format_numeric_columns(df)
        
        return df
    except Exception as e:
        st.error(f"계정과목 조회 오류: {e}")
        return pd.DataFrame()


def fetch_departments():
    """부서 데이터 조회"""
    try:
        # 정규화된 '부서' 테이블 우선 조회
        if check_table_exists('부서'):
            query = "SELECT * FROM 부서 ORDER BY 부서코드"
        elif check_table_exists('departments'):
            query = "SELECT * FROM departments ORDER BY dept_code"
        else:
            return pd.DataFrame()
            
        df = db_read_dataframe(query, DB_PATH)
        
        # 숫자 포맷팅 적용
        df = format_numeric_columns(df)
        
        return df
    except Exception as e:
        st.error(f"부서 조회 오류: {e}")
        return pd.DataFrame()

def fetch_projects():
    """프로젝트 데이터 조회"""
    try:
        if check_table_exists('프로젝트'):
            query = "SELECT * FROM 프로젝트 ORDER BY 프로젝트코드"
            df = db_read_dataframe(query, DB_PATH)
            df = format_numeric_columns(df)
            return df
        return pd.DataFrame()
    except Exception as e:
        st.error(f"프로젝트 조회 오류: {e}")
        return pd.DataFrame()


def render_dashboard_empty_state(title: str, description: str) -> None:
    """Render a neutral empty-state card inside the dashboard workspace."""
    safe_title = html.escape(title)
    safe_description = html.escape(description)
    st.markdown(f"""
    <div class="dashboard-empty-state" role="status">
        <span class="dashboard-empty-state__marker" aria-hidden="true"></span>
        <div>
            <div class="dashboard-empty-state__title">{safe_title}</div>
            <div class="dashboard-empty-state__description">{safe_description}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)


def render_dashboard_section_header(title: str, description: str, meta: str = "") -> None:
    """Render a compact header for a dashboard tab section."""
    safe_title = html.escape(title)
    safe_description = html.escape(description)
    safe_meta = html.escape(meta)
    meta_html = (
        f'<div class="dashboard-section-header__meta">{safe_meta}</div>'
        if safe_meta else ""
    )
    st.markdown(f"""
    <div class="dashboard-section-header">
        <div>
            <div class="dashboard-section-header__title" role="heading" aria-level="3">{safe_title}</div>
            <div class="dashboard-section-header__description">{safe_description}</div>
        </div>
        {meta_html}
    </div>
    """, unsafe_allow_html=True)


def render_dashboard_page():
    """대시보드 페이지 - 채팅창과 그래프"""
    # 헤더 - 제목 (중앙 정렬)
    st.markdown("<h1 style='text-align: center; color: black;'>TalkToData</h1>", unsafe_allow_html=True)
    
    # 서브 타이틀 (중앙 정렬)
    st.markdown("<p style='text-align: center; color: black; font-size: 18px; margin-bottom: 0.5rem;'>데이터와 대화하듯 회계를 분석하세요</p>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #666666; font-size: 14px; margin-bottom: 2rem;'>복잡한 쿼리 대신 자연어로, AI가 당신의 언어를 데이터 언어로 번역합니다</p>", unsafe_allow_html=True)
    
    # 공백 추가
    st.write("")
    
    # 애니메이션 placeholder JavaScript 추가 (3초마다 부드럽게 교체)
    st.markdown("""
    <script>
    const placeholders = [
        "올해 접대비 지출 100만원 이상 거래처 상위 5곳",
        "1분기와 2분기의 부가세 대급금과 예수금 변동액을 알려줘",
        "지난 3개월 동안 급여 지출이 가장 많았던 부서는?",
        "이번달 매출 상위 10개 거래처와 전월 대비 증감률"
    ];
    
    let currentIndex = 0;
    let animationTimer = null;
    
    function changePlaceholder() {
        const input = document.querySelector('input[aria-label="질문"]');
        if (!input) {
            // Streamlit의 다른 selector 시도
            const altInput = document.querySelector('div[data-testid="stTextInput"] input');
            if (altInput) {
                handlePlaceholderChange(altInput);
            }
            return;
        }
        handlePlaceholderChange(input);
    }
    
    function handlePlaceholderChange(input) {
        // 사용자가 입력 중이면 애니메이션 중지
        if (input.value && input.value.length > 0) {
            if (animationTimer) {
                clearInterval(animationTimer);
                animationTimer = null;
            }
            return;
        }
        
        // Fade out 애니메이션
        input.style.transition = 'opacity 0.5s ease-in-out';
        input.style.opacity = '0';
        
        setTimeout(() => {
            // 입력값이 여전히 없을 때만 변경
            if (!input.value || input.value.length === 0) {
                currentIndex = (currentIndex + 1) % placeholders.length;
                input.placeholder = placeholders[currentIndex];
                
                // Fade in 애니메이션
                input.style.opacity = '1';
            }
        }, 500);
    }
    
    // 페이지 로드 시 첫 placeholder 설정
    window.addEventListener('load', () => {
        const input = document.querySelector('input[aria-label="질문"]') || 
                      document.querySelector('div[data-testid="stTextInput"] input');
        if (input) {
            input.placeholder = placeholders[0];
            currentIndex = 1;
            
            // 사용자 입력 감지
            input.addEventListener('input', function() {
                if (this.value && this.value.length > 0) {
                    if (animationTimer) {
                        clearInterval(animationTimer);
                        animationTimer = null;
                    }
                } else {
                    // 입력이 비워지면 다시 애니메이션 시작
                    if (!animationTimer) {
                        animationTimer = setInterval(changePlaceholder, 3000);
                    }
                }
            });
            
            // 3초마다 변경
            animationTimer = setInterval(changePlaceholder, 3000);
        }
    });
    
    // DOMContentLoaded도 처리
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            const input = document.querySelector('input[aria-label="질문"]') || 
                          document.querySelector('div[data-testid="stTextInput"] input');
            if (input && !input.placeholder) {
                input.placeholder = placeholders[0];
                currentIndex = 1;
                animationTimer = setInterval(changePlaceholder, 3000);
            }
        });
    }
    </script>
    """, unsafe_allow_html=True)
    
    # 2. 입력창 (구분선 등 불필요한 UI 제거, 심플하게 유지)
    user_input = st.text_input(
        label="질문",
        placeholder="예: 지난달 100만원 이상 지출한 거래처 중 상위 5곳 보여줘",
        label_visibility="collapsed",
        key="main_search"
    )
    
    # 입력창과 탭 사이 간격 추가
    st.markdown("<div style='height: 2rem;'></div>", unsafe_allow_html=True)
    
    # 탭 구성 (항상 표시)
    tab1, tab2, tab3, tab4 = st.tabs(["SQL · 데이터", "저장된 표", "AI 분석", "시각화"])
    
    # 메시지 처리 (탭 외부에서 사용자 메시지 표시)
    if st.session_state.messages:
        # 큰 대화창 영역 (입력창 아래)
        st.markdown('<div class="messages-container">', unsafe_allow_html=True)
        
        for msg in st.session_state.messages:
            if msg['role'] == 'user':
                st.markdown(f"""<div class="message user fade-in"><strong>👤 사용자:</strong><br>{msg['content'].replace(chr(10), '<br>')}</div>""", unsafe_allow_html=True)
        
        st.markdown('</div>', unsafe_allow_html=True)
    
    # 현재 메시지에서 SQL과 데이터 추출
    current_sql_query = ''
    current_df = pd.DataFrame()
    current_question = ''
    
    if st.session_state.messages:
        for msg in st.session_state.messages:
            if msg['role'] == 'assistant':
                current_sql_query = msg.get('sql', '')
                current_df = msg.get('result_df', pd.DataFrame())
                current_question = msg.get('user_question', '')
                break
    
    # [탭 1] SQL & 데이터
    with tab1:
        if current_sql_query and not current_df.empty:
            st.markdown("### 상세 데이터")
            
            # 숫자 포맷팅 (기존 로직 유지)
            numeric_cols = current_df.select_dtypes(include=['number']).columns
            exclude_keywords = ['id', 'code', '코드', '번호', 'no', 'year', 'month', '일자']
            format_dict = {}
            for col in numeric_cols:
                if any(k in col.lower() for k in exclude_keywords): continue
                is_integer_like = False
                if pd.api.types.is_integer_dtype(current_df[col]): is_integer_like = True
                elif pd.api.types.is_float_dtype(current_df[col]):
                    valid_vals = current_df[col].dropna()
                    if not valid_vals.empty and valid_vals.apply(lambda x: x.is_integer()).all(): is_integer_like = True
                format_dict[col] = "{:,.0f}" if is_integer_like else "{:,.2f}"

            st.dataframe(current_df.style.format(format_dict), use_container_width=True, height=400)
            
            # [핵심 구현] 표 저장 버튼 (표 바로 아래)
            col_save, col_dummy = st.columns([1, 4])
            with col_save:
                save_key = f"save_{hash(current_sql_query)}"
                if st.button("💾 이 표 저장하기", key=save_key, help="종합 분석을 위해 이 데이터를 보관함에 저장합니다."):
                    # 데이터 저장 로직
                    saved_item = {
                        "query": current_question or 'No Question',
                        "sql": current_sql_query,
                        "data": current_df,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
                    }
                    # 중복 방지
                    if not any(s['sql'] == current_sql_query for s in st.session_state.saved_tables):
                        candidate_tables = [*st.session_state.saved_tables, saved_item]
                        username = st.session_state.get('username')
                        if not username:
                            st.error("로그인 정보를 확인할 수 없어 표를 저장하지 못했습니다.")
                        elif save_tables_to_file(candidate_tables, username):
                            st.session_state.saved_tables = candidate_tables
                            st.success(f"✅ 저장 완료! (현재 {len(candidate_tables)}개)")
                        else:
                            st.error("표를 영구 저장하지 못했습니다. 기존 보관함은 그대로 유지됩니다.")
                    else:
                        st.warning("⚠️ 이미 저장된 표입니다.")
            
            st.markdown("### 생성된 SQL")
            st.code(current_sql_query, language='sql')
        else:
            render_dashboard_empty_state(
                "분석할 질문을 입력하세요",
                "위 검색창에 자연어로 질문하면 생성된 SQL과 분석 결과를 이 영역에서 확인할 수 있습니다."
            )

    # [탭 2] 저장된 표 관리
    with tab2:
        saved_table_count = len(st.session_state.saved_tables)
        render_dashboard_section_header(
            "저장된 표",
            "분석 결과를 다시 확인하고 AI 분석에 사용할 데이터를 관리합니다.",
            f"{saved_table_count}개 저장" if saved_table_count else ""
        )
        
        if not st.session_state.saved_tables:
            render_dashboard_empty_state(
                "아직 저장된 표가 없습니다",
                "SQL · 데이터 탭에서 분석 결과를 저장하면 이곳에서 다시 확인할 수 있습니다."
            )
        else:
            # 저장된 표 목록
            for i, item in enumerate(st.session_state.saved_tables):
                # 숫자 포맷팅 (SQL & 데이터 탭과 동일한 로직)
                df_display = item['data'].copy()
                numeric_cols = df_display.select_dtypes(include=['number']).columns
                exclude_keywords = ['id', 'code', '코드', '번호', 'no', 'year', 'month', '일자']
                format_dict = {}
                for col in numeric_cols:
                    if any(k in col.lower() for k in exclude_keywords): 
                        continue
                    is_integer_like = False
                    if pd.api.types.is_integer_dtype(df_display[col]): 
                        is_integer_like = True
                    elif pd.api.types.is_float_dtype(df_display[col]):
                        valid_vals = df_display[col].dropna()
                        if not valid_vals.empty and valid_vals.apply(lambda x: x.is_integer()).all(): 
                            is_integer_like = True
                    format_dict[col] = "{:,.0f}" if is_integer_like else "{:,.2f}"

                safe_query = html.escape(str(item.get('query') or '제목 없는 분석'))
                safe_timestamp = html.escape(str(item.get('timestamp') or '저장 시각 없음'))
                row_count, column_count = df_display.shape
                table_height = min(280, max(170, 35 * (min(row_count, 7) + 1)))

                with st.container(border=True, key=f"saved_table_card_{i}"):
                    info_col, action_col = st.columns(
                        [5, 1],
                        gap="medium",
                        vertical_alignment="center"
                    )

                    with info_col:
                        st.markdown(f"""
                        <div class="saved-table-card__title">{safe_query}</div>
                        <div class="saved-table-card__meta">
                            {safe_timestamp} · {row_count:,}행 × {column_count:,}열
                        </div>
                        """, unsafe_allow_html=True)

                    with action_col:
                        if st.button(
                            "삭제",
                            key=f"delete_saved_{i}",
                            help="이 저장 데이터를 삭제합니다.",
                            use_container_width=True
                        ):
                            candidate_tables = list(st.session_state.saved_tables)
                            candidate_tables.pop(i)
                            username = st.session_state.get('username')
                            if not username:
                                st.error("로그인 정보를 확인할 수 없어 저장 데이터를 삭제하지 못했습니다.")
                            elif save_tables_to_file(candidate_tables, username):
                                st.session_state.saved_tables = candidate_tables
                                st.session_state.pop('ai_analysis_sources', None)
                                st.session_state.pop('ai_analysis_report', None)
                                st.session_state.pop('ai_analysis_report_meta', None)
                                st.rerun()
                            else:
                                st.error("저장 데이터를 영구 삭제하지 못했습니다. 기존 보관함은 그대로 유지됩니다.")

                    st.dataframe(
                        df_display.style.format(format_dict),
                        use_container_width=True,
                        hide_index=True,
                        height=table_height
                    )
    
    # [탭 3] 저장 데이터 종합 분석
    with tab3:
        saved_table_count = len(st.session_state.saved_tables)
        render_dashboard_section_header(
            "AI 분석",
            "선택한 저장 데이터를 함께 살펴보고 핵심 변화, 리스크, 실행 과제를 정리합니다.",
            f"{saved_table_count}개 데이터" if saved_table_count else ""
        )
        
        if not st.session_state.saved_tables:
            render_dashboard_empty_state(
                "AI 분석을 위한 데이터가 필요합니다",
                "SQL · 데이터 탭에서 하나 이상의 결과를 저장한 뒤 종합 분석을 시작하세요."
            )
        else:
            source_options = list(range(saved_table_count))

            def format_ai_source(source_index: int) -> str:
                source_item = st.session_state.saved_tables[source_index]
                source_df = source_item.get('data', pd.DataFrame())
                source_title = str(source_item.get('query') or '제목 없는 분석').strip()
                if len(source_title) > 54:
                    source_title = f"{source_title[:53]}…"
                return f"{source_title} · {len(source_df):,}행 × {len(source_df.columns):,}열"

            with st.container(border=True, key="ai_analysis_workspace"):
                st.markdown("""
                <div class="ai-workspace__intro">
                    <div class="ai-workspace__title">분석 설정</div>
                    <div class="ai-workspace__description">
                        함께 비교할 저장 데이터를 고르고, 필요한 경우 분석 관점을 덧붙여 주세요.
                    </div>
                </div>
                """, unsafe_allow_html=True)

                selected_source_indices = st.multiselect(
                    "분석 대상",
                    options=source_options,
                    default=source_options,
                    format_func=format_ai_source,
                    key="ai_analysis_sources",
                    help="종합 분석에 포함할 저장 데이터를 선택하세요."
                )

                user_additional_prompt = st.text_area(
                    "분석 요청 (선택)",
                    placeholder="예: 전년 대비 변동 원인과 다음 분기 리스크를 중심으로 분석해 주세요.",
                    height=110,
                    key="ai_analysis_prompt"
                )

                st.markdown(
                    f'<div class="ai-workspace__selection-note">선택한 표 {len(selected_source_indices)}개를 내부 데이터에 근거해 종합합니다.</div>',
                    unsafe_allow_html=True
                )

                if st.button(
                    "종합 분석 시작",
                    type="primary",
                    use_container_width=True,
                    key="run_ai_analysis",
                    disabled=not selected_source_indices
                ):
                    selected_tables = [
                        st.session_state.saved_tables[index]
                        for index in selected_source_indices
                    ]
                    with st.spinner("선택한 데이터를 분석하고 있습니다..."):
                        report = generate_comprehensive_report(
                            selected_tables,
                            additional_prompt=user_additional_prompt
                        )
                        st.session_state.ai_analysis_report = report
                        st.session_state.ai_analysis_report_meta = {
                            "source_count": len(selected_tables),
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
                        }

            saved_report = st.session_state.get('ai_analysis_report')
            if saved_report:
                report_meta = st.session_state.get('ai_analysis_report_meta', {})
                safe_report_time = html.escape(str(report_meta.get('created_at', '')))
                report_source_count = report_meta.get('source_count', 0)
                st.markdown(f"""
                <div class="ai-analysis-result-header">
                    <div class="ai-analysis-result-header__title">분석 결과</div>
                    <div class="ai-analysis-result-header__meta">
                        데이터 {report_source_count}개 · {safe_report_time}
                    </div>
                </div>
                """, unsafe_allow_html=True)

                import streamlit.components.v1 as components
                components.html(saved_report, height=800, scrolling=True)
    
    # [탭 4] 시각화
    with tab4:
        if current_sql_query and not current_df.empty:
            charts = create_visualizations(current_df, current_question)
            if charts:
                for chart in charts:
                    st.plotly_chart(chart[1], use_container_width=True)
            else:
                render_dashboard_empty_state(
                    "시각화할 수 있는 데이터가 없습니다",
                    "다른 질문으로 분석하거나 숫자형 데이터가 포함된 결과를 선택해 주세요."
                )
        else:
            render_dashboard_empty_state(
                "시각화할 질문을 입력하세요",
                "분석 결과에 차트로 표현할 수 있는 데이터가 있으면 이 영역에 자동으로 표시됩니다."
            )
    
    # 검색 실행
    query_to_process = None
    
    if user_input:
        if 'main_search' in st.session_state and st.session_state.main_search:
            query_to_process = st.session_state.main_search
    
    if query_to_process and query_to_process not in [msg['content'] for msg in st.session_state.messages if msg['role'] == 'user']:
        # ⭐ 새 질문 입력 시 기존 메시지 모두 삭제
        st.session_state.messages = []
        
        # 사용자 메시지 추가
        st.session_state.messages.append({
            'role': 'user',
            'content': query_to_process
        })
        
        # OpenAI로 SQL 생성
        with st.spinner("🤖 AI가 SQL 쿼리를 생성하고 있습니다..."):
            sql_query = generate_sql_with_openai(query_to_process)
        
        # SQL 생성 오류 체크
        if sql_query is None or sql_query.strip() == "":
            ai_response = "❌ SQL 쿼리가 생성되지 않았습니다.\n\n💡 팁: 질문을 더 구체적으로 작성하거나 다른 표현으로 시도해보세요."
            response_msg = {
                'role': 'assistant',
                'content': ai_response,
                'sql': None
            }
            st.session_state.messages.append(response_msg)
            st.rerun()
            return
        
        # SQL 쿼리 히스토리에 추가
        if sql_query:
            st.session_state.sql_history.append(sql_query)
        
        # SQL 실행
        with st.spinner("⚡ 쿼리를 실행하고 있습니다..."):
            df = execute_sql_query(sql_query)
        
        # 결과 처리
        if df is None or df.empty:
            if df is None:
                ai_response = f"❌ 쿼리 실행 중 오류가 발생했습니다.\n\n생성된 SQL:\n```sql\n{sql_query}\n```"
            else:
                ai_response = "조회된 데이터가 없습니다."
        else:
            # 분석 보고서는 버튼을 눌렀을 때만 생성하도록 변경 (API 절약)
            ai_response = None  # 초기에는 보고서 생성하지 않음
        
        # AI 응답 추가
        response_msg = {
            'role': 'assistant',
            'content': ai_response,  # None이면 탭2에서 버튼으로 생성
            'sql': sql_query,
            'user_question': query_to_process  # 사용자 질문 저장
        }
        
        if not df.empty:
            response_msg['result_df'] = df
        
        st.session_state.messages.append(response_msg)
        
        # 페이지 새로고침
        st.rerun()


def render_data_manager_page():
    """데이터 관리 페이지 - 동적 테이블 뷰어"""
    # 제목 제거 (탭에서 이미 표시됨)
    
    # JSON에서 관리되는 테이블 목록 가져오기
    if get_managed_tables is not None:
        config = load_config()
        managed_tables = get_managed_tables(config)
        
        # 관리되는 테이블이 없으면 DB에서 전체 테이블 가져오기 (하위 호환성)
        if not managed_tables:
            tables = get_all_tables()
            st.info("💡 아직 업로드된 테이블이 없습니다. '설정' 메뉴에서 파일을 업로드하세요.")
        else:
            # 관리되는 테이블만 필터링 (실제 DB에 존재하는 테이블만)
            all_db_tables = get_all_tables()
            tables = [t for t in managed_tables if t in all_db_tables]
            
            if not tables:
                st.warning("⚠️ JSON에 등록된 테이블이 데이터베이스에 존재하지 않습니다.")
                return
    else:
        # get_managed_tables가 없는 경우 기존 로직 사용
        tables = get_all_tables()
    
    if not tables:
        st.warning("데이터베이스에 테이블이 없습니다.")
        return

    # 테이블 선택 드롭다운
    selected_table = st.selectbox(
        "조회할 테이블 선택",
        tables,
        index=0
    )
    
    # 개별 테이블 업로드 섹션
    st.markdown("---")
    st.markdown("### 📤 개별 테이블 업로드")
    with st.container():
        st.markdown(f"**선택된 테이블: `{selected_table}`**")
        
        uploaded_file = st.file_uploader(
            f"`{selected_table}` 테이블에 업로드할 파일 선택 (.xlsx, .xls, .csv)",
            type=['xlsx', 'xls', 'csv'],
            help="선택한 테이블에 데이터를 추가하거나 덮어쓸 수 있습니다.",
            key=f"upload_{selected_table}"
        )
        
        if uploaded_file is not None:
            # 업로드 모드 선택
            upload_mode = st.radio(
                "업로드 모드",
                ["➕ 데이터 추가 (Append)", "🔄 데이터 덮어쓰기 (Replace)"],
                help="**데이터 추가**: 기존 데이터에 새 데이터를 추가합니다.\n**데이터 덮어쓰기**: 기존 데이터를 모두 삭제하고 새 데이터로 교체합니다.",
                key=f"mode_{selected_table}"
            )
            
            if st.button(f"📤 `{selected_table}` 테이블에 업로드", key=f"btn_{selected_table}"):
                try:
                    # 파일 확장자 확인
                    file_name = uploaded_file.name.lower()
                    is_csv = file_name.endswith('.csv')
                    is_excel = file_name.endswith(('.xlsx', 'xls'))
                    
                    # 파일 포인터를 처음으로 리셋
                    uploaded_file.seek(0)
                    
                    with st.spinner("📂 파일 읽는 중..."):
                        # CSV 파일 처리
                        if is_csv:
                            # 여러 인코딩 시도
                            encodings = ['utf-8', 'cp949', 'euc-kr', 'latin-1']
                            df_upload = None
                            for enc in encodings:
                                try:
                                    uploaded_file.seek(0)
                                    df_upload = pd.read_csv(uploaded_file, encoding=enc)
                                    break
                                except UnicodeDecodeError:
                                    continue
                            
                            if df_upload is None:
                                st.error("❌ CSV 파일 인코딩을 인식할 수 없습니다.")
                                return
                        
                        # Excel 파일 처리
                        elif is_excel:
                            uploaded_file.seek(0)
                            df_upload = pd.read_excel(uploaded_file, engine='openpyxl')
                        
                        else:
                            st.error("❌ 지원하지 않는 파일 형식입니다.")
                            return
                    
                    if df_upload.empty:
                        st.warning("⚠️ 업로드한 파일에 데이터가 없습니다.")
                        return
                    
                    # 업로드 파일의 모든 컬럼
                    upload_columns = list(df_upload.columns)
                    
                    # 기존 테이블 스키마 확인 (정보 표시용)
                    existing_columns = (
                        db_table_columns(selected_table, DB_PATH)
                        if db_table_exists(selected_table, DB_PATH)
                        else []
                    )
                    
                    # 컬럼 정보 표시
                    st.info(f"📊 **컬럼 정보:**")
                    st.info(f"• 기존 테이블 컬럼 수: {len(existing_columns)}개")
                    st.info(f"• 업로드 파일 컬럼 수: {len(upload_columns)}개")
                    
                    if set(upload_columns) != set(existing_columns):
                        st.warning(f"⚠️ 테이블 스키마가 업로드 파일과 다릅니다. 테이블을 재생성합니다.")
                        if existing_columns:
                            removed_cols = [col for col in existing_columns if col not in upload_columns]
                            added_cols = [col for col in upload_columns if col not in existing_columns]
                            if removed_cols:
                                st.info(f"🗑️ 제거될 컬럼: {', '.join(removed_cols)}")
                            if added_cols:
                                st.info(f"➕ 추가될 컬럼: {', '.join(added_cols)}")
                    
                    # 업로드 모드에 따라 처리
                    mode = 'append' if "추가" in upload_mode else 'replace'
                    if mode == "append" and existing_columns and set(upload_columns) != set(existing_columns):
                        st.error("추가 업로드는 기존 테이블과 컬럼이 같아야 합니다. 컬럼을 맞추거나 덮어쓰기를 선택해주세요.")
                        return
                    
                    # 덮어쓰기는 트랜잭션으로 교체하고, 추가는 기존 행을 보존한다.
                    with st.spinner(f"💾 `{selected_table}` 테이블에 데이터 저장 중..."):
                        db_write_dataframe(selected_table, df_upload, DB_PATH, if_exists=mode)
                    
                    # 🔥 config.json의 required_columns 업데이트 (업로드 파일의 모든 컬럼으로 교체)
                    with st.spinner("📋 config.json 업데이트 중..."):
                        try:
                            config = load_config()
                            if update_required_columns is not None:
                                # 업로드 파일의 모든 컬럼을 required_columns에 반영 (기존 컬럼 삭제 후 새 컬럼만)
                                success = update_required_columns(selected_table, upload_columns, config)
                                if success:
                                    st.success(f"✅ config.json에 `{selected_table}` 테이블의 필수 컬럼이 업데이트되었습니다!")
                                    st.info(f"📊 새 필수 컬럼 ({len(upload_columns)}개): {', '.join(upload_columns)}")
                                else:
                                    st.warning("⚠️ config.json 업데이트에 실패했습니다.")
                        except Exception as e:
                            st.warning(f"⚠️ config.json 업데이트 중 오류: {str(e)}")
                    
                    st.success(f"✅ `{selected_table}` 테이블에 {len(df_upload)}건의 데이터가 업로드되었습니다!")
                    st.info(f"📊 업로드된 컬럼 ({len(upload_columns)}개): {', '.join(upload_columns)}")
                    
                    # 페이지 새로고침을 위한 버튼
                    if st.button("🔄 페이지 새로고침", key=f"refresh_{selected_table}"):
                        st.rerun()
                    
                except Exception as e:
                    st.error(f"❌ 업로드 중 오류 발생: {str(e)}")
                    import traceback
                    st.code(traceback.format_exc())
    
    st.markdown("---")
    st.subheader(f"📋 {selected_table}")
    
    try:
        # 선택된 테이블 데이터 조회
        quoted_table = quote_identifier(selected_table)
        # 전표내역인 경우 정렬 적용
        if selected_table == '전표내역':
            query = f'SELECT * FROM {quoted_table} ORDER BY "거래일자" DESC, "전표번호"'
        elif selected_table == '회계전표':
             query = f'SELECT * FROM {quoted_table} ORDER BY "거래일자" DESC, "전표번호"'
        else:
            query = f"SELECT * FROM {quoted_table}"
            
        df = read_sql_query(query)
        
        if not df.empty:
            # 숫자 포맷팅
            df = format_numeric_columns(df)
            
            # 테이블 높이 계산
            table_height = calculate_table_height(df)
            
            # 데이터 표시
            st.dataframe(df, use_container_width=True, height=table_height)
            st.caption(f"총 {len(df)}건의 데이터가 있습니다.")
        else:
            st.info("데이터가 없습니다.")
            
    except Exception as e:
            st.error(f"데이터 조회 오류: {e}")


def fetch_top_contributors(start_date: datetime, end_date: datetime, limit: int = 5) -> Dict[str, pd.DataFrame]:
    """기간 내 매출/비용 상위 거래처 및 계정 조회 (증감요인 분석용)"""
    try:
        
        # 1. 매출 상위 거래처
        query_sales_client = """
        SELECT g.거래처명, SUM(j.대변금액) as 금액
        FROM 회계전표 j
        LEFT JOIN 거래처마스터 g ON j.거래처코드 = g.거래처코드
        WHERE j.계정명 LIKE '%매출%'
          AND j.거래일자 BETWEEN ? AND ?
        GROUP BY g.거래처명
        ORDER BY 금액 DESC
        LIMIT ?
        """
        df_sales_client = read_sql_query(query_sales_client, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'), limit))
        
        # 2. 비용 상위 거래처
        query_cost_client = """
        SELECT g.거래처명, SUM(j.차변금액) as 금액
        FROM 회계전표 j
        LEFT JOIN 거래처마스터 g ON j.거래처코드 = g.거래처코드
        WHERE (j.계정명 LIKE '%비용%' OR j.계정명 LIKE '%매입%')
          AND j.거래일자 BETWEEN ? AND ?
        GROUP BY g.거래처명
        ORDER BY 금액 DESC
        LIMIT ?
        """
        df_cost_client = read_sql_query(query_cost_client, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'), limit))
        
        # 3. 주요 변동 계정 (매출/비용 통합)
        query_account = """
        SELECT j.계정명, SUM(COALESCE(j.대변금액, 0) + COALESCE(j.차변금액, 0)) as 금액
        FROM 회계전표 j
        WHERE j.거래일자 BETWEEN ? AND ?
        GROUP BY j.계정명
        ORDER BY 금액 DESC
        LIMIT ?
        """
        df_account = read_sql_query(query_account, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'), limit))
        
        
        return {
            "sales_client": df_sales_client,
            "cost_client": df_cost_client,
            "account": df_account
        }
    except Exception as e:
        st.error(f"상위 거래처 조회 오류: {e}")
        return {"sales_client": pd.DataFrame(), "cost_client": pd.DataFrame(), "account": pd.DataFrame()}


def render_tax_analysis_page():
    """세무 분석 페이지"""
    
    # CSS 스타일 추가
    st.markdown("""
    <style>
    /* 카드 스타일 (세무 허브용 버튼) */
    button[data-testid="baseButton-secondary"][aria-label*="btn_"] {
        height: 200px !important;
        background-color: white !important;
        border: 1px solid #e2e8f0 !important;
        border-radius: 12px !important;
        padding: 24px !important;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06) !important;
        text-align: center !important;
        white-space: pre-line !important;
        font-size: 1rem !important;
        transition: all 0.3s ease !important;
        cursor: pointer !important;
    }
    button[data-testid="baseButton-secondary"][aria-label*="btn_"]:hover {
        transform: translateY(-5px) !important;
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05) !important;
        border-color: #cbd5e1 !important;
    }
    button[data-testid="baseButton-secondary"][aria-label*="btn_"] p {
        margin: 0 !important;
        line-height: 1.6 !important;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # 세션 상태 초기화
    if 'tax_analysis_mode' not in st.session_state:
        st.session_state.tax_analysis_mode = 'main'
    
    # 탭 구성
    tab1, tab2, tab3 = st.tabs(["💼 세무분석허브", "📊 소득금액조정합계표", "📄 세무조정계산서"])
    
    with tab1:
        # 세무분석허브 - 카드 그리드
        if st.session_state.tax_analysis_mode == 'main':
            # 헤더
            st.markdown("""
            <div style="text-align: center; margin-bottom: 2rem; padding-top: 1rem;">
                <h1 style="font-size: 1.75rem; font-weight: 700; color: #1e293b; margin-bottom: 0.5rem;">
                    💼 세무 분석 허브
                </h1>
                <p style="color: #64748b; font-size: 0.9rem; margin: 0;">
                    법인세 세무조정 분석을 선택하세요
                </p>
            </div>
            """, unsafe_allow_html=True)
            
            # 카드 버튼 스타일 CSS
            st.markdown("""
            <style>
            /* 세무분석 카드 버튼 스타일 */
            div[data-testid="stVerticalBlock"] div[data-testid="column"] button[kind="secondary"] {
                height: auto !important;
                min-height: 160px !important;
                padding: 1.5rem 1rem !important;
                border-radius: 16px !important;
                border: 1px solid #e2e8f0 !important;
                background: linear-gradient(145deg, #ffffff 0%, #f8fafc 100%) !important;
                box-shadow: 0 2px 8px rgba(0,0,0,0.04) !important;
                transition: all 0.3s ease !important;
            }
            div[data-testid="stVerticalBlock"] div[data-testid="column"] button[kind="secondary"]:hover {
                transform: translateY(-2px) !important;
                box-shadow: 0 8px 20px rgba(0,0,0,0.08) !important;
                border-color: #cbd5e1 !important;
            }
            div[data-testid="stVerticalBlock"] div[data-testid="column"] button[kind="secondary"] p {
                white-space: pre-line !important;
                line-height: 1.6 !important;
            }
            </style>
            """, unsafe_allow_html=True)
            
            # 카드 그리드 (3열 + 2열)
            col1, col2, col3 = st.columns(3, gap="medium")
            
            with col1:
                if st.button(
                    "🍽️\n\n**접대비 분석**\n\n업무추진비 한도 계산",
                    key="btn_entertainment",
                    use_container_width=True,
                    type="secondary"
                ):
                    st.session_state.tax_analysis_mode = 'entertainment'
                    st.rerun()
            
            with col2:
                if st.button(
                    "💰\n\n**부가세 납부액**\n\n매출·매입세액 분석",
                    key="btn_vat",
                    use_container_width=True,
                    type="secondary"
                ):
                    st.session_state.tax_analysis_mode = 'vat'
                    st.rerun()
            
            with col3:
                if st.button(
                    "🚗\n\n**업무용승용차**\n\n소득처분·경비처리",
                    key="btn_vehicle",
                    use_container_width=True,
                    type="secondary"
                ):
                    st.session_state.tax_analysis_mode = 'vehicle'
                    st.rerun()
            
            st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)
            
            col4, col5 = st.columns(2, gap="medium")
            
            with col4:
                if st.button(
                    "🎁\n\n**기부금 분석**\n\n한도 및 세액공제 계산",
                    key="btn_donation",
                    use_container_width=True,
                    type="secondary"
                ):
                    st.session_state.tax_analysis_mode = 'donation'
                    st.rerun()
            
            with col5:
                if st.button(
                    "🔬\n\n**R&D 세액공제**\n\n연구개발비 공제 계산",
                    key="btn_rd",
                    use_container_width=True,
                    type="secondary"
                ):
                    st.session_state.tax_analysis_mode = 'rd'
                    st.rerun()
            
        else:
            # 선택된 분석 양식 표시
            # 뒤로가기 버튼 (깔끔한 스타일)
            st.markdown("""
            <style>
            .back-btn-container {
                margin-bottom: 1.5rem;
            }
            </style>
            """, unsafe_allow_html=True)
            
            col_back, col_title = st.columns([1, 5])
            with col_back:
                if st.button("← 메뉴로 돌아가기", key="back_to_menu", type="secondary"):
                    st.session_state.tax_analysis_mode = 'main'
                    st.rerun()
            
            st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)
            
            # 선택된 기능 실행
            if st.session_state.tax_analysis_mode == 'entertainment':
                analyze_entertainment_expense()
            elif st.session_state.tax_analysis_mode == 'vat':
                analyze_vat_payment()
            elif st.session_state.tax_analysis_mode == 'donation':
                st.markdown("""
                <div style="background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%); 
                            border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem;
                            border-left: 4px solid #f59e0b;">
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <span style="font-size: 1.5rem;">🎁</span>
                        <div>
                            <div style="font-weight: 600; color: #92400e; font-size: 1rem;">기부금 분석</div>
                            <div style="color: #a16207; font-size: 0.875rem;">이 기능은 현재 준비 중입니다</div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                st.markdown("""
                **📋 구현 예정 항목:**
                - 기부금 한도 계산 (이익금액의 100% 이내, 각종 특별법에 따른 기부금 한도)
                - 기부금 세액공제 계산
                - 기부금 세부 내역 관리
                """)
            elif st.session_state.tax_analysis_mode == 'vehicle':
                st.markdown("""
                <div style="background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%); 
                            border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem;
                            border-left: 4px solid #f59e0b;">
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <span style="font-size: 1.5rem;">🚗</span>
                        <div>
                            <div style="font-weight: 600; color: #92400e; font-size: 1rem;">업무용승용차 분석</div>
                            <div style="color: #a16207; font-size: 0.875rem;">이 기능은 현재 준비 중입니다</div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                st.markdown("""
                **📋 구현 예정 항목:**
                - 업무용승용차 소득처분 금액 계산
                - 경비처리 가능 금액 분석
                - 감가상각비 처리 분석
                """)
            elif st.session_state.tax_analysis_mode == 'rd':
                st.markdown("""
                <div style="background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%); 
                            border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem;
                            border-left: 4px solid #f59e0b;">
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <span style="font-size: 1.5rem;">🔬</span>
                        <div>
                            <div style="font-weight: 600; color: #92400e; font-size: 1rem;">R&D 세액공제 분석</div>
                            <div style="color: #a16207; font-size: 0.875rem;">이 기능은 현재 준비 중입니다</div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                st.markdown("""
                **📋 구현 예정 항목:**
                - 연구개발비 세액공제 계산
                - 중소기업 특별 세액공제 적용
                - 연구개발비 세부 항목별 공제율 계산
                """)
    
    with tab2:
        st.subheader("📊 소득금액조정합계표 (별지 제15호 서식)")
        
        # --- [날짜 필터 추가] ---
        col_date1, col_date2 = st.columns(2)
        with col_date1:
            t2_start = st.date_input("조정 시작일", value=datetime(datetime.now().year, 1, 1), key="t2_start_date")
        with col_date2:
            t2_end = st.date_input("조정 종료일", value=datetime.now(), key="t2_end_date")
            
        t2_start_str = t2_start.strftime("%Y-%m-%d")
        t2_end_str = t2_end.strftime("%Y-%m-%d")

        # --- [실제 세무조정 로직 구현] ---
        def get_tax_adjustments(start_date, end_date):
            try:
                params = (start_date, end_date)

                def scalar(query: str):
                    value = read_sql_query(query, params=params).iloc[0, 0]
                    return 0 if pd.isna(value) else value
                
                # 1. 간주임대료 (Deemed Rent) 로직
                # 보증금 잔액 (Short-term deposit account 21300) - 필터 내 마지막 잔액이 아닌 총합계 기준(일반적)
                deposit_bal = scalar(
                    'SELECT SUM("대변금액"-"차변금액") FROM "회계전표" '
                    'WHERE "거래일자" BETWEEN ? AND ? AND "계정코드"=\'21300\''
                )
                # 관련 이자수익 (Account 70100)
                interest_inc = scalar(
                    'SELECT SUM("대변금액"-"차변금액") FROM "회계전표" '
                    'WHERE "거래일자" BETWEEN ? AND ? AND "계정코드"=\'70100\''
                )
                
                # 간주임대료 계산 (보증금 * 2.9% - 이자수익)
                deemed_rent_raw = (deposit_bal * 0.029) - interest_inc
                deemed_rent = max(0, int(deemed_rent_raw))
                
                # 2. 채무면제이익 / 출자전환 (Debt Waiver)
                waiver_q = (
                    'SELECT SUM("대변금액"-"차변금액") FROM "회계전표" '
                    'WHERE "거래일자" BETWEEN ? AND ? '
                    'AND ("라인텍스트" LIKE \'%채무면제%\' OR "라인텍스트" LIKE \'%출자전환%\')'
                )
                waiver_amt = scalar(waiver_q)
                
                # 3. 익금/손금 항목 (Inclusion/Exclusion)
                # 벌과금/과태료 가산세 등
                fines_q = (
                    'SELECT SUM("차변금액"-"대변금액") FROM "회계전표" '
                    'WHERE "거래일자" BETWEEN ? AND ? '
                    'AND ("라인텍스트" LIKE \'%벌금%\' OR "라인텍스트" LIKE \'%과태료%\' '
                    'OR "라인텍스트" LIKE \'%가산세%\')'
                )
                tax_non_deduct = scalar(fines_q)
                
                # 접대비 한도초과 (60600) - 기본 한도 적용 (중소기업 기준 3,600만원)
                ent_bal = scalar(
                    'SELECT SUM("차변금액"-"대변금액") FROM "회계전표" '
                    'WHERE "거래일자" BETWEEN ? AND ? AND "계정코드"=\'60600\''
                )
                # 기간 안분 계산 (개월수)
                months = max(1, (t2_end.year - t2_start.year) * 12 + t2_end.month - t2_start.month + 1)
                ent_limit = int(36000000 * (months / 12)) # 중소기업 기본한도 월할계산
                ent_excess = max(0, int(ent_bal - ent_limit))
                
                return {
                    "deemed_rent": deemed_rent,
                    "waiver_amt": waiver_amt,
                    "ent_excess": ent_excess,
                    "tax_non_deduct": tax_non_deduct
                }
            except Exception:
                return {"deemed_rent": 0, "waiver_amt": 0, "ent_excess": 0, "tax_non_deduct": 0}

        adj_data = get_tax_adjustments(t2_start_str, t2_end_str)
        
        # HTML Table for Form 15 (소득금액조정합계표)
        # 0인 항목도 로직 작동을 보여주기 위해 표기
        inclusion_rows = [
            ("접대비 한도초과액", f"{adj_data['ent_excess']:,}", "기타사외유출"),
            ("간주임대료", f"{adj_data['deemed_rent']:,}", "기타사외유출"),
            ("출자전환 채무면제이익", f"{adj_data['waiver_amt']:,}", "기타"),
            ("세금과공과(벌과금 등)", f"{adj_data['tax_non_deduct']:,}", "기타사외유출"),
            ("감가상각비 한도초과", "0", "유보"),
        ]
        
        exclusion_rows = [
            ("국세환급금이자", "250,000", "기타"),
            ("수입배당금 익금불산입", "12,000,000", "기타"),
            ("-", "-", "-"),
            ("-", "-", "-"),
            ("-", "-", "-"),
        ]

        rows_html = ""
        for i in range(max(len(inclusion_rows), len(exclusion_rows))):
            inc = inclusion_rows[i] if i < len(inclusion_rows) else ("-", "-", "-")
            exc = exclusion_rows[i] if i < len(exclusion_rows) else ("-", "-", "-")
            
            rows_html += f"""
            <tr>
                <td style="text-align:left;">{inc[0]}</td>
                <td style="text-align:right;">{inc[1]}</td>
                <td style="text-align:center;">{inc[2]}</td>
                <td style="text-align:left; border-left: 2px solid #334155;">{exc[0]}</td>
                <td style="text-align:right;">{exc[1]}</td>
                <td style="text-align:center;">{exc[2]}</td>
            </tr>
            """

        form_15_html = f"""
        <style>
            .form15-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.85rem; border: 2px solid #334155; }}
            .form15-table th, .form15-table td {{ border: 1px solid #cbd5e1; padding: 8px; }}
            .form15-header {{ background-color: #f1f5f9; font-weight: bold; text-align: center; color: #1e293b; }}
            .section-header-inc {{ background-color: #fef2f2; color: #991b1b; font-weight: bold; text-align: center; }}
            .section-header-exc {{ background-color: #f0fdf4; color: #166534; font-weight: bold; text-align: center; }}
        </style>
        
        <table class="form15-table">
            <thead>
                <tr>
                    <th colspan="3" class="section-header-inc">익금산입 및 손금불산입</th>
                    <th colspan="3" class="section-header-exc">익금불산입 및 손금산입</th>
                </tr>
                <tr class="form15-header">
                    <th width="25%">과목</th>
                    <th width="15%">금액</th>
                    <th width="10%">처분</th>
                    <th width="25%">과목</th>
                    <th width="15%">금액</th>
                    <th width="10%">처분</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
            <tfoot>
                <tr style="background-color: #f8fafc; font-weight: bold;">
                    <td>합 계</td>
                    <td style="text-align:right;">{ (sum([int(r[1].replace(',','')) for r in inclusion_rows if r[1] != '-'])):,}</td>
                    <td></td>
                    <td>합 계</td>
                    <td style="text-align:right;">{ (sum([int(r[1].replace(',','')) for r in exclusion_rows if r[1] != '-'])):,}</td>
                    <td></td>
                </tr>
            </tfoot>
        </table>
        """
        
        st.markdown(re.sub(r'^\s+', '', form_15_html, flags=re.MULTILINE), unsafe_allow_html=True)
        
        st.info("💡 **세무조정 안내:** 위 내역은 전표 데이터에서 추출된 이자수익, 임대보증금 등을 법인세법에 따라 계산한 결과입니다. (채무면제이익 및 한도초과액은 시뮬레이션 로직 포함)")

    
    with tab3:
        st.subheader("📄 법인세 과세표준 및 세액신고서")
        
        # HTML Based Report mimicking the official form
        date_today = datetime.now().strftime("%Y.%m.%d")
        
        html_form = f"""
<style>
    .form-container {{
        width: 100%;
        font-family: 'Malgun Gothic', 'Dotum', sans-serif;
        font-size: 11px;
        color: #000;
        background-color: #fff;
        border: 2px solid #000;
        padding: 5px;
    }}
    .form-header {{
        display: flex;
        border-bottom: 2px solid #000;
        margin-bottom: 5px;
    }}
    .form-header-left {{
        width: 15%;
        border-right: 1px solid #000;
        text-align: center;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }}
    .form-header-center {{
        width: 55%;
        border-right: 1px solid #000;
        text-align: center;
        font-size: 20px;
        font-weight: bold;
        padding: 15px 0;
        display: flex;
        align-items: center;
        justify-content: center;
    }}
    .form-header-right {{
        width: 30%;
        display: flex;
        flex-direction: column;
    }}
    .header-row {{
        display: flex;
        border-bottom: 1px solid #000;
        height: 50%;
    }}
    .header-row:last-child {{
        border-bottom: none;
    }}
    .header-label {{
        width: 40%;
        background-color: #f0f0f0;
        border-right: 1px solid #000;
        display: flex;
        align-items: center;
        justify-content: center;
    }}
    .header-val {{
        width: 60%;
        display: flex;
        align-items: center;
        justify-content: center;
    }}

    .form-body {{
        display: flex;
        gap: 5px;
    }}
    .col-left, .col-right {{
        width: 50%;
        display: flex;
        flex-direction: column;
        gap: 5px;
    }}

    .section-table {{
        width: 100%;
        border-collapse: collapse;
        border: 1px solid #000;
        height: 100%;
    }}
    .section-table th, .section-table td {{
        border: 1px solid #777;
        padding: 2px 4px;
    }}
    .section-table th {{
        background-color: #f5f5f5;
        text-align: center;
        font-weight: normal;
    }}
    .row-num {{
        text-align: center;
        color: #555;
        width: 25px;
        background-color: #f9f9f9;
    }}
    .input-cell {{
        text-align: right;
    }}
    
    .section-group {{
        display: flex;
        border: 1px solid #000;
    }}
    .section-title-vert {{
        width: 25px;
        background-color: #f0f0f0;
        border-right: 1px solid #000;
        display: flex;
        align-items: center;
        justify-content: center;
        text-align: center;
        padding: 5px;
        writing-mode: vertical-lr; /* Vertical text */
        font-size: 10px;
        line-height: 1.2;
        letter-spacing: 2px;
    }}
    .section-content {{
        flex: 1;
    }}
    
    /* Specific adjustments */
    td {{ height: 21px; }}
</style>

<div class="form-container">
    <!-- Header -->
    <div class="form-header">
        <div class="form-header-left">
            <div>사업<br>연도</div>
            <div style="margin-top:5px; font-size:10px;">{datetime.now().year}.01.01<br>~<br>{datetime.now().year}.12.31</div>
        </div>
        <div class="form-header-center">
            법인세 과세표준 및 세액조정계산서
        </div>
        <div class="form-header-right">
            <div class="header-row">
                <div class="header-label">법 인 명</div>
                <div class="header-val">샘플제조 주식회사</div>
            </div>
            <div class="header-row">
                <div class="header-label">사업자등록번호</div>
                <div class="header-val">123-45-67890</div>
            </div>
        </div>
    </div>

    <!-- Body -->
    <div class="form-body">
        <!-- Left Column -->
        <div class="col-left">
            
            <!-- 1. 각 사업연도 소득계산 -->
            <div class="section-group">
                <div class="section-title-vert">①각 사 업 연 도 소 득 계 산</div>
                <div class="section-content">
                    <table class="section-table" style="border:none;">
                        <tr>
                            <td colspan="2" style="text-align:center;">101 결산서상당기순손익</td>
                            <td class="row-num">01</td>
                            <td class="input-cell" width="30%">142,748,392</td>
                        </tr>
                        <tr>
                            <td rowspan="2" width="15%" style="text-align:center;">소득조정<br>금액</td>
                            <td style="text-align:center;">102 익 금 산 입</td>
                            <td class="row-num">02</td>
                            <td class="input-cell">17,844,441</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">103 손 금 산 입</td>
                            <td class="row-num">03</td>
                            <td class="input-cell">0</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">104 차 가 감 소 득 금 액<br><span style="font-size:9px">(101+102-103)</span></td>
                            <td class="row-num">04</td>
                            <td class="input-cell">160,592,833</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">105 기 부 금 한 도 초 과 액</td>
                            <td class="row-num">05</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                             <td colspan="2" style="text-align:center;">106 기부금한도초과이월액<br>손금산입</td>
                            <td class="row-num">54</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                             <td colspan="2" style="text-align:center;">107 각사업연도소득금액<br><span style="font-size:9px">(104+105-106)</span></td>
                            <td class="row-num">06</td>
                            <td class="input-cell">160,592,833</td>
                        </tr>
                    </table>
                </div>
            </div>

            <!-- 2. 과세표준 계산 -->
            <div class="section-group">
                <div class="section-title-vert">②과 세 표 준 계 산</div>
                <div class="section-content">
                    <table class="section-table" style="border:none;">
                        <tr>
                            <td style="text-align:center;">108 각사업연도소득금액<br><span style="font-size:9px">(107 = 06)</span></td>
                            <td class="row-num"></td>
                            <td class="input-cell" width="30%">160,592,833</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">109 이 월 결 손 금</td>
                            <td class="row-num">07</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">110 비 과 세 소 득</td>
                            <td class="row-num">08</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">111 소 득 공 제</td>
                            <td class="row-num">09</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">112 과 세 표 준<br><span style="font-size:9px">(108-109-110-111)</span></td>
                            <td class="row-num">10</td>
                            <td class="input-cell" style="background:#e0f2f1;">160,592,833</td>
                        </tr>
                         <tr>
                            <td style="text-align:center;">150 선 박 표 준 이 익</td>
                            <td class="row-num">55</td>
                            <td class="input-cell">-</td>
                        </tr>
                    </table>
                </div>
            </div>

            <!-- 3. 산출세액 계산 -->
            <div class="section-group">
                <div class="section-title-vert">③산 출 세 액 계 산</div>
                <div class="section-content">
                    <table class="section-table" style="border:none;">
                        <tr>
                            <td style="text-align:center;">113 과 세 표 준 ( 112 + 150 )</td>
                            <td class="row-num">56</td>
                            <td class="input-cell" width="30%">160,592,833</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">114 세            율</td>
                            <td class="row-num">11</td>
                            <td class="input-cell">19%</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">115 산   출   세   액</td>
                            <td class="row-num">12</td>
                            <td class="input-cell">30,512,638</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">116 지 점 유 보 소 득<br><span style="font-size:9px">(법인세법 제96조)</span></td>
                            <td class="row-num">13</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td style="text-align:center;">117 세            율</td>
                            <td class="row-num">14</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">118 산   출   세   액</td>
                            <td class="row-num">15</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">119 합 계 ( 115 + 118 )</td>
                            <td class="row-num">16</td>
                            <td class="input-cell" style="background:#e0f2f1;">30,512,638</td>
                        </tr>
                    </table>
                </div>
            </div>

            <!-- 4. 납부할세액 계산 (Part 1) -->
            <div class="section-group">
                <div class="section-title-vert">④납 부 할 세 액 계 산</div>
                <div class="section-content">
                    <table class="section-table" style="border:none;">
                        <tr>
                            <td colspan="2" style="text-align:center;">120 산출세액 ( 120 = 119 )</td>
                            <td class="row-num"></td>
                            <td class="input-cell" width="30%">30,512,638</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">121 최저한세 적용대상<br>공 제 감 면 세 액</td>
                            <td class="row-num">17</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">122 차 감 세 액</td>
                            <td class="row-num">18</td>
                            <td class="input-cell">30,512,638</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">123 최저한세 적용제외<br>공 제 감 면 세 액</td>
                            <td class="row-num">19</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">124 가 산 세 액</td>
                            <td class="row-num">20</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">125 가감계 ( 122 - 123 + 124 )</td>
                            <td class="row-num">21</td>
                            <td class="input-cell">30,512,638</td>
                        </tr>
                        <!-- Refactored Paid Tax Section for Readability -->
                        <tr>
                            <td colspan="2" style="text-align:center; background-color:#f0f8ff; font-weight:bold;">기 납 부 세 액 (Prepaid Tax)</td>
                            <td class="row-num" style="background-color:#e6f3ff;"></td>
                            <td class="input-cell" style="background-color:#f0f8ff;"></td>
                        </tr>
                        <tr>
                             <td colspan="2" style="text-align:center;">126 중 간 예 납 세 액</td>
                            <td class="row-num">22</td>
                            <td class="input-cell">15,000,000</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">127 수 시 부 과 세 액</td>
                            <td class="row-num">23</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">128 원 천 납 부 세 액</td>
                            <td class="row-num">24</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">129 간접투자회사등의외국납부세액</td>
                            <td class="row-num">25</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">130 소계 ( 126+127+128+129 )</td>
                            <td class="row-num">26</td>
                            <td class="input-cell">15,000,000</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">131 신고납부전가산세액</td>
                            <td class="row-num">27</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">132 합 계 ( 130 + 131 )</td>
                            <td class="row-num">28</td>
                            <td class="input-cell">15,000,000</td>
                        </tr>
                    </table>
                </div>
            </div>
        </div>

        <!-- Right Column -->
        <div class="col-right">
             <!-- 4. 납부할세액 계산 (Part 2) -->
             <div class="section-group">
                <div class="section-content">
                    <table class="section-table" style="border:none; border-top:none;">
                        <tr>
                            <td style="text-align:center;">133 감면분추가납부세액</td>
                            <td class="row-num">29</td>
                            <td class="input-cell" width="30%">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center; font-weight:bold;">134 차 감 납 부 할 세 액<br><span style="font-size:9px">( 125 - 132 + 133 )</span></td>
                            <td class="row-num">30</td>
                            <td class="input-cell" style="font-weight:bold; color:red;">15,512,638</td>
                        </tr>
                    </table>
                </div>
            </div>

            <!-- 5. 토지등 양도소득 -->
            <div class="section-group">
                <div class="section-title-vert">⑤토 지 등 양 도 소 득 에 대 한 법 인 세 계 산</div>
                <div class="section-content">
                    <table class="section-table" style="border:none;">
                         <tr>
                            <td rowspan="2" width="15%" style="text-align:center;">양도<br>차익</td>
                            <td style="text-align:center;">135 등 기 자 산</td>
                            <td class="row-num">31</td>
                            <td class="input-cell" width="30%">-</td>
                        </tr>
                         <tr>
                            <td style="text-align:center;">136 미 등 기 자 산</td>
                            <td class="row-num">32</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">137 비 과 세 소 득</td>
                            <td class="row-num">33</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">138 과 세 표 준<br><span style="font-size:9px">( 135 + 136 - 137 )</span></td>
                            <td class="row-num">34</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">139 세             율</td>
                            <td class="row-num">35</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">140 산   출   세   액</td>
                            <td class="row-num">36</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">141 감   면   세   액</td>
                            <td class="row-num">37</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">142 차 감 세 액 ( 140 - 141 )</td>
                            <td class="row-num">38</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">143 공 재 세 액</td>
                            <td class="row-num">39</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">144 동업기업법인세배분액<br><span style="font-size:9px">(가산세 제외)</span></td>
                            <td class="row-num">58</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">145 가   산   세   액</td>
                            <td class="row-num">40</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">146 가 감 계<br><span style="font-size:9px">( 142 - 143 + 144 + 145 )</span></td>
                            <td class="row-num">41</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                             <td rowspan="3" width="10%" style="text-align:center; writing-mode:vertical-lr; padding:0;">기납부세액</td>
                            <td style="text-align:center;">147 수 시 부 과 세 액</td>
                            <td class="row-num">42</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">148 ( &nbsp;&nbsp;&nbsp;&nbsp;&nbsp; ) 세 액</td>
                            <td class="row-num">43</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">149 계 ( 147 + 148 )</td>
                            <td class="row-num">44</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">150 차감납부할세액 ( 146 - 149 )</td>
                            <td class="row-num">45</td>
                            <td class="input-cell">-</td>
                        </tr>
                    </table>
                </div>
            </div>

            <!-- 6. 미환류소득 -->
            <div class="section-group">
                <div class="section-title-vert">⑥미 환 류 소 득 법 인 세</div>
                <div class="section-content">
                    <table class="section-table" style="border:none;">
                       <tr>
                            <td style="text-align:center;">161 과세대상 미환류소득</td>
                            <td class="row-num">59</td>
                            <td class="input-cell" width="30%">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">162 세           율</td>
                            <td class="row-num">60</td>
                            <td class="input-cell">20%</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">163 산   출   세   액</td>
                            <td class="row-num">61</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">164 가   산   세   액</td>
                            <td class="row-num">62</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td style="text-align:center;">165 이 자 상 당 액</td>
                            <td class="row-num">63</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td style="text-align:center;">166 납부할세액 ( 163+164+165 )</td>
                            <td class="row-num">64</td>
                            <td class="input-cell">-</td>
                        </tr>
                    </table>
                </div>
            </div>

            <!-- 7. 세액계 -->
            <div class="section-group">
                <div class="section-title-vert">⑦세 액 계</div>
                <div class="section-content">
                    <table class="section-table" style="border:none;">
                        <tr>
                            <td colspan="2" style="text-align:center;">151 차 감 납 부 할 세 액  계<br><span style="font-size:9px">( 134 + 150 + 166 )</span></td>
                            <td class="row-num">46</td>
                            <td class="input-cell" width="30%" style="font-weight:bold; color:red;">15,512,638</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">152 사실과 다른회계처리<br>경정세액공제</td>
                            <td class="row-num">57</td>
                            <td class="input-cell">-</td>
                        </tr>
                        <tr>
                            <td colspan="2" style="text-align:center;">153 분납세액 계산범위액<br><span style="font-size:9px">( 151 - 152 - 33 - 59 - 61 + 31 )</span></td>
                            <td class="row-num">47</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">154 분   납   할   세   액</td>
                            <td class="row-num">48</td>
                            <td class="input-cell">-</td>
                        </tr>
                         <tr>
                            <td colspan="2" style="text-align:center;">155 차   감   납   부   세  액<br><span style="font-size:9px">( 151 - 152 - 154 )</span></td>
                            <td class="row-num">49</td>
                            <td class="input-cell" style="font-weight:bold; color:red;">15,512,638</td>
                        </tr>
                    </table>
                </div>
            </div>
            
        </div>
    </div>
</div>
"""
        # Remove all leading whitespaces from each line to prevent Markdown code block rendering
        html_form_clean = re.sub(r'^\s+', '', html_form, flags=re.MULTILINE)
        st.markdown(html_form_clean, unsafe_allow_html=True)

        st.info("⚠️ 위 데이터는 예시 데이터입니다. 실제 DB 데이터와 연동하려면 백엔드 로직 구현이 필요합니다.")


def analyze_vat_payment():
    """부가세 납부액 분석 함수"""
    st.subheader("💰 부가세 납부액 분석")
    
    # 신고 유형 선택
    col_type, col_period = st.columns(2)
    with col_type:
        report_type = st.selectbox(
            "신고 유형",
            ["예정신고 (1기)", "확정신고 (1기)", "예정신고 (2기)", "확정신고 (2기)"],
            help="부가세 신고 유형을 선택하세요",
            key="vat_report_type"
        )
    
    with col_period:
        year = st.selectbox(
            "과세연도",
            list(range(datetime.now().year, datetime.now().year - 5, -1)),
            key="vat_year"
        )
    
    # 신고 유형에 따른 기간 자동 설정
    if "1기" in report_type:
        if "예정" in report_type:
            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 3, 31)
        else:  # 확정
            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 6, 30)
    else:  # 2기
        if "예정" in report_type:
            start_date = datetime(year, 7, 1)
            end_date = datetime(year, 9, 30)
        else:  # 확정
            start_date = datetime(year, 7, 1)
            end_date = datetime(year, 12, 31)
    
    st.info(f"📅 분석 기간: **{start_date.strftime('%Y-%m-%d')}** ~ **{end_date.strftime('%Y-%m-%d')}**")
    
    # 추가 옵션
    st.markdown("---")
    st.markdown("#### ⚙️ 불공제 매입세액 항목")
    
    col1, col2 = st.columns(2)
    with col1:
        include_entertainment = st.checkbox("접대비 관련 매입세액 불공제", value=True, 
                                           help="접대비 지출 관련 매입세액은 불공제됩니다")
        include_vehicle = st.checkbox("비영업용 승용차 관련 불공제", value=True,
                                     help="비영업용 승용차 구입/유지 관련 매입세액 불공제")
    with col2:
        include_personal = st.checkbox("개인적 공급 관련 불공제", value=True,
                                      help="사업과 무관한 개인적 지출 관련 매입세액 불공제")
        include_exempt = st.checkbox("면세사업 관련 불공제", value=False,
                                    help="면세사업에 사용되는 재화/용역 관련 매입세액")
    
    if st.button("📊 부가세 분석 실행", type="primary", use_container_width=True):
        try:
            with st.spinner("부가세 데이터를 분석 중입니다..."):
                # ==========================================
                # 1. 현재 기간 데이터 계산
                # ==========================================
                # 1.1 매출세액 계산
                과세매출, 영세매출, 면세매출, 매출세액 = calculate_vat_output(start_date, end_date)
                
                # 1.2 매입세액 계산
                총매입액, 매입세액_총액, 세금계산서_매입, 카드_매입, 현금영수증_매입 = calculate_vat_input(start_date, end_date)
                
                # 1.3 불공제 매입세액 계산
                불공제_접대비 = calculate_nondeductible_entertainment_vat(start_date, end_date) if include_entertainment else 0
                불공제_승용차 = calculate_nondeductible_vehicle_vat(start_date, end_date) if include_vehicle else 0
                불공제_개인 = calculate_nondeductible_personal_vat(start_date, end_date) if include_personal else 0
                불공제_면세 = calculate_nondeductible_exempt_vat(start_date, end_date) if include_exempt else 0
                
                불공제_합계 = 불공제_접대비 + 불공제_승용차 + 불공제_개인 + 불공제_면세
                
                # 1.4 공제가능 매입세액 & 납부세액
                공제_매입세액 = 매입세액_총액 - 불공제_합계
                납부세액 = 매출세액 - 공제_매입세액

                # ==========================================
                # 2. 비교 기간 데이터 계산 (전분기, 전년동기)
                # ==========================================
                # 전분기 날짜 계산
                prev_q_start = start_date - pd.DateOffset(months=3)
                prev_q_end = start_date - pd.DateOffset(days=1)
                
                # 전년 동기 날짜 계산
                prev_y_start = start_date - pd.DateOffset(years=1)
                prev_y_end = end_date - pd.DateOffset(years=1)
                
                # 데이터 조회 함수 (내부 헬퍼)
                def get_summary_data(s_date, e_date):
                     _, _, _, s_tax = calculate_vat_output(s_date, e_date)
                     _, p_tax_total, _, _, _ = calculate_vat_input(s_date, e_date)
                     # 불공제는 약식으로 0으로 가정하거나 전체 비율 적용 가능하지만, 여기서는 핵심 로직만 호출
                     # 정확한 비교를 위해 불공제 로직도 호출
                     n_ent = calculate_nondeductible_entertainment_vat(s_date, e_date)
                     n_car = calculate_nondeductible_vehicle_vat(s_date, e_date)
                     n_per = calculate_nondeductible_personal_vat(s_date, e_date)
                     n_exe = calculate_nondeductible_exempt_vat(s_date, e_date)
                     n_total = n_ent + n_car + n_per + n_exe
                     return s_tax, p_tax_total - n_total
                
                # 전분기 데이터
                prev_q_sales_tax, prev_q_purchase_tax = get_summary_data(prev_q_start, prev_q_end)
                prev_q_payable = prev_q_sales_tax - prev_q_purchase_tax
                
                # 전년 동기 데이터
                prev_y_sales_tax, prev_y_purchase_tax = get_summary_data(prev_y_start, prev_y_end)
                prev_y_payable = prev_y_sales_tax - prev_y_purchase_tax
                
                # 비교 데이터 딕셔너리
                comparison_data = {
                    "prev_q": {
                        "sales_tax": prev_q_sales_tax,
                        "purchase_tax": prev_q_purchase_tax,
                        "payable": prev_q_payable
                    },
                    "prev_y": {
                        "sales_tax": prev_y_sales_tax,
                        "purchase_tax": prev_y_purchase_tax,
                        "payable": prev_y_payable
                    }
                }

                # ==========================================
                # 3. 증감요인 분석 (상위 거래처/계정)
                # ==========================================
                top_contributors = fetch_top_contributors(start_date, end_date)
                
                # 6. 결과 표시
                display_vat_analysis_results(
                    report_type, year, start_date, end_date,
                    과세매출, 영세매출, 면세매출, 매출세액,
                    총매입액, 매입세액_총액, 세금계산서_매입, 카드_매입, 현금영수증_매입,
                    불공제_접대비, 불공제_승용차, 불공제_개인, 불공제_면세, 불공제_합계,
                    공제_매입세액, 납부세액,
                    comparison_data, top_contributors # 추가된 인자
                )
                
        except Exception as e:
            st.error(f"❌ 분석 중 오류 발생: {str(e)}")
            import traceback
            st.code(traceback.format_exc())


def calculate_vat_output(start_date, end_date):
    """매출세액 계산 (과세/영세/면세 구분)"""
    try:
        
        # 과세매출 (일반 매출)
        # 수정: 이자수익, 배당금수익, 선수수익 등 부가세 과세표준이 아닌 계정 제외
        query_taxable = """
        SELECT COALESCE(SUM(대변금액), 0) as 매출액
        FROM 회계전표
        WHERE (계정명 LIKE '%매출%' OR 계정명 LIKE '%수익%')
          AND 계정명 NOT LIKE '%면세%'
          AND 계정명 NOT LIKE '%영세%'
          AND 계정명 NOT LIKE '%수출%'
          AND 계정명 NOT LIKE '%이자수익%' 
          AND 계정명 NOT LIKE '%배당금%' 
          AND 계정명 NOT LIKE '%선수수익%' 
          AND 계정명 NOT LIKE '%외환차익%'
          AND 계정명 NOT LIKE '%외화환산이익%'
          AND 거래일자 BETWEEN ? AND ?
        """
        df_taxable = read_sql_query(query_taxable, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        과세매출 = float(df_taxable.iloc[0]['매출액']) if not df_taxable.empty else 0.0
        
        # 영세율매출 (수출 등)
        query_zero = """
        SELECT COALESCE(SUM(대변금액), 0) as 매출액
        FROM 회계전표
        WHERE (계정명 LIKE '%수출%' OR 계정명 LIKE '%영세%')
          AND 거래일자 BETWEEN ? AND ?
        """
        df_zero = read_sql_query(query_zero, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        영세매출 = float(df_zero.iloc[0]['매출액']) if not df_zero.empty else 0.0
        
        # 면세매출
        query_exempt = """
        SELECT COALESCE(SUM(대변금액), 0) as 매출액
        FROM 회계전표
        WHERE 계정명 LIKE '%면세%'
          AND 거래일자 BETWEEN ? AND ?
        """
        df_exempt = read_sql_query(query_exempt, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        면세매출 = float(df_exempt.iloc[0]['매출액']) if not df_exempt.empty else 0.0
        
        
        # 매출세액 = 과세매출 × 10%
        매출세액 = int(과세매출 * 0.1)
        
        return 과세매출, 영세매출, 면세매출, 매출세액
        
    except Exception as e:
        st.error(f"매출세액 계산 오류: {e}")
        return 0, 0, 0, 0


def calculate_vat_input(start_date, end_date):
    """매입세액 계산 (증빙유형별)"""
    try:
        
        # 총 매입액 (부가세대급금 또는 매입 계정)
        query_total = """
        SELECT COALESCE(SUM(차변금액), 0) as 매입액
        FROM 회계전표
        WHERE (계정명 LIKE '%매입%' OR 계정명 LIKE '%원재료%' OR 계정명 LIKE '%상품%' 
               OR 계정명 LIKE '%소모품%' OR 계정명 LIKE '%비용%')
          AND 거래일자 BETWEEN ? AND ?
        """
        df_total = read_sql_query(query_total, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        총매입액 = float(df_total.iloc[0]['매입액']) if not df_total.empty else 0.0
        
        # 부가세대급금 (실제 매입세액)
        query_vat = """
        SELECT COALESCE(SUM(차변금액), 0) as 세액
        FROM 회계전표
        WHERE 계정명 LIKE '%부가세대급금%'
          AND 거래일자 BETWEEN ? AND ?
        """
        df_vat = read_sql_query(query_vat, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        매입세액_총액 = float(df_vat.iloc[0]['세액']) if not df_vat.empty else 0.0
        
        # 부가세대급금이 없으면 매입액의 10/110으로 추정
        if 매입세액_총액 == 0 and 총매입액 > 0:
            매입세액_총액 = int(총매입액 * 10 / 110)
        
        # 세금계산서 매입
        # 수정: 실제 DB의 증빙유형 값('과세', '매입', '불공제', '영세율') 반영
        query_invoice = """
        SELECT COALESCE(SUM(차변금액), 0) as 매입액
        FROM 회계전표
        WHERE (계정명 LIKE '%매입%' OR 계정명 LIKE '%원재료%' OR 계정명 LIKE '%상품%' OR 계정명 LIKE '%비용%')
          AND (증빙유형 IN ('과세', '매입', '불공제', '영세율', '수입') OR 증빙유형 LIKE '%전자%' OR 증빙유형 LIKE '%세금%')
          AND 거래일자 BETWEEN ? AND ?
        """
        df_invoice = read_sql_query(query_invoice, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        세금계산서_매입 = float(df_invoice.iloc[0]['매입액']) if not df_invoice.empty else 0.0
        
        # 신용카드 매입
        # 증빙유형에 '카드', '카과' 등이 포함될 수 있음
        query_card = """
        SELECT COALESCE(SUM(차변금액), 0) as 매입액
        FROM 회계전표
        WHERE (계정명 LIKE '%매입%' OR 계정명 LIKE '%비용%' OR 계정명 LIKE '%소모품%')
          AND (증빙유형 LIKE '%카드%' OR 증빙유형 LIKE '%신용%' OR 증빙유형 LIKE '%카과%')
          AND 거래일자 BETWEEN ? AND ?
        """
        df_card = read_sql_query(query_card, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        카드_매입 = float(df_card.iloc[0]['매입액']) if not df_card.empty else 0.0
        
        # 현금영수증 매입
        query_cash = """
        SELECT COALESCE(SUM(차변금액), 0) as 매입액
        FROM 회계전표
        WHERE (계정명 LIKE '%매입%' OR 계정명 LIKE '%비용%')
          AND (증빙유형 LIKE '%현금%' OR 증빙유형 LIKE '%현영%')
          AND 거래일자 BETWEEN ? AND ?
        """
        df_cash = read_sql_query(query_cash, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        현금영수증_매입 = float(df_cash.iloc[0]['매입액']) if not df_cash.empty else 0.0
        
        
        return 총매입액, 매입세액_총액, 세금계산서_매입, 카드_매입, 현금영수증_매입
        
    except Exception as e:
        st.error(f"매입세액 계산 오류: {e}")
        return 0, 0, 0, 0, 0


def calculate_nondeductible_entertainment_vat(start_date, end_date):
    """접대비 관련 불공제 매입세액"""
    try:
        # 증빙유형이 '불공제'인 경우도 포함하거나, 계정명으로 판단
        query = """
        SELECT COALESCE(SUM(차변금액), 0) as 금액
        FROM 회계전표
        WHERE (계정명 LIKE '%접대비%' OR (증빙유형 = '불공제' AND 계정명 LIKE '%접대%'))
          AND 거래일자 BETWEEN ? AND ?
        """
        df = read_sql_query(query, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        접대비 = float(df.iloc[0]['금액']) if not df.empty else 0.0
        # 접대비의 부가세 상당액 (10/110)
        return int(접대비 * 10 / 110)
    except:
        return 0


def calculate_nondeductible_vehicle_vat(start_date, end_date):
    """비영업용 승용차 관련 불공제 매입세액"""
    try:
        query = """
        SELECT COALESCE(SUM(차변금액), 0) as 금액
        FROM 회계전표
        WHERE (계정명 LIKE '%차량%' OR 계정명 LIKE '%승용차%' OR 계정명 LIKE '%자동차%')
          AND (계정명 LIKE '%유지%' OR 계정명 LIKE '%수선%' OR 계정명 LIKE '%보험%' 
               OR 계정명 LIKE '%감가상각%' OR 라인텍스트 LIKE '%주유%' OR 라인텍스트 LIKE '%유류%')
          AND 거래일자 BETWEEN ? AND ?
        """
        df = read_sql_query(query, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        차량비 = float(df.iloc[0]['금액']) if not df.empty else 0.0
        return int(차량비 * 10 / 110)
    except:
        return 0


def calculate_nondeductible_personal_vat(start_date, end_date):
    """개인적 공급 관련 불공제 매입세액"""
    try:
        query = """
        SELECT COALESCE(SUM(차변금액), 0) as 금액
        FROM 회계전표
        WHERE (라인텍스트 LIKE '%개인%' OR 라인텍스트 LIKE '%사적%' OR 라인텍스트 LIKE '%가사%')
          AND 거래일자 BETWEEN ? AND ?
        """
        df = read_sql_query(query, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        개인비용 = float(df.iloc[0]['금액']) if not df.empty else 0.0
        return int(개인비용 * 10 / 110)
    except:
        return 0


def calculate_nondeductible_exempt_vat(start_date, end_date):
    """면세사업 관련 불공제 매입세액"""
    try:
        query = """
        SELECT COALESCE(SUM(차변금액), 0) as 금액
        FROM 회계전표
        WHERE (라인텍스트 LIKE '%면세%' OR 계정명 LIKE '%면세%')
          AND (계정명 LIKE '%매입%' OR 계정명 LIKE '%비용%')
          AND 거래일자 BETWEEN ? AND ?
        """
        df = read_sql_query(query, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        면세매입 = float(df.iloc[0]['금액']) if not df.empty else 0.0
        return int(면세매입 * 10 / 110)
    except:
        return 0



def display_vat_analysis_results(report_type, year, start_date, end_date,
                                 과세매출, 영세매출, 면세매출, 매출세액,
                                 총매입액, 매입세액_총액, 세금계산서_매입, 카드_매입, 현금영수증_매입,
                                 불공제_접대비, 불공제_승용차, 불공제_개인, 불공제_면세, 불공제_합계,
                                 공제_매입세액, 납부세액,
                                 comparison_data=None, top_contributors=None):
    """부가세 분석 결과 HTML 리포트 (접대비 분석 스타일 적용)"""
    
    # 데이터 포맷팅 헬퍼
    def fmt(val):
        return f"{int(val):,}"


    # 비교 데이터 안전하게 가져오기
    prev_q = comparison_data.get('prev_q', {}) if comparison_data else {}
    prev_y = comparison_data.get('prev_y', {}) if comparison_data else {}
    
    # 날짜 포맷팅
    기간_시작 = start_date.strftime('%Y.%m.%d')
    기간_종료 = end_date.strftime('%Y.%m.%d')
    분기_표시 = "1기" if start_date.month < 7 else "2기"
    신고_구분 = "예정" if "예정" in report_type else "확정"
    
    # 납부/환급 상태 확인
    is_payment = 납부세액 >= 0
    납부환급_상태 = "납부" if is_payment else "환급"
    납부환급_색상 = "#b91c1c" if is_payment else "#15803d" 

    # --- 계산 로직 보완 (테이블 표시용) ---
    # 1. 매출 관련
    # 과세매출 -> 세액은 별도 계산되어 있음 (매출세액)
    # 영세매출 -> 세액 0
    매출_소계_금액 = 과세매출 + 영세매출 + 면세매출
    매출_소계_세액 = 매출세액
    
    # 2. 매입 관련
    # 세금계산서 매입 세액 (추정: 10%)
    세금계산서_세액 = int(세금계산서_매입 * 0.1)
    
    # 그 밖의 공제 매입세액 (신용카드 + 현금영수증)
    기타공제_매입액 = 카드_매입 + 현금영수증_매입
    기타공제_세액 = int(기타공제_매입액 * 0.1)
    
    # 매입 총계 (불공제 포함 전)
    매입_소계_금액 = 세금계산서_매입 + 기타공제_매입액
    매입_소계_세액 = 세금계산서_세액 + 기타공제_세액
    
    # 공제받지 못할 매입세액 (불공제)
    # 불공제_합계는 '세액'임. 공급가액 역산 (약식)
    불공제_공급가액 = 불공제_합계 * 10
    
    # 차가감 계 (공제받을 매입세액)
    차가감_매입액 = 매입_소계_금액 - 불공제_공급가액
    차가감_매입세액 = 매입_소계_세액 - 불공제_합계
    
    # HTML 생성
    html_report = f"""
    <style>
        @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
        .vat-container {{
            font-family: 'Pretendard', sans-serif;
            background: #ffffff;
            padding: 2rem;
            border-radius: 4px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            border: 1px solid #e2e8f0;
            color: #1e293b;
            max-width: 1000px;
            margin: 0 auto;
        }}
        
        /* 타이틀 섹션 */
        .report-header {{
            text-align: center;
            border-bottom: 3px solid #0f172a;
            padding-bottom: 1rem;
            margin-bottom: 2rem;
        }}
        .report-title {{
            font-size: 1.8rem;
            font-weight: 800;
            color: #0f172a;
            margin: 0;
            letter-spacing: -0.5px;
        }}
        .report-period {{
            margin-top: 0.5rem;
            font-size: 1rem;
            color: #64748b;
            font-weight: 500;
        }}
        
        /* 폼 테이블 스타일 */
        .form-section-title {{
            font-size: 1.1rem;
            font-weight: 700;
            color: #0f172a;
            margin-bottom: 0.5rem;
            border-left: 4px solid #3b82f6;
            padding-left: 0.8rem;
            margin-top: 2rem;
        }}
        
        .form-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
            border-top: 2px solid #334155;
            border-bottom: 1px solid #cbd5e1;
            margin-bottom: 1rem;
        }}
        
        .form-table th {{
            background-color: #f1f5f9;
            color: #334155;
            font-weight: 600;
            padding: 0.6rem;
            border: 1px solid #cbd5e1;
            text-align: center;
        }}
        
        .form-table td {{
            padding: 0.6rem 0.8rem;
            border: 1px solid #cbd5e1;
            color: #1e293b;
        }}
        
        /* 컬럼 너비 조정 */
        .col-category {{ width: 15%; background-color: #f8fafc; font-weight: 600; text-align: center; }}
        .col-item {{ width: 25%; }}
        .col-amount {{ width: 25%; text-align: right; font-family: 'Courier New', monospace; font-weight: 500; }}
        .col-tax {{ width: 25%; text-align: right; font-family: 'Courier New', monospace; font-weight: 500; }}
        .col-note {{ width: 10%; text-align: center; color: #64748b; }}
        
        /* 특별한 행 스타일 */
        .row-subtotal {{ background-color: #fffbeb; font-weight: 700; }}
        .row-total {{ background-color: #f0fdf4; font-weight: 700; border-top: 2px solid #16a34a; }}
        .row-deduction {{ color: #dc2626; }}
        
        /* 양수/음수 색상 */
        .amt-plus {{ color: #1e293b; }}
        .amt-minus {{ color: #dc2626; }}
        .amt-result {{ color: {납부환급_색상}; }}

        .unit-label {{
            font-size: 0.8rem;
            color: #64748b;
            text-align: right;
            margin-bottom: 0.2rem;
        }}

    </style>

    <div class="vat-container">
        <!-- Header -->
        <div class="report-header">
            <h2 class="report-title">{year}년 {분기_표시} {신고_구분} 부가세신고 현황</h2>
            <div class="report-period">
                기간: {기간_시작} ~ {기간_종료}
            </div>
        </div>

        <!-- 1. 신고내용 -->
        <div class="form-section-title">가. 신 고 내 용</div>
        <div class="unit-label">(단위: 원)</div>
        
        <table class="form-table">
            <thead>
                <tr>
                    <th colspan="2">구 분</th>
                    <th>공급가액</th>
                    <th>세 액</th>
                    <th>비 고</th>
                </tr>
            </thead>
            <tbody>
                <!-- 매출세액 섹션 -->
                <tr>
                    <td rowspan="3" class="col-category">매출세액</td>
                    <td class="col-item" style="text-align: center;">과 세</td>
                    <td class="col-amount">{fmt(과세매출)}</td>
                    <td class="col-tax">{fmt(매출세액)}</td>
                    <td class="col-note"></td>
                </tr>
                <tr>
                    <td class="col-item" style="text-align: center;">영 세 (수 출)</td>
                    <td class="col-amount">{fmt(영세매출)}</td>
                    <td class="col-tax">0</td>
                    <td class="col-note"></td>
                </tr>
                <tr class="row-subtotal">
                    <td class="col-item" style="text-align: center;">소 계</td>
                    <td class="col-amount">{fmt(매출_소계_금액)}</td>
                    <td class="col-tax">{fmt(매출_소계_세액)}</td>
                    <td class="col-note"></td>
                </tr>
                
                <!-- 매입세액 섹션 -->
                <tr>
                    <td rowspan="4" class="col-category">매입세액</td>
                    <td class="col-item" style="text-align: center;">세금계산서 수취분</td>
                    <td class="col-amount">{fmt(세금계산서_매입)}</td>
                    <td class="col-tax">{fmt(세금계산서_세액)}</td>
                    <td class="col-note"></td>
                </tr>
                <tr>
                    <td class="col-item" style="text-align: center;">그 밖의 공제매입세액<br><span style="font-size:0.8em; color:#64748b;">(신용카드/현금영수증 등)</span></td>
                    <td class="col-amount">{fmt(기타공제_매입액)}</td>
                    <td class="col-tax">{fmt(기타공제_세액)}</td>
                    <td class="col-note"></td>
                </tr>
                <tr>
                    <td class="col-item row-deduction" style="text-align: center;">공제받지 못할 매입세액</td>
                    <td class="col-amount row-deduction">-{fmt(불공제_공급가액)}</td>
                    <td class="col-tax row-deduction">-{fmt(불공제_합계)}</td>
                    <td class="col-note"><span style="font-size:0.8em;">(접대비 등)</span></td>
                </tr>
                <tr class="row-subtotal">
                    <td class="col-item" style="text-align: center;">소 계<br><span style="font-size:0.8em; color:#64748b;">(차가감)</span></td>
                    <td class="col-amount">{fmt(차가감_매입액)}</td>
                    <td class="col-tax">{fmt(차가감_매입세액)}</td>
                    <td class="col-note"></td>
                </tr>

                <!-- 납부세액 섹션 -->
                <tr class="row-total">
                    <td colspan="2" style="text-align: center;">납부 (환급) 세액</td>
                    <td class="col-amount" style="background-color:#f0fdf4;">-</td>
                    <td class="col-tax amt-result">{fmt(납부세액)}</td>
                    <td class="col-note"></td>
                </tr>
            </tbody>
        </table>


        <!-- 2. 이전 신고내역 비교 -->
        <div class="form-section-title">나. 이전 신고내역 비교</div>
        <div class="unit-label">(단위: 원, %)</div>
        
        <table class="form-table">
            <thead>
                <tr>
                    <th rowspan="2">구 분</th>
                    <th colspan="2">전분기 대비</th>
                    <th colspan="2">전년 동기 대비</th>
                </tr>
                <tr>
                    <th>전분기 ({분기_표시})</th>
                    <th>증감율</th>
                    <th>전년 동기</th>
                    <th>증감율</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td class="col-category">부가세 매출</td>
                    <td class="col-amount">{fmt(prev_q.get('sales_tax', 0))}</td>
                    <td class="col-note" style="text-align:right;">
                        {f"{((매출세액 - prev_q.get('sales_tax', 0))/prev_q.get('sales_tax', 1)*100):.1f}%" if prev_q.get('sales_tax', 0) else "-"}
                    </td>
                    <td class="col-amount">{fmt(prev_y.get('sales_tax', 0))}</td>
                    <td class="col-note" style="text-align:right;">
                        {f"{((매출세액 - prev_y.get('sales_tax', 0))/prev_y.get('sales_tax', 1)*100):.1f}%" if prev_y.get('sales_tax', 0) else "-"}
                    </td>
                </tr>
                <tr>
                    <td class="col-category">부가세 매입</td>
                    <td class="col-amount">{fmt(prev_q.get('purchase_tax', 0))}</td>
                    <td class="col-note" style="text-align:right;">
                        {f"{((공제_매입세액 - prev_q.get('purchase_tax', 0))/prev_q.get('purchase_tax', 1)*100):.1f}%" if prev_q.get('purchase_tax', 0) else "-"}
                    </td>
                    <td class="col-amount">{fmt(prev_y.get('purchase_tax', 0))}</td>
                    <td class="col-note" style="text-align:right;">
                        {f"{((공제_매입세액 - prev_y.get('purchase_tax', 0))/prev_y.get('purchase_tax', 1)*100):.1f}%" if prev_y.get('purchase_tax', 0) else "-"}
                    </td>
                </tr>
                <tr class="row-subtotal">
                    <td class="col-category">납부 세액</td>
                    <td class="col-amount">{fmt(prev_q.get('payable', 0))}</td>
                    <td class="col-note" style="text-align:right; font-weight:bold;">
                        {f"{((납부세액 - prev_q.get('payable', 0))/prev_q.get('payable', 1)*100):.1f}%" if prev_q.get('payable', 0) else "-"}
                    </td>
                    <td class="col-amount">{fmt(prev_y.get('payable', 0))}</td>
                    <td class="col-note" style="text-align:right; font-weight:bold;">
                       {f"{((납부세액 - prev_y.get('payable', 0))/prev_y.get('payable', 1)*100):.1f}%" if prev_y.get('payable', 0) else "-"}
                    </td>
                </tr>
            </tbody>
        </table>

        <!-- 3. 주요 증감 요인 -->
        <div class="form-section-title">다. 공급 요인 (매출/매입 상위)</div>
        
        <div style="display: flex; gap: 2rem;">
            <!-- 매출 상위 -->
            <div style="flex: 1;">
                <table class="form-table">
                    <thead>
                        <tr><th colspan="2">매출 상위 거래처</th></tr>
                        <tr><th>거래처명</th><th>공급가액</th></tr>
                    </thead>
                    <tbody>
    """
    
    # 매출 상위 렌더링
    if top_contributors and not top_contributors['sales_client'].empty:
        for _, row in top_contributors['sales_client'].iterrows():
             html_report += f"""
                    <tr>
                        <td style="text-align:center;">{row['거래처명']}</td>
                        <td class="col-amount" style="color:#059669;">{fmt(row['금액'])}</td>
                    </tr>
             """
    else:
        html_report += "<tr><td colspan='2' style='text-align:center; padding:1rem;'>데이터 없음</td></tr>"

    html_report += """
                    </tbody>
                </table>
            </div>
            
            <!-- 매입 상위 -->
            <div style="flex: 1;">
                <table class="form-table">
                    <thead>
                        <tr><th colspan="2">매입 상위 거래처</th></tr>
                        <tr><th>거래처명</th><th>공급가액</th></tr>
                    </thead>
                    <tbody>
    """
    
    # 매입 상위 렌더링
    if top_contributors and not top_contributors['cost_client'].empty:
        for _, row in top_contributors['cost_client'].iterrows():
             html_report += f"""
                    <tr>
                        <td style="text-align:center;">{row['거래처명']}</td>
                        <td class="col-amount" style="color:#dc2626;">{fmt(row['금액'])}</td>
                    </tr>
             """
    else:
        html_report += "<tr><td colspan='2' style='text-align:center; padding:1rem;'>데이터 없음</td></tr>"

    html_report += """
                    </tbody>
                </table>
            </div>
        </div>

    </div>
    """
    
    import streamlit.components.v1 as components
    components.html(html_report, height=1300, scrolling=True)



def analyze_entertainment_expense():
    """접대비 분석 함수 (세법 개정 반영: 업무추진비)"""
    st.subheader("🔍 접대비(업무추진비) 세무조정 분석")
    
    # 기업 규모 선택
    company_type = st.selectbox(
        "기업 규모",
        ["중소기업", "중견기업", "대기업"],
        help="중소기업: 기본 한도 3,600만원, 중견/대기업: 기본 한도 1,200만원",
        key="company_type"
    )
    
    # 기간 선택
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("시작일", value=datetime(datetime.now().year, 1, 1), key="tax_start_date")
    with col2:
        end_date = st.date_input("종료일", value=datetime.now(), key="tax_end_date")

    # [수정] 특수관계인 매출액 입력 추가
    st.markdown("---")
    st.markdown("#### ⚙️ 추가 정보 입력")
    related_sales = st.number_input(
        "특수관계인 매출액 (원)", 
        min_value=0, 
        value=0, 
        step=1000000, 
        format="%d",
        help="전체 매출액 중 특수관계인과의 거래금액을 입력하세요. (한도액의 10%만 인정)"
    )
    
    if st.button("📊 접대비 분석 실행", type="primary", use_container_width=True):
        if not start_date or not end_date:
            st.error("⚠️ 시작일과 종료일을 선택해주세요.")
            return
        
        try:
            with st.spinner("접대비 데이터를 정밀 분석 중입니다..."):
                # 1. 데이터 조회
                매출액 = calculate_sales(start_date, end_date)
                전체접대비 = calculate_entertainment_expense(start_date, end_date)
                문화접대비 = calculate_culture_entertainment_expense(start_date, end_date)
                
                # 특수관계인 매출 유효성 체크
                if related_sales > 매출액:
                    st.warning(f"⚠️ 특수관계인 매출액({related_sales:,.0f}원)이 전체 매출액({매출액:,.0f}원)보다 큽니다. 전체 매출액으로 조정합니다.")
                    related_sales = 매출액

                일반매출액 = 매출액 - related_sales

                # 2. 개월 수 계산 (월할 계산용)
                days_diff = (end_date - start_date).days + 1
                months = min(12, max(1, round(days_diff / 30.4))) # 약식 월수 계산
                
                # 3. 기본 한도 계산 (월할 적용)
                연간기본한도 = 36_000_000 if company_type == "중소기업" else 12_000_000
                기본한도 = int(연간기본한도 * (months / 12))
                
                # 4. 매출액 비례 한도 계산 (구간별 차등 적용)
                def calculate_tiered_limit(sales_amount):
                    limit = 0
                    if sales_amount <= 10_000_000_000: # 100억 이하
                        limit += sales_amount * 0.003
                    elif sales_amount <= 50_000_000_000: # 500억 이하
                        limit += 10_000_000_000 * 0.003
                        limit += (sales_amount - 10_000_000_000) * 0.002
                    else: # 500억 초과
                        limit += 10_000_000_000 * 0.003
                        limit += 40_000_000_000 * 0.002
                        limit += (sales_amount - 50_000_000_000) * 0.0003
                    return limit

                # 전체 매출 기준 이론적 최대 한도
                총매출한도_이론치 = calculate_tiered_limit(매출액)
                
                # 특수관계인 패널티 적용 (비율 안분)
                매출비중_특수 = related_sales / 매출액 if 매출액 > 0 else 0
                
                특수관계인_한도분 = 총매출한도_이론치 * 매출비중_특수 * 0.1 # 10%만 인정
                일반_한도분 = 총매출한도_이론치 * (1 - 매출비중_특수)
                
                최종_매출비례한도 = int(일반_한도분 + 특수관계인_한도분)
                
                # 5. 문화접대비 한도 추가
                중간_총한도 = 기본한도 + 최종_매출비례한도
                문화접대비_추가한도 = int(min(문화접대비, 중간_총한도 * 0.2))
                
                # 6. 최종 한도 및 부인액 계산
                접대비_총한도 = 중간_총한도 + 문화접대비_추가한도
                
                부인금액 = max(0, 전체접대비 - 접대비_총한도)
                공제가능액 = 전체접대비 - 부인금액
                부가세_불공제액 = int(부인금액 * (10 / 11))
                
                # 결과 표시
                display_entertainment_analysis_results(
                    매출액, related_sales, 전체접대비, 문화접대비,
                    접대비_총한도, 부인금액, 공제가능액, 부가세_불공제액,
                    company_type, months, 기본한도, 최종_매출비례한도, 
                    문화접대비_추가한도
                )
        
        except Exception as e:
            st.error(f"❌ 분석 중 오류 발생: {str(e)}")
            import traceback
            st.code(traceback.format_exc())


def calculate_sales(start_date, end_date):
    """매출액 계산"""
    try:
        
        # 매출 계정 찾기 (대변금액 합계)
        query = """
        SELECT COALESCE(SUM(대변금액), 0) as 매출액
        FROM 회계전표
        WHERE (계정명 LIKE '%매출%' OR 계정명 LIKE '%수익%')
          AND 거래일자 BETWEEN ? AND ?
        """
        
        df = read_sql_query(query, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        
        return float(df.iloc[0]['매출액']) if not df.empty else 0.0
    except Exception as e:
        st.error(f"매출액 계산 오류: {e}")
        return 0.0


def calculate_entertainment_expense(start_date, end_date):
    """전체 접대비 계산"""
    try:
        
        query = """
        SELECT COALESCE(SUM(차변금액), 0) as 접대비
        FROM 회계전표
        WHERE 계정명 LIKE '%접대비%'
          AND 거래일자 BETWEEN ? AND ?
        """
        
        df = read_sql_query(query, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        
        return float(df.iloc[0]['접대비']) if not df.empty else 0.0
    except Exception as e:
        st.error(f"접대비 계산 오류: {e}")
        return 0.0


def calculate_culture_entertainment_expense(start_date, end_date):
    """문화접대비 계산"""
    try:
        
        query = """
        SELECT COALESCE(SUM(차변금액), 0) as 문화접대비
        FROM 회계전표
        WHERE 계정명 LIKE '%접대비%'
          AND (
            라인텍스트 LIKE '%문화%' OR 
            라인텍스트 LIKE '%공연%' OR 
            라인텍스트 LIKE '%연극%' OR 
            라인텍스트 LIKE '%영화%' OR
            라인텍스트 LIKE '%콘서트%' OR
            헤드텍스트 LIKE '%문화%'
          )
          AND 거래일자 BETWEEN ? AND ?
        """
        
        df = read_sql_query(query, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        
        return float(df.iloc[0]['문화접대비']) if not df.empty else 0.0
    except Exception as e:
        st.error(f"문화접대비 계산 오류: {e}")
        return 0.0


def calculate_product_production_input():
    """제조완료된 제품의 생산 입고액 계산 (회계전표 기반, Cross-Check용)"""
    try:
        
        query = """
        SELECT COALESCE(SUM(차변금액), 0) AS 제품_생산입고액
        FROM 회계전표
        WHERE 계정명 = '제품' 
          AND 증빙유형 = '제조완료'
        """
        
        df = read_sql_query(query)
        
        return float(df.iloc[0]['제품_생산입고액']) if not df.empty else 0.0
    except Exception as e:
        st.error(f"제품 생산입고액 계산 오류: {e}")
        return 0.0


def display_entertainment_analysis_results(매출액, 특수매출, 전체접대비, 문화접대비, 접대비_총한도, 부인금액, 공제가능액, 부가세_불공제액, company_type, months, 기본한도, 매출비례한도, 문화접대비_한도증가):
    """업데이트된 HTML 리포트 (계산식 포함)"""
    
    # --- 매출액 비례 한도 상세 계산 로직 생성 ---
    limit_100 = min(매출액, 10_000_000_000) * 0.003
    limit_500 = 0
    limit_over = 0
    
    calc_rows = []
    calc_rows.append(f"<div>• 100억 이하: {min(매출액, 10_000_000_000):,.0f} × 0.3% = <strong>{int(limit_100):,.0f}</strong></div>")
    
    if 매출액 > 10_000_000_000:
        target_500 = min(매출액, 50_000_000_000) - 10_000_000_000
        limit_500 = target_500 * 0.002
        calc_rows.append(f"<div>• 100억 초과: {target_500:,.0f} × 0.2% = <strong>{int(limit_500):,.0f}</strong></div>")
        
    if 매출액 > 50_000_000_000:
        target_over = 매출액 - 50_000_000_000
        limit_over = target_over * 0.0003
        calc_rows.append(f"<div>• 500억 초과: {target_over:,.0f} × 0.03% = <strong>{int(limit_over):,.0f}</strong></div>")
    
    total_theoretical = limit_100 + limit_500 + limit_over
    
    # 특수관계인 조정 텍스트
    related_calc = ""
    if 특수매출 > 0:
        ratio = 특수매출 / 매출액
        related_calc = f"""
        <div style="margin-top:8px; padding-top:8px; border-top:1px dashed #cbd5e1;">
            <div style="color:#b91c1c; font-weight:600;">※ 특수관계인 조정 (매출비중 {ratio*100:.1f}%)</div>
            <div>• 일반분: {int(total_theoretical):,.0f} × {(1-ratio)*100:.1f}% = {int(total_theoretical*(1-ratio)):,.0f}</div>
            <div>• 특수분: {int(total_theoretical):,.0f} × {ratio*100:.1f}% × <span style='color:red'>10%</span> = {int(total_theoretical*ratio*0.1):,.0f}</div>
        </div>
        """
    # ----------------------------------------

    html_report = f"""
    <div style="font-family: 'Pretendard', sans-serif; background: #ffffff; padding: 2.5rem; border-radius: 16px; box-shadow: 0 10px 40px rgba(0,0,0,0.08); border: 1px solid #e2e8f0; color: #1e293b;">
        <div style="border-bottom: 2px solid #f1f5f9; padding-bottom: 1.5rem; margin-bottom: 2rem; text-align: center;">
            <h2 style="font-size: 1.8rem; font-weight: 800; margin: 0; background: linear-gradient(135deg, #1e293b 0%, #334155 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">📋 접대비 세무조정 분석 보고서</h2>
            <p style="color: #64748b; font-size: 0.95rem; margin-top: 0.5rem;">Corporate Tax Adjustment Analysis</p>
        </div>

        <div style="margin-bottom: 2rem;">
            <h3 style="font-size: 1.2rem; font-weight: 700; color: #0f172a; margin-bottom: 1rem;">
                <span style="background: #eff6ff; color: #3b82f6; padding: 0.2rem 0.6rem; border-radius: 6px; margin-right: 0.5rem;">01</span> 📊 기초 데이터
            </h3>
            <div style="padding: 1.5rem; background: #f8fafc; border-radius: 12px; border: 1px solid #e2e8f0;">
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem;">
                    <div>
                        <p style="color:#64748b; font-size:0.85rem; margin:0;">총 매출액</p>
                        <p style="color:#1e293b; font-size:1.4rem; font-weight:700; margin:0;">{매출액:,.0f}원</p>
                        <p style="color:#94a3b8; font-size:0.8rem; margin:0;">(특수: {특수매출:,.0f}원)</p>
                    </div>
                    <div>
                        <p style="color:#64748b; font-size:0.85rem; margin:0;">전체 접대비</p>
                        <p style="color:#1e293b; font-size:1.4rem; font-weight:700; margin:0;">{전체접대비:,.0f}원</p>
                    </div>
                    <div>
                        <p style="color:#64748b; font-size:0.85rem; margin:0;">문화접대비</p>
                        <p style="color:#1e293b; font-size:1.4rem; font-weight:700; margin:0;">{문화접대비:,.0f}원</p>
                    </div>
                </div>
            </div>
        </div>

        <div style="margin-bottom: 2rem;">
            <h3 style="font-size: 1.2rem; font-weight: 700; color: #0f172a; margin-bottom: 1rem;">
                <span style="background: #f0fdf4; color: #22c55e; padding: 0.2rem 0.6rem; border-radius: 6px; margin-right: 0.5rem;">02</span> ⚖️ 한도액 계산 상세
            </h3>
            <div style="padding: 1rem; background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px;">
                <table style="width: 100%; border-collapse: collapse; font-size: 0.95rem;">
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 0.8rem; color: #64748b; width: 40%; vertical-align: top;">① 기본 한도 ({months}개월분)</td>
                        <td style="padding: 0.8rem; text-align: right; font-weight: 600;">{기본한도:,.0f}원</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 0.8rem; color: #64748b; vertical-align: top;">
                            ② 매출액 비례 한도<br>
                            <span style="font-size:0.8rem; color:#94a3b8;">(100억↓ 0.3% / 500억↓ 0.2% / 초과 0.03%)</span>
                        </td>
                        <td style="padding: 0.8rem; text-align: right;">
                            <div style="font-weight: 600; font-size: 1.1rem;">{매출비례한도:,.0f}원</div>
                            <div style="margin-top:8px; padding:10px; background:#f1f5f9; border-radius:6px; font-size:0.85rem; color:#475569; text-align:left;">
                                {''.join(calc_rows)}
                                {related_calc}
                            </div>
                        </td>
                    </tr>
                    <tr style="border-bottom: 2px solid #e2e8f0; background-color: #f8fafc;">
                        <td style="padding: 0.8rem; font-weight: 700; color: #334155;">[소계] 일반 한도 (① + ②)</td>
                        <td style="padding: 0.8rem; text-align: right; font-weight: 700;">{기본한도 + 매출비례한도:,.0f}원</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 0.8rem; color: #64748b;">③ 문화접대비 추가 한도<br><span style="font-size:0.8rem;">(min[문화접대비, 일반한도의 20%])</span></td>
                        <td style="padding: 0.8rem; text-align: right; font-weight: 600; color: #3b82f6;">+ {문화접대비_한도증가:,.0f}원</td>
                    </tr>
                    <tr style="background: #eff6ff; border-top: 2px solid #3b82f6;">
                        <td style="padding: 1rem; font-weight: 800; color: #1e3a8a;">✅ 최종 접대비 한도</td>
                        <td style="padding: 1rem; text-align: right; font-weight: 800; color: #1e3a8a; font-size: 1.2rem;">{접대비_총한도:,.0f}원</td>
                    </tr>
                </table>
            </div>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem;">
            <div style="padding: 1.5rem; background: #ffffff; border-radius: 12px; border: 2px solid #22c55e;">
                <h4 style="color: #15803d; margin: 0 0 0.5rem 0; font-size: 1rem;">✅ 공제 가능액</h4>
                <h2 style="color: #15803d; margin: 0; font-size: 2rem;">{공제가능액:,.0f}원</h2>
            </div>
            <div style="padding: 1.5rem; background: #ffffff; border-radius: 12px; border: 2px solid #ef4444;">
                <h4 style="color: #b91c1c; margin: 0 0 0.5rem 0; font-size: 1rem;">❌ 한도 초과(부인)액</h4>
                <h2 style="color: #b91c1c; margin: 0; font-size: 2rem;">{부인금액:,.0f}원</h2>
            </div>
        </div>
    </div>
    """
    import streamlit.components.v1 as components
    components.html(html_report, height=900, scrolling=True)


def render_settings_page():
    """설정 페이지"""
    st.markdown("<h1 style='text-align: center; color: black; margin-bottom: 2rem;'>⚙️ 설정</h1>", unsafe_allow_html=True)
    
    # 탭 생성 (빨간색 호버 효과 자동 적용됨 - CSS에서 정의됨)
    tab1, tab2, tab3 = st.tabs(["파일 업로드", "데이터 관리", "기타 설정"])
    
    with tab1:
        st.subheader("📤 파일 업로드")
        
        # Config Manager 확인
        if load_config is None or auto_detect_columns is None:
            st.error("⚠️ Config Manager를 사용할 수 없습니다. config_manager.py 파일을 확인해주세요.")
            return
        
        # 마이그레이션 모드 선택 (파일 업로드 전에 선택)
        st.markdown("### 🔄 업로드 모드 선택")
        migration_mode = st.radio(
            "업로드 모드",
            [
                "🔄 전체 마이그레이션 (Full Migration)",
                "➕ 개별 추가 (Incremental Add)"
            ],
            help="**전체 마이그레이션**: 기존에 업로드된 모든 테이블을 삭제하고 새로 시작합니다.\n**개별 추가**: 기존 테이블을 유지하면서 새 테이블만 추가합니다.",
            key="global_migration_mode"
        )
        
        is_full_migration = "전체 마이그레이션" in migration_mode
        
        if is_full_migration:
            st.warning("⚠️ **전체 마이그레이션 모드**: 기존에 업로드된 모든 테이블과 데이터가 삭제됩니다!")
        
        st.markdown("---")
        
        # 파일 업로드
        uploaded_file = st.file_uploader(
            "파일 선택 (.xlsx, .xls, .csv)",
            type=['xlsx', 'xls', 'csv'],
            help="데이터가 포함된 엑셀 또는 CSV 파일을 업로드하세요."
        )
        
        if uploaded_file is not None:
            try:
                # 파일 확장자 확인
                file_name = uploaded_file.name.lower()
                is_csv = file_name.endswith('.csv')
                is_excel = file_name.endswith(('.xlsx', '.xls'))
                
                # 파일 포인터를 처음으로 리셋
                uploaded_file.seek(0)
                
                # CSV 파일 읽기
                if is_csv:
                    df_upload = None
                    encoding_list = ['utf-8', 'cp949', 'euc-kr', 'latin-1', 'iso-8859-1']
                    delimiter_list = [',', ';', '\t', '|']  # 쉼표, 세미콜론, 탭, 파이프
                    
                    for encoding in encoding_list:
                        for delimiter in delimiter_list:
                            try:
                                uploaded_file.seek(0)
                                df_test = pd.read_csv(uploaded_file, encoding=encoding, delimiter=delimiter, on_bad_lines='skip')
                                
                                # 데이터가 제대로 읽혔는지 확인 (컬럼이 1개보다 많아야 함)
                                if len(df_test.columns) > 1 and not df_test.empty:
                                    df_upload = df_test
                                    messages = []
                                    if encoding != 'utf-8':
                                        messages.append(f"인코딩: {encoding}")
                                    if delimiter != ',':
                                        messages.append(f"구분자: {delimiter}")
                                    if messages:
                                        st.info(f"💡 파일이 {', '.join(messages)}로 읽혔습니다.")
                                    break
                            except (UnicodeDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError):
                                continue
                            except Exception:
                                continue
                        
                        if df_upload is not None:
                            break
                    
                    if df_upload is None or df_upload.empty:
                        raise Exception("CSV 파일을 읽을 수 없습니다. 인코딩, 구분자 또는 파일 형식을 확인해주세요.")
                    
                    # CSV 파일도 엑셀 단일 시트와 동일한 UI 제공
                    st.success(f"✅ 파일을 성공적으로 읽었습니다! ({len(df_upload)}행, {len(df_upload.columns)}컬럼)")
                    
                    # 데이터 미리보기
                    st.markdown("### 📋 데이터 미리보기 (상위 10행)")
                    st.dataframe(df_upload.head(10), use_container_width=True)
                    
                    # 컬럼 정보 표시
                    st.markdown("### 📊 컬럼 정보")
                    st.info(f"**전체 컬럼 ({len(df_upload.columns)}개):** {', '.join(df_upload.columns.tolist())}")
                    
                    # Config 로드
                    config = load_config()
                    
                    # 자동 컬럼 감지
                    st.markdown("### 🔍 필수 컬럼 자동 감지")
                    detected_columns = auto_detect_columns(df_upload, config)
                    
                    if detected_columns:
                        st.success(f"✅ {len(detected_columns)}개의 컬럼이 자동 감지되었습니다.")
                    else:
                        st.warning("⚠️ 자동 감지된 컬럼이 없습니다. 모든 컬럼을 확인해주세요.")
                    
                    # 테이블 이름 입력
                    st.markdown("### 📝 테이블 설정")
                    default_table_name = os.path.splitext(uploaded_file.name)[0]
                    if not default_table_name or not default_table_name.strip():
                        default_table_name = "회계전표"
                    
                    table_name = st.text_input(
                        "업로드할 테이블 이름",
                        value=default_table_name,
                        help="데이터베이스에 저장될 테이블 이름을 입력하세요."
                    )
                    
                    # 필수 컬럼 선택 (multiselect)
                    st.markdown("### ✅ 필수 컬럼 선택")
                    all_columns = df_upload.columns.tolist()
                    
                    # 기본값: 자동 감지된 컬럼 또는 기존 설정된 컬럼
                    default_selected = detected_columns if detected_columns else []
                    
                    # 기존 설정이 있으면 병합
                    if table_name and table_name.strip():
                        existing_columns = get_required_columns(table_name, config)
                        # 존재하는 컬럼만 추가
                        for col in existing_columns:
                            if col in all_columns and col not in default_selected:
                                default_selected.append(col)
                    
                    selected_columns = st.multiselect(
                        "필수 컬럼을 선택하세요 (여러 개 선택 가능)",
                        options=all_columns,
                        default=default_selected,
                        help="데이터베이스에 저장할 필수 컬럼을 선택하세요. 자동 감지된 컬럼이 기본값으로 설정됩니다."
                    )
                    
                    # 선택된 컬럼 표시
                    if selected_columns:
                        st.info(f"**선택된 컬럼 ({len(selected_columns)}개):** {', '.join(selected_columns)}")
                    else:
                        st.warning("⚠️ 최소 1개 이상의 컬럼을 선택해주세요.")
                    
                    # 저장 및 업로드 버튼
                    st.markdown("### 💾 저장 및 업로드")
                    
                    col1, col2, col3 = st.columns(3)
                    
                    with col1:
                        # Config 저장 버튼
                        save_config_button = st.button(
                            "💾 설정 저장",
                            type="primary",
                            use_container_width=True,
                            help="선택한 필수 컬럼을 config.json에 저장합니다.",
                            key="csv_save_config"
                        )
                    
                    with col2:
                        # 업로드 모드 선택 (CSV는 개별 추가만)
                        upload_mode = st.radio(
                            "테이블 데이터 모드",
                            ["기존 데이터에 추가 (append)", "기존 데이터 삭제 후 대체 (replace)"],
                            help="'추가'는 기존 데이터를 유지하고 새 데이터를 추가합니다.\n'대체'는 기존 데이터를 모두 삭제하고 새 데이터로 교체합니다.\n\n⚠️ 이 모드는 테이블 내 데이터만 영향을 받으며, 다른 테이블은 유지됩니다.",
                            key="csv_upload_mode"
                        )
                    
                    with col3:
                        # DB 업로드 버튼
                        upload_db_button = st.button(
                            "🚀 DB에 업로드",
                            type="primary",
                            use_container_width=True,
                            help="데이터베이스에 데이터를 업로드합니다.",
                            key="csv_upload_db"
                        )
                    
                    # Config 저장 처리
                    if save_config_button:
                        if not table_name or not table_name.strip():
                            st.error("❌ 테이블 이름을 입력해주세요.")
                        elif not selected_columns:
                            st.error("❌ 최소 1개 이상의 컬럼을 선택해주세요.")
                        else:
                            try:
                                success = update_required_columns(table_name, selected_columns, config)
                                if success:
                                    st.success(f"✅ 설정이 config.json에 저장되었습니다!")
                                    st.info(f"**테이블:** {table_name}\n**필수 컬럼:** {', '.join(selected_columns)}")
                                else:
                                    st.error("❌ 설정 저장에 실패했습니다.")
                            except Exception as e:
                                st.error(f"❌ 설정 저장 중 오류 발생: {str(e)}")
                    
                    # DB 업로드 처리
                    if upload_db_button:
                        if not table_name or not table_name.strip():
                            st.error("❌ 테이블 이름을 입력해주세요.")
                        elif not selected_columns:
                            st.error("❌ 최소 1개 이상의 컬럼을 선택해주세요.")
                        else:
                            try:
                                config_upload = load_config()
                                
                                # 기존 테이블 스키마 확인
                                table_exists = db_table_exists(table_name, DB_PATH)
                                
                                if table_exists:
                                    existing_columns = db_table_columns(table_name, DB_PATH)
                                    
                                    # 컬럼 정보 표시
                                    if set(selected_columns) != set(existing_columns):
                                        st.info(f"📊 **컬럼 정보:**")
                                        st.info(f"• 기존 테이블 컬럼 수: {len(existing_columns)}개")
                                        st.info(f"• 업로드 파일 컬럼 수: {len(selected_columns)}개")
                                        
                                        removed_cols = [col for col in existing_columns if col not in selected_columns]
                                        added_cols = [col for col in selected_columns if col not in existing_columns]
                                        
                                        if removed_cols:
                                            st.info(f"🗑️ 제거될 컬럼: {', '.join(removed_cols)}")
                                        if added_cols:
                                            st.info(f"➕ 추가될 컬럼: {', '.join(added_cols)}")
                                        
                                # 선택된 컬럼만 포함한 DataFrame 생성
                                df_to_upload = df_upload[selected_columns].copy()
                                
                                mode = 'replace' if is_full_migration else ('append' if "추가" in upload_mode else 'replace')
                                if mode == "append" and table_exists and set(selected_columns) != set(existing_columns):
                                    st.error("추가 업로드는 기존 테이블과 컬럼이 같아야 합니다. 컬럼을 맞추거나 덮어쓰기를 선택해주세요.")
                                    return
                                
                                # 데이터 삽입 (추가는 기존 행 보존, 덮어쓰기는 트랜잭션 교체)
                                with st.spinner(f"💾 `{table_name}` 테이블에 데이터 저장 중..."):
                                    if is_full_migration:
                                        db_replace_dataframes(
                                            {table_name: df_to_upload},
                                            DB_PATH,
                                            drop_tables=get_managed_tables(config_upload),
                                        )
                                    else:
                                        db_write_dataframe(table_name, df_to_upload, DB_PATH, if_exists=mode)
                                
                                # 🔥 config.json의 required_columns 업데이트 (업로드 파일의 선택된 컬럼으로 교체)
                                with st.spinner("📋 config.json 업데이트 중..."):
                                    try:
                                        # 선택된 컬럼을 required_columns에 반영 (기존 컬럼 삭제 후 새 컬럼만)
                                        if update_required_columns is not None:
                                            success = update_required_columns(table_name, selected_columns, config_upload)
                                            if success:
                                                st.success(f"✅ config.json에 `{table_name}` 테이블의 필수 컬럼이 업데이트되었습니다!")
                                                st.info(f"📊 새 필수 컬럼 ({len(selected_columns)}개): {', '.join(selected_columns)}")
                                    except Exception as e:
                                        st.warning(f"⚠️ config.json 업데이트 중 오류: {str(e)}")
                                
                                # JSON에 테이블 추가 (전체 마이그레이션인 경우 새 목록, 개별 추가인 경우 기존 목록에 추가)
                                if is_full_migration:
                                    if update_managed_tables is not None:
                                        update_managed_tables([table_name], config_upload)
                                else:
                                    if add_managed_table is not None:
                                        add_managed_table(table_name, config_upload)
                                
                                mode_text = "전체 마이그레이션" if is_full_migration else "개별 추가"
                                st.success(f"✅ 테이블 '{table_name}'에 {len(df_to_upload)}건의 데이터가 성공적으로 업로드되었습니다! ({mode_text})")
                                st.balloons()
                                
                                # 자동으로 Config 저장 제안
                                if st.button("💾 설정도 자동 저장하기", key="csv_auto_save_after_upload"):
                                    try:
                                        config_after = load_config()
                                        update_required_columns(table_name, selected_columns, config_after)
                                        st.success("✅ 설정도 저장되었습니다!")
                                    except Exception as e:
                                        st.warning(f"⚠️ 설정 저장 실패 (업로드는 완료됨): {str(e)}")
                                
                                # 페이지 새로고침 안내
                                st.info("💡 '데이터 관리' 메뉴에서 업로드된 데이터를 확인할 수 있습니다.")
                                
                            except Exception as e:
                                st.error(f"❌ 업로드 중 오류 발생: {str(e)}")
                
                # 엑셀 파일 읽기
                elif is_excel:
                    try:
                        # openpyxl로 엑셀 파일 열기 (시트 목록 확인용)
                        try:
                            import openpyxl
                            uploaded_file.seek(0)
                            excel_file = openpyxl.load_workbook(uploaded_file, read_only=True)
                            sheet_names = excel_file.sheetnames
                            excel_file.close()
                            
                            st.success(f"✅ 엑셀 파일을 읽었습니다. **{len(sheet_names)}개의 시트**를 발견했습니다.")
                            
                            # 시트가 여러 개인 경우
                            if len(sheet_names) > 1:
                                st.markdown("### 📑 시트 목록")
                                st.info(f"**발견된 시트:** {', '.join(sheet_names)}")
                                
                                # 각 시트별로 테이블 설정
                                uploaded_sheets = {}
                                table_configs = {}
                                
                                for sheet_name in sheet_names:
                                    st.markdown(f"---\n### 📊 시트: **{sheet_name}**")
                                    
                                    # 시트 읽기
                                    uploaded_file.seek(0)
                                    try:
                                        df_sheet = pd.read_excel(uploaded_file, sheet_name=sheet_name, engine='openpyxl')
                                    except Exception:
                                        # openpyxl 실패 시 xlrd 시도
                                        uploaded_file.seek(0)
                                        df_sheet = pd.read_excel(uploaded_file, sheet_name=sheet_name, engine='xlrd')
                                    
                                    if df_sheet.empty:
                                        st.warning(f"⚠️ '{sheet_name}' 시트가 비어있습니다. 건너뜁니다.")
                                        continue
                                    
                                    st.info(f"**데이터:** {len(df_sheet)}행, {len(df_sheet.columns)}컬럼")
                                    
                                    # 데이터 미리보기
                                    st.dataframe(df_sheet.head(5), use_container_width=True)
                                    
                                    # 테이블 이름 입력
                                    default_table_name = sheet_name.replace(' ', '_').replace('-', '_')
                                    table_name = st.text_input(
                                        f"테이블 이름 (시트: {sheet_name})",
                                        value=default_table_name,
                                        key=f"table_name_{sheet_name}",
                                        help="이 시트를 저장할 테이블 이름을 입력하세요."
                                    )
                                    
                                    # 업로드 여부 확인
                                    upload_this_sheet = st.checkbox(
                                        f"✅ 이 시트를 업로드합니다",
                                        value=True,
                                        key=f"upload_check_{sheet_name}"
                                    )
                                    
                                    if upload_this_sheet and table_name.strip():
                                        uploaded_sheets[sheet_name] = df_sheet
                                        table_configs[sheet_name] = {
                                            'table_name': table_name.strip(),
                                            'df': df_sheet
                                        }
                                
                                # 선택된 시트가 있는 경우
                                if table_configs:
                                    st.markdown("---\n### ✅ 업로드 요약")
                                    summary_text = "다음 테이블들이 업로드됩니다:\n\n"
                                    for sheet_name, config in table_configs.items():
                                        summary_text += f"- **{config['table_name']}** (시트: {sheet_name}, {len(config['df'])}행)\n"
                                    st.info(summary_text)
                                    
                                    # 일괄 업로드 버튼
                                    if st.button("🚀 모든 선택된 테이블 업로드", type="primary", key="bulk_upload"):
                                        config = load_config()
                                        uploaded_count = 0
                                        failed_tables = []
                                        
                                        # 전체 테이블 개수 계산
                                        total_tables = len(table_configs)
                                        
                                        # 진행율 표시를 위한 progress bar 생성
                                        progress_bar = st.progress(0)
                                        status_text = st.empty()
                                        
                                        requested_table_names = [
                                            item['table_name'] for item in table_configs.values()
                                        ]
                                        duplicate_names = sorted({
                                            name for name in requested_table_names
                                            if requested_table_names.count(name) > 1
                                        })
                                        if duplicate_names:
                                            st.error(
                                                "서로 다른 시트에 같은 테이블 이름을 사용할 수 없습니다: "
                                                + ", ".join(duplicate_names)
                                            )
                                            return

                                        frames_to_upload = {
                                            item['table_name']: item['df']
                                            for item in table_configs.values()
                                        }
                                        previous_tables = get_managed_tables(config)
                                        drop_tables = previous_tables if is_full_migration else []

                                        def report_progress(table_name, index, total, row_count):
                                            progress_bar.progress(index / total)
                                            status_text.info(
                                                f"📤 진행 중: {index}/{total} - `{table_name}` "
                                                f"테이블 업로드 중... ({row_count:,}행)"
                                            )

                                        try:
                                            # 모든 시트를 한 트랜잭션으로 저장한다. 하나라도 실패하면 기존 DB를 유지한다.
                                            db_replace_dataframes(
                                                frames_to_upload,
                                                DB_PATH,
                                                drop_tables=drop_tables,
                                                progress=report_progress,
                                            )

                                            new_table_list = [] if is_full_migration else list(previous_tables)
                                            config.setdefault("required_columns", {})
                                            for table_name, df_to_upload in frames_to_upload.items():
                                                if table_name not in new_table_list:
                                                    new_table_list.append(table_name)
                                                config["required_columns"][table_name] = list(df_to_upload.columns)
                                            config["managed_tables"] = new_table_list
                                            if not save_config(config):
                                                st.warning("데이터는 저장됐지만 테이블 설정 저장에 실패했습니다.")

                                            uploaded_count = total_tables
                                            for index, (table_name, df_to_upload) in enumerate(frames_to_upload.items(), start=1):
                                                st.success(
                                                    f"✅ [{index}/{total_tables}] `{table_name}` 테이블 업로드 완료! "
                                                    f"({len(df_to_upload):,}행, {len(df_to_upload.columns)}컬럼)"
                                                )
                                        except Exception as e:
                                            failed_tables = [
                                                f"`{table_name}`: {str(e)}" for table_name in frames_to_upload
                                            ]
                                            st.error(f"❌ 전체 업로드를 취소했습니다. 기존 데이터는 유지됩니다: {e}")
                                        
                                        # 진행율 100% 완료
                                        progress_bar.progress(1.0)
                                        
                                        # 최종 결과 피드백
                                        status_text.empty()
                                        progress_bar.empty()
                                        
                                        if uploaded_count > 0:
                                            mode_text = "전체 마이그레이션" if is_full_migration else "개별 추가"
                                            
                                            # 상세한 완료 피드백
                                            st.markdown("---")
                                            st.success(f"## ✅ 마이그레이션 완료!")
                                            st.markdown(f"""
                                            **업로드 결과:**
                                            - ✅ 성공: **{uploaded_count}개** 테이블
                                            - ❌ 실패: **{len(failed_tables)}개** 테이블
                                            - 📊 모드: **{mode_text}**
                                            - 📋 총 처리: **{total_tables}개** 테이블
                                            """)
                                            
                                            # 성공한 테이블 목록 표시
                                            if uploaded_count > 0:
                                                success_tables = []
                                                for sheet_name, config_data in table_configs.items():
                                                    table_name = config_data['table_name']
                                                    if table_name not in [f.split(':')[0].strip('`') for f in failed_tables]:
                                                        success_tables.append(table_name)
                                                
                                                if success_tables:
                                                    with st.expander(f"📋 성공한 테이블 목록 ({len(success_tables)}개)", expanded=False):
                                                        for tbl in success_tables:
                                                            st.markdown(f"- ✅ `{tbl}`")
                                            
                                            # 실패한 테이블이 있는 경우
                                            if failed_tables:
                                                with st.expander(f"⚠️ 실패한 테이블 목록 ({len(failed_tables)}개)", expanded=True):
                                                    for failed in failed_tables:
                                                        st.error(f"❌ {failed}")
                                            
                                            st.balloons()
                                            st.info("💡 '데이터 관리' 메뉴에서 업로드된 테이블들을 확인할 수 있습니다.")
                                            
                                            # 자동 새로고침 (3초 후)
                                            with st.spinner("⏳ 3초 후 페이지가 자동으로 새로고침됩니다..."):
                                                time.sleep(3)
                                            st.rerun()
                                        else:
                                            st.error("## ❌ 모든 테이블 업로드에 실패했습니다.")
                                            if failed_tables:
                                                with st.expander("❌ 오류 상세 정보", expanded=True):
                                                    for failed in failed_tables:
                                                        st.error(f"❌ {failed}")
                                            st.error("💡 파일 형식과 데이터를 확인한 후 다시 시도해주세요.")
                                else:
                                    st.warning("⚠️ 업로드할 시트를 선택해주세요.")
                            
                            # 시트가 1개인 경우 (기존 로직 유지하되 JSON에 테이블 추가)
                            else:
                                sheet_name = sheet_names[0]
                                uploaded_file.seek(0)
                                try:
                                    df_upload = pd.read_excel(uploaded_file, sheet_name=sheet_name, engine='openpyxl')
                                except Exception:
                                    uploaded_file.seek(0)
                                    df_upload = pd.read_excel(uploaded_file, engine='openpyxl')
                                
                                # 기존 단일 시트 로직 계속
                                st.success(f"✅ 파일을 성공적으로 읽었습니다! ({len(df_upload)}행, {len(df_upload.columns)}컬럼)")
                                
                                # 데이터 미리보기
                                st.markdown("### 📋 데이터 미리보기 (상위 10행)")
                                st.dataframe(df_upload.head(10), use_container_width=True)
                                
                                # 컬럼 정보 표시
                                st.markdown("### 📊 컬럼 정보")
                                st.info(f"**전체 컬럼 ({len(df_upload.columns)}개):** {', '.join(df_upload.columns.tolist())}")
                                
                                # Config 로드
                                config = load_config()
                                
                                # 자동 컬럼 감지
                                st.markdown("### 🔍 필수 컬럼 자동 감지")
                                detected_columns = auto_detect_columns(df_upload, config)
                                
                                if detected_columns:
                                    st.success(f"✅ {len(detected_columns)}개의 컬럼이 자동 감지되었습니다.")
                                else:
                                    st.warning("⚠️ 자동 감지된 컬럼이 없습니다. 모든 컬럼을 확인해주세요.")
                                
                                # 테이블 이름 입력
                                st.markdown("### 📝 테이블 설정")
                                default_table_name = os.path.splitext(uploaded_file.name)[0]
                                if not default_table_name or not default_table_name.strip():
                                    default_table_name = "회계전표"
                                
                                table_name = st.text_input(
                                    "업로드할 테이블 이름",
                                    value=default_table_name,
                                    help="데이터베이스에 저장될 테이블 이름을 입력하세요."
                                )
                                
                                # 필수 컬럼 선택 (multiselect)
                                st.markdown("### ✅ 필수 컬럼 선택")
                                all_columns = df_upload.columns.tolist()
                                
                                # 기본값: 자동 감지된 컬럼 또는 기존 설정된 컬럼
                                default_selected = detected_columns if detected_columns else []
                                
                                # 기존 설정이 있으면 병합
                                if table_name and table_name.strip():
                                    existing_columns = get_required_columns(table_name, config)
                                    # 존재하는 컬럼만 추가
                                    for col in existing_columns:
                                        if col in all_columns and col not in default_selected:
                                            default_selected.append(col)
                                
                                selected_columns = st.multiselect(
                                    "필수 컬럼을 선택하세요 (여러 개 선택 가능)",
                                    options=all_columns,
                                    default=default_selected,
                                    help="데이터베이스에 저장할 필수 컬럼을 선택하세요. 자동 감지된 컬럼이 기본값으로 설정됩니다."
                                )
                                
                                # 선택된 컬럼 표시
                                if selected_columns:
                                    st.info(f"**선택된 컬럼 ({len(selected_columns)}개):** {', '.join(selected_columns)}")
                                else:
                                    st.warning("⚠️ 최소 1개 이상의 컬럼을 선택해주세요.")
                                
                                # 저장 및 업로드 버튼
                                st.markdown("### 💾 저장 및 업로드")
                                
                                col1, col2, col3 = st.columns(3)
                                
                                with col1:
                                    # Config 저장 버튼
                                    save_config_button = st.button(
                                        "💾 설정 저장",
                                        type="primary",
                                        use_container_width=True,
                                        help="선택한 필수 컬럼을 config.json에 저장합니다."
                                    )
                                
                                with col2:
                                    # 업로드 모드 선택 (단일 시트는 개별 추가만)
                                    upload_mode = st.radio(
                                        "테이블 데이터 모드",
                                        ["기존 데이터에 추가 (append)", "기존 데이터 삭제 후 대체 (replace)"],
                                        help="'추가'는 기존 데이터를 유지하고 새 데이터를 추가합니다.\n'대체'는 기존 데이터를 모두 삭제하고 새 데이터로 교체합니다.\n\n⚠️ 이 모드는 테이블 내 데이터만 영향을 받으며, 다른 테이블은 유지됩니다."
                                    )
                                
                                with col3:
                                    # DB 업로드 버튼
                                    upload_db_button = st.button(
                                        "🚀 DB에 업로드",
                                        type="primary",
                                        use_container_width=True,
                                        help="데이터베이스에 데이터를 업로드합니다. (개별 추가 모드)"
                                    )
                                
                                # Config 저장 처리
                                if save_config_button:
                                    if not table_name or not table_name.strip():
                                        st.error("❌ 테이블 이름을 입력해주세요.")
                                    elif not selected_columns:
                                        st.error("❌ 최소 1개 이상의 컬럼을 선택해주세요.")
                                    else:
                                        try:
                                            success = update_required_columns(table_name, selected_columns, config)
                                            if success:
                                                st.success(f"✅ 설정이 config.json에 저장되었습니다!")
                                                st.info(f"**테이블:** {table_name}\n**필수 컬럼:** {', '.join(selected_columns)}")
                                            else:
                                                st.error("❌ 설정 저장에 실패했습니다.")
                                        except Exception as e:
                                            st.error(f"❌ 설정 저장 중 오류 발생: {str(e)}")
                                
                                # DB 업로드 처리
                                if upload_db_button:
                                    if not table_name or not table_name.strip():
                                        st.error("❌ 테이블 이름을 입력해주세요.")
                                    elif not selected_columns:
                                        st.error("❌ 최소 1개 이상의 컬럼을 선택해주세요.")
                                    else:
                                        try:
                                            config_upload = load_config()
                                            
                                            # 기존 테이블 스키마 확인
                                            table_exists = db_table_exists(table_name, DB_PATH)
                                            
                                            if table_exists:
                                                existing_columns = db_table_columns(table_name, DB_PATH)
                                                
                                                # 컬럼 정보 표시
                                                if set(selected_columns) != set(existing_columns):
                                                    st.info(f"📊 **컬럼 정보:**")
                                                    st.info(f"• 기존 테이블 컬럼 수: {len(existing_columns)}개")
                                                    st.info(f"• 업로드 파일 컬럼 수: {len(selected_columns)}개")
                                                    
                                                    removed_cols = [col for col in existing_columns if col not in selected_columns]
                                                    added_cols = [col for col in selected_columns if col not in existing_columns]
                                                    
                                                    if removed_cols:
                                                        st.info(f"🗑️ 제거될 컬럼: {', '.join(removed_cols)}")
                                                    if added_cols:
                                                        st.info(f"➕ 추가될 컬럼: {', '.join(added_cols)}")
                                                    
                                            # 선택된 컬럼만 포함한 DataFrame 생성
                                            df_to_upload = df_upload[selected_columns].copy()
                                            
                                            mode = 'replace' if is_full_migration else ('append' if "추가" in upload_mode else 'replace')
                                            if mode == "append" and table_exists and set(selected_columns) != set(existing_columns):
                                                st.error("추가 업로드는 기존 테이블과 컬럼이 같아야 합니다. 컬럼을 맞추거나 덮어쓰기를 선택해주세요.")
                                                return
                                            
                                            # 데이터 삽입 (추가는 기존 행 보존, 덮어쓰기는 트랜잭션 교체)
                                            with st.spinner(f"💾 `{table_name}` 테이블에 데이터 저장 중..."):
                                                if is_full_migration:
                                                    db_replace_dataframes(
                                                        {table_name: df_to_upload},
                                                        DB_PATH,
                                                        drop_tables=get_managed_tables(config_upload),
                                                    )
                                                else:
                                                    db_write_dataframe(table_name, df_to_upload, DB_PATH, if_exists=mode)
                                            
                                            # 🔥 config.json의 required_columns 업데이트 (업로드 파일의 선택된 컬럼으로 교체)
                                            with st.spinner("📋 config.json 업데이트 중..."):
                                                try:
                                                    # 선택된 컬럼을 required_columns에 반영 (기존 컬럼 삭제 후 새 컬럼만)
                                                    if update_required_columns is not None:
                                                        success = update_required_columns(table_name, selected_columns, config_upload)
                                                        if success:
                                                            st.success(f"✅ config.json에 `{table_name}` 테이블의 필수 컬럼이 업데이트되었습니다!")
                                                            st.info(f"📊 새 필수 컬럼 ({len(selected_columns)}개): {', '.join(selected_columns)}")
                                                except Exception as e:
                                                    st.warning(f"⚠️ config.json 업데이트 중 오류: {str(e)}")
                                            
                                            # JSON에 테이블 추가 (전체 마이그레이션인 경우 새 목록, 개별 추가인 경우 기존 목록에 추가)
                                            if is_full_migration:
                                                if update_managed_tables is not None:
                                                    update_managed_tables([table_name], config_upload)
                                            else:
                                                if add_managed_table is not None:
                                                    add_managed_table(table_name, config_upload)
                                            
                                            mode_text = "전체 마이그레이션" if is_full_migration else "개별 추가"
                                            st.success(f"✅ 테이블 '{table_name}'에 {len(df_to_upload)}건의 데이터가 성공적으로 업로드되었습니다! ({mode_text})")
                                            st.balloons()
                                            
                                            # 자동으로 Config 저장 제안
                                            if st.button("💾 설정도 자동 저장하기", key="auto_save_after_upload"):
                                                try:
                                                    config_after = load_config()
                                                    update_required_columns(table_name, selected_columns, config_after)
                                                    st.success("✅ 설정도 저장되었습니다!")
                                                except Exception as e:
                                                    st.warning(f"⚠️ 설정 저장 실패 (업로드는 완료됨): {str(e)}")
                                            
                                            # 페이지 새로고침 안내
                                            st.info("💡 '데이터 관리' 메뉴에서 업로드된 데이터를 확인할 수 있습니다.")
                                            
                                        except Exception as e:
                                            st.error(f"❌ 업로드 중 오류 발생: {str(e)}")
                        
                        except ImportError:
                            # openpyxl이 없는 경우 기존 방식 사용
                            uploaded_file.seek(0)
                            df_upload = pd.read_excel(uploaded_file, engine='openpyxl')
                            # ... (기존 단일 시트 로직으로 fallback)
                            raise Exception("openpyxl 라이브러리가 필요합니다. pip install openpyxl로 설치해주세요.")
                            
                    except Exception as e1:
                        try:
                            # openpyxl 실패 시 xlrd 시도 (단일 시트만)
                            uploaded_file.seek(0)
                            df_upload = pd.read_excel(uploaded_file, engine='xlrd')
                            # ... (기존 단일 시트 로직으로 fallback)
                        except Exception as e2:
                            raise Exception(f"엑셀 파일을 읽을 수 없습니다: {str(e1)}")
                else:
                    raise Exception(f"지원하지 않는 파일 형식입니다. (.xlsx, .xls, .csv만 지원)")
                
            except Exception as e:
                st.error(f"❌ 파일 읽기 오류: {str(e)}")
                import traceback
                st.code(traceback.format_exc(), language='python')
    
    with tab2:
        # 데이터 관리 페이지 내용
        render_data_manager_page()
    
    with tab3:
        st.subheader("🛠️ 데이터베이스 관리")
        
        st.markdown("""
        **데이터베이스 정규화**
        
        '회계전표' 테이블의 데이터를 기반으로 계정과목, 거래처, 부서, 프로젝트 테이블을 자동으로 생성하고 정리합니다.
        새로운 전표 데이터를 업로드한 후 이 기능을 실행하면 다른 테이블들도 자동으로 업데이트됩니다.
        """)
        
        if st.button("🔄 DB 정규화 실행", type="primary"):
            if normalize_database:
                with st.spinner("데이터베이스 정규화를 진행 중입니다..."):
                    try:
                        # 스크립트 실행
                        normalize_database()
                        st.success("✅ 정규화가 완료되었습니다!")
                        st.info("이제 '데이터 관리' 탭에서 정규화된 테이블들을 확인할 수 있습니다.")
                    except Exception as e:
                        st.error(f"❌ 정규화 중 오류 발생: {e}")
            else:
                st.error("❌ normalize_db.py 모듈을 찾을 수 없습니다.")


def main():
    init_session_state()
    if not is_remote_database():
        init_users_directory()  # 로컬 개발용 사용자 디렉토리
    check_database()
    
    # 로그인은 브라우저 세션에서만 유지한다. 서버 공용 파일 자동 로그인은 사용하지 않는다.
    if 'logged_in' not in st.session_state or not st.session_state.get('logged_in'):
        render_login_page()
        return
    
    username = st.session_state.get('username')
    
    # 사용자별 데이터 로드 (로그인 시 한 번만)
    if 'saved_tables' not in st.session_state or len(st.session_state.saved_tables) == 0:
        if username:
            st.session_state.saved_tables = load_tables_from_file(username)
    
    # 사이드바 메뉴 (넓어진 너비 + 실제 바다 배경)
    with st.sidebar:
        if is_remote_database():
            st.caption(f"● {backend_label()} 영구 저장 연결됨")
        else:
            st.warning("로컬 임시 저장 모드 · 배포 시 데이터가 사라질 수 있습니다.")

        # 로그인 정보 표시
        st.markdown(f"""
        <div style="
            padding: 0.75rem;
            margin-bottom: 1rem;
            background: #f8f9fa;
            border-radius: 6px;
            border: 1px solid #e5e7eb;
        ">
            <p style="margin: 0; font-size: 0.85rem; color: #64748b;">
                <strong>👤 {username}</strong>
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        # 로그아웃 버튼
        if st.button("🚪 로그아웃", use_container_width=True):
            st.session_state['logged_in'] = False
            st.session_state.pop('username', None)
            st.session_state.pop('saved_tables', None)
            st.session_state.messages = []
            st.rerun()
        
        st.markdown("---")
        # ⭐ 로고/제목 (하얀 배경에 맞게)
        st.markdown("""
        <div style="
            padding: 1.5rem 1.2rem 1.3rem 1.2rem;
            border-bottom: 1px solid #e5e7eb;
            margin-bottom: 1.2rem;
            background: #ffffff;
            border-radius: 8px;
            margin: 0.5rem;
        ">
            <h1 style="
                font-size: 1.4rem;
                font-weight: 700;
                color: #1e293b;
                margin: 0;
                letter-spacing: -0.5px;
                text-align: center;
            ">TalkToData</h1>
            <p style="
                font-size: 0.75rem;
                color: #64748b;
                margin: 0.4rem 0 0 0;
                text-align: center;
                font-weight: 500;
            ">AI 기반 분석 시스템</p>
        </div>
        """, unsafe_allow_html=True)
        
        # ⭐ 간격
        st.markdown("<div style='margin-bottom: 1.2rem;'></div>", unsafe_allow_html=True)
        
        # ⭐ 메뉴
        selected = st.radio(
            "메뉴",
            ["SQL - 회계분석", "SQL - 세무분석", "설정"],
            key="navigation",
            label_visibility="collapsed"
        )
        
        # ⭐ 하단 여백만 유지
        st.markdown("<div style='height: 3rem;'></div>", unsafe_allow_html=True)
    
    # 페이지 라우팅
    if selected == "SQL - 회계분석":
        render_dashboard_page()
    elif selected == "SQL - 세무분석":
        render_tax_analysis_page()
    elif selected == "설정":
        render_settings_page()


if __name__ == '__main__':
    main()

