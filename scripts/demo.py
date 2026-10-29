"""Runs the whole walkthrough from the README against a fresh database (leaseledger_demo):
migrations, the sample portfolio, reports, and every control trying to stop something.

    python scripts/demo.py
"""
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ledger import db  # noqa: E402

NAME = "leaseledger_demo"


def dsn_for(name):
    p = conninfo_to_dict(db.url())
    p["dbname"] = name
    return make_conninfo(**p)


DSN = dsn_for(NAME)
ENV = {**os.environ, "DATABASE_URL": DSN}


def title(t):
    print(f"\n## {t}\n")


def cli(*args):
    print("$ python -m ledger " + " ".join(f'"{a}"' if " " in a else a for a in args))
    r = subprocess.run([sys.executable, "-m", "ledger", *args], cwd=ROOT, env=ENV, capture_output=True, text=True)
    print((r.stdout + r.stderr).rstrip())
    print()


def sql(conn, text, params=None, show=True):
    print("leaseledger=# " + " ".join(text.split()))
    try:
        with conn.transaction():
            cur = conn.execute(text, params)
            rows = cur.fetchall() if cur.description else []
        if show and rows:
            right = [i for i, v in enumerate(rows[0]) if isinstance(v, (int, float)) or type(v).__name__ == "Decimal"]
            print(db.table(rows, [d.name for d in cur.description], right=right))
        elif not rows:
            print(cur.statusmessage)
    except psycopg.Error as e:
        print(f"ERROR:  {e.diag.message_primary}")
    print()


def main():
    with psycopg.connect(dsn_for("postgres"), autocommit=True) as admin:
        admin.execute(f"drop database if exists {NAME} with (force)")
        admin.execute(f"create database {NAME}")

    title("Set up")
    cli("migrate")
    cli("seed")

    title("Month-end reports")
    cli("tb", "--from", "2026-10-01", "--to", "2026-10-31")
    cli("aging", "--as-of", "2026-10-29")
    cli("reconcile", "--as-of", "2026-10-29")
    cli("rentroll")

    title("A bounced cheque")
    with psycopg.connect(DSN, autocommit=True) as c:
        pid, lid = c.execute("select payment_id, lease_id from payments where reversed_on is not null").fetchone()
    cli("statement", "--lease", lid, "--last", "6")
    cli("reverse", "--payment", str(pid), "--on", "2026-10-29", "--reason", "trying again")

    title("A payment that covers more than what's owed")
    cli("pay", "--lease", "L-0043", "--amount", "4000", "--on", "2026-10-29", "--ref", "EFT-9001")
    cli("statement", "--lease", "L-0043", "--last", "4")
    cli("pay", "--lease", "L-0043", "--amount", "4000", "--on", "2026-10-29", "--ref", "EFT-9001")

    title("Billing November twice")
    cli("bill", "--period", "2026-11-01")
    cli("bill", "--period", "2026-11-01")

    title("Things the database refuses")
    with psycopg.connect(DSN, autocommit=True) as c:
        sql(c, """insert into leases values ('L-0999', (select unit_id from leases where lease_id = 'L-0001'),
                  1, '2026-12-01', '2027-12-01', 1500, 1)""")
        sql(c, """select post_entry('2026-10-29', 'Adjustment', 'rent',
                  '[{"account": "1100", "lease_id": "L-0001", "debit": 100}, {"account": "4000", "credit": 10}]')""")
        sql(c, "update journal_lines set credit = 0 where lease_id = 'L-0004' and entry_date = '2026-09-09'")
        sql(c, "delete from payments where reference = 'EFT-9001'")
        sql(c, "update payments set amount = 1 where reference = 'EFT-9001'")
        sql(c, "select bill_rent('2026-11-15')")

    title("Row-level security: what a property manager can see")
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("set role mgr_north")
        sql(c, "select current_user, string_agg(property_id, ', ') as properties from properties group by 1")
        sql(c, "select count(*) as leases from leases")
        sql(c, "select property_id, count(*) as leases, sum(charged) as charged, sum(paid) as paid, sum(balance) as balance from lease_balances group by 1 order by 1")
        sql(c, "select count(*) from journal_lines")
        sql(c, "select count(*) from rent_roll")
    cli("aging", "--as-of", "2026-10-29", "--as", "mgr_south")

    title("Audit trail")
    with psycopg.connect(DSN, autocommit=True) as c:
        sql(c, "update leases set monthly_rent = 1780, end_date = '2028-02-01' where lease_id = 'L-0004'", show=False)
        sql(c, "update tenants set email = 'hana.ali@newmail.example' where tenant_id = (select tenant_id from leases where lease_id = 'L-0004')", show=False)
    cli("audit", "--limit", "4")

    title("Someone edits a migration that already ran")
    mig = ROOT / "migrations" / "003_billing.sql"
    original = mig.read_text(encoding="utf-8")
    try:
        mig.write_text(original.replace("p_fee numeric default 50", "p_fee numeric default 75"), encoding="utf-8")
        print("$ sed -i 's/default 50/default 75/' migrations/003_billing.sql")
        cli("migrate")
    finally:
        mig.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    main()
