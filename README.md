# lease-ledger

Rent billing and tenant receivables for a property portfolio, written in PostgreSQL.

Most of the logic lives in the database itself: constraints, triggers, PL/pgSQL functions, row-level security, partitioning and a materialized view. The Python part is a thin command line and a test suite. I wanted a project where Postgres does the heavy lifting, because in a finance setting the database is the last line of defence. If a rule only lives in application code, the next script, report tool or person with a SQL client can walk right past it.

## What it does

- **Bills rent** every month, prorated by day for leases that start or end mid-month. Billing the same month twice does nothing.
- **Records payments** and applies them to the oldest open charge first. Anything left over stays as a credit and is used by the next bill.
- **Handles bounced cheques** with a reversing entry that puts the balance back on the tenant.
- **Charges late fees** once per month when rent isn't paid within the grace period.
- **Keeps a double-entry general ledger.** Every charge and payment is a journal entry whose debits equal its credits.
- **Reports** a trial balance, aged receivables (0-30, 31-60, 61-90, 90+ days), tenant statements and a rent roll.
- **Reconciles** the tenant sub-ledger to the general ledger, lease by lease.

## The PostgreSQL parts

| Feature | Where | What it's for |
|---|---|---|
| Exclusion constraint (`btree_gist`) | `001_core.sql` | two leases on the same unit can't overlap in time |
| Deferred constraint trigger | `002_ledger.sql` | a journal entry must balance when the transaction commits, after all its lines exist |
| Append-only triggers | `002`, `003` | ledger lines, charges and payments can't be edited or deleted; mistakes are reversed |
| Declarative partitioning | `002_ledger.sql` | ledger lines split by month, so a month's report only reads that month |
| PL/pgSQL functions | `003_billing.sql` | billing, payment allocation, reversals and late fees as one transaction each |
| Row locks (`select ... for update`) | `003_billing.sql` | two payments for the same tenant can't allocate the same balance |
| JSONB audit trigger | `004_audit.sql` | every lease change and payment reversal logged with only the columns that changed |
| Row-level security | `005_security.sql` | property managers only see their own buildings, in any tool |
| `security_invoker` views | `006_reports.sql` | so the report views respect row-level security instead of running as their owner |
| CTEs, `FILTER`, window functions | `006_reports.sql`, `ledger/__main__.py` | aging buckets and running balances on statements |
| Materialized view + unique index | `006_reports.sql` | rent roll that can be refreshed `CONCURRENTLY` without blocking readers |
| Checksummed migrations | `ledger/db.py` | an old migration edited after it ran is refused |

## Quick start

You need PostgreSQL 14 or newer. With Docker:

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
docker exec pg createdb -U postgres leaseledger

python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m ledger migrate
python -m ledger seed
python -m ledger aging --as-of 2026-10-29
```

The connection string defaults to `postgresql://postgres:postgres@localhost:5432/leaseledger`. Set `DATABASE_URL` to use another server.

```bash
pytest -q                     # 12 tests, in their own database (leaseledger_test)
python scripts/demo.py        # everything below, in a fresh database (leaseledger_demo)
python scripts/race.py        # two payments at the same moment
python scripts/perf.py        # query plans on ~3,000 units (builds leaseledger_perf, about a minute)
```

## Output

All of this is real output from `scripts/demo.py`. The seed loads 6 properties and 54 leases, then runs ten months of billing and payments from January to October 2026 through the same functions the CLI uses. Most tenants pay on time, some pay late, a few pay short, and one cheque bounces in September.

### Set up

```text
$ python -m ledger migrate
  001_core.sql           applied
  002_ledger.sql         applied
  003_billing.sql        applied
  004_audit.sql          applied
  005_security.sql       applied
  006_reports.sql        applied

$ python -m ledger seed
Loading the sample portfolio
  2026-01  billed   48   payments   48   late fees   6
  2026-02  billed   50   payments   52   late fees   7
  2026-03  billed   51   payments   52   late fees   7
  2026-04  billed   52   payments   53   late fees   7
  2026-05  billed   53   payments   54   late fees   7
  2026-06  billed   54   payments   54   late fees   8
  2026-07  billed   54   payments   55   late fees   8
  2026-08  billed   54   payments   57   late fees   8
  2026-09  billed   54   payments   56   late fees   9
  2026-10  billed   54   payments   57   late fees  10

6 properties, 60 units, 54 leases, 601 charges, 538 payments, 2280 ledger lines
```

