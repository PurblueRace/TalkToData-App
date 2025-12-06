"""
TalkToData - AI 기반 대화형 SQL 쿼리 시스템
Wireframer/Framer 스타일 대시보드
"""

import streamlit as st
import sqlite3
import pandas as pd
import os
import re
import json
import time
import io
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from pathlib import Path

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
# 🔑 OpenAI API 키를 Streamlit Secrets에서 읽어옵니다
# ============================================
try:
    OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
except (KeyError, AttributeError):
    st.error("⚠️ OpenAI API 키를 Streamlit Secrets에 설정해주세요!")
    st.info("💡 Streamlit Cloud에서 Secrets를 설정하는 방법:")
    st.markdown("""
    1. Streamlit Cloud 대시보드로 이동
    2. 앱 선택 → Settings → Secrets
    3. 다음 형식으로 추가:
    ```
    OPENAI_API_KEY = "your-api-key-here"
    ```
    """)
    st.stop()

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
FROM BOM마스터 b
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
  FROM BOM마스터 b
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
       탭 호버 효과 - 사이드바 스타일 동일 적용
       ========================================== */
    
    /* 탭 버튼 기본 스타일 */
    button[data-baseweb="tab"] {
        padding: 0.75rem 1.5rem !important;
        margin: 0 0.25rem !important;
        border-radius: 0 !important;
        border: none !important;
        border-bottom: 3px solid transparent !important;
        background: transparent !important;
        color: #475569 !important;
        font-size: 0.95rem !important;
        font-weight: 500 !important;
        transition: all 0.25s ease !important;
        cursor: pointer !important;
    }
    
    /* 호버 시 빨간색 + 밑줄 */
    button[data-baseweb="tab"]:hover {
        color: #dc2626 !important;
        border-bottom: 3px solid #dc2626 !important;
        background: transparent !important;
    }
    
    /* 선택된 탭 - 진한 빨간색 + 굵은 밑줄 */
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #b91c1c !important;
        border-bottom: 3px solid #b91c1c !important;
        font-weight: 700 !important;
        background: transparent !important;
    }
    
    /* 탭 컨테이너 */
    div[data-baseweb="tab-list"] {
        border-bottom: 1px solid #e5e7eb !important;
        background: transparent !important;
        gap: 0 !important;
    }
    
    /* 탭 패널 */
    div[data-baseweb="tab-panel"] {
        padding-top: 2rem !important;
    }
    </style>
 """, unsafe_allow_html=True)


def check_database():
    """데이터베이스 파일 존재 확인"""
    if not os.path.exists(DB_PATH):
        st.error(f"⚠️ '{DB_PATH}' 파일을 찾을 수 없습니다.")
        st.info("💡 먼저 `python setup_db.py`를 실행하여 데이터베이스를 생성하세요.")
        st.stop()
    return True


def get_db_connection():
    """데이터베이스 연결"""
    return sqlite3.connect(DB_PATH)


def get_db_schema() -> Dict[str, List[str]]:
    """데이터베이스 스키마 정보 가져오기"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    
    schema = {}
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        schema[table] = columns
    
    conn.close()
    return schema


def get_all_tables() -> List[str]:
    """데이터베이스의 모든 테이블 목록 가져오기"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        return tables
    except Exception as e:
        st.error(f"테이블 목록 조회 오류: {e}")
        return []


def delete_table(table_name: str) -> bool:
    """데이터베이스에서 테이블 삭제"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
        conn.commit()
        conn.close()
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
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_V2},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.03,
            max_tokens=500
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
        st.error(f"SQL 생성 오류: {str(e)}")
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
        #     model="gpt-4o",
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
        st.error(f"SQL 생성 오류: {str(e)}")
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
        df = pd.read_sql_query(sql_query, get_db_connection())
        return df
    except Exception as e:
        st.error(f"쿼리 실행 오류: {str(e)}")
        return pd.DataFrame()


def get_schema_context() -> str:
    """config.json의 required_columns, column_keywords, managed_tables만 사용하여 스키마 정보 반환"""
    try:
        config = load_config()
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # managed_tables 가져오기
        managed_tables = get_managed_tables(config) if get_managed_tables else []
        if not managed_tables:
            conn.close()
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
        
        conn.close()
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
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "당신은 구체적인 수치 기반 분석을 제공하는 회계 데이터 분석 전문가입니다. 일반적인 문구를 사용하지 말고, 실제 데이터의 구체적인 수치와 인사이트만 제공하세요. 핵심 결론 요약은 반드시 1), 2), 3) 형식으로 각 줄에 작성하세요."},
                {"role": "user", "content": analysis_prompt}
            ],
            temperature=0.3,  # ⭐ 더 정확한 수치 분석을 위해 낮춤
            max_tokens=1500
        )
        
        insight_report = response.choices[0].message.content.strip()
        return insight_report
        
    except Exception as e:
        return f"인사이트 생성 오류: {str(e)}\n\n기본 요약:\n데이터 {len(df)}건 조회됨."


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
USERS_DIR = Path("users")
USERS_DB_FILE = USERS_DIR / "users_db.json"
CURRENT_USER_FILE = USERS_DIR / "current_user.json"

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
    users_db = load_users_db()
    if username in users_db:
        # 간단한 비밀번호 검증 (실제 운영 시에는 해시 사용 권장)
        return users_db[username].get('password') == password
    return False

