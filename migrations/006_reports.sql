-- Reports. Views use security_invoker so row-level security applies to whoever is reading;
-- a plain view runs as its owner and would quietly show a manager every property.

create view lease_balances with (security_invoker = true) as
select l.lease_id, u.property_id, u.unit_no, t.name as tenant,
       coalesce(c.charged, 0) as charged,
       coalesce(p.paid, 0)    as paid,
       coalesce(c.charged, 0) - coalesce(p.paid, 0) as balance
from leases l
join units u using (unit_id)
join tenants t using (tenant_id)
left join (select lease_id, sum(amount) as charged from charges group by lease_id) c using (lease_id)
left join (select lease_id, sum(amount) as paid from payments where reversed_entry_id is null group by lease_id) p using (lease_id);

grant select on lease_balances to property_manager;

-- Aged receivables as of a date. Open amount per charge, bucketed by days past due, minus any
-- unused payments (tenant credit). The total per lease equals that lease's A/R balance in the GL.
create function aged_receivables(p_as_of date)
returns table (property_id text, lease_id text, tenant text,
               current_0_30 numeric, days_31_60 numeric, days_61_90 numeric, over_90 numeric,
               credit numeric, total numeric)
language sql stable as $$
    with valid_payments as (
        select p.payment_id, p.lease_id, p.amount
        from payments p
        where p.received_on <= p_as_of and (p.reversed_on is null or p.reversed_on > p_as_of)
    ),
    posted as (
        select c.* from charges c where c.posted_on <= p_as_of
    ),
    open_charges as (
        select c.lease_id, p_as_of - c.due_date as days_late,
               c.amount - coalesce(sum(a.amount) filter (where vp.payment_id is not null), 0) as open
        from posted c
        left join allocations a on a.charge_id = c.charge_id
        left join valid_payments vp on vp.payment_id = a.payment_id
        group by c.charge_id, c.lease_id, c.due_date, c.amount
    ),
    credits as (
        select vp.lease_id, sum(vp.amount - coalesce(used.amt, 0)) as unused
        from valid_payments vp
        left join lateral (select sum(a.amount) as amt from allocations a
                           join posted c on c.charge_id = a.charge_id
                           where a.payment_id = vp.payment_id) used on true
        group by vp.lease_id
    ),
    per_lease as (
        select lease_id,
               coalesce(sum(open) filter (where days_late <= 30), 0)               as b0,
               coalesce(sum(open) filter (where days_late between 31 and 60), 0)   as b1,
               coalesce(sum(open) filter (where days_late between 61 and 90), 0)   as b2,
               coalesce(sum(open) filter (where days_late > 90), 0)                as b3
        from open_charges group by lease_id
    )
    select u.property_id, l.lease_id, t.name, pl.b0, pl.b1, pl.b2, pl.b3,
           coalesce(cr.unused, 0), pl.b0 + pl.b1 + pl.b2 + pl.b3 - coalesce(cr.unused, 0)
    from per_lease pl
    join leases l using (lease_id)
    join units u using (unit_id)
    join tenants t using (tenant_id)
    left join credits cr using (lease_id)
    where pl.b0 + pl.b1 + pl.b2 + pl.b3 - coalesce(cr.unused, 0) <> 0
    order by u.property_id, l.lease_id
$$;

create function trial_balance(p_from date, p_to date)
returns table (account text, name text, debits numeric, credits numeric, net numeric)
language sql stable as $$
    select a.code, a.name, coalesce(sum(j.debit), 0), coalesce(sum(j.credit), 0),
           coalesce(sum(j.debit), 0) - coalesce(sum(j.credit), 0)
    from accounts a
    left join journal_lines j on j.account = a.code and j.entry_date between p_from and p_to
    group by a.code, a.name
    order by a.code
$$;

-- A/R per lease from the general ledger, to reconcile the sub-ledger against.
create function gl_receivables(p_as_of date)
returns table (lease_id text, balance numeric)
language sql stable as $$
    select lease_id, sum(debit) - sum(credit)
    from journal_lines
    where account = '1100' and entry_date <= p_as_of
    group by lease_id
    having sum(debit) - sum(credit) <> 0
$$;

-- One row per unit, occupied or not. Refreshed nightly; the unique index lets
-- REFRESH ... CONCURRENTLY rebuild it without blocking people reading it.
create materialized view rent_roll as
select u.property_id, u.unit_id, u.unit_no, u.sqft,
       l.lease_id, t.name as tenant, l.start_date, l.end_date - 1 as last_day, l.monthly_rent,
       round(l.monthly_rent * 12 / u.sqft, 2) as rent_psf_year,
       coalesce(b.balance, 0) as balance,
       case when l.lease_id is null then 'vacant'
            when l.end_date <= current_date + 90 then 'expiring'
            else 'occupied' end as status
from units u
left join leases l on l.unit_id = u.unit_id and daterange(l.start_date, l.end_date, '[)') @> current_date
left join tenants t on t.tenant_id = l.tenant_id
left join lease_balances b on b.lease_id = l.lease_id;

create unique index on rent_roll (unit_id);

-- Not granted to property_manager: row-level security doesn't apply to materialized views,
-- so a manager would see every property in it.
