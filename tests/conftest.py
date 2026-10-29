import itertools
from datetime import date

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ledger import db

TEST_DB = "leaseledger_test"


def test_url():
    params = conninfo_to_dict(db.url())
    params["dbname"] = TEST_DB
    return make_conninfo(**params)


@pytest.fixture(scope="session")
def dsn():
    params = conninfo_to_dict(db.url())
    params["dbname"] = "postgres"
    with psycopg.connect(make_conninfo(**params), autocommit=True) as admin:
        admin.execute(f"drop database if exists {TEST_DB} with (force)")
        admin.execute(f"create database {TEST_DB}")
    url = test_url()
    with psycopg.connect(url, autocommit=True) as conn:
        db.migrate(conn)
    return url


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


_ids = itertools.count(1)


@pytest.fixture
def make_lease(conn):
    """Creates a property, unit, tenant and lease of its own in a year no other test uses,
    so billing a month in one test never touches another test's lease."""
    def make(rent=1500, months=3, start_day=1, due_day=1, property_id=None):
        n = next(_ids)
        year = 2030 + n
        pid = property_id or f"P{900 + n:03d}"
        conn.execute("insert into properties values (%s, %s, 'Sudbury') on conflict do nothing", [pid, f"Test {pid}"])
        unit = conn.execute("insert into units (property_id, unit_no, sqft) values (%s, %s, 700) returning unit_id",
                            [pid, f"T{n}"]).fetchone()[0]
        tenant = conn.execute("insert into tenants (name) values (%s) returning tenant_id", [f"Tenant {n}"]).fetchone()[0]
        lid = f"L-{8000 + n}"
        conn.execute("insert into leases values (%s, %s, %s, %s, %s, %s, %s)",
                     [lid, unit, tenant, date(year, 1, start_day), date(year, 1 + months, 1), rent, due_day])
        return lid, year
    return make


def balance(conn, lease_id):
    return conn.execute("select balance from lease_balances where lease_id = %s", [lease_id]).fetchone()[0]
