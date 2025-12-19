
import sqlite3
import pandas as pd

def inspect():
    conn = sqlite3.connect("finance_data.db")
    
    # 1. Check Tables
    print("Tables:")
    tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", conn)
    print(tables)
    print("-" * 20)

    # 2. Search for Keywords in Accounts or Entries
    keywords = ['보증금', '임대', '채무면제', '이자']
    
    # Check if 'accounts' table exists and has 'account_name'
    try:
        print("\nChecking 'accounts' table for keywords:")
        query = "SELECT * FROM accounts WHERE " + " OR ".join([f"account_name LIKE '%{k}%'" for k in keywords])
        df = pd.read_sql(query, conn)
        print(df)
    except Exception as e:
        print(f"Error querying accounts: {e}")

    # Check 'journal_entries' (assuming headers/lines text might have info if no accounts table matches)
    try:
        print("\nChecking 'journal_entries' (limit 10):")
        # Just getting schema or sample to know columns
        df_sample = pd.read_sql("SELECT * FROM journal_entries LIMIT 1", conn)
        print(df_sample.columns)
    except Exception as e:
        print(f"Error querying journal_entries: {e}")

    conn.close()

if __name__ == "__main__":
    inspect()
