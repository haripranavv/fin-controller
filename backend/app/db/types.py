"""Shared portable column types.

BIGINT_PK: use for every surrogate primary key. Plain `BigInteger` on
Postgres (the real target), but SQLite gives a BIGINT-affinity primary key
column ordinary-integer semantics only — it loses the special
"INTEGER PRIMARY KEY" rowid-alias behavior that makes autoincrement work,
which breaks the SQLite-backed unit test suite (NOT NULL constraint on
insert). The per-dialect variant keeps Postgres on BigInteger while giving
SQLite a plain Integer so both actually autoincrement.
"""
from sqlalchemy import BigInteger, Integer

BIGINT_PK = BigInteger().with_variant(Integer(), "sqlite")