def create_user(username: str, password: str) -> bool:
    """새 사용자 생성"""
    if not username or not password:
        return False
    users_db = load_users_db()
    if username in users_db:
        return False  # 이미 존재
    users_db[username] = {
        'password': password,  # 실제 운영 시에는 bcrypt 등으로 해시 저장
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

def save_current_user(username: str) -> bool:
    """현재 로그인한 사용자 저장"""
    try:
        USERS_DIR.mkdir(exist_ok=True)
        with open(CURRENT_USER_FILE, 'w', encoding='utf-8') as f:
            json.dump({"username": username, "logged_in_at": datetime.now().isoformat()}, f, ensure_ascii=False)
        return True
    except Exception as e:
        return False

def load_current_user() -> str:
    """저장된 현재 사용자 로드"""
    try:
        if CURRENT_USER_FILE.exists():
            with open(CURRENT_USER_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('username', '')
        return ''
    except Exception:
        return ''

def clear_current_user() -> bool:
    """로그인 상태 파일 삭제"""
    try:
        if CURRENT_USER_FILE.exists():
            CURRENT_USER_FILE.unlink()
        return True
    except Exception:
        return False

# ============================================================
# 저장된 표 파일 관리 (사용자별)
# ============================================================
def save_tables_to_file(saved_tables: List[Dict], username: str) -> bool:
    """사용자별로 저장된 표 목록을 JSON 파일에 저장"""
    try:
        if not username:
            return False
        user_file = get_user_data_path(username, "saved_tables.json")
        # 디렉토리 생성
        user_file.parent.mkdir(parents=True, exist_ok=True)
        
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
        
        with open(user_file, 'w', encoding='utf-8') as f:
            json.dump(tables_data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        return False

def load_tables_from_file(username: str) -> List[Dict]:
    """사용자별 JSON 파일에서 저장된 표 목록 로드"""
    try:
        if not username:
            return []
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
        return []

# ============================================================
# 로그인 페이지 렌더링
# ============================================================
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
                # 로그인 상태 파일에 저장
                save_current_user(username)
                # 사용자별 데이터 로드
                st.session_state.saved_tables = load_tables_from_file(username)
                st.success("✅ 로그인 성공!")
                st.rerun()
            else:
                st.error("❌ 사용자명 또는 비밀번호가 올바르지 않습니다.")
    
    with tab2:
        st.markdown("### 회원가입")
        st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        new_username = st.text_input("새 사용자명", key="signup_username")
        st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        new_password = st.text_input("새 비밀번호", type="password", key="signup_password")
        st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        confirm_password = st.text_input("비밀번호 확인", type="password", key="signup_confirm")
        st.markdown("<div style='height: 2rem;'></div>", unsafe_allow_html=True)  # 간격 추가
        
        if st.button("회원가입", type="primary", use_container_width=True):
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
# 2. [신규] 시장/뉴스 융합 심층 분석 함수
# ============================================================
def generate_comprehensive_report(saved_tables: List[Dict], additional_prompt: str = "", custom_system_prompt: str = None) -> str:
    """저장된 여러 표 + 시장/뉴스 정보를 융합한 심층 보고서 생성 (HTML 카드 스타일 적용)"""
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

    # 데이터에서 키워드 추출 (외부 검색용)
    keywords = []
    for item in saved_tables:
        query = item.get('query', '')
        df = item['data']
        # 컬럼명에서 키워드 추출
        for col in df.columns:
            if any(word in str(col) for word in ['부서', '산업', '제품', '프로젝트', '계정', '거래처']):
                keywords.append(str(col))
        # 쿼리에서 키워드 추출
        if query:
            keywords.append(query)
    
    keyword_str = ", ".join(list(set(keywords))[:10])  # 중복 제거 후 상위 10개
    
    # 2. 시스템 프롬프트: HTML/CSS 스타일링 전문가 역할 부여 + 외부 데이터 검색 요구
    if custom_system_prompt:
        system_prompt = custom_system_prompt
    else:
        system_prompt = f"""
    당신은 대기업 최고 전략 책임자(CSO)이자 UI/UX 디자인 감각이 뛰어난 데이터 분석가입니다.
    
    **매우 중요한 요구사항:**
    1. 반드시 **실제 외부 데이터**를 검색하여 활용해야 합니다. 가상의 정보를 만들어내지 마세요.
    2. 분석에 사용한 **모든 기사, 논문, 연구의 출처를 명확히 명시**해야 합니다 (제목, 출처, 날짜 등).
    3. 보고서는 **최소 2000단어 이상**으로 매우 상세하고 깊이 있게 작성해야 합니다.
    4. 각 섹션은 충분히 길고 구체적인 내용을 포함해야 합니다.
    5. 출력 결과는 **반드시 세련된 HTML 포맷**이어야 합니다. Markdown을 쓰지 마십시오.
    
    현재 분석 주제 관련 키워드: {keyword_str}
    이 키워드들을 기반으로 실제 최신 뉴스, 산업 리포트, 학술 논문, 시장 조사 자료를 검색하여 활용하세요.
    """

    # 3. 사용자 프롬프트: 구체적인 디자인 가이드라인 제공
    additional_context = f"\n\n[사용자 추가 요청사항]\n{additional_prompt}\n" if additional_prompt else ""
    
    user_prompt = f"""
    아래 [저장된 표 데이터]를 **기반(base)**으로 하여 종합 경영 인사이트 보고서를 작성해줘.
    표 데이터는 분석의 핵심 근거이며, 모든 인사이트는 이 데이터에서 도출되어야 합니다.
    
    **필수 요구사항:**
    1. **실제 외부 데이터 검색 필수**: 다음 주제들에 대해 실제 최신 정보를 검색하여 활용하세요:
       - 관련 산업의 최신 트렌드 및 시장 동향
       - 업계 뉴스 및 경제 리포트
       - 관련 학술 논문 및 연구 결과
       - 벤치마킹 데이터 및 경쟁사 분석
       
    2. **출처 명시 필수**: 모든 외부 인용 자료는 다음 형식으로 명시해야 합니다:
       - 기사: "제목" (출판사/매체, 발행일)
       - 논문/연구: "연구 제목" (저자, 학술지/기관, 발행연도)
       - 리포트: "보고서 제목" (기관명, 발행연도)
       
    3. **보고서 길이**: 최소 2000단어 이상의 상세한 분석이어야 합니다.
    
    4. **상세한 내용 요구**:
       - 데이터 융합 섹션: 표 간의 연관성을 깊이 있게 분석하고, 수치의 의미를 상세히 설명
       - 시장 동향 섹션: 실제 검색한 외부 자료를 기반으로 최소 3개 이상의 구체적인 시장 트렌드 제시
       - 핵심 리스크: 각 리스크를 3-4줄 이상 상세히 설명하고, 발생 가능성 및 영향도 분석 포함
       - 성장 기회: 각 기회를 3-4줄 이상 상세히 설명하고, 실현 가능성 및 예상 효과 포함
       - C-Level 실행 전략: 각 전략을 5-7줄 이상의 매우 구체적이고 실행 가능한 단계별 액션 플랜으로 작성
         * 각 전략은 다음을 포함해야 함: 구체적 실행 단계, 예상 일정, 필요 자원, 담당 부서/인력, 예상 비용, 성공 지표(KPI), 리스크 관리 방안
       
    {additional_context}
    
    [디자인 요구사항]
    1. 전체를 감싸는 메인 카드는 흰색 배경, 둥근 모서리(16px), 부드러운 그림자(box-shadow)를 가질 것.
    2. 제목은 그라데이션 텍스트나 진한 네이비색을 사용하여 강조할 것.
    3. 각 섹션(데이터 융합, 뉴스 분석, 리스크/기회, 실행 전략)은 구분선이나 연한 회색 박스로 구분할 것.
    4. 'Risk'는 붉은 계열 배경의 뱃지 스타일, 'Opportunity'는 푸른 계열 배경의 뱃지 스타일을 적용할 것.
    5. 모든 텍스트는 가독성을 위해 적절한 줄간격(line-height: 1.6)을 유지할 것.
    6. **절대 Markdown 코드 블록(```html)을 사용하지 말고, 순수 HTML 코드만 출력할 것.**
    7. 출처는 각 섹션 하단에 작은 글씨로 명시하거나, 별도의 "참고문헌" 섹션을 추가할 것.

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

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.6,
            max_tokens=8000
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
        return f"<div style='color: red; padding: 1rem;'>분석 중 오류 발생: {str(e)}</div>"


def check_table_exists(table_name: str) -> bool:
    """테이블 존재 여부 확인"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except:
        return False


def fetch_journal_entries():
    """전표내역 데이터 조회 (JOIN 포함)"""
    try:
        conn = get_db_connection()
        
        # 정규화된 테이블(전표내역)이 있는지 확인
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='전표내역'")
        is_normalized = cursor.fetchone() is not None
        
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
        
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        # 숫자 포맷팅 적용
        df = format_numeric_columns(df)
        
        return df
    except Exception as e:
        st.error(f"전표내역 조회 오류: {e}")
        return pd.DataFrame()


def fetch_clients():
    """거래처 데이터 조회"""
    try:
        conn = get_db_connection()
        # 정규화된 '거래처' 테이블 우선 조회
        if check_table_exists('거래처'):
            query = "SELECT * FROM 거래처 ORDER BY 거래처명"
        elif check_table_exists('clients'):
            query = "SELECT * FROM clients ORDER BY client_name"
        else:
            return pd.DataFrame()
            
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        # 숫자 포맷팅 적용
        df = format_numeric_columns(df)
        
        return df
    except Exception as e:
        st.error(f"거래처 조회 오류: {e}")
        return pd.DataFrame()


def fetch_accounts():
    """계정과목 데이터 조회"""
    try:
        conn = get_db_connection()
        # 정규화된 '계정과목' 테이블 우선 조회
        if check_table_exists('계정과목'):
            query = "SELECT * FROM 계정과목 ORDER BY 계정코드"
        elif check_table_exists('accounts'):
            query = "SELECT * FROM accounts ORDER BY account_code"
        else:
            return pd.DataFrame()
            
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        # 숫자 포맷팅 적용
        df = format_numeric_columns(df)
        
        return df
    except Exception as e:
        st.error(f"계정과목 조회 오류: {e}")
        return pd.DataFrame()


def fetch_departments():
    """부서 데이터 조회"""
    try:
        conn = get_db_connection()
        # 정규화된 '부서' 테이블 우선 조회
        if check_table_exists('부서'):
            query = "SELECT * FROM 부서 ORDER BY 부서코드"
        elif check_table_exists('departments'):
            query = "SELECT * FROM departments ORDER BY dept_code"
        else:
            return pd.DataFrame()
            
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        # 숫자 포맷팅 적용
        df = format_numeric_columns(df)
        
        return df
    except Exception as e:
        st.error(f"부서 조회 오류: {e}")
        return pd.DataFrame()

def fetch_projects():
    """프로젝트 데이터 조회"""
    try:
        conn = get_db_connection()
        if check_table_exists('프로젝트'):
            query = "SELECT * FROM 프로젝트 ORDER BY 프로젝트코드"
            df = pd.read_sql_query(query, conn)
            conn.close()
            df = format_numeric_columns(df)
            return df
        return pd.DataFrame()
    except Exception as e:
        st.error(f"프로젝트 조회 오류: {e}")
        return pd.DataFrame()


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
    
    # 공백 추가
    st.write("")
    
    # 탭 구성 (항상 표시)
    tab1, tab2, tab3, tab4 = st.tabs(["📝 SQL & 데이터", "📊 저장된 표 관리", "🧠 AI 융합 분석", "📈 시각화"])
    
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
            st.markdown("### 생성된 SQL")
            st.code(current_sql_query, language='sql')
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
                        st.session_state.saved_tables.append(saved_item)
                        # 파일에 저장 (사용자별)
                        username = st.session_state.get('username')
                        if username:
                            save_tables_to_file(st.session_state.saved_tables, username)
                        st.success(f"✅ 저장 완료! (현재 {len(st.session_state.saved_tables)}개)")
                    else:
                        st.warning("⚠️ 이미 저장된 표입니다.")
        else:
            st.info("💡 위의 입력창에 질문을 입력하면 SQL 쿼리 결과가 여기에 표시됩니다.")

    # [탭 2] 저장된 표 관리
    with tab2:
        st.subheader("📂 저장된 데이터 보관함")
        
        if not st.session_state.saved_tables:
            st.info("📭 보관함이 비어있습니다. 'SQL & 데이터' 탭에서 표를 저장해주세요.")
        else:
            st.markdown(f"총 **{len(st.session_state.saved_tables)}개**의 표가 저장되었습니다.")
            
            # 저장된 표 목록 (카드 형태로 표시)
            for i, item in enumerate(st.session_state.saved_tables):
                # 카드 스타일 컨테이너
                st.markdown(f"""
                <div style="
                    border: 1px solid #e0e0e0;
                    border-radius: 8px;
                    padding: 1rem;
                    margin-bottom: 1rem;
                    background-color: #ffffff;
                    box-shadow: 0px 2px 4px rgba(0,0,0,0.1);
                ">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                        <h4 style="margin: 0; color: #333;">📑 {item['query']}</h4>
                        <small style="color: #666;">{item['timestamp']}</small>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                
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
                
                # 전체 데이터 표시 (스크롤 가능)
                st.dataframe(
                    df_display.style.format(format_dict), 
                    use_container_width=True, 
                    height=300
                )
                
                col1, col2 = st.columns([1, 4])
                with col1:
                    if st.button(f"🗑️ 삭제하기", key=f"del_{i}"):
                        st.session_state.saved_tables.pop(i)
                        # 파일에 저장 (삭제 반영, 사용자별)
                        username = st.session_state.get('username')
                        if username:
                            save_tables_to_file(st.session_state.saved_tables, username)
                        st.rerun()
                
                st.markdown("---")
    
    # [탭 3] AI 융합 심층 분석
    with tab3:
        st.subheader("🧠 AI 융합 심층 분석")
        st.caption("저장된 모든 데이터와 시장/뉴스 정보를 결합하여 의미 있는 인사이트를 도출합니다.")
        
        if not st.session_state.saved_tables:
            st.info("📭 분석할 데이터가 없습니다. 'SQL & 데이터' 탭에서 표를 저장해주세요.")
        else:
            # 사용자 추가 프롬프트 입력창
            user_additional_prompt = st.text_area(
                "💭 추가 분석 요청사항 (선택사항)",
                placeholder="예: 관련 학술 논문을 인용하여 학술적 근거를 제시해줘 / 해당 산업의 최신 트렌드와 시장 동향을 조사하여 데이터와 연계 분석해줘 / 국내외 연구사례를 참고하여 비교 분석해줘",
                height=100,
                key="ai_analysis_prompt"
            )
            
            analyze_key = f"analyze_{hash(str(st.session_state.saved_tables))}"
            if st.button("🚀 데이터+시장+뉴스 융합 분석하기", type="primary", use_container_width=True, key=analyze_key):
                with st.spinner("🤖 시장 트렌드 검색 및 데이터 융합 분석 중..."):
                    report = generate_comprehensive_report(
                        st.session_state.saved_tables,
                        additional_prompt=user_additional_prompt
                    )
                    # 보고서 출력 (HTML 직접 렌더링)
                    import streamlit.components.v1 as components
                    components.html(report, height=800, scrolling=True)
    
    # [탭 4] 시각화
    with tab4:
        if current_sql_query and not current_df.empty:
            charts = create_visualizations(current_df, current_question)
            if charts:
                for chart in charts:
                    st.plotly_chart(chart[1], use_container_width=True)
            else:
                st.info("시각화할 수 있는 데이터가 없습니다.")
        else:
            st.info("💡 위의 입력창에 질문을 입력하면 데이터 시각화가 여기에 표시됩니다.")
    
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
                    
                    # 🔥 테이블 스키마 재생성 (기존 테이블 삭제 후 업로드 파일의 모든 컬럼으로 재생성)
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    
                    # 기존 테이블 스키마 확인 (정보 표시용)
                    cursor.execute(f"PRAGMA table_info({selected_table})")
                    existing_columns = [row[1] for row in cursor.fetchall()]
                    
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
                    
                    # 기존 테이블 삭제
                    with st.spinner("🔄 테이블 스키마 재생성 중..."):
                        cursor.execute(f"DROP TABLE IF EXISTS {selected_table}")
                        conn.commit()
                    
                    # 업로드 모드에 따라 처리
                    mode = 'append' if "추가" in upload_mode else 'replace'
                    
                    # 업로드 파일의 모든 컬럼으로 테이블 재생성 및 데이터 업로드
                    with st.spinner(f"💾 `{selected_table}` 테이블에 데이터 저장 중..."):
                        # 업로드 파일의 모든 컬럼 사용
                        df_upload.to_sql(selected_table, conn, if_exists=mode, index=False)
                    
                    conn.close()
                    
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
        conn = get_db_connection()
        
        # 선택된 테이블 데이터 조회
        # 전표내역인 경우 정렬 적용
        if selected_table == '전표내역':
            query = f"SELECT * FROM {selected_table} ORDER BY 거래일자 DESC, 전표번호"
        elif selected_table == '회계전표':
             query = f"SELECT * FROM {selected_table} ORDER BY 거래일자 DESC, 전표번호"
        else:
            query = f"SELECT * FROM {selected_table}"
            
        df = pd.read_sql_query(query, conn)
        conn.close()
        
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
    tab1, tab2, tab3 = st.tabs(["💼 세무분석허브", "📊 기타 세무조정", "📄 세무 보고서"])
    
    with tab1:
        # 세무분석허브 - 카드 그리드
        if st.session_state.tax_analysis_mode == 'main':
            st.markdown("<h1 style='text-align: center; margin-bottom: 0.5rem;'>💼 세무 분석 허브</h1>", unsafe_allow_html=True)
            st.markdown("<p style='text-align: center; color: #64748b; font-size: 14px; margin-bottom: 3rem;'>법인세 세무조정 분석을 선택하세요</p>", unsafe_allow_html=True)
            
            # 2x2 카드 그리드
            col1, col2 = st.columns(2, gap="large")
            
            with col1:
                # 접대비 분석 카드
                if st.button(
                    label="🍽️\n\n**접대비 분석**\n\n업무추진비 한도 계산 및 세무조정 분석",
                    key="btn_entertainment",
                    use_container_width=True,
                    help="접대비(업무추진비) 세무조정 분석"
                ):
                    st.session_state.tax_analysis_mode = 'entertainment'
                    st.rerun()
                
                st.markdown("<br>", unsafe_allow_html=True)
                
                # 기부금 분석 카드
                if st.button(
                    label="🎁\n\n**기부금 분석**\n\n기부금 한도 계산 및 세액공제 분석",
                    key="btn_donation",
                    use_container_width=True,
                    help="기부금 한도 계산 및 세무조정 분석"
                ):
                    st.session_state.tax_analysis_mode = 'donation'
                    st.rerun()
            
            with col2:
                # 업무용승용차 분석 카드
                if st.button(
                    label="🚗\n\n**업무용승용차**\n\n소득처분 및 경비처리 분석",
                    key="btn_vehicle",
                    use_container_width=True,
                    help="업무용승용차 소득처분 및 경비처리 분석"
                ):
                    st.session_state.tax_analysis_mode = 'vehicle'
                    st.rerun()
                
                st.markdown("<br>", unsafe_allow_html=True)
                
                # R&D 분석 카드
                if st.button(
                    label="🔬\n\n**R&D 세액공제**\n\n연구개발비 세액공제 계산 및 분석",
                    key="btn_rd",
                    use_container_width=True,
                    help="연구개발비 세액공제 분석"
                ):
                    st.session_state.tax_analysis_mode = 'rd'
                    st.rerun()
        else:
            # 선택된 분석 양식 표시
            # 뒤로가기 버튼
            if st.button("⬅️ 메뉴로", key="back_to_menu"):
                st.session_state.tax_analysis_mode = 'main'
                st.rerun()
            
            st.markdown("<br>", unsafe_allow_html=True)
            
            # 선택된 기능 실행
            if st.session_state.tax_analysis_mode == 'entertainment':
                analyze_entertainment_expense()
            elif st.session_state.tax_analysis_mode == 'donation':
                st.info("💡 기부금 분석 기능은 향후 구현 예정입니다.")
                st.markdown("""
                **구현 예정 항목:**
                - 기부금 한도 계산 (이익금액의 100% 이내, 각종 특별법에 따른 기부금 한도)
                - 기부금 세액공제 계산
                - 기부금 세부 내역 관리
                """)
            elif st.session_state.tax_analysis_mode == 'vehicle':
                st.info("💡 업무용승용차 분석 기능은 향후 구현 예정입니다.")
                st.markdown("""
                **구현 예정 항목:**
                - 업무용승용차 소득처분 금액 계산
                - 경비처리 가능 금액 분석
                - 감가상각비 처리 분석
                """)
            elif st.session_state.tax_analysis_mode == 'rd':
                st.info("💡 R&D 세액공제 분석 기능은 향후 구현 예정입니다.")
                st.markdown("""
                **구현 예정 항목:**
                - 연구개발비 세액공제 계산
                - 중소기업 특별 세액공제 적용
                - 연구개발비 세부 항목별 공제율 계산
                """)
    
    with tab2:
        st.info("💡 기타 세무조정 항목은 향후 구현 예정입니다.")
        st.markdown("""
        **구현 예정 항목:**
        - 기부금 한도 계산
        - 연구개발비 세액공제
        - 중소기업 취업세액공제
        - 외국인근로자 고용세액공제
        """)
    
    with tab3:
        st.info("💡 종합 세무 보고서는 향후 구현 예정입니다.")
        st.markdown("""
        **기능:**
        - 접대비, 기부금 등 종합 세무조정 계산
        - 세무조정 내역서 자동 생성
        - 법인세 신고서 양식 연동
        """)


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
        conn = get_db_connection()
        
        # 매출 계정 찾기 (대변금액 합계)
        query = """
        SELECT COALESCE(SUM(대변금액), 0) as 매출액
        FROM 회계전표
        WHERE (계정명 LIKE '%매출%' OR 계정명 LIKE '%수익%')
          AND 거래일자 BETWEEN ? AND ?
        """
        
        df = pd.read_sql_query(query, conn, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        conn.close()
        
        return float(df.iloc[0]['매출액']) if not df.empty else 0.0
    except Exception as e:
        st.error(f"매출액 계산 오류: {e}")
        return 0.0


def calculate_entertainment_expense(start_date, end_date):
    """전체 접대비 계산"""
    try:
        conn = get_db_connection()
        
        query = """
        SELECT COALESCE(SUM(차변금액), 0) as 접대비
        FROM 회계전표
        WHERE 계정명 LIKE '%접대비%'
          AND 거래일자 BETWEEN ? AND ?
        """
        
        df = pd.read_sql_query(query, conn, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        conn.close()
        
        return float(df.iloc[0]['접대비']) if not df.empty else 0.0
    except Exception as e:
        st.error(f"접대비 계산 오류: {e}")
        return 0.0


def calculate_culture_entertainment_expense(start_date, end_date):
    """문화접대비 계산"""
    try:
        conn = get_db_connection()
        
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
        
        df = pd.read_sql_query(query, conn, params=(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
        conn.close()
        
        return float(df.iloc[0]['문화접대비']) if not df.empty else 0.0
    except Exception as e:
        st.error(f"문화접대비 계산 오류: {e}")
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
                                
                                # 전체 마이그레이션 모드인 경우 기존 테이블 삭제
                                if is_full_migration:
                                    with st.spinner("🗑️ 기존 테이블 삭제 중..."):
                                        deleted_count = delete_managed_tables()
                                        if clear_managed_tables is not None:
                                            clear_managed_tables(config_upload)
                                        if deleted_count > 0:
                                            st.info(f"🗑️ 기존 {deleted_count}개의 테이블이 삭제되었습니다.")
                                
                                conn = get_db_connection()
                                cursor = conn.cursor()
                                
                                # 🔥 테이블 스키마 재생성 (기존 테이블 삭제 후 선택된 컬럼으로 재생성)
                                # 기존 테이블이 있는지 확인
                                cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
                                table_exists = cursor.fetchone() is not None
                                
                                if table_exists:
                                    # 기존 테이블 스키마 확인 (정보 표시용)
                                    cursor.execute(f"PRAGMA table_info({table_name})")
                                    existing_columns = [row[1] for row in cursor.fetchall()]
                                    
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
                                        
                                        # 기존 테이블 삭제
                                        with st.spinner("🔄 테이블 스키마 재생성 중..."):
                                            cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
                                            conn.commit()
                                
                                # 선택된 컬럼만 포함한 DataFrame 생성
                                df_to_upload = df_upload[selected_columns].copy()
                                
                                mode = 'append' if "추가" in upload_mode else 'replace'
                                
                                # 데이터 삽입 (테이블이 삭제되었으면 새로 생성됨)
                                with st.spinner(f"💾 `{table_name}` 테이블에 데이터 저장 중..."):
                                    df_to_upload.to_sql(table_name, conn, if_exists=mode, index=False)
                                
                                conn.close()
                                
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
                                if 'conn' in locals(): 
                                    try:
                                        conn.close()
                                    except:
                                        pass
                
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
                                        
                                        # 전체 마이그레이션 모드인 경우 기존 테이블 삭제
                                        if is_full_migration:
                                            status_text.info("🗑️ 기존 테이블 삭제 중...")
                                            with st.spinner("🗑️ 기존 테이블 삭제 중..."):
                                                deleted_count = delete_managed_tables()
                                                if clear_managed_tables is not None:
                                                    clear_managed_tables(config)
                                                if deleted_count > 0:
                                                    st.info(f"🗑️ 기존 {deleted_count}개의 테이블이 삭제되었습니다.")
                                        
                                        # 새 테이블 목록 초기화 (전체 마이그레이션인 경우)
                                        new_table_list = [] if is_full_migration else get_managed_tables(config)
                                        
                                        # 각 테이블 업로드 진행
                                        current_index = 0
                                        for sheet_name, config_data in table_configs.items():
                                            current_index += 1
                                            try:
                                                table_name = config_data['table_name']
                                                df_to_upload = config_data['df']
                                                row_count = len(df_to_upload)
                                                
                                                # 진행율 업데이트
                                                progress = current_index / total_tables
                                                progress_bar.progress(progress)
                                                status_text.info(f"📤 진행 중: {current_index}/{total_tables} - `{table_name}` 테이블 업로드 중... ({row_count:,}행)")
                                                
                                                # 업로드 파일의 모든 컬럼 사용
                                                upload_columns = list(df_to_upload.columns)
                                                
                                                # 🔥 테이블 스키마 재생성 (기존 테이블 삭제 후 업로드 파일의 모든 컬럼으로 재생성)
                                                conn = get_db_connection()
                                                cursor = conn.cursor()
                                                
                                                # 기존 테이블이 있는지 확인
                                                cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
                                                table_exists = cursor.fetchone() is not None
                                                
                                                if table_exists:
                                                    # 기존 테이블 스키마 확인 (정보 표시용)
                                                    cursor.execute(f"PRAGMA table_info({table_name})")
                                                    existing_columns = [row[1] for row in cursor.fetchall()]
                                                    
                                                    if set(upload_columns) != set(existing_columns):
                                                        # 기존 테이블 삭제
                                                        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
                                                        conn.commit()
                                                
                                                # 업로드 파일의 모든 컬럼으로 테이블 생성 및 데이터 업로드
                                                with st.spinner(f"💾 `{table_name}` 테이블에 {row_count:,}건의 데이터 업로드 중..."):
                                                    df_to_upload.to_sql(
                                                        table_name, 
                                                        conn, 
                                                        if_exists='replace', 
                                                        index=False
                                                    )
                                                conn.close()
                                                
                                                # 새 테이블 목록에 추가
                                                if table_name not in new_table_list:
                                                    new_table_list.append(table_name)
                                                
                                                # 🔥 config.json의 required_columns 업데이트 (업로드 파일의 모든 컬럼으로 교체)
                                                if update_required_columns is not None:
                                                    success = update_required_columns(table_name, upload_columns, config)
                                                    if not success:
                                                        st.warning(f"⚠️ `{table_name}` 테이블의 config.json 업데이트에 실패했습니다.")
                                                
                                                uploaded_count += 1
                                                # 개별 테이블 업로드 완료 메시지
                                                st.success(f"✅ [{current_index}/{total_tables}] `{table_name}` 테이블 업로드 완료! ({row_count:,}행, {len(upload_columns)}컬럼)")
                                                
                                            except Exception as e:
                                                failed_tables.append(f"`{table_name}`: {str(e)}")
                                                st.error(f"❌ [{current_index}/{total_tables}] `{table_name}` 테이블 업로드 실패: {str(e)}")
                                        
                                        # 진행율 100% 완료
                                        progress_bar.progress(1.0)
                                        
                                        # JSON에 테이블 목록 업데이트
                                        if update_managed_tables is not None:
                                            with st.spinner("💾 테이블 목록 저장 중..."):
                                                update_managed_tables(new_table_list, config)
                                        
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
                                            
                                            # 전체 마이그레이션 모드인 경우 기존 테이블 삭제
                                            if is_full_migration:
                                                with st.spinner("🗑️ 기존 테이블 삭제 중..."):
                                                    deleted_count = delete_managed_tables()
                                                    if clear_managed_tables is not None:
                                                        clear_managed_tables(config_upload)
                                                    if deleted_count > 0:
                                                        st.info(f"🗑️ 기존 {deleted_count}개의 테이블이 삭제되었습니다.")
                                            
                                            conn = get_db_connection()
                                            cursor = conn.cursor()
                                            
                                            # 🔥 테이블 스키마 재생성 (기존 테이블 삭제 후 선택된 컬럼으로 재생성)
                                            # 기존 테이블이 있는지 확인
                                            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
                                            table_exists = cursor.fetchone() is not None
                                            
                                            if table_exists:
                                                # 기존 테이블 스키마 확인 (정보 표시용)
                                                cursor.execute(f"PRAGMA table_info({table_name})")
                                                existing_columns = [row[1] for row in cursor.fetchall()]
                                                
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
                                                    
                                                    # 기존 테이블 삭제
                                                    with st.spinner("🔄 테이블 스키마 재생성 중..."):
                                                        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
                                                        conn.commit()
                                            
                                            # 선택된 컬럼만 포함한 DataFrame 생성
                                            df_to_upload = df_upload[selected_columns].copy()
                                            
                                            mode = 'append' if "추가" in upload_mode else 'replace'
                                            
                                            # 데이터 삽입 (테이블이 삭제되었으면 새로 생성됨)
                                            with st.spinner(f"💾 `{table_name}` 테이블에 데이터 저장 중..."):
                                                df_to_upload.to_sql(table_name, conn, if_exists=mode, index=False)
                                            
                                            conn.close()
                                            
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
                                            if 'conn' in locals(): 
                                                try:
                                                    conn.close()
                                                except:
                                                    pass
                        
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
    init_users_directory()  # 사용자 디렉토리 초기화
    check_database()
    
    # 로그인 확인 및 자동 로그인 복원
    if 'logged_in' not in st.session_state or not st.session_state.get('logged_in'):
        saved_username = load_current_user()
        if saved_username:
            # 저장된 사용자가 있으면 자동 로그인
            st.session_state['logged_in'] = True
            st.session_state['username'] = saved_username
            st.session_state.saved_tables = load_tables_from_file(saved_username)
        else:
            render_login_page()
            return
    
    username = st.session_state.get('username')
    
    # 사용자별 데이터 로드 (로그인 시 한 번만)
    if 'saved_tables' not in st.session_state or len(st.session_state.saved_tables) == 0:
        if username:
            st.session_state.saved_tables = load_tables_from_file(username)
    
    # 사이드바 메뉴 (넓어진 너비 + 실제 바다 배경)
    with st.sidebar:
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
            clear_current_user()  # 로그인 상태 파일 삭제
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

