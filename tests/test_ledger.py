import shutil
import threading
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from ledger import db
from conftest import balance


def test_edited_migration_is_refused(conn, tmp_path):
    folder = tmp_path / "migrations"
    shutil.copytree(db.MIGRATIONS, folder)
    assert all(status == "already applied" for _, status in db.migrate(conn, folder))
    f = folder / "003_billing.sql"
    f.write_text(f.read_text() + "\n-- quick fix\n")
    with pytest.raises(db.MigrationError, match="003_billing.sql was changed after it was applied"):
        db.migrate(conn, folder)


def test_overlapping_leases_are_rejected(conn, make_lease):
    lid, year = make_lease()
    unit = conn.execute("select unit_id, tenant_id from leases where lease_id = %s", [lid]).fetchone()
    with pytest.raises(psycopg.errors.ExclusionViolation):
        conn.execute("insert into leases values ('L-7001', %s, %s, %s, %s, 900, 1)",
                     [unit[0], unit[1], date(year, 2, 15), date(year + 1, 2, 15)])
    # starting the day the old one ends is fine (end dates are exclusive)
    conn.execute("insert into leases values ('L-7002', %s, %s, %s, %s, 900, 1)",
                 [unit[0], unit[1], date(year, 4, 1), date(year + 1, 4, 1)])


def test_unbalanced_entry_is_rejected_at_commit(conn):
    with pytest.raises(psycopg.errors.CheckViolation, match="does not balance"):
        with conn.transaction():
            conn.execute("""select post_entry('2026-10-05', 'typo', 'rent',
                            '[{"account": "1100", "debit": 100}, {"account": "4000", "credit": 10}]')""")


def test_ledger_rows_cannot_be_changed(conn, make_lease):
    lid, year = make_lease()
    conn.execute("select bill_rent(%s)", [date(year, 1, 1)])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        conn.execute("update journal_lines set debit = 1 where lease_id = %s", [lid])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        conn.execute("delete from charges where lease_id = %s", [lid])


def test_billing_is_prorated_and_safe_to_rerun(conn, make_lease):
    lid, year = make_lease(rent=3100, start_day=15)
    jan = date(year, 1, 1)
    conn.execute("select bill_rent(%s)", [jan])
    conn.execute("select bill_rent(%s)", [jan])
    rows = conn.execute("select amount, due_date from charges where lease_id = %s", [lid]).fetchall()
    assert rows == [(Decimal("1700.00"), date(year, 1, 15))]          # 17 of 31 days


def test_payment_pays_oldest_charge_first_and_keeps_the_rest_as_credit(conn, make_lease):
    lid, year = make_lease(rent=1000)
    conn.execute("select bill_rent(%s)", [date(year, 1, 1)])
    conn.execute("select bill_rent(%s)", [date(year, 2, 1)])
    conn.execute("select record_payment(%s, 2500, %s, 'EFT-1')", [lid, date(year, 2, 3)])
    paid = conn.execute("""select c.period, sum(a.amount) from charges c join allocations a using (charge_id)
                           where c.lease_id = %s group by c.period order by c.period""", [lid]).fetchall()
    assert paid == [(date(year, 1, 1), 1000), (date(year, 2, 1), 1000)]
    assert balance(conn, lid) == -500
    conn.execute("select bill_rent(%s)", [date(year, 3, 1)])               # credit is used up by the next bill
    assert conn.execute("""select sum(a.amount) from allocations a join charges c using (charge_id)
                           where c.lease_id = %s and c.period = %s""", [lid, date(year, 3, 1)]).fetchone()[0] == 500


def test_bounced_payment_puts_the_balance_back(conn, make_lease):
    lid, year = make_lease(rent=1200)
    conn.execute("select bill_rent(%s)", [date(year, 1, 1)])
    pid = conn.execute("select record_payment(%s, 1200, %s, 'CHQ-7')", [lid, date(year, 1, 2)]).fetchone()[0]
    assert balance(conn, lid) == 0
    conn.execute("select reverse_payment(%s, %s, 'NSF')", [pid, date(year, 1, 8)])
    assert balance(conn, lid) == 1200
    assert conn.execute("select count(*) from allocations where payment_id = %s", [pid]).fetchone()[0] == 0
    with pytest.raises(psycopg.errors.RaiseException, match="already reversed"):
        conn.execute("select reverse_payment(%s, %s, 'again')", [pid, date(year, 1, 9)])
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("select record_payment(%s, 1200, %s, 'CHQ-7')", [lid, date(year, 1, 10)])