Month-end: the trial balance has to balance, and the aging report has to agree with the A/R account in the general ledger. `reconcile` checks both and exits with code 1 if anything is off, so it can run as a scheduled control.

### Month-end reports

```text
$ python -m ledger tb --from 2026-10-01 --to 2026-10-31
Trial balance 2026-10-01 to 2026-10-31

account  name                    debits    credits         net
-------  -------------------  ---------  ---------  ----------
1000     Cash                 96,174.25       0.00   96,174.25
1100     Accounts receivable  95,765.00  96,174.25     -409.25
4000     Rent revenue              0.00  95,265.00  -95,265.00
4100     Late fee revenue          0.00     500.00     -500.00

Total debits 191,939.25   total credits 191,939.25   balanced

$ python -m ledger aging --as-of 2026-10-29
Aged receivables as of 2026-10-29

property  lease   tenant              0-30     31-60   61-90   90+  credit     total
--------  ------  --------------  --------  --------  ------  ----  ------  --------
P001      L-0004  Hana Ali        1,760.00     50.00    0.00  0.00    0.00  1,810.00
P002      L-0012  Omar Martin       250.00      0.00    0.00  0.00    0.00    250.00
P002      L-0015  Maya Taylor         0.00      0.00    0.00  0.00  252.25   -252.25
P004      L-0026  Mateo Tremblay  2,145.00    504.75    0.00  0.00    0.00  2,649.75
P004      L-0033  Priya Tremblay    500.00      0.00    0.00  0.00    0.00    500.00
P004      L-0035  Omar Lavoie     1,364.00      0.00    0.00  0.00    0.00  1,364.00
P005      L-0037  Arjun Nguyen      500.00      0.00    0.00  0.00    0.00    500.00
P005      L-0043  Leah Taylor     1,635.00  1,635.00  400.00  0.00    0.00  3,670.00
P006      L-0047  Liam Lavoie       500.00      0.00    0.00  0.00    0.00    500.00
P006      L-0052  Aria Brown        500.00      0.00    0.00  0.00    0.00    500.00

10 leases with a balance.  0-30 9,154.00   31-60 2,189.75   61-90 400.00   90+ 0.00   credits 252.25   net 11,491.50

$ python -m ledger reconcile --as-of 2026-10-29
Reconciliation as of 2026-10-29
  ledger balances (debits - credits)                 0.00   OK
  A/R in the general ledger (1100)              11,491.50
  A/R in the tenant sub-ledger (aging)          11,491.50   OK
  leases that don't tie                                 0   OK

$ python -m ledger rentroll
Rent roll summary

property  units  leased   occ %  expiring 90d  monthly rent  $/sqft/yr      owed
--------  -----  ------  ------  ------------  ------------  ---------  --------
P001         10       9   90.0%             4     19,060.00      35.60  1,810.00
P002          8       8  100.0%             1     12,820.00      28.95    250.00
P003          8       8  100.0%             2     12,205.00      23.40
P004         13      11   84.6%             1     19,860.00      28.50  4,513.75
P005          9       7   77.8%             1     16,550.00      36.00  4,170.00
P006         12      11   91.7%             2     14,770.00      21.88  1,000.00
```

L-0004's September cheque bounced. The reversal is a new entry, the original payment stays on the record, and the late fee follows because the rent was no longer paid inside the grace period.

### A bounced cheque

```text
$ python -m ledger statement --lease L-0004 --last 6
L-0004  Hana Ali  P001 unit 104  rent 1,710.00  2025-01-01 to 2026-12-21

date        description                                                            charge   payment   balance
----------  -------------------------------------------------------------------  --------  --------  --------
2026-09-05  Payment CHQ-1429 on L-0004                                                     1,710.00      0.00
2026-09-09  Reversal of payment CHQ-1429 (L-0004): NSF: cheque returned by bank  1,710.00            1,710.00
2026-09-10  Late fee 2026-09 L-0004                                                 50.00            1,760.00
2026-10-01  Rent 2026-10 L-0004                                                  1,710.00            3,470.00
2026-10-04  Payment EFT-1485 on L-0004                                                     1,710.00  1,760.00
2026-10-10  Late fee 2026-10 L-0004                                                 50.00            1,810.00

$ python -m ledger reverse --payment 429 --on 2026-10-29 --reason "trying again"
ERROR: payment 429 was already reversed
```

A tenant who was three months behind pays it all at once, plus a bit more. The payment clears the oldest charges first and the rest becomes a credit.

### A payment that covers more than what's owed

