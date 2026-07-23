"""One-time migration from the bundled SQLite database to Supabase Postgres."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from persistent_db import (
    PersistenceError,
    bootstrap_metadata_from_local,
    initialize_metadata,
    is_remote_database,
    migrate_sqlite_database,
    ping_database,
    quote_identifier,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / "accounting.db"


def sqlite_manifest(source: Path) -> dict[str, int]:
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        table_names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            table_name: int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table_name)}"
                ).fetchone()[0]
            )
            for table_name in table_names
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TalkToData SQLite 데이터를 Supabase PostgreSQL로 옮깁니다."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="원본 DB만 점검하고 PostgreSQL에는 쓰지 않습니다.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="PostgreSQL에 같은 이름의 테이블이 있으면 트랜잭션으로 교체합니다.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    if not source.exists():
        print(f"원본 DB를 찾을 수 없습니다: {source}", file=sys.stderr)
        return 1

    manifest = sqlite_manifest(source)
    print(f"원본: {source}")
    print(f"SHA256: {sha256_file(source)}")
    print(f"테이블: {len(manifest)}개 / 전체 행: {sum(manifest.values()):,}건")
    for table_name, row_count in manifest.items():
        print(f"  - {table_name}: {row_count:,}건")

    if args.dry_run:
        print("점검 완료: PostgreSQL에는 아무것도 쓰지 않았습니다.")
        return 0
    if not is_remote_database():
        print(
            "SUPABASE_DB_URL을 Streamlit secrets 또는 환경변수에 먼저 설정해주세요.",
            file=sys.stderr,
        )
        return 1

    try:
        ping_database(source)
        initialize_metadata()
        # Metadata is idempotent and must succeed before any data table is swapped.
        bootstrap_metadata_from_local(ROOT / "config.json", ROOT / "users")

        def progress(table_name: str, index: int, total: int, row_count: int) -> None:
            print(f"[{index}/{total}] {table_name}: {row_count:,}건 적재")

        migrate_sqlite_database(
            source,
            allow_nonempty=args.replace_existing,
            progress=progress,
        )
        print("완료: 데이터 검증과 원자적 교체, 설정·사용자·저장 표 이전을 마쳤습니다.")
        return 0
    except PersistenceError as exc:
        print(f"마이그레이션 실패: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
