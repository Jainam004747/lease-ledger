"""Query plans on a bigger portfolio: what the indexes and the monthly partitions buy.

Builds leaseledger_perf (about 3,000 units, two years of billing; takes a few minutes the
first time), then for each query compares the plan with and without its index. The index is
dropped inside a transaction that is rolled back, so nothing is changed.

    python scripts/perf.py            # reuse the perf database if it exists
    python scripts/perf.py --rebuild
"""
import re
import sys
import time
from datetime import date
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import db  # noqa: E402
from ledger.seed import seed  # noqa: E402

NAME = "leaseledger_perf"


def dsn_for(name):
    p = conninfo_to_dict(db.url())
    p["dbname"] = name
    return make_conninfo(**p)


def build():
    with psycopg.connect(dsn_for("postgres"), autocommit=True) as admin:
        admin.execute(f"drop database if exists {NAME} with (force)")
        admin.execute(f"create database {NAME}")
    t0 = time.perf_counter()
    with psycopg.connect(dsn_for(NAME), autocommit=True) as c:
        db.migrate(c)
        seed(c, scale=10, first=date(2025, 1, 1), n_months=22, progress=lambda s: None)
        c.execute("vacuum analyze")
    print(f"built {NAME} in {time.perf_counter() - t0:.0f} s")


def plan(c, sql, params):
    rows = [r[0] for r in c.execute("explain (analyze, costs off) " + sql, params)]
    ms = float(re.search(r"Execution Time: ([\d.]+)", rows[-1]).group(1))
    return rows, ms


def best_of(c, sql, params, n=3):
    runs = [plan(c, sql, params) for _ in range(n)]
    return min(runs, key=lambda r: r[1])


def scans(rows):
    lines = [re.sub(r"actual time=\S+ ", "", l.strip()) for l in rows if "Scan" in l]
    return lines[:4] + ([f"... {len(lines) - 4} more scan nodes"] if len(lines) > 4 else [])


def compare(c, title, sql, params, index):
    with c.transaction():
        c.execute(f"drop index {index}")
        before, ms_before = best_of(c, sql, params)
        raise psycopg.Rollback()                      # put the index back
    after, ms_after = best_of(c, sql, params)
    print(f"\n{title}")
    print(f"  without {index}: {ms_before:9.2f} ms")
    for l in scans(before):
        print(f"      {l}")
    print(f"  with it: {ms_after:9.2f} ms   ({ms_before / ms_after:,.0f}x faster)")
    for l in scans(after):
        print(f"      {l}")


def main():
    with psycopg.connect(dsn_for("postgres"), autocommit=True) as admin:
        exists = admin.execute("select 1 from pg_database where datname = %s", [NAME]).fetchone() is not None
    if "--rebuild" in sys.argv or not exists:
        build()
    with psycopg.connect(dsn_for(NAME), autocommit=True) as c:
        n = c.execute("""select (select count(*) from units), (select count(*) from leases), (select count(*) from charges),
                                (select count(*) from payments), (select count(*) from allocations), (select count(*) from journal_lines),
                                (select count(*) from pg_inherits where inhparent = 'journal_lines'::regclass)""").fetchone()
        print(f"{n[0]:,} units, {n[1]:,} leases, {n[2]:,} charges, {n[3]:,} payments, {n[4]:,} allocations,")
        print(f"{n[5]:,} ledger lines in {n[6]} monthly partitions")
        lease = c.execute("select lease_id from leases order by lease_id offset 1000 limit 1").fetchone()[0]

        compare(c, f"1. Tenant statement ({lease}): every A/R line for one lease, in date order",
                "select entry_date, debit, credit from journal_lines where lease_id = %s and account = '1100' order by entry_date",
                [lease], "journal_lines_lease_id_entry_date_idx")

        compare(c, f"2. Open charges for one lease (what allocate() reads on every payment)",
                """select c.charge_id, c.amount - coalesce((select sum(a.amount) from allocations a where a.charge_id = c.charge_id), 0)
                   from charges c where c.lease_id = %s order by c.due_date""",
                [lease], "allocations_charge_id_idx")

        print("\n3. One month's trial balance: partition pruning")
        month, ms = best_of(c, """select account, sum(debit), sum(credit) from journal_lines
                                  where entry_date >= %s and entry_date < %s group by account""",
                            [date(2026, 10, 1), date(2026, 11, 1)])
        whole, ms_all = best_of(c, "select account, sum(debit), sum(credit) from journal_lines group by account", [])
        parts = sorted({re.search(r"on (journal_lines_\w+)", l).group(1) for l in month if "Scan on journal_lines_" in l})
        print(f"  October only: {ms:7.2f} ms, partitions read: {', '.join(parts)}")
        print(f"  all time:     {ms_all:7.2f} ms, partitions read: {sum('Scan on journal_lines_' in l for l in whole)}")


if __name__ == "__main__":
    main()
