
import sqlite3
import pandas as pd

def find_gains():
    conn = sqlite3.connect('accounting.db')
    q = "SELECT 계정코드, 계정명 FROM 계정마스터 WHERE 계정명 LIKE '%면제%' OR 계정명 LIKE '%수증%' OR 계정명 LIKE '%이익%'"
    df = pd.read_sql(q, conn)
    print(df)
    conn.close()

if __name__ == "__main__":
    find_gains()
