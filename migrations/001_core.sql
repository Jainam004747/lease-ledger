-- Properties, units, tenants and leases.

create extension if not exists btree_gist;

create table properties (
    property_id  text primary key check (property_id ~ '^P\d{3}$'),
    name         text not null,
    city         text not null
);

create table units (
    unit_id      bigint generated always as identity primary key,
    property_id  text not null references properties,
    unit_no      text not null,
    sqft         int  not null check (sqft > 0),
    unique (property_id, unit_no)
);

create table tenants (
    tenant_id    bigint generated always as identity primary key,
    name         text not null,
    email        text
);

create table leases (
    lease_id      text primary key check (lease_id ~ '^L-\d{4,}$'),
    unit_id       bigint not null references units,
    tenant_id     bigint not null references tenants,
    start_date    date not null,
    end_date      date not null,                        -- exclusive
    monthly_rent  numeric(12,2) not null check (monthly_rent > 0),
    due_day       int not null default 1 check (due_day between 1 and 28),
    check (end_date > start_date),
    -- Two leases on the same unit can't overlap in time. A unique index can't express this;
    -- an exclusion constraint compares every pair of rows with = on the unit and && on the dates.
    constraint no_overlapping_leases
        exclude using gist (unit_id with =, daterange(start_date, end_date, '[)') with &&)
);

create index on leases (tenant_id);
