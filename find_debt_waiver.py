
import sqlite3
import pandas as pd

def find_debt_waiver():
    conn = sqlite3.connect('accounting.db')
    q = "SELECT * FROM 회계전표 WHERE 헤드텍스트 LIKE '%채무면제%' OR 라인텍스트 LIKE '%채무면제%'"
    df = pd.read_sql(q, conn)
    print("Debt Waiver Transactions:")
    print(df)
    
    q2 = "SELECT 계정코드, SUM(대변금액 - 차변금액) as balance FROM 회계전표 WHERE 계정코드 = '21300' GROUP BY 계정코드"
    df2 = pd.read_sql(q2, conn)
    print("\nDeposit (21300) Balance:")
    print(df2)
    
    conn.close()

if __name__ == "__main__":
    find_debt_waiver()
