"""
엑셀 파일을 SQLite 데이터베이스로 변환하는 스크립트
"""
import pandas as pd
import sqlite3
import os
from pathlib import Path

def excel_to_sqlite(excel_file_path, db_file_path=None, table_name=None):
    """
    엑셀 파일을 SQLite 데이터베이스로 변환
    
    Args:
        excel_file_path: 엑셀 파일 경로
        db_file_path: 출력할 SQLite DB 파일 경로 (없으면 자동 생성)
        table_name: 테이블 이름 (없으면 엑셀 파일명 사용)
    """
    # 파일 경로 확인
    if not os.path.exists(excel_file_path):
        print(f"❌ 파일을 찾을 수 없습니다: {excel_file_path}")
        return False
    
    # DB 파일 경로 설정 (없으면 엑셀 파일과 같은 이름으로 생성)
    if db_file_path is None:
        excel_path = Path(excel_file_path)
        db_file_path = excel_path.with_suffix('.db')
    
    # 테이블 이름 설정 (없으면 엑셀 파일명 사용)
    if table_name is None:
        excel_path = Path(excel_file_path)
        table_name = excel_path.stem
    
    try:
        print(f"📖 엑셀 파일 읽는 중: {excel_file_path}")
        
        # 엑셀 파일의 모든 시트 읽기
        excel_data = pd.read_excel(excel_file_path, sheet_name=None)
        
        # SQLite 연결
        conn = sqlite3.connect(db_file_path)
        
        # 각 시트를 테이블로 변환
        for sheet_name, df in excel_data.items():
            # 시트 이름을 테이블 이름으로 사용 (시트가 여러 개인 경우)
            if len(excel_data) > 1:
                current_table_name = f"{table_name}_{sheet_name}"
            else:
                current_table_name = table_name
            
            # 테이블 이름 정리 (SQLite 테이블명 규칙에 맞게)
            current_table_name = current_table_name.replace(' ', '_').replace('-', '_')
            
            print(f"  📊 시트 '{sheet_name}' -> 테이블 '{current_table_name}' 변환 중...")
            
            # DataFrame을 SQLite 테이블로 저장
            df.to_sql(current_table_name, conn, if_exists='replace', index=False)
            
            # 행 수 출력
            row_count = len(df)
            col_count = len(df.columns)
            print(f"    ✅ 완료: {row_count}행, {col_count}열")
        
        conn.close()
        print(f"\n✅ 변환 완료!")
        print(f"📁 SQLite DB 파일: {db_file_path}")
        print(f"📋 테이블 목록:")
        
        # 생성된 테이블 목록 확인
        conn = sqlite3.connect(db_file_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        for table in tables:
            cursor.execute(f"SELECT COUNT(*) FROM {table[0]}")
            count = cursor.fetchone()[0]
            print(f"  - {table[0]} ({count}행)")
        conn.close()
        
        return True
        
    except Exception as e:
        print(f"❌ 오류 발생: {str(e)}")
        return False


def main():
    """메인 함수"""
    print("=" * 60)
    print("📊 엑셀 → SQLite 변환기")
    print("=" * 60)
    
    # 현재 디렉토리의 엑셀 파일 목록 확인
    current_dir = Path(".")
    excel_files = list(current_dir.glob("*.xlsx")) + list(current_dir.glob("*.xls"))
    
    if not excel_files:
        print("\n❌ 현재 디렉토리에 엑셀 파일(.xlsx, .xls)이 없습니다.")
        print("\n사용법:")
        print("  1. 이 스크립트를 엑셀 파일이 있는 폴더에 복사하세요")
        print("  2. 엑셀 파일명을 직접 입력하거나")
        print("  3. 아래에 파일 경로를 입력하세요\n")
        
        excel_file_path = input("엑셀 파일 경로를 입력하세요: ").strip().strip('"')
        if not excel_file_path:
            return
    else:
        print(f"\n📁 현재 디렉토리에서 엑셀 파일을 찾았습니다:\n")
        for i, file in enumerate(excel_files, 1):
            print(f"  {i}. {file.name}")
        
        print(f"\n  0. 직접 경로 입력")
        
        choice = input("\n변환할 파일 번호를 선택하세요 (또는 Enter로 첫 번째 파일): ").strip()
        
        if choice == "0":
            excel_file_path = input("엑셀 파일 경로를 입력하세요: ").strip().strip('"')
        elif choice == "":
            excel_file_path = str(excel_files[0])
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(excel_files):
                    excel_file_path = str(excel_files[idx])
                else:
                    print("❌ 잘못된 번호입니다.")
                    return
            except ValueError:
                print("❌ 숫자를 입력해주세요.")
                return
    
    # DB 파일명 입력 (선택사항)
    db_name = input("\nDB 파일명 (Enter로 자동 생성): ").strip()
    if db_name and not db_name.endswith('.db'):
        db_name += '.db'
    
    # 변환 실행
    print()
    excel_to_sqlite(excel_file_path, db_name if db_name else None)


if __name__ == "__main__":
    main()