```text
$ python -m ledger pay --lease L-0043 --amount 4000 --on 2026-10-29 --ref EFT-9001
Payment 539 recorded: 4,000.00 from L-0043
  applied     350.00 to rent 2026-08
  applied      50.00 to late_fee 2026-08
  applied   1,585.00 to rent 2026-09
  applied      50.00 to late_fee 2026-09
  applied   1,585.00 to rent 2026-10
  applied      50.00 to late_fee 2026-10
              330.00 kept as credit for the next charge

$ python -m ledger statement --lease L-0043 --last 4
L-0043  Leah Taylor  P005 unit 203  rent 1,585.00  2025-11-27 to 2026-11-21

date        description                   charge   payment   balance
----------  --------------------------  --------  --------  --------
2026-10-01  Rent 2026-10 L-0043         1,585.00            4,412.50
2026-10-10  Late fee 2026-10 L-0043        50.00            4,462.50
2026-10-24  Payment EFT-1538 on L-0043              792.50  3,670.00
2026-10-29  Payment EFT-9001 on L-0043            4,000.00   -330.00

$ python -m ledger pay --lease L-0043 --amount 4000 --on 2026-10-29 --ref EFT-9001
ERROR: duplicate key value violates unique constraint "payments_lease_id_reference_key"
```

### Billing November twice

```text
$ python -m ledger bill --period 2026-11-01
Billed 54 lease(s) for 2026-11

$ python -m ledger bill --period 2026-11-01
Billed 0 lease(s) for 2026-11 (already billed)
```

Every rule below is enforced by the database, so it holds for the CLI, a script, or someone typing SQL directly.

### Things the database refuses

```text
leaseledger=# insert into leases values ('L-0999', (select unit_id from leases where lease_id = 'L-0001'), 1, '2026-12-01', '2027-12-01', 1500, 1)
ERROR:  conflicting key value violates exclusion constraint "no_overlapping_leases"

leaseledger=# select post_entry('2026-10-29', 'Adjustment', 'rent', '[{"account": "1100", "lease_id": "L-0001", "debit": 100}, {"account": "4000", "credit": 10}]')
ERROR:  journal entry 1197 does not balance: debits 100.00, credits 10.00, 2 line(s)

leaseledger=# update journal_lines set credit = 0 where lease_id = 'L-0004' and entry_date = '2026-09-09'
ERROR:  journal_lines is append-only (update refused): post a reversing entry instead

leaseledger=# delete from payments where reference = 'EFT-9001'
ERROR:  payments are append-only: reverse the payment instead

leaseledger=# update payments set amount = 1 where reference = 'EFT-9001'
ERROR:  payment 539 can only be changed by reversing it once

leaseledger=# select bill_rent('2026-11-15')
ERROR:  period must be the first of a month, got 2026-11-15
```

`mgr_north` manages P003 and P006. The same queries return only their rows, the general ledger isn't granted at all, and the rent roll isn't granted because row-level security doesn't apply to materialized views.

### Row-level security: what a property manager can see

```text
leaseledger=# select current_user, string_agg(property_id, ', ') as properties from properties group by 1
current_user  properties
------------  ----------
mgr_north     P003, P006

leaseledger=# select count(*) as leases from leases
leases
------
    19

leaseledger=# select property_id, count(*) as leases, sum(charged) as charged, sum(paid) as paid, sum(balance) as balance from lease_balances group by 1 order by 1
property_id  leases     charged        paid    balance
-----------  ------  ----------  ----------  ---------
P003              8  131,200.50  119,990.00  11,210.50
P006             11  163,470.00  147,700.00  15,770.00

leaseledger=# select count(*) from journal_lines
ERROR:  permission denied for table journal_lines

leaseledger=# select count(*) from rent_roll
ERROR:  permission denied for materialized view rent_roll

$ python -m ledger aging --as-of 2026-10-29 --as mgr_south
Aged receivables as of 2026-10-29 (as mgr_south)

property  lease   tenant            0-30  31-60  61-90   90+  credit     total
--------  ------  ------------  --------  -----  -----  ----  ------  --------
P001      L-0004  Hana Ali      1,760.00  50.00   0.00  0.00    0.00  1,810.00
P005      L-0037  Arjun Nguyen    500.00   0.00   0.00  0.00    0.00    500.00
P005      L-0043  Leah Taylor       0.00   0.00   0.00  0.00  330.00   -330.00

3 leases with a balance.  0-30 2,260.00   31-60 50.00   61-90 0.00   90+ 0.00   credits 330.00   net 1,980.00
```

