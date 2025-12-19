
import sqlite3
import pandas as pd

def list_db():
    conn = sqlite3.connect('accounting.db')
    cursor = conn.cursor()
    
    print("Tables:")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    for row in cursor.fetchall():
        print(f"- {row[0]}")
        
    print("\nAccounts (First 20):")
    df = pd.read_sql("SELECT * FROM 계정마스터 LIMIT 50", conn)
    for i, r in df.iterrows():
        print(f"{r['계정코드']}: {r['계정명']}")
        
    print("\nRecent Transactions (회계전표):")
    df_t = pd.read_sql("SELECT * FROM 회계전표 ORDER BY 거래일자 DESC LIMIT 5", conn)
    print(df_t)
    
    conn.close()

if __name__ == "__main__":
    list_db()
