"""lease-ledger: rent billing and receivables on PostgreSQL.

    python -m ledger migrate                      apply pending migrations
    python -m ledger seed                         load the sample portfolio (Jan to Oct 2026)
    python -m ledger bill --period 2026-11-01     bill a month's rent (safe to re-run)
    python -m ledger pay --lease L-0001 --amount 1500 --on 2026-11-02 --ref EFT-9001
    python -m ledger reverse --payment 42 --on 2026-11-09 --reason "NSF"
    python -m ledger late-fees --as-of 2026-11-10
    python -m ledger statement --lease L-0001
    python -m ledger aging --as-of 2026-10-29 [--as mgr_north]
    python -m ledger tb --from 2026-10-01 --to 2026-10-31
    python -m ledger reconcile --as-of 2026-10-29
    python -m ledger rentroll [--refresh]
    python -m ledger audit
"""
import argparse
import sys
from datetime import date
from decimal import Decimal

import psycopg
from psycopg import sql

from . import db
from .seed import seed


def d(s):
    return date.fromisoformat(s)


def cmd_migrate(conn, a):
    for name, status in db.migrate(conn):
        print(f"  {name:<22} {status}")


def cmd_seed(conn, a):
    print("Loading the sample portfolio")
    seed(conn, scale=a.scale, n_months=a.months)
    n = conn.execute("""select (select count(*) from properties), (select count(*) from units), (select count(*) from leases),
                               (select count(*) from charges), (select count(*) from payments), (select count(*) from journal_lines)""").fetchone()
    print(f"\n{n[0]} properties, {n[1]} units, {n[2]} leases, {n[3]} charges, {n[4]} payments, {n[5]} ledger lines")


def cmd_bill(conn, a):
    with conn.transaction():
        n = conn.execute("select bill_rent(%s)", [a.period]).fetchone()[0]
    print(f"Billed {n} lease(s) for {a.period:%Y-%m}" + ("" if n else " (already billed)"))


def cmd_pay(conn, a):
    with conn.transaction():
        pid = conn.execute("select record_payment(%s, %s, %s, %s)", [a.lease, a.amount, a.on, a.ref]).fetchone()[0]
    rows = conn.execute("""select c.kind, c.period, a.amount from allocations a join charges c using (charge_id)
                           where a.payment_id = %s order by c.due_date""", [pid]).fetchall()
    used = sum(r[2] for r in rows)
    print(f"Payment {pid} recorded: {a.amount:,.2f} from {a.lease}")
    for kind, period, amt in rows:
        print(f"  applied {amt:>10,.2f} to {kind} {period:%Y-%m}")
    if a.amount - used:
        print(f"  {a.amount - used:>18,.2f} kept as credit for the next charge")


def cmd_reverse(conn, a):
    with conn.transaction():
        e = conn.execute("select reverse_payment(%s, %s, %s)", [a.payment, a.on, a.reason]).fetchone()[0]
    print(f"Payment {a.payment} reversed by journal entry {e}")


def cmd_late_fees(conn, a):
    with conn.transaction():
        n = conn.execute("select assess_late_fees(%s)", [a.as_of]).fetchone()[0]
    print(f"Late fees assessed: {n}")


def cmd_statement(conn, a):
    head = conn.execute("""select l.lease_id, t.name, u.property_id, u.unit_no, l.monthly_rent, l.start_date, l.end_date - 1
                           from leases l join tenants t using (tenant_id) join units u using (unit_id) where lease_id = %s""",
                        [a.lease]).fetchone()
    if not head:
        sys.exit(f"no lease {a.lease}")
    print(f"{head[0]}  {head[1]}  {head[2]} unit {head[3]}  rent {head[4]:,.2f}  {head[5]} to {head[6]}\n")
    rows = conn.execute("""
        select entry_date, memo, debit, credit, sum(debit - credit) over (order by entry_date, entry_id, line_no) as balance
        from journal_lines join journal_entries using (entry_id, entry_date)
        where lease_id = %s and account = '1100'
        order by entry_date, entry_id, line_no""", [a.lease]).fetchall()
    print(db.table([(r[0], r[1], r[2] or None, r[3] or None, r[4]) for r in rows[-a.last:]],
                   ["date", "description", "charge", "payment", "balance"], right=(2, 3, 4)))


def cmd_aging(conn, a):
    if a.as_role:
        conn.execute(f"set role {sql.Identifier(a.as_role).as_string(conn)}")
    rows = conn.execute("select * from aged_receivables(%s)", [a.as_of]).fetchall()
    print(f"Aged receivables as of {a.as_of}" + (f" (as {a.as_role})" if a.as_role else "") + "\n")
    print(db.table(rows, ["property", "lease", "tenant", "0-30", "31-60", "61-90", "90+", "credit", "total"], right=range(3, 9)))
    tot = [sum(r[i] for r in rows) for i in range(3, 9)]
    print(f"\n{len(rows)} leases with a balance.  0-30 {tot[0]:,.2f}   31-60 {tot[1]:,.2f}   61-90 {tot[2]:,.2f}   "
          f"90+ {tot[3]:,.2f}   credits {tot[4]:,.2f}   net {tot[5]:,.2f}")


