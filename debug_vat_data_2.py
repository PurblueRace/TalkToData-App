import sqlite3
import pandas as pd
import os

def check_more_details():
    if not os.path.exists("accounting.db"):
        return

    conn = sqlite3.connect("accounting.db")
    
    with open("debug_output_2.txt", "w", encoding="utf-8") as f:
        # Check for Card/Cash in Proof Type
        f.write("=== 1. Proof Types (증빙유형) searching for Card/Cash ===\n")
        try:
            df = pd.read_sql_query("SELECT DISTINCT 증빙유형 FROM 회계전표 WHERE 증빙유형 LIKE '%카드%' OR 증빙유형 LIKE '%현금%' OR 증빙유형 LIKE '%신용%'", conn)
            if df.empty:
                f.write("No 'Card' or 'Cash' found in 증빙유형.\n")
            else:
                f.write(df.to_string() + "\n")
        except Exception as e:
            f.write(str(e) + "\n")

        # Check '수익' accounts to see if we satisfy exclusion logic
        f.write("\n=== 2. 'Revenue/Income' Accounts (checking for Interest/Dividend) ===\n")
        try:
            df = pd.read_sql_query("SELECT DISTINCT 계정명 FROM 회계전표 WHERE 계정명 LIKE '%수익%'", conn)
            f.write(df.to_string() + "\n")
        except Exception as e:
            f.write(str(e) + "\n")

    conn.close()

if __name__ == "__main__":
    check_more_details()
