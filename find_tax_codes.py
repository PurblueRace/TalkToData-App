
import sqlite3
import pandas as pd

def find_codes():
    conn = sqlite3.connect('accounting.db')
    keywords = ['임대보증금', '채무면제', '이자수익', '기부금', '접대비', '세금과공과']
    
    for k in keywords:
        q = f"SELECT 계정코드, 계정명 FROM 계정마스터 WHERE 계정명 LIKE '%{k}%'"
        df = pd.read_sql(q, conn)
        if not df.empty:
            print(f"Match for {k}:")
            print(df)
            print("-" * 10)
    
    conn.close()

if __name__ == "__main__":
    find_codes()
