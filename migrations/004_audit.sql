-- Who changed what. Lease terms and payment reversals are the changes auditors ask about.

create table audit_log (
    audit_id    bigint generated always as identity primary key,
    logged_at   timestamptz not null default now(),
    table_name  text not null,
    op          text not null,
    row_key     text not null,
    old_row     jsonb,
    new_row     jsonb,
    changed     jsonb,          -- only the columns that changed, as {"col": [old, new]}
    changed_by  text not null default current_user,
    txid        bigint not null default txid_current()
);

create function audit_row() returns trigger language plpgsql as $$
declare
    key_col text := tg_argv[0];
    o jsonb := case when tg_op <> 'INSERT' then to_jsonb(old) end;
    n jsonb := case when tg_op <> 'DELETE' then to_jsonb(new) end;
    diff jsonb;
begin
    if tg_op = 'UPDATE' then
        select jsonb_object_agg(k, jsonb_build_array(o->k, n->k)) into diff
        from jsonb_object_keys(n) as k where o->k is distinct from n->k;
        if diff is null then
            return null;                        -- nothing really changed
        end if;
    end if;
    insert into audit_log (table_name, op, row_key, old_row, new_row, changed)
    values (tg_table_name, lower(tg_op), coalesce(n, o)->>key_col, o, n, diff);
    return null;
end $$;

create trigger audit after insert or update or delete on leases
    for each row execute function audit_row('lease_id');
create trigger audit after update on payments
    for each row execute function audit_row('payment_id');
create trigger audit after update or delete on tenants
    for each row execute function audit_row('tenant_id');

create trigger no_edits before update or delete on audit_log
    for each row execute function ledger_is_append_only();
