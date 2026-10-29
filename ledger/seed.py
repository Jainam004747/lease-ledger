"""Sample portfolio: properties, units, tenants, leases, and ten months of billing and payments.

Everything goes through the same database functions the CLI uses (bill_rent, record_payment,
assess_late_fees, reverse_payment), so the history is built by the real rules.
Fixed random seed, so the numbers are the same every run.
"""
import random
from decimal import Decimal
from datetime import date, timedelta

PROPERTIES = [
    ("P001", "Bayview Towers", "Toronto"),
    ("P002", "Riverside Lofts", "Ottawa"),
    ("P003", "Kingsway Commons", "Sudbury"),
    ("P004", "Maple Court", "Waterloo"),
    ("P005", "Harbour Point", "Toronto"),
    ("P006", "Lakehead Plaza", "Thunder Bay"),
]
BASE_RENT = {"Toronto": 2.95, "Ottawa": 2.45, "Waterloo": 2.35, "Sudbury": 1.95, "Thunder Bay": 1.80}  # $/sqft/month
FIRST = ["Ava", "Liam", "Noah", "Emma", "Mia", "Lucas", "Zoe", "Ethan", "Aria", "Owen", "Leah", "Ryan",
         "Nora", "Arjun", "Priya", "Chen", "Sofia", "Omar", "Hana", "Mateo", "Isla", "Dev", "Maya", "Jack"]
LAST = ["Tremblay", "Singh", "Wong", "Martin", "Roy", "Patel", "Gagnon", "Smith", "Nguyen", "Brown",
        "Ali", "Lee", "Kim", "Côté", "Wilson", "Shah", "Lavoie", "Taylor", "Chen", "Fortin"]
MANAGERS = {"mgr_north": ["P003", "P006"], "mgr_south": ["P001", "P005"], "mgr_east": ["P002"]}


def months(start, n):
    d = start
    for _ in range(n):
        yield d
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def seed(conn, scale=1, first=date(2026, 1, 1), n_months=10, rnd_seed=7, progress=print):
    rnd = random.Random(rnd_seed)
    props = PROPERTIES if scale == 1 else [
        (f"P{i:03d}", f"{rnd.choice(LAST)} {rnd.choice(['Towers', 'Court', 'Commons', 'Place', 'Lofts'])}",
         rnd.choice(list(BASE_RENT))) for i in range(1, 6 * scale + 1)]
    units_per = (8, 13) if scale == 1 else (40, 60)

    with conn.transaction():
        conn.cursor().executemany("insert into properties values (%s, %s, %s)", props)
        unit_rows = []
        for pid, _, city in props:
            for n in range(rnd.randint(*units_per)):
                unit_rows.append((pid, f"{(n // 6 + 1)}{(n % 6 + 1):02d}", rnd.choice([550, 620, 700, 780, 860, 950, 1100])))
        conn.cursor().executemany("insert into units (property_id, unit_no, sqft) values (%s, %s, %s)", unit_rows)
        units = conn.execute("select u.unit_id, u.sqft, p.city from units u join properties p using (property_id) order by unit_id").fetchall()

        leases, n_lease = [], 0
        for unit_id, sqft, city in units:
            if rnd.random() < 0.08:
                continue                                    # vacant
            n_lease += 1
            tenant = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
            tid = conn.execute("insert into tenants (name, email) values (%s, %s) returning tenant_id",
                               [tenant, tenant.lower().replace(" ", ".").replace("ô", "o") + "@example.com"]).fetchone()[0]
            start = first - timedelta(days=rnd.randint(0, 600)) if rnd.random() < 0.85 else first + timedelta(days=rnd.randint(10, 200))
            start = start.replace(day=1) if rnd.random() < 0.8 else start
            term = rnd.choice([12, 12, 12, 24]) * 30 + rnd.choice([0, 5, 6])
            end = start + timedelta(days=term)
            if end < first + timedelta(days=n_months * 30):
                end = first + timedelta(days=rnd.randint(n_months * 30 + 10, n_months * 30 + 420))
            rent = round(sqft * BASE_RENT[city] * rnd.uniform(0.92, 1.08) / 5) * 5
            leases.append((f"L-{n_lease:04d}", unit_id, tid, start, end, rent, rnd.choice([1, 1, 1, 1, 15])))
        conn.cursor().executemany("insert into leases values (%s, %s, %s, %s, %s, %s, %s)", leases)

        for role, pids in MANAGERS.items():
            conn.execute(f"do $$ begin if not exists (select from pg_roles where rolname = '{role}') then "
                         f"create role {role} nologin in role property_manager; end if; end $$")
            conn.cursor().executemany("insert into manager_properties values (%s, %s)", [(role, p) for p in pids if p in {x[0] for x in props}])

    # How each tenant pays: mostly on time, some late, a few short or skipping a month.
    habit = {l[0]: rnd.choices(["on_time", "late", "short", "erratic"], [0.78, 0.12, 0.05, 0.05])[0] for l in leases}
    rent = {l[0]: l[5] for l in leases}
    ref = 1000
    bounced = None
    for period in months(first, n_months):
        with conn.transaction():
            billed = conn.execute("select bill_rent(%s)", [period]).fetchone()[0]
        due = {lid: amt for lid, amt in conn.execute(
            "select lease_id, amount from charges where kind = 'rent' and period = %s", [period])}
        early, later = [], []
        for lid, amt in due.items():
            h = habit[lid]
            if h == "erratic" and rnd.random() < 0.35:
                continue
            if h == "short":
                amt = round(amt * Decimal(rnd.choice(["0.5", "0.75", "0.9"])), 2)
            ref += 1
            kind = "EFT" if rnd.random() < 0.7 else "CHQ"
            if h == "late" or (h == "erratic" and rnd.random() < 0.5):
                later.append((lid, amt, period + timedelta(days=rnd.randint(9, 24)), f"{kind}-{ref}"))
            else:
                early.append((lid, amt, period + timedelta(days=rnd.randint(0, 4)), f"{kind}-{ref}"))
        catch_up = []
        for lid in habit:
            if habit[lid] in ("short", "erratic") and rnd.random() < 0.4:
                ref += 1
                catch_up.append((lid, Decimal(rent[lid]) / 2, period + timedelta(days=rnd.randint(12, 26)), f"EFT-{ref}"))
        with conn.transaction():
            conn.cursor().executemany("select record_payment(%s, %s, %s, %s)", early)
        if bounced is None and period.month == 9 and scale == 1:
            pid, lid, amt = conn.execute("""select payment_id, lease_id, amount from payments
                                            where reference like 'CHQ-%%' and received_on >= %s order by payment_id limit 1""",
                                         [period]).fetchone()
            with conn.transaction():
                conn.execute("select reverse_payment(%s, %s, %s)", [pid, period + timedelta(days=8), "NSF: cheque returned by bank"])
            bounced = (pid, lid, amt)
        with conn.transaction():
            fees = conn.execute("select assess_late_fees(%s)", [period + timedelta(days=9)]).fetchone()[0]
        with conn.transaction():
            conn.cursor().executemany("select record_payment(%s, %s, %s, %s)", later + catch_up)
        progress(f"  {period:%Y-%m}  billed {billed:>4}   payments {len(early) + len(later) + len(catch_up):>4}   late fees {fees:>3}")
    conn.execute("refresh materialized view rent_roll")
    return bounced
