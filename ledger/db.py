"""Connection and migrations.

Migrations are plain numbered .sql files. Each one runs in its own transaction and is recorded
with its SHA-256, so a file that was edited after it ran is caught instead of silently ignored.
"""
import hashlib
import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"
DEFAULT_URL = "postgresql://postgres:postgres@localhost:5432/leaseledger"


class MigrationError(Exception):
    pass


def url():
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def connect(dsn=None, autocommit=True):
    return psycopg.connect(dsn or url(), autocommit=autocommit)


def migrate(conn, folder=MIGRATIONS):
    """Applies pending migrations in order. Returns [(file, status)]."""
    conn.execute("""create table if not exists schema_migrations (
                        version    text primary key,
                        filename   text not null,
                        sha256     text not null,
                        applied_at timestamptz not null default now(),
                        applied_by text not null default current_user)""")
    applied = {v: (f, h) for v, f, h in conn.execute("select version, filename, sha256 from schema_migrations")}
    done = []
    for path in sorted(Path(folder).glob("*.sql")):
        version = path.name.split("_", 1)[0]
        sql = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(sql.encode()).hexdigest()
        if version in applied:
            if applied[version][1] != digest:
                raise MigrationError(f"{path.name} was changed after it was applied "
                                     f"(recorded {applied[version][1][:12]}, now {digest[:12]}). "
                                     "Write a new migration instead of editing an old one.")
            done.append((path.name, "already applied"))
            continue
        with conn.transaction():
            conn.execute(sql)
            conn.execute("insert into schema_migrations (version, filename, sha256) values (%s, %s, %s)",
                         [version, path.name, digest])
        done.append((path.name, "applied"))
    return done


def table(rows, headers, right=()):
    """Plain text table, numbers right-aligned."""
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float) or type(v).__name__ == "Decimal":
            return f"{v:,.2f}"
        return str(v)
    data = [[cell(v) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in data]) for i, h in enumerate(headers)]
    def line(vals):
        return "  ".join(v.rjust(w) if (i in right) else v.ljust(w) for i, (v, w) in enumerate(zip(vals, widths))).rstrip()
    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in data]
    return "\n".join(out)
