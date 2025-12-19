
import sqlite3
import pandas as pd

def check_real_data():
    conn = sqlite3.connect('accounting.db')
    
    # 1. Non-deductible Taxes/Fines
    q = "SELECT 계정명, 라인텍스트, 차변금액 FROM 회계전표 WHERE 라인텍스트 LIKE '%벌금%' OR 라인텍스트 LIKE '%과태료%' OR 라인텍스트 LIKE '%가산세%' OR 라인텍스트 LIKE '%위약금%'"
    fines = pd.read_sql(q, conn)
    print("Potential Non-deductible Items:")
    print(fines)
    
    # 2. Debt Waiver
    q2 = "SELECT 계정명, 라인텍스트, 대변금액 FROM 회계전표 WHERE 라인텍스트 LIKE '%채무%' OR 라인텍스트 LIKE '%면제%'"
    waiver = pd.read_sql(q2, conn)
    print("\nDebt Waiver Items:")
    print(waiver)
    
    conn.close()

if __name__ == "__main__":
    check_real_data()
