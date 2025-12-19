
import sqlite3
import pandas as pd

def get_final_data():
    conn = sqlite3.connect('accounting.db')
    
    # 1. Income (Interest)
    interest_bal = pd.read_sql("SELECT SUM(대변금액-차변금액) FROM 회계전표 WHERE 계정코드='70100'", conn).iloc[0,0] or 0
    
    # 2. Deposit
    deposit_bal = pd.read_sql("SELECT SUM(대변금액-차변금액) FROM 회계전표 WHERE 계정코드='21300'", conn).iloc[0,0] or 0
    
    # 3. Entertainment
    ent_bal = pd.read_sql("SELECT SUM(차변금액-대변금액) FROM 회계전표 WHERE 계정코드='60600'", conn).iloc[0,0] or 0
    
    # 4. Debt Waiver (Search headers/lines)
    waiver_q = "SELECT * FROM 회계전표 WHERE 헤드텍스트 LIKE '%채무%' OR 라인텍스트 LIKE '%채무%'"
    waiver_df = pd.read_sql(waiver_q, conn)
    
    print(f"INTEREST_BAL: {interest_bal}")
    print(f"DEPOSIT_BAL: {deposit_bal}")
    print(f"ENT_BAL: {ent_bal}")
    print("WAIVER_DATA:")
    print(waiver_df)
    
    conn.close()

if __name__ == "__main__":
    get_final_data()