### Audit trail

```text
leaseledger=# update leases set monthly_rent = 1780, end_date = '2028-02-01' where lease_id = 'L-0004'
UPDATE 1

leaseledger=# update tenants set email = 'hana.ali@newmail.example' where tenant_id = (select tenant_id from leases where lease_id = 'L-0004')
UPDATE 1

$ python -m ledger audit --limit 4
#   when              who       table     op      key     changed
--  ----------------  --------  --------  ------  ------  -----------------------------------------------------------------------------------------------------------------------------------
54  2026-10-29 00:21  postgres  leases    insert  L-0054
55  2026-10-29 00:21  postgres  payments  update  429     {"reversed_on": [null, "2026-09-09"], "reversal_reason": [null, "NSF: cheque returned by bank"], "reversed_entry_id": [null, 1002]}
56  2026-10-29 00:21  postgres  leases    update  L-0004  {"end_date": ["2026-12-22", "2028-02-01"], "monthly_rent": [1710.00, 1780.00]}
57  2026-10-29 00:21  postgres  tenants   update  4       {"email": ["hana.ali@example.com", "hana.ali@newmail.example"]}
```

### Someone edits a migration that already ran

```text
$ sed -i 's/default 50/default 75/' migrations/003_billing.sql
$ python -m ledger migrate
ERROR: 003_billing.sql was changed after it was applied (recorded e0f9763b30a3, now 405c6d831e6a). Write a new migration instead of editing an old one.
```

### Two payments at the same moment

`scripts/race.py` sends two payments of 1,000 against one open rent of 1,500 at the same time. The naive version reads the open balance and then writes, with nothing stopping the other session from reading the same number in between.

```text
Rent of 1,500.00 is open. Two payments of 1,000.00 arrive at the same time.

1) Naive: read the open amount, then write, no lock
  EFT-A sees 1500.00 open
  EFT-B sees 1500.00 open
  EFT-A committed after 1.02 s
  EFT-B FAILED after 1.02 s: over-allocated charge 1: allocated 2000.00 of 1500.00
  result: 1 payment(s) saved, 1,000.00 of 1,500.00 rent allocated, balance 500.00

2) record_payment: lock the lease row first
  EFT-A committed after 0.52 s
  EFT-B committed after 0.98 s
  result: 2 payment(s) saved, 1,500.00 of 1,500.00 rent allocated, balance -500.00
```

In the naive case the allocation check (a deferred constraint trigger) saved the data: by the time B committed, A had already committed, so the check saw 2,000 allocated against 1,500 and rejected B. But a real payment was lost along with it, and if both had committed at exactly the same instant neither check would have seen the other's row. The lock in `record_payment` is the actual fix: B waits for A, then sees 500 open, applies 500 and keeps 500 as credit.

### Query plans on a bigger portfolio

`scripts/perf.py` builds about 3,000 units with 22 months of history, then runs each query with and without its index (the index is dropped inside a transaction that is rolled back). Best of three runs on my machine:

```text
2,977 units, 2,776 leases, 72,109 charges, 61,378 payments, 83,444 allocations,
266,974 ledger lines in 22 monthly partitions

1. Tenant statement (L-1001): every A/R line for one lease, in date order
  without journal_lines_lease_id_entry_date_idx:     18.41 ms
      ->  Parallel Seq Scan on journal_lines_2026_05 journal_lines_17 (rows=2 loops=1)
      ->  Parallel Seq Scan on journal_lines_2026_09 journal_lines_21 (rows=2 loops=1)
      ->  Parallel Seq Scan on journal_lines_2025_07 journal_lines_7 (rows=2 loops=1)
      ->  Parallel Seq Scan on journal_lines_2026_04 journal_lines_16 (rows=2 loops=1)
      ... 18 more scan nodes
  with it:      0.29 ms   (63x faster)
      ->  Bitmap Heap Scan on journal_lines_2025_01 journal_lines_1 (rows=2 loops=1)
      ->  Bitmap Index Scan on journal_lines_2025_01_lease_id_entry_date_idx (rows=3 loops=1)
      ->  Bitmap Heap Scan on journal_lines_2025_02 journal_lines_2 (rows=2 loops=1)
      ->  Bitmap Index Scan on journal_lines_2025_02_lease_id_entry_date_idx (rows=3 loops=1)
      ... 40 more scan nodes

2. Open charges for one lease (what allocate() reads on every payment)
  without allocations_charge_id_idx:     72.97 ms
      Index Scan using charges_lease_id_due_date_idx on charges c (rows=22 loops=1)
      ->  Seq Scan on allocations a (rows=1 loops=22)
  with it:      0.12 ms   (598x faster)
      ->  Bitmap Heap Scan on charges c (rows=22 loops=1)
      ->  Bitmap Index Scan on charges_lease_id_due_date_idx (rows=22 loops=1)
      ->  Index Scan using allocations_charge_id_idx on allocations a (rows=1 loops=22)

3. One month's trial balance: partition pruning
  October only:    3.87 ms, partitions read: journal_lines_2026_10
  all time:       57.32 ms, partitions read: 22
```

