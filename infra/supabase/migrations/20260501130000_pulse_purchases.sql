-- S14-PULSE-02 — pulse 12-pack prepay ledger.
--
-- One row per purchase. A wallet can hold multiple unexpired packs; the
-- decrement path picks the oldest pack with remaining_calls > 0 (so older
-- packs expire naturally even if the wallet keeps buying new ones).
--
-- Pricing context:
--   * per-call SKU:  $0.50  (PULSE_CALL_PRICE)
--   * 12-pack SKU:   $5.40  (PULSE_12PACK_PRICE)  -> $0.45/call (10% bulk)
--
-- The Python ledger lives in
-- ``packages/gecko-core/src/gecko_core/payments/pulse_credits.py``. In stub
-- mode the ledger is in-memory; this table is the live-mode store.

create table if not exists pulse_purchases (
    id uuid primary key default gen_random_uuid(),
    user_wallet text not null,
    remaining_calls integer not null check (remaining_calls >= 0),
    purchased_at timestamptz not null default now(),
    expires_at timestamptz not null,
    tx_signature text,
    created_at timestamptz not null default now()
);

create index if not exists pulse_purchases_wallet_active_idx
    on pulse_purchases (user_wallet, expires_at desc)
    where remaining_calls > 0;

create index if not exists pulse_purchases_wallet_purchased_at_idx
    on pulse_purchases (user_wallet, purchased_at asc);

comment on table pulse_purchases is
    'S14-PULSE-02 — 12-pack prepay credits ledger. One row per purchase.';
comment on column pulse_purchases.remaining_calls is
    'Calls left on this pack. Decremented atomically by the pulse engine.';
