import sqlite3
import pandas as pd
import os

def check_db():
    if not os.path.exists("accounting.db"):
        print("accounting.db not found")
        return

    conn = sqlite3.connect("accounting.db")
    cursor = conn.cursor()

    print("=== 1. Schema of 회계전표 ===")
    try:
        cursor.execute("PRAGMA table_info(회계전표)")
        columns = cursor.fetchall()
        for col in columns:
            print(col)
    except Exception as e:
        print(f"Error reading schema: {e}")
        conn.close()
        return

    with open("debug_output.txt", "w", encoding="utf-8") as f:
        f.write("=== 1. Schema of 회계전표 ===\n")
        try:
            cursor.execute("PRAGMA table_info(회계전표)")
            columns = cursor.fetchall()
            for col in columns:
                f.write(str(col) + "\n")
        except Exception as e:
            f.write(f"Error reading schema: {e}\n")
            conn.close()
            return

        f.write("\n=== 2. Account Names (계정명) Sample - Sales ===\n")
        try:
            df = pd.read_sql_query("SELECT DISTINCT 계정명 FROM 회계전표 WHERE 계정명 LIKE '%매출%' OR 계정명 LIKE '%수익%' LIMIT 20", conn)
            f.write(df.to_string() + "\n")
        except Exception as e:
            f.write(str(e) + "\n")
        
        f.write("\n=== 3. Account Names (계정명) Sample - VAT Assets ===\n")
        try:
            df = pd.read_sql_query("SELECT DISTINCT 계정명 FROM 회계전표 WHERE 계정명 LIKE '%부가세%' LIMIT 20", conn)
            f.write(df.to_string() + "\n")
        except Exception as e:
            f.write(str(e) + "\n")

        f.write("\n=== 4. Proof Types (증빙유형) Sample ===\n")
        try:
            # Check if column exists first from schema
            col_names = [c[1] for c in columns]
            if '증빙유형' in col_names:
                df = pd.read_sql_query("SELECT DISTINCT 증빙유형 FROM 회계전표 LIMIT 20", conn)
                f.write(df.to_string() + "\n")
            else:
                f.write("'증빙유형' column not found!\n")
        except Exception as e:
            f.write(str(e) + "\n")

        f.write("\n=== 5. Account Names (계정명) Sample - Entertainment (접대비) ===\n")
        try:
            df = pd.read_sql_query("SELECT DISTINCT 계정명 FROM 회계전표 WHERE 계정명 LIKE '%접대비%' LIMIT 20", conn)
            f.write(df.to_string() + "\n")
        except Exception as e:
            f.write(str(e) + "\n")

        f.write("\n=== 6. Line Text (라인텍스트/적요) Sample for Non-deductible ===\n")
        try:
            if '라인텍스트' in col_names:
                df = pd.read_sql_query("SELECT DISTINCT 라인텍스트 FROM 회계전표 WHERE 라인텍스트 LIKE '%개인%' OR 라인텍스트 LIKE '%비용%' LIMIT 20", conn)
                f.write(df.to_string() + "\n")
            elif '적요' in col_names:
                f.write("'라인텍스트' not found, found '적요' instead.\n")
                df = pd.read_sql_query("SELECT DISTINCT 적요 FROM 회계전표 WHERE 적요 LIKE '%개인%' OR 적요 LIKE '%비용%' LIMIT 20", conn)
                f.write(df.to_string() + "\n")
            else:
                f.write("Neither '라인텍스트' nor '적요' column found.\n")
        except Exception as e:
            f.write(str(e) + "\n")

    conn.close()

if __name__ == "__main__":
    check_db()