def cmd_tb(conn, a):
    rows = conn.execute("select * from trial_balance(%s, %s)", [getattr(a, "from"), a.to]).fetchall()
    print(f"Trial balance {getattr(a, 'from')} to {a.to}\n")
    print(db.table(rows, ["account", "name", "debits", "credits", "net"], right=(2, 3, 4)))
    dr, cr = sum(r[2] for r in rows), sum(r[3] for r in rows)
    print(f"\nTotal debits {dr:,.2f}   total credits {cr:,.2f}   " + ("balanced" if dr == cr else f"OUT BY {dr - cr:,.2f}"))


def cmd_reconcile(conn, a):
    """Sub-ledger (aging) against the general ledger, lease by lease. Exit 1 on any difference."""
    rows = conn.execute("""
        select coalesce(s.lease_id, g.lease_id), coalesce(s.total, 0), coalesce(g.balance, 0)
        from aged_receivables(%(d)s) s full join gl_receivables(%(d)s) g using (lease_id)
        where coalesce(s.total, 0) <> coalesce(g.balance, 0)""", {"d": a.as_of}).fetchall()
    sub, gl = conn.execute("""select (select coalesce(sum(total), 0) from aged_receivables(%(d)s)),
                                     (select coalesce(sum(balance), 0) from gl_receivables(%(d)s))""", {"d": a.as_of}).fetchone()
    tb = conn.execute("select sum(debit) - sum(credit) from journal_lines where entry_date <= %s", [a.as_of]).fetchone()[0]
    print(f"Reconciliation as of {a.as_of}")
    print(f"  ledger balances (debits - credits)       {tb or 0:>14,.2f}   {'OK' if not tb else 'FAIL'}")
    print(f"  A/R in the general ledger (1100)         {gl:>14,.2f}")
    print(f"  A/R in the tenant sub-ledger (aging)     {sub:>14,.2f}   {'OK' if sub == gl else 'FAIL'}")
    print(f"  leases that don't tie                    {len(rows):>14}   {'OK' if not rows else 'FAIL'}")
    for lid, s, g in rows[:20]:
        print(f"    {lid}: sub-ledger {s:,.2f}, GL {g:,.2f}")
    return 1 if (rows or tb or sub != gl) else 0


def cmd_rentroll(conn, a):
    if a.refresh:
        conn.execute("refresh materialized view concurrently rent_roll")
    rows = conn.execute("""
        select property_id, count(*) as units,
               count(*) filter (where status <> 'vacant') as leased,
               to_char(100.0 * count(*) filter (where status <> 'vacant') / count(*), 'FM990.0') || '%' as occupancy,
               count(*) filter (where status = 'expiring') as expiring_90d,
               sum(monthly_rent) as monthly_rent,
               round(avg(rent_psf_year), 2) as avg_psf_year,
               sum(balance) filter (where balance > 0) as owed
        from rent_roll group by property_id order by property_id""").fetchall()
    print("Rent roll summary\n")
    print(db.table(rows, ["property", "units", "leased", "occ %", "expiring 90d", "monthly rent", "$/sqft/yr", "owed"], right=range(1, 8)))


def cmd_audit(conn, a):
    rows = conn.execute("""select audit_id, to_char(logged_at, 'YYYY-MM-DD HH24:MI'), changed_by, table_name, op, row_key,
                                  coalesce(changed::text, '')
                           from audit_log order by audit_id desc limit %s""", [a.limit]).fetchall()
    print(db.table(rows[::-1], ["#", "when", "who", "table", "op", "key", "changed"]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ledger", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    s = sub.add_parser("seed"); s.add_argument("--scale", type=int, default=1); s.add_argument("--months", type=int, default=10)
    s = sub.add_parser("bill"); s.add_argument("--period", type=d, required=True)
    s = sub.add_parser("pay"); s.add_argument("--lease", required=True); s.add_argument("--amount", type=Decimal, required=True)
    s.add_argument("--on", type=d, required=True); s.add_argument("--ref", required=True)
    s = sub.add_parser("reverse"); s.add_argument("--payment", type=int, required=True); s.add_argument("--on", type=d, required=True)
    s.add_argument("--reason", required=True)
    s = sub.add_parser("late-fees"); s.add_argument("--as-of", type=d, required=True)
    s = sub.add_parser("statement"); s.add_argument("--lease", required=True); s.add_argument("--last", type=int, default=100)
    s = sub.add_parser("aging"); s.add_argument("--as-of", type=d, default=date.today()); s.add_argument("--as", dest="as_role")
    s = sub.add_parser("tb"); s.add_argument("--from", type=d, required=True); s.add_argument("--to", type=d, required=True)
    s = sub.add_parser("reconcile"); s.add_argument("--as-of", type=d, default=date.today())
    s = sub.add_parser("rentroll"); s.add_argument("--refresh", action="store_true")
    s = sub.add_parser("audit"); s.add_argument("--limit", type=int, default=15)
    a = ap.parse_args(argv)
    handler = globals()["cmd_" + a.cmd.replace("-", "_")]
    try:
        with db.connect() as conn:
            return handler(conn, a) or 0
    except (psycopg.Error, db.MigrationError) as e:
        msg = getattr(getattr(e, "diag", None), "message_primary", None) or str(e)
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
