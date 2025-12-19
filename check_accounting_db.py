
import sqlite3
import pandas as pd
import os

def find_tax_data():
    db_path = 'accounting.db'
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found.")
        return
        
    conn = sqlite3.connect(db_path)
    
    # Check for accounts
    print("Checking for accounts related to tax adjustments:")
    keywords = ['채무면제', '임대보증금', '이자수익', '기부금', '접대비', '세금과공과']
    
    accounts_q = "SELECT 계정코드, 계정명 FROM 계정마스터 WHERE " + " OR ".join([f"계정명 LIKE '%{k}%'" for k in keywords])
    try:
        df_acc = pd.read_sql(accounts_q, conn)
        print("Relevant Accounts:")
        print(df_acc)
    except Exception as e:
        print(f"Error: {e}")
        
    # Check for entries
    print("\nChecking for transactions in '회계전표':")
    try:
        count_q = "SELECT COUNT(*) FROM 회계전표"
        count = pd.read_sql(count_q, conn).iloc[0,0]
        print(f"Total Transactions: {count}")
        
        sample_q = "SELECT * FROM 회계전표 LIMIT 5"
        print(pd.read_sql(sample_q, conn))
    except Exception as e:
        print(f"Error: {e}")
        
    conn.close()

if __name__ == "__main__":
    find_tax_data()
