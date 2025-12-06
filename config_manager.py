"""
TalkToData - Config Manager
JSON 기반 컬럼 설정 관리 시스템
"""

import json
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Set
import re

# Config 파일 경로
CONFIG_PATH = Path(__file__).parent / 'config.json'

# 기본 컬럼 키워드 (자동 감지용)
DEFAULT_COLUMN_KEYWORDS = {
    "id": ["번호", "id", "코드", "idx", "index", "no"],
    "date": ["날짜", "date", "일자", "기간", "시작", "종료", "등록일", "수정일"],
    "amount": ["금액", "차변", "대변", "amount", "금", "가격", "단가", "합계", "총액"],
    "account_code": ["계정코드", "account", "account_code", "계정"],
    "account_name": ["계정명", "account_name", "계정이름"],
    "client_code": ["거래처코드", "client_code", "거래처"],
    "client_name": ["거래처명", "client_name", "거래처이름"],
    "dept_code": ["부서코드", "dept_code", "department", "부서"],
    "dept_name": ["부서명", "dept_name", "부서이름"],
    "project_code": ["프로젝트코드", "project_code", "project", "프로젝트"],
    "project_name": ["프로젝트명", "project_name", "프로젝트이름"],
    "journal_id": ["전표번호", "journal_id", "전표", "번호"],
    "description": ["설명", "description", "내용", "텍스트", "메모", "비고"],
    "head_text": ["헤드텍스트", "head_text", "헤더", "제목"],
    "line_text": ["라인텍스트", "line_text", "라인", "상세"],
    "evidence_type": ["증빙유형", "evidence_type", "증빙", "유형"]
}


def load_config() -> Dict:
    """
    config.json 파일을 읽어서 반환
    파일이 없으면 기본 구조 생성
    UTF-8 인코딩 사용
    """
    try:
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except UnicodeDecodeError:
                # UTF-8 실패 시 cp949 시도
                print("⚠️ UTF-8 인코딩 실패, cp949로 재시도...")
                with open(CONFIG_PATH, 'r', encoding='cp949') as f:
                    config = json.load(f)
            
            # 기본 구조 확인 및 보완
            if not isinstance(config, dict):
                config = {}
                
            if "required_columns" not in config or not isinstance(config["required_columns"], dict):
                config["required_columns"] = {}
            if "table_aliases" not in config or not isinstance(config["table_aliases"], dict):
                config["table_aliases"] = {}
            if "column_keywords" not in config or not isinstance(config["column_keywords"], dict):
                config["column_keywords"] = DEFAULT_COLUMN_KEYWORDS.copy()
            if "managed_tables" not in config or not isinstance(config["managed_tables"], list):
                config["managed_tables"] = []
            
            return config
        else:
            # 파일이 없으면 기본 구조 생성
            default_config = {
                "required_columns": {},
                "table_aliases": {},
                "column_keywords": DEFAULT_COLUMN_KEYWORDS.copy(),
                "managed_tables": []
            }
            save_config(default_config)
            return default_config
    except json.JSONDecodeError as e:
        print(f"⚠️ Config JSON 파싱 오류: {e}")
        # JSON 파싱 오류 시 기본 구조 반환
        return {
            "required_columns": {},
            "table_aliases": {},
            "column_keywords": DEFAULT_COLUMN_KEYWORDS.copy(),
            "managed_tables": []
        }
    except Exception as e:
        print(f"⚠️ Config 로드 오류: {e}")
        # 오류 발생 시 기본 구조 반환
        return {
            "required_columns": {},
            "table_aliases": {},
            "column_keywords": DEFAULT_COLUMN_KEYWORDS.copy(),
            "managed_tables": []
        }


def save_config(config: Dict) -> bool:
    """
    config.json 파일에 설정 저장
    UTF-8 인코딩 사용, 에러 처리 강화
    """
    try:
        # 입력 검증
        if not isinstance(config, dict):
            print(f"❌ Config는 dict 타입이어야 합니다. 현재 타입: {type(config)}")
            return False
        
        # 디렉토리가 없으면 생성
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        # 임시 파일로 저장 후 이동 (원자적 연산)
        temp_path = CONFIG_PATH.with_suffix('.tmp')
        
        # JSON 파일 저장 (UTF-8, 들여쓰기 2칸, ensure_ascii=False로 한글 유지)
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        
        # 원본 파일 백업 (존재하는 경우)
        if CONFIG_PATH.exists():
            backup_path = CONFIG_PATH.with_suffix('.bak')
            import shutil
            shutil.copy2(CONFIG_PATH, backup_path)
        
        # 임시 파일을 원본으로 이동
        temp_path.replace(CONFIG_PATH)
        
        return True
    except PermissionError as e:
        print(f"❌ Config 저장 권한 오류: {e}")
        print(f"   파일 경로: {CONFIG_PATH}")
        return False
    except Exception as e:
        print(f"❌ Config 저장 오류: {e}")
        import traceback
        print(traceback.format_exc())
        return False


def normalize_column_name(column_name: str) -> str:
    """
    컬럼명 정규화 (공백 제거, 소문자 변환 등)
    """
    if pd.isna(column_name):
        return ""
    
    # 문자열로 변환
    column_name = str(column_name).strip()
    
    # 공백 제거
    column_name = column_name.replace(" ", "").replace("\t", "")
    
    return column_name


