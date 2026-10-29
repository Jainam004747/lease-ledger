-- Tenant sub-ledger: charges, payments, and which payment paid which charge.

create table charges (
    charge_id   bigint generated always as identity primary key,
    lease_id    text not null references leases,
    kind        text not null check (kind in ('rent', 'late_fee')),
    period      date not null check (extract(day from period) = 1),
    posted_on   date not null,
    due_date    date not null,
    amount      numeric(12,2) not null check (amount > 0),
    entry_id    bigint not null references journal_entries,
    unique (lease_id, kind, period)                     -- billing twice for a month is impossible
);

create table payments (
    payment_id         bigint generated always as identity primary key,
    lease_id           text not null references leases,
    received_on        date not null,
    amount             numeric(12,2) not null check (amount > 0),
    reference          text not null,
    entry_id           bigint not null references journal_entries,
    reversed_entry_id  bigint references journal_entries,  -- set when a cheque bounces
    reversed_on        date,
    reversal_reason    text,
    unique (lease_id, reference)                         -- the same cheque can't be entered twice
);

create table allocations (
    payment_id  bigint not null references payments,
    charge_id   bigint not null references charges,
    amount      numeric(12,2) not null check (amount > 0),
    primary key (payment_id, charge_id)
);

create index on charges (lease_id, due_date);
create index on payments (lease_id, received_on);
create index on allocations (charge_id);

-- Payments are append-only too, except for marking one as reversed (once).
create function payments_guard() returns trigger language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'payments are append-only: reverse the payment instead' using errcode = 'insufficient_privilege';
    end if;
    if old.reversed_entry_id is not null
       or (to_jsonb(new) - 'reversed_entry_id' - 'reversed_on' - 'reversal_reason') <> (to_jsonb(old) - 'reversed_entry_id' - 'reversed_on' - 'reversal_reason') then
        raise exception 'payment % can only be changed by reversing it once', old.payment_id using errcode = 'insufficient_privilege';
    end if;
    return new;
end $$;
create trigger payments_guard before update or delete on payments for each row execute function payments_guard();

create trigger no_edits before update or delete on charges for each row execute function ledger_is_append_only();

-- No charge can receive more than its amount, and no payment can give more than it has.
create function check_allocation_limits() returns trigger language plpgsql as $$
declare
    over text;
begin
    select format('charge %s: allocated %s of %s', c.charge_id, sum(a.amount), c.amount) into over
    from charges c join allocations a using (charge_id)
    where c.charge_id = new.charge_id group by c.charge_id, c.amount having sum(a.amount) > c.amount;
    if over is null then
        select format('payment %s: allocated %s of %s', p.payment_id, sum(a.amount), p.amount) into over
        from payments p join allocations a using (payment_id)
        where p.payment_id = new.payment_id group by p.payment_id, p.amount having sum(a.amount) > p.amount;
    end if;
    if over is not null then
        raise exception 'over-allocated %', over using errcode = 'check_violation';
    end if;
    return null;
end $$;
create constraint trigger allocation_limits after insert or update on allocations
    deferrable initially deferred for each row execute function check_allocation_limits();

-- Applies every unused payment amount on a lease to its open charges, oldest charge first.
-- Callers must hold the lease row lock (select ... for update) so two sessions can't
-- allocate the same open balance at the same time.
create function allocate(p_lease text) returns int language plpgsql as $$
declare
    pay record; chg record; left_to_apply numeric; take numeric; n int := 0;
begin
    for pay in
        select p.payment_id, p.amount - coalesce((select sum(a.amount) from allocations a where a.payment_id = p.payment_id), 0) as unused
        from payments p
        where p.lease_id = p_lease and p.reversed_entry_id is null
        order by p.received_on, p.payment_id
    loop
        continue when pay.unused <= 0;
        left_to_apply := pay.unused;
        for chg in
            select c.charge_id, c.amount - coalesce((select sum(a.amount) from allocations a where a.charge_id = c.charge_id), 0) as open
            from charges c
            where c.lease_id = p_lease
            order by c.due_date, c.charge_id
        loop
            continue when chg.open <= 0;
            take := least(left_to_apply, chg.open);
            insert into allocations values (pay.payment_id, chg.charge_id, take)
                on conflict (payment_id, charge_id) do update set amount = allocations.amount + excluded.amount;
            n := n + 1;
            left_to_apply := left_to_apply - take;
            exit when left_to_apply = 0;
        end loop;
    end loop;
    return n;
end $$;

-- Bills rent for one month. Partial months are prorated by day. Running it twice does nothing
-- the second time, so a failed batch can simply be re-run.
create function bill_rent(p_period date) returns int language plpgsql as $$
declare
    l record; e bigint; n int := 0;
    nxt date := (p_period + interval '1 month')::date;
    days int := nxt - p_period;
    amt numeric;