The second one matters most. `allocate()` runs that query on every payment, and without an index on `allocations(charge_id)` each charge in the lease scans the whole allocations table. Postgres doesn't index foreign keys automatically, which is easy to forget.

## How it's put together

```text
migrations/
  001_core.sql        properties, units, tenants, leases (exclusion constraint)
  002_ledger.sql      accounts, journal entries, monthly partitioned lines, balance check, post_entry()
  003_billing.sql     charges, payments, allocations; bill_rent(), record_payment(), reverse_payment(), assess_late_fees()
  004_audit.sql       audit_log and the JSONB diff trigger
  005_security.sql    property_manager role and row-level security policies
  006_reports.sql     lease_balances, aged_receivables(), trial_balance(), gl_receivables(), rent_roll
ledger/
  db.py               connection, checksummed migration runner, table printing
  seed.py             sample portfolio, built through the real billing and payment functions
  __main__.py         the CLI
scripts/              demo.py, race.py, perf.py
tests/                12 tests against a real PostgreSQL database
```

## Decisions

- **Rules in the database, not the app.** A balance check in Python only protects the Python path. A deferred constraint trigger protects every path, including a Power BI service account or a manual fix at 11 pm.
- **Append-only money tables.** Correcting a payment by editing it destroys the evidence of what happened. A reversing entry keeps both, and the audit trail shows who did it.
- **The sub-ledger has to tie to the GL.** The aging report is computed from charges and allocations, the GL from journal lines. They're built separately on purpose, so `reconcile` comparing them actually proves something.
- **Lock the lease, not the table.** `select ... for update` on one lease row serializes payments for that tenant only. Everyone else keeps working.
- **Plain SQL migrations.** No ORM. Each file runs in a transaction and is recorded with its SHA-256, so the database can always say exactly which version of each file built it.
- **One entry per charge.** Simpler to read and to reverse than batching a whole month into one entry, at the cost of more rows.

## What I learned

- **A constraint that spans rows needs to run at commit.** "Debits equal credits" can't be a CHECK, because the first line of an entry is always unbalanced on its own. A deferred constraint trigger waits until the whole entry is there.
- **Exclusion constraints solve problems unique indexes can't.** Overlapping date ranges looked like something I'd have to check in code. One line with `btree_gist` and the database refuses it, even with two sessions inserting at once.
- **A check is not a lock.** My allocation check caught the race in the demo, but only because the timing worked out. I had to understand why it could still miss, and that the real fix was making the second payment wait.
- **Row-level security has edges.** Views run as their owner by default, so a plain view would show a manager every property; `security_invoker` fixes that. Materialized views ignore RLS entirely, so the rent roll isn't granted to managers. And my aging function first read the reversal date from the journal, which managers can't see, so it failed for them until I stored the date on the payment.
- **Postgres doesn't index foreign keys for you.** The allocations lookup went from 73 ms to 0.12 ms with one index, and it runs on every payment.
- **Partitioning is mostly about the queries you don't run.** The October trial balance reads one partition out of 22. The catch is that every partition needs to exist before a row arrives, so `post_entry()` creates the month if it's missing.
- **Order matters in batch jobs.** Reading a tenant statement end to end showed a late fee dated a month late. In the sample data the September bounce was being processed after that month's late fee run, so the fee only appeared in October. The tests passed; the statement looked wrong.
- **Reconciliation is a good test.** The aging report and the GL are built from different tables, so comparing them lease by lease checks the whole flow at once. It runs in CI and in the demo.

## Limitations

- One currency, no taxes, and a single late fee rule for every lease.
- Payments are matched to a lease by the person entering them. A real system would import bank files and match them.
- Partitions are created on demand; a production setup would create them ahead of time and detach old ones for archiving.
- The managers in the demo are database roles used with `set role`. In practice they'd be login roles mapped from the company's directory.