def match_keyword(column_name: str, keywords: List[str]) -> bool:
    """
    컬럼명이 키워드 리스트와 매칭되는지 확인
    대소문자 구분 없음, 부분 매칭 지원
    """
    normalized_col = normalize_column_name(column_name).lower()
    
    for keyword in keywords:
        keyword_lower = str(keyword).lower().strip()
        if keyword_lower in normalized_col or normalized_col in keyword_lower:
            return True
    
    return False


def auto_detect_columns(df: pd.DataFrame, config: Optional[Dict] = None) -> List[str]:
    """
    DataFrame의 컬럼들을 분석하여 필수 컬럼 자동 감지
    
    Returns:
        감지된 필수 컬럼 리스트
    """
    if config is None:
        config = load_config()
    
    detected_columns = []
    all_columns = [str(col).strip() for col in df.columns]
    
    # 컬럼 키워드 가져오기
    column_keywords = config.get("column_keywords", DEFAULT_COLUMN_KEYWORDS)
    
    # 각 컬럼을 키워드와 매칭
    for col in all_columns:
        normalized_col = normalize_column_name(col).lower()
        
        # 각 키워드 카테고리별로 확인
        for keyword_type, keywords in column_keywords.items():
            if match_keyword(col, keywords):
                detected_columns.append(col)
                break  # 한 번 매칭되면 다른 키워드는 확인 안 함
    
    # 중복 제거 (순서 유지)
    seen = set()
    unique_detected = []
    for col in detected_columns:
        if col not in seen:
            seen.add(col)
            unique_detected.append(col)
    
    return unique_detected


def update_required_columns(table_name: str, columns: List[str], config: Optional[Dict] = None) -> bool:
    """
    특정 테이블의 필수 컬럼 업데이트
    """
    if config is None:
        config = load_config()
    
    # required_columns 업데이트
    if "required_columns" not in config:
        config["required_columns"] = {}
    
    config["required_columns"][table_name] = columns
    
    # 저장
    return save_config(config)


def get_required_columns(table_name: str, config: Optional[Dict] = None) -> List[str]:
    """
    특정 테이블의 필수 컬럼 가져오기
    """
    if config is None:
        config = load_config()
    
    required_columns = config.get("required_columns", {})
    return required_columns.get(table_name, [])


def get_column_keywords(config: Optional[Dict] = None) -> Dict[str, List[str]]:
    """
    컬럼 키워드 가져오기
    """
    if config is None:
        config = load_config()
    
    return config.get("column_keywords", DEFAULT_COLUMN_KEYWORDS.copy())


def get_managed_tables(config: Optional[Dict] = None) -> List[str]:
    """
    JSON에서 관리되는 테이블 목록 가져오기
    """
    if config is None:
        config = load_config()
    
    if "managed_tables" not in config:
        config["managed_tables"] = []
    
    return config.get("managed_tables", [])


def add_managed_table(table_name: str, config: Optional[Dict] = None) -> bool:
    """
    JSON에 테이블 추가 (중복 방지)
    """
    if config is None:
        config = load_config()
    
    if "managed_tables" not in config:
        config["managed_tables"] = []
    
    if table_name not in config["managed_tables"]:
        config["managed_tables"].append(table_name)
        return save_config(config)
    
    return True


def remove_managed_table(table_name: str, config: Optional[Dict] = None) -> bool:
    """
    JSON에서 테이블 제거
    """
    if config is None:
        config = load_config()
    
    if "managed_tables" not in config:
        config["managed_tables"] = []
    
    if table_name in config["managed_tables"]:
        config["managed_tables"].remove(table_name)
        return save_config(config)
    
    return True


def update_managed_tables(tables: List[str], config: Optional[Dict] = None) -> bool:
    """
    JSON에 테이블 목록 전체 업데이트
    """
    if config is None:
        config = load_config()
    
    config["managed_tables"] = list(set(tables))  # 중복 제거
    return save_config(config)


def clear_managed_tables(config: Optional[Dict] = None) -> bool:
    """
    JSON에서 관리되는 모든 테이블 목록 초기화 (전체 마이그레이션용)
    """
    if config is None:
        config = load_config()
    
    config["managed_tables"] = []
    return save_config(config)


if __name__ == "__main__":
    # 테스트 코드
    print("Config Manager 테스트")
    
    # Config 로드
    config = load_config()
    print(f"✅ Config 로드 성공: {config}")
    
    # 샘플 DataFrame 생성
    df = pd.DataFrame({
        "전표번호": [1, 2, 3],
        "거래일자": ["2025-01-01", "2025-01-02", "2025-01-03"],
        "계정코드": ["100", "200", "300"],
        "계정명": ["현금", "매출", "비용"],
        "차변금액": [1000, 2000, 3000],
        "대변금액": [0, 2000, 0],
        "기타컬럼": ["a", "b", "c"]
    })
    
    # 자동 감지 테스트
    detected = auto_detect_columns(df, config)
    print(f"✅ 자동 감지된 컬럼: {detected}")
    
    # 필수 컬럼 저장 테스트
    success = update_required_columns("회계전표", detected, config)
    print(f"✅ 필수 컬럼 저장: {'성공' if success else '실패'}")

