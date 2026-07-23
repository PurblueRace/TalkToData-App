import pandas as pd
from pathlib import Path

from persistent_db import read_dataframe, replace_dataframes

# DB 경로 설정
SCRIPT_DIR = Path(__file__).parent.absolute()
DB_PATH = str(SCRIPT_DIR / 'accounting.db')

# Config Manager 임포트
try:
    from config_manager import load_config, get_required_columns
except ImportError:
    print("⚠️ config_manager를 찾을 수 없습니다. 기본 컬럼을 사용합니다.")
    load_config = None
    get_required_columns = None

def get_account_type(code, name):
    """계정코드로 계정구분(자산/부채/자본/수익/비용) 추정"""
    # 코드가 숫자인 경우
    try:
        code_int = int(code)
        if 100 <= code_int < 200: return "자산"
        elif 200 <= code_int < 300: return "부채"
        elif 300 <= code_int < 400: return "자본"
        elif 400 <= code_int < 500: return "수익" # 매출
        elif 500 <= code_int < 600: return "비용" # 매출원가
        elif 600 <= code_int < 800: return "비용" # 판관비
        elif 800 <= code_int < 900: return "수익" # 영업외수익
        elif 900 <= code_int < 1000: return "비용" # 영업외비용
    except:
        pass
    
    # 이름으로 추정
    if '자산' in name or '현금' in name or '예금' in name or '채권' in name: return "자산"
    if '부채' in name or '차입금' in name or '매입채무' in name or '예수금' in name: return "부채"
    if '자본' in name or '이익잉여금' in name: return "자본"
    if '매출' in name or '수익' in name: return "수익"
    if '비용' in name or '급여' in name or '비' in name or '원가' in name: return "비용"
    
    return "기타"

def normalize_database():
    print(f"📂 데이터베이스 연결: {DB_PATH}")
    
    # 1. 원본 데이터 읽기
    try:
        df_journal = read_dataframe('SELECT * FROM "회계전표"', DB_PATH)
        print(f"✅ 원본 데이터 로드 완료: {len(df_journal)}건")
    except Exception as e:
        print(f"❌ 오류: '회계전표' 테이블을 찾을 수 없습니다. ({e})")
        return

    normalized_tables = {}

    # 2. 계정과목 테이블 생성
    print("🔨 계정과목 테이블 생성 중...")
    if '계정코드' in df_journal.columns and '계정명' in df_journal.columns:
        df_accounts = df_journal[['계정코드', '계정명']].drop_duplicates().copy()
        df_accounts['계정구분'] = df_accounts.apply(lambda x: get_account_type(x['계정코드'], x['계정명']), axis=1)
        df_accounts['비고'] = '' # 추가 컬럼
        df_accounts['사용여부'] = 'Y' # 추가 컬럼
        
        normalized_tables['계정과목'] = df_accounts
        print(f"   - 계정과목 {len(df_accounts)}개 생성 완료")

    # 3. 거래처 테이블 생성
    print("🔨 거래처 테이블 생성 중...")
    if '거래처코드' in df_journal.columns and '거래처명' in df_journal.columns:
        df_clients = df_journal[['거래처코드', '거래처명']].drop_duplicates().copy()
        # 상세 컬럼 추가 (빈 값)
        df_clients['사업자등록번호'] = ''
        df_clients['대표자명'] = ''
        df_clients['업태'] = ''
        df_clients['종목'] = ''
        df_clients['주소'] = ''
        df_clients['전화번호'] = ''
        df_clients['담당자'] = ''
        df_clients['이메일'] = ''
        
        normalized_tables['거래처'] = df_clients
        print(f"   - 거래처 {len(df_clients)}개 생성 완료")

    # 4. 부서 테이블 생성
    print("🔨 부서 테이블 생성 중...")
    if '부서코드' in df_journal.columns and '부서명' in df_journal.columns:
        df_depts = df_journal[['부서코드', '부서명']].drop_duplicates().copy()
        # 상세 컬럼 추가
        df_depts['부서장'] = ''
        df_depts['위치'] = ''
        df_depts['전화번호'] = ''
        
        normalized_tables['부서'] = df_depts
        print(f"   - 부서 {len(df_depts)}개 생성 완료")

    # 5. 프로젝트 테이블 생성
    print("🔨 프로젝트 테이블 생성 중...")
    if '프로젝트코드' in df_journal.columns and '프로젝트명' in df_journal.columns:
        # 프로젝트 코드가 있는 경우만 (NaN 제외)
        df_projects = df_journal[['프로젝트코드', '프로젝트명']].dropna().drop_duplicates().copy()
        if not df_projects.empty:
            # 상세 컬럼 추가
            df_projects['기간_시작'] = ''
            df_projects['기간_종료'] = ''
            df_projects['상태'] = '진행중' # 진행중, 완료, 보류 등
            df_projects['PM'] = ''
            df_projects['예산'] = 0
            
            normalized_tables['프로젝트'] = df_projects
            print(f"   - 프로젝트 {len(df_projects)}개 생성 완료")
        else:
            print("   - 프로젝트 데이터가 없습니다.")

    # 6. 전표내역 테이블 생성 (정규화됨)
    print("🔨 전표내역(Normalized) 테이블 생성 중...")
    
    # Config에서 필수 컬럼 가져오기, 없으면 기본값 사용
    cols_to_keep = [
        '전표번호', '거래일자', '계정코드', '거래처코드', '원재료코드', 
        '부서코드', '프로젝트코드', '차변금액', '대변금액', 
        '헤드텍스트', '라인텍스트', '증빙유형'
    ]
    
    # Config가 있으면 Config에서 가져오기
    if get_required_columns is not None:
        try:
            config = load_config() if load_config else None
            if config:
                # '회계전표' 또는 첫 번째 테이블의 컬럼 사용
                config_cols = get_required_columns('회계전표', config)
                if config_cols:
                    cols_to_keep = config_cols
                    print(f"   - Config에서 {len(cols_to_keep)}개 컬럼 로드됨")
        except Exception as e:
            print(f"   ⚠️ Config 로드 실패, 기본 컬럼 사용: {e}")
    
    # 존재하는 컬럼만 선택
    valid_cols = [col for col in cols_to_keep if col in df_journal.columns]
    df_normalized = df_journal[valid_cols].copy()
    
    # 전표내역 테이블로 저장
    normalized_tables['전표내역'] = df_normalized
    print(f"   - 전표내역 {len(df_normalized)}건 생성 완료")
    
    replace_dataframes(normalized_tables, DB_PATH)
    print("\n✨ 모든 작업이 완료되었습니다!")

if __name__ == "__main__":
    normalize_database()
