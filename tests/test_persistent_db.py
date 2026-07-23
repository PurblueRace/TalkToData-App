import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pandas as pd

from persistent_db import (
    PersistenceError,
    get_read_database_url,
    hash_password,
    normalize_dataframe_for_postgres,
    read_dataframe,
    table_columns,
    table_exists,
    validate_read_only_sql,
    verify_password,
    write_dataframe,
)


class PersistentDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.remote_keys = (
            "SUPABASE_DB_URL",
            "DATABASE_URL",
            "SUPABASE_READ_DB_URL",
            "READ_DATABASE_URL",
            "SUPABASE_READER_PASSWORD",
        )
        self.previous_remote_values = {
            key: os.environ.pop(key, None) for key in self.remote_keys
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute('CREATE TABLE "샘플" ("코드" TEXT, "금액" INTEGER)')
            conn.execute('INSERT INTO "샘플" VALUES (?, ?)', ("A01", 100))
            conn.commit()

    def tearDown(self):
        self.temp_dir.cleanup()
        for key in self.remote_keys:
            os.environ.pop(key, None)
            if self.previous_remote_values[key] is not None:
                os.environ[key] = self.previous_remote_values[key]

    def test_local_read_with_qmark_params(self):
        frame = read_dataframe(
            'SELECT * FROM "샘플" WHERE "코드"=?',
            self.db_path,
            params=("A01",),
        )
        self.assertEqual(frame.iloc[0]["금액"], 100)

    def test_append_preserves_existing_rows(self):
        write_dataframe(
            "샘플",
            pd.DataFrame({"코드": ["A02"], "금액": [200]}),
            self.db_path,
            if_exists="append",
        )
        result = read_dataframe('SELECT COUNT(*) AS count FROM "샘플"', self.db_path)
        self.assertEqual(int(result.iloc[0]["count"]), 2)
        self.assertTrue(table_exists("샘플", self.db_path))
        self.assertEqual(table_columns("샘플", self.db_path), ["코드", "금액"])

    def test_read_only_guard_rejects_mutation_and_internal_schema(self):
        invalid = (
            "WITH changed AS (DELETE FROM x RETURNING *) SELECT * FROM changed",
            "SELECT 1; SELECT 2",
            "SELECT * FROM ttd_meta.app_users",
            'SELECT * FROM "pg_roles"',
            "SELECT pg_sleep(1)",
            "SELECT query_to_xml('SELECT 1', true, true, '')",
            'SELECT "query_to_xml"(\'SELECT 1\', true, true, \'\')',
            'SELECT "set_config"(\'statement_timeout\', \'0\', true)',
            "SELECT schema_to_xml('talktodata', true, true, '')",
            'SELECT "database_to_xmlschema"(true, true, \'\')',
            'SELECT "set_config"/**/(\'statement_timeout\', \'0\', true)',
            'SELECT "query_to_xml"/* nested /* comment */ still */(\'SELECT 1\', true, true, \'\')',
            'SELECT "schema_to_xml" -- comment\n(\'talktodata\', true, true, \'\')',
        )
        for sql in invalid:
            with self.subTest(sql=sql), self.assertRaises(PersistenceError):
                validate_read_only_sql(sql)

    def test_passwords_are_salted_and_verified(self):
        first = hash_password("correct horse battery staple")
        second = hash_password("correct horse battery staple")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("wrong", first))

    def test_postgres_normalization_keeps_timestamps_and_codes(self):
        frame = pd.DataFrame(
            {
                "품목코드": [1001, 1002],
                "거래일자": ["2026-07-01", "2026-07-02"],
                "created_at": ["2026-07-01 09:30:00+09:00", "2026-07-02 10:00:00+09:00"],
            }
        )
        normalized = normalize_dataframe_for_postgres(frame)
        self.assertEqual(normalized["품목코드"].tolist(), ["1001", "1002"])
        self.assertEqual(str(normalized["거래일자"].iloc[0]), "2026-07-01")
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(normalized["created_at"]))

    def test_reader_url_uses_a_separate_physical_login(self):
        os.environ["SUPABASE_DB_URL"] = (
            "postgresql://postgres.projectref:owner-password@pooler.example.com:5432/postgres"
        )
        os.environ["SUPABASE_READER_PASSWORD"] = "reader pass/with:symbols"
        reader_url = get_read_database_url()
        parsed = urlsplit(reader_url)
        self.assertEqual(unquote(parsed.username or ""), "talktodata_reader.projectref")
        self.assertEqual(unquote(parsed.password or ""), "reader pass/with:symbols")
        self.assertNotIn("owner-password", reader_url)


if __name__ == "__main__":
    unittest.main()
