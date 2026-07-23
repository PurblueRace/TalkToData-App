# TalkToData

제조업 데이터를 자연어로 조회·분석하는 Streamlit 앱입니다. 로컬 개발은 SQLite를 사용하고, 배포 환경은 Supabase PostgreSQL에 업로드 데이터·설정·계정·저장된 표를 영구 보관합니다.

## 주요 기능

- 자연어 질문을 읽기 전용 SQL로 변환
- 제조업 회계·매출·원가·재고 데이터 분석
- CSV/Excel 단일·다중 테이블 업로드
- 사용자별 분석 결과와 저장된 표 보관
- SQLite 개발 모드와 Supabase 운영 모드 자동 전환

## Supabase 영구 저장 전환

### 1. Supabase 프로젝트 준비

Supabase 프로젝트의 **Connect → Session pooler**에서 `postgresql://...` 형식의 연결 주소를 복사합니다. Project URL이나 anon key가 아니라 데이터베이스 연결 주소가 필요합니다.

로컬에서는 `.streamlit/secrets.toml.example`을 `.streamlit/secrets.toml`로 복사한 뒤 값을 넣습니다.

```toml
SUPABASE_DB_URL = "postgresql://postgres.PROJECT_REF:PASSWORD@POOLER_HOST:5432/postgres?sslmode=require"
SUPABASE_READER_PASSWORD = "replace-with-a-long-random-reader-password"
ALLOW_SIGNUP = false
OPENAI_API_KEY = "sk-..."
OPENAI_MODEL = "gpt-5.2"
```

`SUPABASE_READER_PASSWORD`는 기존 로그인과 겹치지 않는 긴 임의 값으로 만드세요. 앱이 실제 `talktodata_reader` DB 계정을 만들고 AI 조회 연결을 물리적으로 분리합니다. 비밀번호가 들어 있는 `secrets.toml`은 GitHub에 올리지 않습니다. 연결 주소나 비밀번호도 채팅에 붙여 넣지 마세요.

### 2. 기존 제조업 데이터 확인 및 이전

먼저 원본 SQLite의 테이블과 행 수만 확인합니다.

```bash
python migrate_to_supabase.py --dry-run
```

Supabase 연결값을 설정한 뒤 최초 이전을 실행합니다.

```bash
python migrate_to_supabase.py
```

같은 이름의 기존 Supabase 테이블을 새 원본으로 교체해야 할 때만 다음 옵션을 사용합니다.

```bash
python migrate_to_supabase.py --replace-existing
```

모든 표는 임시 영역에 먼저 적재·검증되며, 검증이 끝난 뒤 한 번에 교체됩니다. 실패하면 기존 PostgreSQL 데이터는 유지됩니다. 로컬에 `users/users_db.json`이 있으면 계정은 비밀번호 해시로 변환되어 함께 이전됩니다.

### 3. Streamlit Community Cloud 연결

앱의 **Settings → Secrets**에 로컬과 같은 값을 저장한 뒤 배포합니다. 연결값이 없으면 로컬 SQLite 개발 모드로 실행되고, 연결값이 잘못되면 임시 저장소로 조용히 돌아가지 않고 오류를 표시합니다.

운영 데이터는 비공개 `talktodata` 스키마에, 로그인·설정·저장된 표는 비공개 `ttd_meta` 스키마에 보관됩니다. 두 스키마를 Supabase의 Exposed schemas에 추가하지 마세요. AI가 만든 쿼리는 관리자 세션을 재사용하지 않고 별도의 읽기 전용 LOGIN 연결과 제한 시간 안에서만 실행됩니다.

## 로컬 실행

```bash
pip install -r requirements.txt
streamlit run sqlite_viewer.py
```

브라우저에서 `http://localhost:8501`을 엽니다.

## 구성

```text
sqlite_viewer.py              Streamlit 앱
persistent_db.py              SQLite/PostgreSQL 공통 저장 계층
migrate_to_supabase.py        최초 데이터 이전·검증 도구
config_manager.py             테이블·컬럼 설정 관리
accounting.db                 로컬 개발 및 최초 이전용 제조업 데이터
.streamlit/secrets.toml.example  비밀값 형식 예시
```

## 보안 메모

- SQL 실행은 `SELECT`와 읽기 전용 `WITH` 한 개만 허용합니다.
- 공개 회원가입은 운영 모드에서 기본적으로 꺼져 있습니다. 필요한 동안만 `ALLOW_SIGNUP = true`로 바꾸고 계정을 만든 뒤 다시 끄세요.
- 과거 Git 기록에 들어간 비밀번호를 다른 곳에서도 사용했다면 반드시 변경해야 합니다.
- 데이터베이스 백업은 Supabase 프로젝트 정책에 맞춰 별도로 관리하세요.
