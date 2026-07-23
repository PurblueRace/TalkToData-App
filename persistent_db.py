"""Persistent database helpers for TalkToData.

The deployed app uses PostgreSQL (Supabase) when ``SUPABASE_DB_URL`` or
``DATABASE_URL`` is configured. Local development keeps using the bundled
SQLite database. A configured remote database is never allowed to silently
fall back to SQLite when the connection fails.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import pandas as pd


class PersistenceError(RuntimeError):
    """Raised when the configured persistent store cannot be used safely."""


_REMOTE_URL_KEYS = ("SUPABASE_DB_URL", "DATABASE_URL")
_META_URL_KEYS = ("SUPABASE_META_DB_URL", "METADATA_DATABASE_URL")
_READ_URL_KEYS = ("SUPABASE_READ_DB_URL", "READ_DATABASE_URL")
_READER_PASSWORD_KEYS = ("SUPABASE_READER_PASSWORD",)
_DATA_SCHEMA = "talktodata"
_READER_ROLE = "talktodata_reader"
_PBKDF2_ITERATIONS = 600_000
_BLOCKED_READ_SCHEMAS = (
    "ttd_meta", "auth", "storage", "vault", "information_schema",
    "pg_catalog", "pg_toast", "pg_temp", "public",
)
_FORBIDDEN_READ_KEYWORDS = {
    "ALTER", "ANALYZE", "ATTACH", "CALL", "COMMENT", "COPY", "CREATE",
    "DELETE", "DETACH", "DO", "DROP", "GRANT", "INSERT", "MERGE",
    "PRAGMA", "REINDEX", "RESET", "REVOKE", "SET", "TRUNCATE", "UPDATE",
    "VACUUM",
}
_FORBIDDEN_READ_FUNCTIONS = {
    "database_to_xml", "dblink", "dblink_exec", "lo_export", "lo_import",
    "pg_cancel_backend", "pg_notify", "pg_read_binary_file", "pg_read_file",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_terminate_backend",
    "query_to_xml", "query_to_xmlschema", "set_config", "table_to_xml",
}


def _streamlit_secret(name: str) -> Optional[str]:
    try:
        import streamlit as st

        value = st.secrets.get(name)
        if value:
            return str(value).strip()

        if name in _REMOTE_URL_KEYS:
            connections = st.secrets.get("connections", {})
            if hasattr(connections, "get"):
                postgres = connections.get("postgresql", {})
                if hasattr(postgres, "get"):
                    nested = postgres.get("url") or postgres.get("connection_string")
                    if nested:
                        return str(nested).strip()
    except ImportError:
        return None
    except Exception as exc:
        exception_name = type(exc).__name__.lower()
        message = str(exc).lower()
        if "secretnotfound" in exception_name or "no secrets found" in message:
            return None
        raise PersistenceError("Streamlit Secrets 설정을 읽을 수 없습니다.") from exc
    return None


def _configured_value(keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
        value = _streamlit_secret(key)
        if value:
            return value
    return None


def get_database_url() -> Optional[str]:
    """Return the configured PostgreSQL URL without logging it."""
    return _configured_value(_REMOTE_URL_KEYS)


def _build_reader_url(database_url: str, reader_password: str) -> str:
    """Build a custom-role URI from the supplied Supabase owner URI."""
    try:
        parsed = urlsplit(database_url)
        owner_username = unquote(parsed.username or "")
        if not parsed.hostname or not owner_username:
            raise ValueError("missing host or username")

        suffix = owner_username.split(".", 1)[1] if "." in owner_username else ""
        reader_username = _READER_ROLE + (f".{suffix}" if suffix else "")
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        netloc = (
            f"{quote(reader_username, safe='')}:{quote(reader_password, safe='')}"
            f"@{host}{port}"
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except (TypeError, ValueError) as exc:
        raise PersistenceError("Supabase 읽기 전용 연결 주소를 만들 수 없습니다.") from exc


def get_read_database_url() -> Optional[str]:
    """Return a physical read-only LOGIN connection, never the owner session."""
    explicit = _configured_value(_READ_URL_KEYS)
    if explicit:
        return explicit
    database_url = get_database_url()
    reader_password = _configured_value(_READER_PASSWORD_KEYS)
    if database_url and reader_password:
        return _build_reader_url(database_url, reader_password)
    return None


def _reader_connection_details() -> Tuple[str, str]:
    reader_url = get_read_database_url()
    if not reader_url:
        raise PersistenceError(
            "SUPABASE_READER_PASSWORD 또는 SUPABASE_READ_DB_URL을 설정해주세요."
        )
    try:
        parsed = urlsplit(reader_url)
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
    except (TypeError, ValueError) as exc:
        raise PersistenceError("Supabase 읽기 전용 연결 주소가 올바르지 않습니다.") from exc
    database_role = username.split(".", 1)[0]
    if database_role != _READER_ROLE or len(password) < 16:
        raise PersistenceError(
            f"읽기 전용 연결은 {_READER_ROLE} 역할과 16자 이상 비밀번호가 필요합니다."
        )
    return reader_url, password


def get_metadata_database_url() -> Optional[str]:
    """Use a separate metadata role when supplied, otherwise the main URL."""
    return _configured_value(_META_URL_KEYS) or get_database_url()


def is_remote_database() -> bool:
    return bool(get_database_url())


def backend_label() -> str:
    return "Supabase PostgreSQL" if is_remote_database() else "로컬 SQLite"


def _sqlalchemy_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


@lru_cache(maxsize=4)
def _create_engine(url: str):
    try:
        from sqlalchemy import create_engine
    except ImportError as exc:
        raise PersistenceError(
            "PostgreSQL 연결 패키지가 없습니다. requirements.txt의 SQLAlchemy와 "
            "psycopg2-binary를 설치해주세요."
        ) from exc

    connect_args: Dict[str, Any] = {"connect_timeout": 10}
    if "supabase" in url.lower():
        connect_args["sslmode"] = "require"

    try:
        return create_engine(
            _sqlalchemy_url(url),
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=5,
            max_overflow=2,
            connect_args=connect_args,
        )
    except Exception as exc:
        raise PersistenceError("PostgreSQL 연결 설정을 만들 수 없습니다.") from exc


def _data_engine():
    url = get_database_url()
    if not url:
        raise PersistenceError("SUPABASE_DB_URL이 설정되지 않았습니다.")
    return _create_engine(url)


def _read_engine():
    url, _ = _reader_connection_details()
    return _create_engine(url)


def _metadata_engine():
    url = get_metadata_database_url()
    if not url:
        raise PersistenceError("SUPABASE_META_DB_URL이 설정되지 않았습니다.")
    return _create_engine(url)


@lru_cache(maxsize=4)
def _initialize_data_store_cached(url: str, reader_url: str, reader_password: str) -> None:
    try:
        from sqlalchemy import text
        from psycopg2 import sql as psycopg2_sql

        engine = _create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('talktodata-reader-setup'))")
            )
            role_exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role_name"),
                {"role_name": _READER_ROLE},
            ).scalar_one_or_none()
            dbapi_connection = conn.connection.driver_connection
            with dbapi_connection.cursor() as cursor:
                role_sql = (
                    "ALTER ROLE {} WITH LOGIN PASSWORD {} NOCREATEDB "
                    "NOCREATEROLE NOINHERIT"
                    if role_exists
                    else
                    "CREATE ROLE {} WITH LOGIN PASSWORD {} NOCREATEDB "
                    "NOCREATEROLE NOINHERIT"
                )
                cursor.execute(
                    psycopg2_sql.SQL(role_sql).format(
                        psycopg2_sql.Identifier(_READER_ROLE),
                        psycopg2_sql.Literal(reader_password),
                    )
                )

                memberships = conn.execute(
                    text(
                        """
                        SELECT parent.rolname
                        FROM pg_auth_members membership
                        JOIN pg_roles parent ON parent.oid=membership.roleid
                        JOIN pg_roles member ON member.oid=membership.member
                        WHERE member.rolname=:role_name
                        """
                    ),
                    {"role_name": _READER_ROLE},
                ).scalars().all()
                for parent_role in memberships:
                    cursor.execute(
                        psycopg2_sql.SQL("REVOKE {} FROM {}").format(
                            psycopg2_sql.Identifier(parent_role),
                            psycopg2_sql.Identifier(_READER_ROLE),
                        )
                    )

            unsafe_attributes = conn.execute(
                text(
                    """
                    SELECT rolsuper OR rolcreaterole OR rolcreatedb
                           OR rolreplication OR rolbypassrls
                    FROM pg_roles
                    WHERE rolname=:role_name
                    """
                ),
                {"role_name": _READER_ROLE},
            ).scalar_one()
            if unsafe_attributes:
                raise PersistenceError(
                    "읽기 전용 역할에 관리자 속성이 있어 연결을 중단했습니다."
                )

            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_DATA_SCHEMA}"))
            conn.execute(text(f"REVOKE ALL ON SCHEMA {_DATA_SCHEMA} FROM PUBLIC"))
            for api_role in ("anon", "authenticated", _READER_ROLE):
                conn.execute(
                    text(
                        f"""
                        DO $revoke$
                        BEGIN
                            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{api_role}') THEN
                                EXECUTE 'REVOKE ALL ON SCHEMA {_DATA_SCHEMA} FROM {api_role}';
                                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA {_DATA_SCHEMA} FROM {api_role}';
                            END IF;
                        END;
                        $revoke$
                        """
                    )
                )
            conn.execute(text(f"GRANT USAGE ON SCHEMA {_DATA_SCHEMA} TO {_READER_ROLE}"))
            conn.execute(
                text(f"REVOKE ALL ON ALL TABLES IN SCHEMA {_DATA_SCHEMA} FROM PUBLIC")
            )
            conn.execute(
                text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {_DATA_SCHEMA} TO {_READER_ROLE}")
            )
            conn.execute(
                text(
                    f"ALTER ROLE {_READER_ROLE} SET default_transaction_read_only = on"
                )
            )
            conn.execute(
                text(f"ALTER ROLE {_READER_ROLE} SET statement_timeout = '30s'")
            )
            conn.execute(
                text(
                    f"ALTER ROLE {_READER_ROLE} SET search_path = {_DATA_SCHEMA}, pg_catalog"
                )
            )
            conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_DATA_SCHEMA} "
                    f"REVOKE ALL ON TABLES FROM PUBLIC"
                )
            )
            conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_DATA_SCHEMA} "
                    f"GRANT SELECT ON TABLES TO {_READER_ROLE}"
                )
            )
    except Exception as exc:
        raise PersistenceError(
            "TalkToData 비공개 스키마와 읽기 전용 역할을 준비하지 못했습니다."
        ) from exc


def initialize_data_store() -> None:
    url = get_database_url()
    if not url:
        return
    reader_url, reader_password = _reader_connection_details()
    _initialize_data_store_cached(url, reader_url, reader_password)


def ping_database(local_db_path: str | Path) -> None:
    """Fail loudly if a configured remote store cannot be reached."""
    if not is_remote_database():
        if not Path(local_db_path).exists():
            raise PersistenceError(f"로컬 데이터베이스를 찾을 수 없습니다: {local_db_path}")
        with closing(sqlite3.connect(str(local_db_path))) as conn:
            conn.execute("SELECT 1")
        return

    try:
        from sqlalchemy import text

        initialize_data_store()
        with _data_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        with _read_engine().connect() as conn:
            with conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                identity = conn.execute(
                    text("SELECT session_user, current_user")
                ).one()
                if identity[0] != _READER_ROLE or identity[1] != _READER_ROLE:
                    raise PersistenceError(
                        "AI 조회 연결이 실제 읽기 전용 계정으로 분리되지 않았습니다."
                    )
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError(
            "Supabase PostgreSQL에 연결하지 못했습니다. 연결 주소와 비밀번호를 확인해주세요."
        ) from exc


def get_connection(local_db_path: str | Path):
    if is_remote_database():
        return _data_engine().connect()
    return sqlite3.connect(str(local_db_path))


def quote_identifier(identifier: str) -> str:
    validate_identifier(identifier)
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_table(table_name: str) -> str:
    return f'{quote_identifier(_DATA_SCHEMA)}.{quote_identifier(table_name)}'


def _secure_table(conn: Any, table_name: str) -> None:
    """Keep app tables private and grant SELECT only to the reader role."""
    from sqlalchemy import text

    qualified = _qualified_table(table_name)
    conn.execute(text(f"REVOKE ALL ON TABLE {qualified} FROM PUBLIC"))
    for api_role in ("anon", "authenticated"):
        role_exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname=:role_name"),
            {"role_name": api_role},
        ).scalar_one_or_none()
        if role_exists:
            conn.execute(
                text(f"REVOKE ALL ON TABLE {qualified} FROM {quote_identifier(api_role)}")
            )
    conn.execute(text(f"GRANT SELECT ON TABLE {qualified} TO {_READER_ROLE}"))


def validate_identifier(identifier: str) -> None:
    if not isinstance(identifier, str) or not identifier.strip():
        raise PersistenceError("테이블/컬럼 이름이 비어 있습니다.")
    if "\x00" in identifier:
        raise PersistenceError("테이블/컬럼 이름에 사용할 수 없는 문자가 있습니다.")
    if len(identifier.encode("utf-8")) > 63:
        raise PersistenceError(
            f"PostgreSQL 이름 제한(UTF-8 63바이트)을 초과했습니다: {identifier}"
        )


def _read_sqlite_tables(local_db_path: str | Path) -> List[str]:
    with closing(sqlite3.connect(str(local_db_path))) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [row[0] for row in rows]


def list_tables(local_db_path: str | Path) -> List[str]:
    if not is_remote_database():
        return _read_sqlite_tables(local_db_path)
    try:
        from sqlalchemy import inspect

        initialize_data_store()
        return sorted(inspect(_data_engine()).get_table_names(schema=_DATA_SCHEMA))
    except Exception as exc:
        raise PersistenceError("PostgreSQL 테이블 목록을 읽지 못했습니다.") from exc


def table_exists(table_name: str, local_db_path: str | Path) -> bool:
    validate_identifier(table_name)
    if not is_remote_database():
        with closing(sqlite3.connect(str(local_db_path))) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
        return row is not None
    try:
        from sqlalchemy import inspect

        initialize_data_store()
        return bool(inspect(_data_engine()).has_table(table_name, schema=_DATA_SCHEMA))
    except Exception as exc:
        raise PersistenceError(f"테이블 존재 여부를 확인하지 못했습니다: {table_name}") from exc


def table_columns(table_name: str, local_db_path: str | Path) -> List[str]:
    validate_identifier(table_name)
    if not is_remote_database():
        with closing(sqlite3.connect(str(local_db_path))) as conn:
            rows = conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
        return [row[1] for row in rows]
    try:
        from sqlalchemy import inspect

        initialize_data_store()
        return [
            item["name"]
            for item in inspect(_data_engine()).get_columns(table_name, schema=_DATA_SCHEMA)
        ]
    except Exception as exc:
        raise PersistenceError(f"테이블 컬럼을 읽지 못했습니다: {table_name}") from exc


def get_schema(local_db_path: str | Path) -> Dict[str, List[str]]:
    return {name: table_columns(name, local_db_path) for name in list_tables(local_db_path)}


def _scrub_sql(sql: str) -> Tuple[str, int]:
    """Remove comments/literal contents and count executable semicolons."""
    output: List[str] = []
    semicolons = 0
    i = 0
    state = "code"
    dollar_tag: Optional[str] = None
    block_depth = 0
    backslash_escapes = False

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if state == "line_comment":
            if ch in "\r\n":
                state = "code"
                output.append(" ")
            i += 1
            continue
        if state == "block_comment":
            if ch == "/" and nxt == "*":
                block_depth += 1
                i += 2
            elif ch == "*" and nxt == "/":
                block_depth -= 1
                i += 2
                if block_depth == 0:
                    state = "code"
                    output.append(" ")
            else:
                i += 1
            continue
        if state == "single_quote":
            if backslash_escapes and ch == "\\" and nxt:
                i += 2
            elif ch == "'" and nxt == "'":
                i += 2
            elif ch == "'":
                state = "code"
                output.append(" ")
                i += 1
            else:
                i += 1
            continue
        if state == "double_quote":
            if ch == '"' and nxt == '"':
                i += 2
            elif ch == '"':
                state = "code"
                output.append(" ")
                i += 1
            else:
                i += 1
            continue
        if state == "dollar_quote":
            assert dollar_tag is not None
            if sql.startswith(dollar_tag, i):
                state = "code"
                output.append(" ")
                i += len(dollar_tag)
            else:
                i += 1
            continue

        if ch == "-" and nxt == "-":
            state = "line_comment"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "block_comment"
            block_depth = 1
            i += 2
            continue
        if ch == "'":
            previous = sql[i - 1] if i else ""
            prefix_boundary = sql[i - 2] if i >= 2 else ""
            before_previous = sql[i - 2] if i >= 2 else ""
            backslash_escapes = (
                previous in {"E", "e"}
                and (i < 2 or not prefix_boundary.isalnum() and prefix_boundary != "_")
            ) or (before_previous in {"U", "u"} and previous == "&")
            state = "single_quote"
            output.append(" ")
            i += 1
            continue
        if ch == '"':
            state = "double_quote"
            output.append(" ")
            i += 1
            continue
        if ch == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if match:
                dollar_tag = match.group(0)
                state = "dollar_quote"
                output.append(" ")
                i += len(dollar_tag)
                continue
        if ch == ";":
            semicolons += 1
        output.append(ch)
        i += 1

    return "".join(output), semicolons


def _scrub_for_quoted_identifiers(sql: str) -> str:
    """Remove comments and string bodies while preserving quoted identifiers."""
    output: List[str] = []
    i = 0
    state = "code"
    dollar_tag: Optional[str] = None
    block_depth = 0
    backslash_escapes = False

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if state == "line_comment":
            if ch in "\r\n":
                state = "code"
                output.append(" ")
            i += 1
            continue
        if state == "block_comment":
            if ch == "/" and nxt == "*":
                block_depth += 1
                i += 2
            elif ch == "*" and nxt == "/":
                block_depth -= 1
                i += 2
                if block_depth == 0:
                    state = "code"
                    output.append(" ")
            else:
                i += 1
            continue
        if state == "single_quote":
            if backslash_escapes and ch == "\\" and nxt:
                i += 2
            elif ch == "'" and nxt == "'":
                i += 2
            elif ch == "'":
                state = "code"
                output.append(" ")
                i += 1
            else:
                i += 1
            continue
        if state == "double_quote":
            output.append(ch)
            if ch == '"' and nxt == '"':
                output.append(nxt)
                i += 2
            elif ch == '"':
                state = "code"
                i += 1
            else:
                i += 1
            continue
        if state == "dollar_quote":
            assert dollar_tag is not None
            if sql.startswith(dollar_tag, i):
                state = "code"
                output.append(" ")
                i += len(dollar_tag)
            else:
                i += 1
            continue

        if ch == "-" and nxt == "-":
            state = "line_comment"
            i += 2
        elif ch == "/" and nxt == "*":
            state = "block_comment"
            block_depth = 1
            i += 2
        elif ch == "'":
            previous = sql[i - 1] if i else ""
            before_previous = sql[i - 2] if i >= 2 else ""
            prefix_boundary = sql[i - 2] if i >= 2 else ""
            backslash_escapes = (
                previous in {"E", "e"}
                and (i < 2 or not prefix_boundary.isalnum() and prefix_boundary != "_")
            ) or (before_previous in {"U", "u"} and previous == "&")
            state = "single_quote"
            output.append(" ")
            i += 1
        elif ch == '"':
            state = "double_quote"
            output.append(ch)
            i += 1
        elif ch == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if match:
                dollar_tag = match.group(0)
                state = "dollar_quote"
                output.append(" ")
                i += len(dollar_tag)
            else:
                output.append(ch)
                i += 1
        else:
            output.append(ch)
            i += 1

    return "".join(output)


def validate_read_only_sql(sql: str) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise PersistenceError("실행할 SQL이 비어 있습니다.")

    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()

    scrubbed, semicolons = _scrub_sql(stripped)
    if semicolons:
        raise PersistenceError("한 번에 하나의 SELECT 쿼리만 실행할 수 있습니다.")

    first = re.match(r"\s*([A-Za-z]+)", scrubbed)
    if not first or first.group(1).upper() not in {"SELECT", "WITH"}:
        raise PersistenceError("조회용 SELECT 또는 WITH 쿼리만 실행할 수 있습니다.")

    tokens = {token.upper() for token in re.findall(r"\b[A-Za-z_]+\b", scrubbed)}
    forbidden = sorted(tokens & _FORBIDDEN_READ_KEYWORDS)
    if forbidden:
        raise PersistenceError(f"조회 쿼리에 허용되지 않는 명령이 있습니다: {forbidden[0]}")

    if re.search(r"\bpg_[a-z0-9_]+\b", scrubbed, flags=re.IGNORECASE):
        raise PersistenceError("PostgreSQL 시스템 객체는 조회할 수 없습니다.")
    if re.search(r'"pg_[^"]+"', sql, flags=re.IGNORECASE):
        raise PersistenceError("PostgreSQL 시스템 객체는 조회할 수 없습니다.")

    functions = {
        name.lower()
        for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", scrubbed)
    }
    quoted_identifier_sql = _scrub_for_quoted_identifiers(sql)
    if re.search(r'(?i)\bU&"', quoted_identifier_sql):
        raise PersistenceError("유니코드 이스케이프 식별자는 조회 쿼리에 사용할 수 없습니다.")
    functions.update(
        name.replace('""', '"').lower()
        for name in re.findall(
            r'"((?:[^"]|"")+)"\s*\(', quoted_identifier_sql
        )
    )
    forbidden_functions = sorted(functions & _FORBIDDEN_READ_FUNCTIONS)
    family_blocked = sorted(
        name
        for name in functions
        if name.startswith("pg_advisory_") or "_to_xml" in name
    )
    if forbidden_functions or family_blocked:
        name = forbidden_functions[0] if forbidden_functions else family_blocked[0]
        raise PersistenceError(f"조회 쿼리에 허용되지 않는 함수가 있습니다: {name}")

    lowered = sql.lower()
    for schema in _BLOCKED_READ_SCHEMAS:
        if re.search(rf"(?<![a-z0-9_])(?:\"?{re.escape(schema)}\"?)\s*\.", lowered):
            raise PersistenceError("앱 내부 또는 시스템 스키마는 조회할 수 없습니다.")


def _convert_qmark_params(sql: str, params: Sequence[Any]) -> Tuple[str, Dict[str, Any]]:
    values = list(params)
    result: List[str] = []
    index = 0
    state = "code"
    i = 0

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if state == "single":
            result.append(ch)
            if ch == "'" and nxt == "'":
                result.append(nxt)
                i += 2
                continue
            if ch == "'":
                state = "code"
            i += 1
            continue
        if state == "double":
            result.append(ch)
            if ch == '"' and nxt == '"':
                result.append(nxt)
                i += 2
                continue
            if ch == '"':
                state = "code"
            i += 1
            continue
        if ch == "'":
            state = "single"
            result.append(ch)
        elif ch == '"':
            state = "double"
            result.append(ch)
        elif ch == "?":
            if index >= len(values):
                raise PersistenceError("SQL 매개변수 수가 맞지 않습니다.")
            result.append(f":p{index}")
            index += 1
        else:
            result.append(ch)
        i += 1

    if index != len(values):
        raise PersistenceError("SQL 매개변수 수가 맞지 않습니다.")
    return "".join(result), {f"p{i}": value for i, value in enumerate(values)}


def read_dataframe(
    sql: str,
    local_db_path: str | Path,
    params: Optional[Sequence[Any] | Mapping[str, Any]] = None,
    connection: Any = None,
) -> pd.DataFrame:
    validate_read_only_sql(sql)

    if not is_remote_database():
        owns_connection = connection is None
        conn = connection or sqlite3.connect(str(local_db_path))
        try:
            return pd.read_sql_query(sql, conn, params=params)
        finally:
            if owns_connection:
                conn.close()

    try:
        from sqlalchemy import text

        sql_params: Mapping[str, Any]
        remote_sql = sql
        if params is None:
            sql_params = {}
        elif isinstance(params, Mapping):
            sql_params = params
        else:
            remote_sql, sql_params = _convert_qmark_params(sql, params)

        if connection is not None:
            raise PersistenceError(
                "PostgreSQL 조회는 지정된 읽기 전용 연결에서만 실행할 수 있습니다."
            )
        conn = _read_engine().connect()
        transaction = conn.begin()
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = 30000"))
            conn.execute(text(f"SET LOCAL search_path TO {_DATA_SCHEMA}, pg_catalog"))
            identity = conn.execute(text("SELECT session_user, current_user")).one()
            if identity[0] != _READER_ROLE or identity[1] != _READER_ROLE:
                raise PersistenceError(
                    "AI 조회 연결이 실제 읽기 전용 계정으로 분리되지 않았습니다."
                )
            frame = pd.read_sql_query(text(remote_sql), conn, params=dict(sql_params))
            transaction.rollback()
            return frame
        except Exception:
            if transaction.is_active:
                transaction.rollback()
            raise
        finally:
            conn.close()
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError(f"PostgreSQL 조회에 실패했습니다: {exc}") from exc


def _is_identifier_column(name: str) -> bool:
    lowered = name.lower()
    return (
        "코드" in name
        or (name.endswith("번호") and name != "순번")
        or "lot" in lowered
        or "전화" in name
        or "이메일" in name
        or "사업자" in name
    )


def _is_date_column(name: str) -> bool:
    lowered = name.lower()
    korean_date_endings = (
        "일자", "날짜", "시작일", "종료일", "예정일", "등록일", "설립일",
        "입사일", "완료일", "계약일", "유통기한", "기간_시작", "기간_종료",
    )
    return name.endswith(korean_date_endings) or lowered.endswith("_date")


def _is_timestamp_column(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith("_at") or lowered.endswith("_timestamp")


def normalize_dataframe_for_postgres(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    for column in normalized.columns:
        validate_identifier(str(column))

    for column in normalized.columns:
        name = str(column)
        series = normalized[column]
        if _is_identifier_column(name):
            normalized[column] = series.map(
                lambda value: None if pd.isna(value) else str(value).strip()
            )
            continue
        if _is_timestamp_column(name):
            non_empty = series[series.notna()].astype(str).str.strip()
            non_empty = non_empty[non_empty != ""]
            if not non_empty.empty:
                converted = pd.to_datetime(series, errors="coerce", utc=True)
                if int(converted.notna().sum()) == int(non_empty.shape[0]):
                    normalized[column] = converted
            continue
        if _is_date_column(name):
            non_empty = series[series.notna()].astype(str).str.strip()
            non_empty = non_empty[non_empty != ""]
            if non_empty.empty:
                normalized[column] = series.map(lambda value: None if pd.isna(value) else value)
                continue
            converted = pd.to_datetime(series, errors="coerce")
            if int(converted.notna().sum()) == int(non_empty.shape[0]):
                normalized[column] = converted.dt.date
    return normalized


def write_dataframe(
    table_name: str,
    frame: pd.DataFrame,
    local_db_path: str | Path,
    if_exists: str = "replace",
) -> None:
    validate_identifier(table_name)
    if if_exists not in {"append", "replace", "fail"}:
        raise PersistenceError(f"지원하지 않는 저장 모드입니다: {if_exists}")

    if not is_remote_database():
        with closing(sqlite3.connect(str(local_db_path))) as conn:
            frame.to_sql(table_name, conn, if_exists=if_exists, index=False)
            conn.commit()
        return

    prepared = normalize_dataframe_for_postgres(frame)
    try:
        initialize_data_store()
        with _data_engine().begin() as conn:
            prepared.to_sql(
                table_name,
                conn,
                schema=_DATA_SCHEMA,
                if_exists=if_exists,
                index=False,
                method="multi",
                chunksize=500,
            )
            _secure_table(conn, table_name)
    except Exception as exc:
        raise PersistenceError(f"'{table_name}' 테이블 저장에 실패했습니다: {exc}") from exc


def replace_dataframes(
    frames: Mapping[str, pd.DataFrame],
    local_db_path: str | Path,
    drop_tables: Optional[Iterable[str]] = None,
    progress: Optional[Callable[[str, int, int, int], None]] = None,
) -> None:
    if not frames:
        raise PersistenceError("저장할 테이블이 없습니다.")
    for table_name in frames:
        validate_identifier(table_name)
    for table_name in drop_tables or []:
        validate_identifier(table_name)

    total = len(frames)
    if not is_remote_database():
        with closing(sqlite3.connect(str(local_db_path))) as conn:
            for table_name in drop_tables or []:
                conn.execute(f"DROP TABLE IF EXISTS {quote_identifier(table_name)}")
            for index, (table_name, frame) in enumerate(frames.items(), start=1):
                frame.to_sql(table_name, conn, if_exists="replace", index=False)
                if progress:
                    progress(table_name, index, total, len(frame))
            conn.commit()
        return

    prepared = {
        table_name: normalize_dataframe_for_postgres(frame)
        for table_name, frame in frames.items()
    }
    try:
        from sqlalchemy import text

        initialize_data_store()
        with _data_engine().begin() as conn:
            for table_name in drop_tables or []:
                conn.execute(text(f"DROP TABLE IF EXISTS {_qualified_table(table_name)}"))
            for index, (table_name, frame) in enumerate(prepared.items(), start=1):
                frame.to_sql(
                    table_name,
                    conn,
                    schema=_DATA_SCHEMA,
                    if_exists="replace",
                    index=False,
                    method="multi",
                    chunksize=500,
                )
                _secure_table(conn, table_name)
                if progress:
                    progress(table_name, index, total, len(frame))
    except Exception as exc:
        raise PersistenceError(f"전체 테이블 저장을 취소했습니다: {exc}") from exc


def drop_table(table_name: str, local_db_path: str | Path) -> None:
    validate_identifier(table_name)
    quoted = quote_identifier(table_name)
    if not is_remote_database():
        with closing(sqlite3.connect(str(local_db_path))) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {quoted}")
            conn.commit()
        return
    try:
        from sqlalchemy import text

        initialize_data_store()
        with _data_engine().begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {_qualified_table(table_name)}"))
    except Exception as exc:
        raise PersistenceError(f"테이블을 삭제하지 못했습니다: {table_name}") from exc


_METADATA_DDL = (
    "CREATE SCHEMA IF NOT EXISTS ttd_meta",
    """
    CREATE TABLE IF NOT EXISTS ttd_meta.app_users (
        username text PRIMARY KEY,
        password_hash text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ttd_meta.saved_table_collections (
        username text PRIMARY KEY REFERENCES ttd_meta.app_users(username) ON DELETE CASCADE,
        payload jsonb NOT NULL DEFAULT '[]'::jsonb,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ttd_meta.app_settings (
        setting_key text PRIMARY KEY,
        value jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
)


@lru_cache(maxsize=4)
def _initialize_metadata_cached(url: str) -> None:
    try:
        from sqlalchemy import text

        with _create_engine(url).begin() as conn:
            for statement in _METADATA_DDL:
                conn.execute(text(statement))
            conn.execute(text("REVOKE ALL ON SCHEMA ttd_meta FROM PUBLIC"))
            conn.execute(text("REVOKE ALL ON ALL TABLES IN SCHEMA ttd_meta FROM PUBLIC"))
            for api_role in ("anon", "authenticated", _READER_ROLE):
                conn.execute(
                    text(
                        f"""
                        DO $revoke$
                        BEGIN
                            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{api_role}') THEN
                                EXECUTE 'REVOKE ALL ON SCHEMA ttd_meta FROM {api_role}';
                                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA ttd_meta FROM {api_role}';
                            END IF;
                        END;
                        $revoke$
                        """
                    )
                )
    except Exception as exc:
        raise PersistenceError("PostgreSQL 앱 저장 영역을 초기화하지 못했습니다.") from exc


def initialize_metadata() -> None:
    if not is_remote_database():
        return
    url = get_metadata_database_url()
    if not url:
        raise PersistenceError("메타데이터 PostgreSQL 연결 주소가 설정되지 않았습니다.")
    _initialize_metadata_cached(url)


def _coerce_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def load_app_setting(setting_key: str) -> Any:
    if not is_remote_database():
        return None
    initialize_metadata()
    try:
        from sqlalchemy import text

        with _metadata_engine().connect() as conn:
            value = conn.execute(
                text("SELECT value FROM ttd_meta.app_settings WHERE setting_key=:key"),
                {"key": setting_key},
            ).scalar_one_or_none()
        return _coerce_json(value)
    except Exception as exc:
        raise PersistenceError("앱 설정을 PostgreSQL에서 읽지 못했습니다.") from exc


def save_app_setting(setting_key: str, value: Any) -> None:
    if not is_remote_database():
        raise PersistenceError("원격 저장소가 설정되지 않았습니다.")
    initialize_metadata()
    try:
        from sqlalchemy import text

        payload = json.dumps(value, ensure_ascii=False)
        with _metadata_engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO ttd_meta.app_settings(setting_key, value, updated_at)
                    VALUES (:key, CAST(:value AS jsonb), now())
                    ON CONFLICT (setting_key) DO UPDATE
                    SET value=EXCLUDED.value, updated_at=now()
                    """
                ),
                {"key": setting_key, "value": payload},
            )
    except Exception as exc:
        raise PersistenceError("앱 설정을 PostgreSQL에 저장하지 못했습니다.") from exc


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt_b64}${digest_b64}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def remote_authenticate_user(username: str, password: str) -> bool:
    initialize_metadata()
    try:
        from sqlalchemy import text

        with _metadata_engine().connect() as conn:
            encoded = conn.execute(
                text("SELECT password_hash FROM ttd_meta.app_users WHERE username=:username"),
                {"username": username},
            ).scalar_one_or_none()
        return bool(encoded and verify_password(password, encoded))
    except Exception as exc:
        raise PersistenceError("사용자 인증 정보를 읽지 못했습니다.") from exc


def remote_create_user(username: str, password: str) -> bool:
    initialize_metadata()
    try:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        try:
            with _metadata_engine().begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO ttd_meta.app_users(username, password_hash) "
                        "VALUES (:username, :password_hash)"
                    ),
                    {"username": username, "password_hash": hash_password(password)},
                )
            return True
        except IntegrityError:
            return False
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError("사용자를 PostgreSQL에 저장하지 못했습니다.") from exc


def save_remote_tables(username: str, payload: List[Dict[str, Any]]) -> None:
    initialize_metadata()
    try:
        from sqlalchemy import text

        value = json.dumps(payload, ensure_ascii=False)
        with _metadata_engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO ttd_meta.saved_table_collections(username, payload, updated_at)
                    VALUES (:username, CAST(:payload AS jsonb), now())
                    ON CONFLICT (username) DO UPDATE
                    SET payload=EXCLUDED.payload, updated_at=now()
                    """
                ),
                {"username": username, "payload": value},
            )
    except Exception as exc:
        raise PersistenceError("저장된 표를 PostgreSQL에 보관하지 못했습니다.") from exc


