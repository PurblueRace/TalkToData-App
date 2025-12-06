# TalkToData - 회계 감사 시스템

AI 기반 자연어 SQL 쿼리 시스템

복식부기(Double Entry) 원칙을 반영한 회계 감사 시스템의 SQLite 데이터베이스를 조회하고 분석할 수 있는 Streamlit 기반 웹 뷰어입니다.

## 📋 기능

- 📊 **대시보드**: 통계 요약 및 계정유형별 분석
- 📑 **전체 데이터 조회**: 필터링 및 검색 기능
- 🔍 **전표별 조회**: 개별 전표 상세 내역 확인
- 💾 **SQL 쿼리 실행**: 직접 SQL 쿼리 실행 가능
- ✅ **복식부기 검증**: 전표별 차변/대변 균형 검증

## 🚀 설치 및 실행

### 1. 필요한 패키지 설치

```bash
pip install -r requirements.txt
```

또는 개별 설치:

```bash
pip install streamlit pandas openpyxl
```

### 2. 데이터베이스 준비

먼저 데이터베이스를 생성하고 데이터를 적재하세요:

```bash
# 샘플 엑셀 파일 생성 (선택사항)
python create_sample_excel.py

# 데이터베이스 구축 및 데이터 적재
python setup_db.py
```

### 3. Streamlit 뷰어 실행

```bash
streamlit run sqlite_viewer.py
```

브라우저가 자동으로 열리며 `http://localhost:8501`에서 뷰어에 접속할 수 있습니다.

## 📁 파일 구조

```
.
├── setup_db.py              # DB 초기화 및 엑셀 Import 스크립트
├── create_sample_excel.py   # 샘플 엑셀 파일 생성 스크립트
├── sqlite_viewer.py         # Streamlit 기반 DB 뷰어
├── requirements.txt         # 필요한 패키지 목록
├── accounting.db            # SQLite 데이터베이스 (자동 생성)
└── dummy_data.xlsx          # 샘플 엑셀 파일 (선택사항)
```

## 🗄️ 데이터베이스 스키마

### journal_entries 테이블

| 칼럼명 | 타입 | 설명 |
|--------|------|------|
| id | INTEGER | 자동 증가 고유 ID (PK) |
| journal_id | TEXT | 전표 식별자 (예: J20250115-001) |
| line_no | INTEGER | 전표 내 순번 |
| transaction_date | DATE | 거래일자 |
| account_name | TEXT | 계정명 |
| account_type | TEXT | 계정유형 (자산/부채/자본/수익/비용) |
| client_name | TEXT | 거래처명 |
| description | TEXT | 설명 |
| debit_amount | INTEGER | 차변 금액 |
| credit_amount | INTEGER | 대변 금액 |
| department | TEXT | 부서 |
| category | TEXT | 카테고리 |

## 📊 사용 예시

### 대시보드
- 전체 통계 요약 확인
- 계정유형별 거래 분석
- 최근 전표 목록

### 전표별 조회
- 특정 전표 선택하여 상세 내역 확인
- 복식부기 원칙 준수 여부 검증

### SQL 쿼리 실행
```sql
-- 계정유형별 합계
SELECT account_type,
       SUM(debit_amount) as 차변합계,
       SUM(credit_amount) as 대변합계
FROM journal_entries
GROUP BY account_type;
```

## ⚠️ 주의사항

- SQL 쿼리 실행은 SELECT 쿼리만 허용됩니다 (보안상의 이유)
- 복식부기 원칙: 각 전표의 차변 합계 = 대변 합계

## 📝 라이센스

이 프로젝트는 회계 감사 시스템을 위한 내부 도구입니다.

