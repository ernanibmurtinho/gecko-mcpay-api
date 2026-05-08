from gecko_core.ingestion.trading_oracle.prompt import (
    SOLANA_DEFI_PROTOCOLS,
    TRADING_ORACLE_PROMPT,
    is_solana_defi_relevant,
)


def test_prompt_mentions_each_protocol():
    for proto in SOLANA_DEFI_PROTOCOLS:
        assert proto.lower() in TRADING_ORACLE_PROMPT.lower(), proto


def test_prompt_does_not_recommend_buy_sell():
    forbidden = ["buy ", "sell ", "long ", "short "]
    body = TRADING_ORACLE_PROMPT.lower()
    for v in forbidden:
        assert v not in body, f"prompt must not contain trade verb {v!r}"


def test_filter_accepts_solana_defi():
    assert (
        is_solana_defi_relevant(
            {
                "name": "Kamino Lend Snapshot",
                "description": "Daily TVL + APY for Kamino USDC reserves on Solana",
                "tags": ["solana", "lending", "kamino"],
            }
        )
        is True
    )


def test_filter_rejects_unrelated():
    assert (
        is_solana_defi_relevant(
            {
                "name": "Hotel Booking API",
                "description": "Search hotels via Ctrip",
                "tags": ["travel"],
            }
        )
        is False
    )


def test_filter_rejects_evm_only():
    assert (
        is_solana_defi_relevant(
            {
                "name": "Aave V3 USDC",
                "description": "Ethereum mainnet lending rate",
                "tags": ["ethereum", "defi", "aave"],
            }
        )
        is False
    )