def test_late_fee_charged_once_per_month(conn, make_lease):
    lid, year = make_lease(rent=900)
    conn.execute("select bill_rent(%s)", [date(year, 1, 1)])
    conn.execute("select record_payment(%s, 900, %s, 'EFT-9')", [lid, date(year, 1, 20)])   # paid, but late
    conn.execute("select assess_late_fees(%s)", [date(year, 1, 25)])
    conn.execute("select assess_late_fees(%s)", [date(year, 1, 28)])
    fees = conn.execute("select count(*), sum(amount) from charges where lease_id = %s and kind = 'late_fee'", [lid]).fetchone()
    assert fees == (1, 50)


def test_concurrent_payments_never_over_allocate(dsn, conn, make_lease):
    lid, year = make_lease(rent=1500)
    conn.execute("select bill_rent(%s)", [date(year, 1, 1)])
    errors = []

    def pay(i):
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute("select record_payment(%s, 300, %s, %s)", [lid, date(year, 1, 2), f"EFT-c{i}"])
        except Exception as e:                                   # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=pay, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    allocated = conn.execute("""select sum(a.amount) from allocations a join charges c using (charge_id)
                                where c.lease_id = %s""", [lid]).fetchone()[0]
    assert allocated == 1500
    assert balance(conn, lid) == -1500


def test_subledger_ties_to_general_ledger(conn, make_lease):
    lid, year = make_lease(rent=1000)
    for m in (1, 2, 3):
        conn.execute("select bill_rent(%s)", [date(year, m, 1)])
    conn.execute("select record_payment(%s, 1000, %s, 'A')", [lid, date(year, 1, 2)])
    pid = conn.execute("select record_payment(%s, 700, %s, 'B')", [lid, date(year, 2, 3)]).fetchone()[0]
    conn.execute("select assess_late_fees(%s)", [date(year, 2, 20)])
    conn.execute("select reverse_payment(%s, %s, 'NSF')", [pid, date(year, 2, 21)])
    for as_of in (date(year, 1, 31), date(year, 2, 10), date(year, 3, 31)):
        sub = conn.execute("select total from aged_receivables(%s) where lease_id = %s", [as_of, lid]).fetchone()
        gl = conn.execute("select balance from gl_receivables(%s) where lease_id = %s", [as_of, lid]).fetchone()
        assert sub == gl
    tb = conn.execute("select sum(debits) - sum(credits) from trial_balance('2000-01-01', '2100-01-01')").fetchone()[0]
    assert tb == 0


def test_managers_only_see_their_own_properties(conn, make_lease):
    mine, _ = make_lease(property_id="P990")
    other, _ = make_lease(property_id="P991")
    conn.execute("do $$ begin if not exists (select from pg_roles where rolname = 'mgr_test') then "
                 "create role mgr_test nologin in role property_manager; end if; end $$")
    conn.execute("insert into manager_properties values ('mgr_test', 'P990') on conflict do nothing")
    with conn.transaction():
        conn.execute("set local role mgr_test")
        seen = {r[0] for r in conn.execute("select lease_id from leases")}
        assert mine in seen and other not in seen
        assert conn.execute("select count(*) from lease_balances where lease_id = %s", [other]).fetchone()[0] == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            conn.execute("set local role mgr_test")
            conn.execute("select * from journal_lines limit 1")


def test_lease_changes_are_audited(conn, make_lease):
    lid, _ = make_lease(rent=1000)
    conn.execute("update leases set monthly_rent = 1050 where lease_id = %s", [lid])
    op, changed = conn.execute("""select op, changed from audit_log where table_name = 'leases' and row_key = %s
                                  order by audit_id desc limit 1""", [lid]).fetchone()
    assert op == "update" and changed == {"monthly_rent": [1000, 1050]}
