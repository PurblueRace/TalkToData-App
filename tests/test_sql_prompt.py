import sqlite3
import unittest
from pathlib import Path

from sql_prompt import (
    build_schema_blueprint,
    build_sql_system_prompt,
    build_sql_user_prompt,
)


ROOT = Path(__file__).resolve().parents[1]


def _live_sqlite_schema() -> dict[str, list[str]]:
    database_uri = (ROOT / "accounting.db").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: [
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for table in tables
        }


class SqlPromptTests(unittest.TestCase):
    def test_blueprint_uses_the_live_schema_and_relationships(self):
        schema = _live_sqlite_schema()
        blueprint = build_schema_blueprint(schema)

        self.assertEqual(
            set(schema),
            {
                "BOM마스터",
                "거래처마스터",
                "계정마스터",
                "부서마스터",
                "사원마스터",
                "원재료마스터",
                "재공품마스터",
                "제품마스터",
                "프로젝트마스터",
                "회계전표",
            },
        )
        for table, columns in schema.items():
            self.assertIn(f"{table}({','.join(columns)})", blueprint)

        self.assertIn("회계전표.계정코드=계정마스터.계정코드", blueprint)
        self.assertIn("사원마스터.부서코드=부서마스터.부서코드", blueprint)
        self.assertIn("부서마스터.상위부서코드=부서마스터.부서코드", blueprint)
        self.assertIn(
            "BOM마스터.대체원재료코드=원재료마스터.원재료코드",
            blueprint,
        )
        self.assertIn(
            "원재료마스터.공급업체코드=거래처마스터.거래처코드",
            blueprint,
        )

    def test_dialect_rules_are_explicit_and_do_not_mix(self):
        postgres_prompt = build_sql_system_prompt(postgres=True)
        sqlite_prompt = build_sql_system_prompt(postgres=False)

        self.assertIn("PostgreSQL", postgres_prompt)
        self.assertIn("CURRENT_DATE", postgres_prompt)
        self.assertIn("ILIKE", postgres_prompt)
        self.assertNotIn("strftime", postgres_prompt)

        self.assertIn("SQLite", sqlite_prompt)
        self.assertIn("strftime", sqlite_prompt)
        self.assertNotIn("CURRENT_DATE", sqlite_prompt)
        self.assertNotIn("ILIKE", sqlite_prompt)

    def test_short_question_gets_schema_semantics_and_data_range(self):
        prompt = build_sql_user_prompt(
            "2026년 월별 매출",
            _live_sqlite_schema(),
            postgres=True,
            data_context={
                "오늘(Asia/Seoul)": "2026-07-24",
                "데이터시작일": "2025-01-01",
                "데이터종료일": "2026-07-31",
            },
        )

        self.assertIn("방언=PostgreSQL", prompt)
        self.assertIn("데이터종료일=2026-07-31", prompt)
        self.assertIn("대분류='수익' AND 중분류='매출액'", prompt)
        self.assertIn("2026년 월별 매출", prompt)
        self.assertNotIn("Q:", prompt)
        self.assertNotIn("A:", prompt)


if __name__ == "__main__":
    unittest.main()