begin
    if extract(day from p_period) <> 1 then
        raise exception 'period must be the first of a month, got %', p_period;
    end if;
    for l in
        select lease_id, monthly_rent, due_day, start_date, end_date
        from leases
        where daterange(start_date, end_date, '[)') && daterange(p_period, nxt, '[)')
          and not exists (select 1 from charges c where c.lease_id = leases.lease_id and c.kind = 'rent' and c.period = p_period)
        order by lease_id
        for update
    loop
        amt := round(l.monthly_rent * (least(l.end_date, nxt) - greatest(l.start_date, p_period)) / days, 2);
        e := post_entry(p_period, format('Rent %s %s', to_char(p_period, 'YYYY-MM'), l.lease_id), 'rent',
                        jsonb_build_array(jsonb_build_object('account', '1100', 'lease_id', l.lease_id, 'debit', amt),
                                          jsonb_build_object('account', '4000', 'lease_id', l.lease_id, 'credit', amt)));
        insert into charges (lease_id, kind, period, posted_on, due_date, amount, entry_id)
        values (l.lease_id, 'rent', p_period, p_period, greatest(p_period + l.due_day - 1, l.start_date), amt, e);
        perform allocate(l.lease_id);                    -- use any prepaid credit
        n := n + 1;
    end loop;
    return n;
end $$;

create function record_payment(p_lease text, p_amount numeric, p_on date, p_ref text) returns bigint language plpgsql as $$
declare
    e bigint; id bigint;
begin
    perform 1 from leases where lease_id = p_lease for update;   -- one payment per lease at a time
    if not found then
        raise exception 'lease % does not exist', p_lease;
    end if;
    e := post_entry(p_on, format('Payment %s on %s', p_ref, p_lease), 'payment',
                    jsonb_build_array(jsonb_build_object('account', '1000', 'debit', p_amount),
                                      jsonb_build_object('account', '1100', 'lease_id', p_lease, 'credit', p_amount)));
    insert into payments (lease_id, received_on, amount, reference, entry_id)
    values (p_lease, p_on, p_amount, p_ref, e) returning payment_id into id;
    perform allocate(p_lease);
    return id;
end $$;

-- A bounced cheque: undo the cash, put the balance back on the tenant, and re-allocate.
create function reverse_payment(p_payment bigint, p_on date, p_reason text) returns bigint language plpgsql as $$
declare
    p payments; e bigint;
begin
    select * into p from payments where payment_id = p_payment;
    if not found then
        raise exception 'payment % does not exist', p_payment;
    end if;
    perform 1 from leases where lease_id = p.lease_id for update;
    if p.reversed_entry_id is not null then
        raise exception 'payment % was already reversed', p_payment;
    end if;
    e := post_entry(p_on, format('Reversal of payment %s (%s): %s', p.reference, p.lease_id, p_reason), 'reversal',
                    jsonb_build_array(jsonb_build_object('account', '1100', 'lease_id', p.lease_id, 'debit', p.amount),
                                      jsonb_build_object('account', '1000', 'credit', p.amount)),
                    p.entry_id);
    update payments set reversed_entry_id = e, reversed_on = p_on, reversal_reason = p_reason where payment_id = p_payment;
    delete from allocations where payment_id = p_payment;
    perform allocate(p.lease_id);
    return e;
end $$;

-- Late fee on rent still unpaid p_grace days after it was due. One fee per lease per month.
create function assess_late_fees(p_as_of date, p_fee numeric default 50, p_grace int default 5) returns int language plpgsql as $$
declare
    r record; e bigint; n int := 0;
begin
    for r in
        select c.lease_id, c.period
        from charges c
        where c.kind = 'rent' and c.due_date + p_grace < p_as_of
          and c.amount > coalesce((select sum(a.amount) from allocations a
                                   join payments p using (payment_id)
                                   where a.charge_id = c.charge_id and p.received_on <= c.due_date + p_grace), 0)
          and not exists (select 1 from charges f where f.lease_id = c.lease_id and f.kind = 'late_fee' and f.period = c.period)
        order by c.lease_id, c.period
    loop
        perform 1 from leases where lease_id = r.lease_id for update;
        e := post_entry(p_as_of, format('Late fee %s %s', to_char(r.period, 'YYYY-MM'), r.lease_id), 'late_fee',
                        jsonb_build_array(jsonb_build_object('account', '1100', 'lease_id', r.lease_id, 'debit', p_fee),
                                          jsonb_build_object('account', '4100', 'lease_id', r.lease_id, 'credit', p_fee)));
        insert into charges (lease_id, kind, period, posted_on, due_date, amount, entry_id)
        values (r.lease_id, 'late_fee', r.period, p_as_of, p_as_of, p_fee, e);
        perform allocate(r.lease_id);
        n := n + 1;
    end loop;
    return n;
end $$;