def load_remote_tables(username: str) -> List[Dict[str, Any]]:
    initialize_metadata()
    try:
        from sqlalchemy import text

        with _metadata_engine().connect() as conn:
            value = conn.execute(
                text(
                    "SELECT payload FROM ttd_meta.saved_table_collections "
                    "WHERE username=:username"
                ),
                {"username": username},
            ).scalar_one_or_none()
        payload = _coerce_json(value, [])
        return payload if isinstance(payload, list) else []
    except Exception as exc:
        raise PersistenceError("저장된 표를 PostgreSQL에서 읽지 못했습니다.") from exc


def bootstrap_metadata_from_local(config_path: str | Path, users_dir: str | Path) -> bool:
    """One-time import of config/users/saved tables into the metadata schema."""
    if not is_remote_database():
        return False
    initialize_metadata()
    config_path = Path(config_path)
    users_dir = Path(users_dir)
    try:
        from sqlalchemy import text

        with _metadata_engine().begin() as conn:
            if config_path.exists():
                with config_path.open("r", encoding="utf-8") as file:
                    config = json.load(file)
                conn.execute(
                    text(
                        """
                        INSERT INTO ttd_meta.app_settings(setting_key, value, updated_at)
                        VALUES ('config', CAST(:value AS jsonb), now())
                        ON CONFLICT (setting_key) DO NOTHING
                        """
                    ),
                    {"value": json.dumps(config, ensure_ascii=False)},
                )

            users: Dict[str, Any] = {}
            users_file = users_dir / "users_db.json"
            if users_file.exists():
                with users_file.open("r", encoding="utf-8") as file:
                    loaded = json.load(file)
                    if isinstance(loaded, dict):
                        users = loaded

            existing_users = {
                row[0]
                for row in conn.execute(
                    text("SELECT username FROM ttd_meta.app_users")
                ).fetchall()
            }

            for username, record in users.items():
                if not isinstance(record, dict):
                    continue
                if username not in existing_users:
                    encoded = record.get("password_hash")
                    if not encoded and record.get("password") is not None:
                        encoded = hash_password(str(record["password"]))
                    if not encoded:
                        continue
                    conn.execute(
                        text(
                            """
                            INSERT INTO ttd_meta.app_users(username, password_hash)
                            VALUES (:username, :password_hash)
                            ON CONFLICT (username) DO NOTHING
                            """
                        ),
                        {"username": username, "password_hash": encoded},
                    )
                    existing_users.add(username)

                tables_file = users_dir / username / "saved_tables.json"
                if tables_file.exists():
                    with tables_file.open("r", encoding="utf-8") as file:
                        saved = json.load(file)
                    if isinstance(saved, list):
                        conn.execute(
                            text(
                                """
                                INSERT INTO ttd_meta.saved_table_collections(username, payload)
                                VALUES (:username, CAST(:payload AS jsonb))
                                ON CONFLICT (username) DO NOTHING
                                """
                            ),
                            {"username": username, "payload": json.dumps(saved, ensure_ascii=False)},
                        )

            marker_payload = json.dumps(
                {"completed_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False
            )
            conn.execute(
                text(
                    """
                    INSERT INTO ttd_meta.app_settings(setting_key, value, updated_at)
                    VALUES ('local_bootstrap_v1', CAST(:value AS jsonb), now())
                    ON CONFLICT (setting_key) DO NOTHING
                    """
                ),
                {"value": marker_payload},
            )
        return True
    except Exception as exc:
        raise PersistenceError("기존 사용자/설정을 PostgreSQL로 옮기지 못했습니다.") from exc


def _validate_staged_frame(conn: Any, table_name: str, frame: pd.DataFrame) -> None:
    """Validate row/null/distinct/numeric aggregates before an atomic table swap."""
    from sqlalchemy import inspect, text

    actual_columns = [
        item["name"]
        for item in inspect(conn).get_columns(table_name, schema=_DATA_SCHEMA)
    ]
    expected_columns = [str(column) for column in frame.columns]
    if actual_columns != expected_columns:
        raise PersistenceError(
            f"'{table_name}' 컬럼 검증 실패: {actual_columns} != {expected_columns}"
        )

    expressions = ["COUNT(*) AS row_count"]
    checks: List[Tuple[str, str, Any]] = []
    for index, column in enumerate(frame.columns):
        quoted = quote_identifier(str(column))
        nonnull_alias = f"nonnull_{index}"
        expressions.append(f"COUNT({quoted}) AS {nonnull_alias}")
        checks.append((nonnull_alias, "exact", int(frame[column].notna().sum())))

        if pd.api.types.is_numeric_dtype(frame[column]) and not pd.api.types.is_bool_dtype(frame[column]):
            sum_alias = f"sum_{index}"
            expressions.append(f"SUM({quoted}) AS {sum_alias}")
            expected_sum = frame[column].dropna().sum()
            checks.append((sum_alias, "numeric", expected_sum))

        if _is_identifier_column(str(column)):
            distinct_alias = f"distinct_{index}"
            expressions.append(f"COUNT(DISTINCT {quoted}) AS {distinct_alias}")
            checks.append((distinct_alias, "exact", int(frame[column].nunique(dropna=True))))

    result = conn.execute(
        text(
            f"SELECT {', '.join(expressions)} "
            f"FROM {_qualified_table(table_name)}"
        )
    ).mappings().one()

    if int(result["row_count"]) != len(frame):
        raise PersistenceError(
            f"'{table_name}' 행 수 검증 실패: {result['row_count']} != {len(frame)}"
        )

    for alias, check_type, expected in checks:
        actual = result[alias]
        if check_type == "exact":
            if int(actual or 0) != int(expected):
                raise PersistenceError(f"'{table_name}' 데이터 검증 실패: {alias}")
            continue

        if pd.isna(expected):
            if actual is not None:
                raise PersistenceError(f"'{table_name}' 합계 검증 실패: {alias}")
            continue
        if not math.isclose(float(actual or 0), float(expected), rel_tol=1e-9, abs_tol=1e-6):
            raise PersistenceError(f"'{table_name}' 합계 검증 실패: {alias}")


def migrate_sqlite_database(
    local_db_path: str | Path,
    allow_nonempty: bool = False,
    progress: Optional[Callable[[str, int, int, int], None]] = None,
) -> Dict[str, int]:
    """Copy all SQLite user tables to PostgreSQL in one transaction."""
    if not is_remote_database():
        raise PersistenceError("SUPABASE_DB_URL을 먼저 설정해주세요.")
    source = Path(local_db_path)
    if not source.exists():
        raise PersistenceError(f"원본 SQLite 파일을 찾을 수 없습니다: {source}")

    source_tables = _read_sqlite_tables(source)
    if not source_tables:
        raise PersistenceError("원본 SQLite에 옮길 테이블이 없습니다.")

    existing = list_tables(source)
    existing_source_tables = [name for name in source_tables if name in existing]
    if existing_source_tables and not allow_nonempty:
        raise PersistenceError(
            "PostgreSQL에 같은 이름의 데이터 테이블이 이미 있어 덮어쓰기를 중단했습니다."
        )

    frames: Dict[str, pd.DataFrame] = {}
    sqlite_uri = source.resolve().as_uri() + "?mode=ro"
    initialize_data_store()
    try:
        from sqlalchemy import text

        migration_id = uuid.uuid4().hex[:10]
        staged_names = {
            table_name: f"__ttd_{migration_id}_{index}"
            for index, table_name in enumerate(source_tables, start=1)
        }

        # Stage, validate, and swap all tables in one PostgreSQL transaction.
        with _data_engine().begin() as conn:
            # Exclude remote uploads before taking the source snapshot. This keeps
            # a replacement from silently discarding a concurrent remote commit.
            for table_name in existing_source_tables:
                conn.execute(
                    text(
                        f"LOCK TABLE {_qualified_table(table_name)} "
                        "IN ACCESS EXCLUSIVE MODE"
                    )
                )

            # Keep every source table on one SQLite snapshot while the remote
            # targets remain locked for the entire replacement.
            with closing(sqlite3.connect(sqlite_uri, uri=True)) as source_conn:
                source_conn.execute("BEGIN")
                snapshot_tables = [
                    row[0]
                    for row in source_conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                ]
                if snapshot_tables != source_tables:
                    raise PersistenceError(
                        "이전 시작 중 SQLite 테이블 구성이 변경되어 안전하게 중단했습니다."
                    )
                for table_name in source_tables:
                    raw_frame = pd.read_sql_query(
                        f"SELECT * FROM {quote_identifier(table_name)}", source_conn
                    )
                    frames[table_name] = normalize_dataframe_for_postgres(raw_frame)
                source_conn.rollback()

            total = len(frames)
            for index, (table_name, frame) in enumerate(frames.items(), start=1):
                staged_name = staged_names[table_name]
                frame.to_sql(
                    staged_name,
                    conn,
                    schema=_DATA_SCHEMA,
                    if_exists="fail",
                    index=False,
                    method="multi",
                    chunksize=500,
                )
                _validate_staged_frame(conn, staged_name, frame)
                if progress:
                    progress(table_name, index, total, len(frame))

            for table_name in source_tables:
                if table_name in existing_source_tables:
                    conn.execute(text(f"DROP TABLE {_qualified_table(table_name)}"))
                staged_name = staged_names[table_name]
                conn.execute(
                    text(
                        f"ALTER TABLE {_qualified_table(staged_name)} "
                        f"RENAME TO {quote_identifier(table_name)}"
                    )
                )
                _secure_table(conn, table_name)
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError(
            "PostgreSQL 적재 또는 검증에 실패해 기존 데이터를 그대로 유지했습니다."
        ) from exc

    return {name: len(frame) for name, frame in frames.items()}
