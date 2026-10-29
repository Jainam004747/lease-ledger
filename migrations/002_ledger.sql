-- Double-entry general ledger. Every money movement is a journal entry whose lines balance.

create table accounts (
    code  text primary key,
    name  text not null,
    kind  text not null check (kind in ('asset', 'liability', 'revenue'))
);

insert into accounts values
    ('1000', 'Cash',                'asset'),
    ('1100', 'Accounts receivable', 'asset'),
    ('4000', 'Rent revenue',        'revenue'),
    ('4100', 'Late fee revenue',    'revenue');

create table journal_entries (
    entry_id    bigint generated always as identity primary key,
    entry_date  date not null,
    memo        text not null,
    source      text not null check (source in ('rent', 'late_fee', 'payment', 'reversal')),
    reverses    bigint references journal_entries,
    created_by  text not null default current_user,
    created_at  timestamptz not null default now()
);

-- Lines are partitioned by month. Reports almost always ask for one period, and the planner
-- skips every partition outside it.
create table journal_lines (
    entry_id    bigint not null references journal_entries,
    entry_date  date   not null,
    line_no     int    not null,
    account     text   not null references accounts,
    lease_id    text   references leases,
    debit       numeric(12,2) not null default 0 check (debit  >= 0),
    credit      numeric(12,2) not null default 0 check (credit >= 0),
    check ((debit = 0) <> (credit = 0)),                  -- exactly one side per line
    primary key (entry_id, line_no, entry_date)
) partition by range (entry_date);

create index on journal_lines (lease_id, entry_date);
create index on journal_lines (account, entry_date);

create function ensure_month_partition(d date) returns void language plpgsql as $$
declare
    first date := date_trunc('month', d)::date;
    name  text := format('journal_lines_%s', to_char(first, 'YYYY_MM'));
begin
    if to_regclass(name) is null then
        execute format('create table %I partition of journal_lines for values from (%L) to (%L)',
                       name, first, (first + interval '1 month')::date);
    end if;
end $$;

-- An entry must balance. The check runs at commit (deferred), after all of its lines exist.
create function check_entry_balanced() returns trigger language plpgsql as $$
declare
    dr numeric; cr numeric; n int;
begin
    select sum(debit), sum(credit), count(*) into dr, cr, n
    from journal_lines where entry_id = new.entry_id and entry_date = new.entry_date;
    if n < 2 or dr <> cr then
        raise exception 'journal entry % does not balance: debits %, credits %, % line(s)', new.entry_id, dr, cr, n
            using errcode = 'check_violation';
    end if;
    return null;
end $$;

create constraint trigger entry_must_balance
    after insert on journal_lines
    deferrable initially deferred
    for each row execute function check_entry_balanced();

-- The ledger is append-only. A mistake is fixed with a reversing entry, never an edit.
create function ledger_is_append_only() returns trigger language plpgsql as $$
begin
    raise exception '% is append-only (% refused): post a reversing entry instead',
        regexp_replace(tg_table_name, '_\d{4}_\d{2}$', ''), lower(tg_op)
        using errcode = 'insufficient_privilege';
end $$;

create trigger no_edits before update or delete on journal_entries
    for each row execute function ledger_is_append_only();
create trigger no_edits before update or delete on journal_lines
    for each row execute function ledger_is_append_only();

-- Posts one entry. lines is a JSON array: [{"account": "1100", "lease_id": "L-0001", "debit": 1500}, ...]
create function post_entry(p_date date, p_memo text, p_source text, p_lines jsonb, p_reverses bigint default null)
returns bigint language plpgsql as $$
declare
    id bigint;
begin
    perform ensure_month_partition(p_date);
    insert into journal_entries (entry_date, memo, source, reverses)
    values (p_date, p_memo, p_source, p_reverses) returning entry_id into id;

    insert into journal_lines (entry_id, entry_date, line_no, account, lease_id, debit, credit)
    select id, p_date, ord, l->>'account', l->>'lease_id',
           coalesce((l->>'debit')::numeric, 0), coalesce((l->>'credit')::numeric, 0)
    from jsonb_array_elements(p_lines) with ordinality as t(l, ord);
    return id;
end $$;
