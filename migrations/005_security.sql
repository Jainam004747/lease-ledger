-- Property managers can read their own properties' tenants and balances, and nothing else.
-- The filtering happens in the database (row-level security), so every tool that connects
-- as a manager gets the same rules, including ad hoc SQL and Power BI.

do $$
begin
    if not exists (select from pg_roles where rolname = 'property_manager') then
        create role property_manager nologin;
    end if;
end $$;

create table manager_properties (
    role_name    text not null,
    property_id  text not null references properties,
    primary key (role_name, property_id)
);

grant usage on schema public to property_manager;
grant select on properties, units, tenants, leases, charges, payments, allocations, manager_properties
    to property_manager;

alter table manager_properties enable row level security;
alter table properties  enable row level security;
alter table units       enable row level security;
alter table tenants     enable row level security;
alter table leases      enable row level security;
alter table charges     enable row level security;
alter table payments    enable row level security;
alter table allocations enable row level security;

-- Each policy leans on the one before it: units -> leases -> charges/payments -> allocations.
create policy own_rows on manager_properties for select using (role_name = current_user);
create policy managed on properties  for select using (property_id in (select property_id from manager_properties));
create policy managed on units       for select using (property_id in (select property_id from manager_properties));
create policy managed on leases      for select using (unit_id in (select unit_id from units));
create policy managed on tenants     for select using (tenant_id in (select tenant_id from leases));
create policy managed on charges     for select using (lease_id in (select lease_id from leases));
create policy managed on payments    for select using (lease_id in (select lease_id from leases));
create policy managed on allocations for select using (charge_id in (select charge_id from charges));
