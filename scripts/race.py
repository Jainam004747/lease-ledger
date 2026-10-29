"""Two payments for the same tenant arrive at the same moment. What happens?

Runs in its own database (leaseledger_race) so it can't disturb anything else.

  1. A naive version of record_payment that reads the open balance, then writes an allocation,
     without locking the lease. Both sessions read 1,500 open and both try to apply 1,000.
  2. The real record_payment, which locks the lease row first. The second payment waits,
     then sees only 500 left.

Each session also spends half a second on other work before committing, like a real request would.
"""
import sys
import threading
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import db  # noqa: E402

NAIVE = """
create or replace function record_payment_naive(p_lease text, p_amount numeric, p_on date, p_ref text)
returns bigint language plpgsql as $$
declare
    e bigint; id bigint; c record;
begin
    e := post_entry(p_on, 'Payment ' || p_ref, 'payment',
                    jsonb_build_array(jsonb_build_object('account', '1000', 'debit', p_amount),
                                      jsonb_build_object('account', '1100', 'lease_id', p_lease, 'credit', p_amount)));
    insert into payments (lease_id, received_on, amount, reference, entry_id)
    values (p_lease, p_on, p_amount, p_ref, e) returning payment_id into id;
    select ch.charge_id, ch.amount - coalesce((select sum(a.amount) from allocations a where a.charge_id = ch.charge_id), 0) as open
      into c from charges ch where ch.lease_id = p_lease order by ch.due_date limit 1;
    raise notice '% sees % open', p_ref, c.open;
    perform pg_sleep(0.5);                                  -- the gap between reading and writing
    insert into allocations values (id, c.charge_id, least(p_amount, c.open));
    return id;
end $$;
"""


def dsn_for(name):
    p = conninfo_to_dict(db.url())
    p["dbname"] = name
    return make_conninfo(**p)


def setup():
    with psycopg.connect(dsn_for("postgres"), autocommit=True) as admin:
        admin.execute("drop database if exists leaseledger_race with (force)")
        admin.execute("create database leaseledger_race")
    with psycopg.connect(dsn_for("leaseledger_race"), autocommit=True) as c:
        db.migrate(c)
        c.execute("insert into properties values ('P001', 'Bayview Towers', 'Toronto')")
        c.execute("insert into tenants (name) values ('Hana Ali')")
        for lid in ("L-0001", "L-0002"):
            unit = c.execute("insert into units (property_id, unit_no, sqft) values ('P001', %s, 700) returning unit_id", [lid[-3:]]).fetchone()[0]
            c.execute("insert into leases values (%s, %s, 1, '2026-11-01', '2027-11-01', 1500, 1)", [lid, unit])
        c.execute("select bill_rent('2026-11-01')")
        c.execute(NAIVE)


def pay(func, lease, ref, out):
    t0 = time.perf_counter()
    try:
        with psycopg.connect(dsn_for("leaseledger_race"), autocommit=True) as c:
            c.add_notice_handler(lambda n: out.append(f"  {n.message_primary}"))
            with c.transaction():
                c.execute(f"select {func}(%s, 1000, '2026-11-02', %s)", [lease, ref])
                c.execute("select pg_sleep(0.5)")             # the rest of a slow transaction
        out.append(f"  {ref} committed after {time.perf_counter() - t0:.2f} s")
    except psycopg.Error as e:
        out.append(f"  {ref} FAILED after {time.perf_counter() - t0:.2f} s: {e.diag.message_primary}")


def run(func, lease):
    out = []
    ts = [threading.Thread(target=pay, args=(func, lease, ref, out)) for ref in ("EFT-A", "EFT-B")]
    for t in ts:
        t.start()
        time.sleep(0.05)
    for t in ts:
        t.join()
    print("\n".join(out))
    with psycopg.connect(dsn_for("leaseledger_race")) as c:
        alloc, pays = c.execute("""select (select coalesce(sum(a.amount), 0) from allocations a join charges ch using (charge_id) where ch.lease_id = %(l)s),
                                          (select count(*) from payments where lease_id = %(l)s)""", {"l": lease}).fetchone()
        bal = c.execute("select balance from lease_balances where lease_id = %s", [lease]).fetchone()[0]
    print(f"  result: {pays} payment(s) saved, {alloc:,.2f} of 1,500.00 rent allocated, balance {bal:,.2f}\n")


if __name__ == "__main__":
    setup()
    print("Rent of 1,500.00 is open. Two payments of 1,000.00 arrive at the same time.\n")
    print("1) Naive: read the open amount, then write, no lock")
    run("record_payment_naive", "L-0001")
    print("2) record_payment: lock the lease row first")
    run("record_payment", "L-0002")
