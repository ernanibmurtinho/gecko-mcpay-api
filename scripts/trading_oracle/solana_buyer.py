"""Phase 7 — Solana-side x402 buyer for paysh providers.

Counterpart to ``run.py::_LiveX402PaidRequester`` (the EVM buyer used for
twit.sh + Bazaar listings on Base mainnet). The wire dance is identical
shape (GET → 402 → sign → GET) but the signing path is fundamentally
different: Solana x402 is **not** EIP-3009. The buyer signs an SPL
``TransferChecked`` instruction inside a partially-signed
``VersionedTransaction``; the facilitator fills in the fee-payer slot
and submits to the network.

Implementation choice: **Python-native via the official ``x402[svm]`` SDK**
(``x402.mechanisms.svm.exact.client.ExactSvmScheme`` +
``x402.mechanisms.svm.signers.KeypairSigner``). The SDK is already pulled
in by ``gecko-core`` / ``gecko-mcp`` ``pyproject.toml`` so no new dep is
required, and it owns all the Solana-specific plumbing (mint metadata
fetch, ATA derivation, MessageV0 compile, ed25519 signing). A Node
sidecar via ``@x402/fetch`` was the documented fallback in the ticket;
we did not need it because the SDK exposes ``ExactSvmScheme`` directly
and the actual ``accepts[]`` shape observed in the wild
(probed against ``https://x402.api.agentmail.to/v0/inboxes`` 2026-05-08)
matches the SDK's expected ``PaymentRequirements`` 1:1, including the
mandatory ``extra.feePayer`` field.

Real wire-observed Solana ``accepts[]`` entry::

    {
      "scheme": "exact",
      "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
      "amount": "0",
      "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
      "payTo": "7r4e5dwNS68MDaxbw7N8jbzHq7RCMBp9z6smHFH4NXWw",
      "maxTimeoutSeconds": 300,
      "extra": {"feePayer": "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"}
    }

Why this lives in ``scripts/`` and not ``packages/gecko-core/``: per the
Phase 6 + Phase 7 ticket scope, all live-buyer wiring stays at the CLI
layer until a second caller appears (CLAUDE.md "split when there are 2
callers"). The seller-side ``cdp_x402_client`` in core is intentionally
not modified — Phase 7's hard guardrail.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CAIP-2 prefix recognition.
# ---------------------------------------------------------------------------


def _is_solana_caip2(network: str) -> bool:
    """True for any ``solana:<genesis>`` CAIP-2 identifier or v1 names.

    paysh sellers advertise mainnet as ``solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp``;
    the SDK also accepts the v1 names ``solana``, ``solana-devnet``,
    ``solana-testnet``. Both must route to the Solana branch of the buyer.
    """
    if not network:
        return False
    if network.startswith("solana:"):
        return True
    return network in ("solana", "solana-mainnet", "solana-devnet", "solana-testnet")


def is_solana_network_env(value: str | None) -> bool:
    """Apply the same Solana detection to a raw ``X402_NETWORK`` env value."""
    if not value:
        return False
    # Accept both ``solana-mainnet`` (CLI-style) and ``solana:<hash>`` (CAIP-2).
    return _is_solana_caip2(value) or value.startswith("solana-")


# ---------------------------------------------------------------------------
# accepts[] selection.
# ---------------------------------------------------------------------------


def pick_solana_accepts_entry(accepts: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Return the first Solana entry from a multi-network ``accepts[]`` list.

    paysh sellers advertise EVM (Base, Avalanche, X-Layer) AND Solana
    options in one 402 challenge. The EVM buyer in ``run.py`` blindly picks
    ``accepts[0]``; on the Solana branch we must filter to the Solana
    entry instead. Raises ``RuntimeError`` when no Solana option is
    advertised — the listing is EVM-only and the buyer should fall back
    or fail loudly rather than silently sign against the wrong network.
    """
    for entry in accepts:
        net = str(entry.get("network", ""))
        if _is_solana_caip2(net):
            return entry
    raise RuntimeError(
        f"402 challenge advertised no Solana network; got "
        f"{[a.get('network') for a in accepts]!r}. Cannot route through "
        "the Solana buyer."
    )


# ---------------------------------------------------------------------------
# Signer + payload construction (thin wrappers over x402[svm]).
# ---------------------------------------------------------------------------


def load_solana_signer(private_key_b58: str) -> Any:
    """Load a ``KeypairSigner`` from a base58-encoded Solana keypair string.

    The base58 form is the standard ``solana-keygen`` export format
    (88-character string carrying both private + public bytes). ``solders``
    raises ``ValueError`` on bad input, which we let bubble — there is no
    safe fallback for an unparseable wallet.
    """
    from x402.mechanisms.svm.signers import KeypairSigner

    return KeypairSigner.from_base58(private_key_b58)


def build_solana_payment_payload(
    *,
    accepts_entry: Mapping[str, Any],
    signer: Any,
    resource_url: str,
    rpc_url: str | None = None,
) -> Any:
    """Construct a signed v2 ``PaymentPayload`` for one Solana ``accepts[]`` entry.

    Builds ``PaymentRequirements`` from the seller's ``accepts[]`` row,
    signs an SPL ``TransferChecked`` via ``ExactSvmScheme.create_payment_payload``,
    wraps the inner payload in a v2 ``PaymentPayload`` envelope. The
    returned object is a Pydantic model — encode at the HTTP boundary
    via ``model_dump_json(by_alias=True, exclude_none=True)`` to match
    the canonical x402 SDK encoding (``run.py::_encode_payment_signature_canonical``).
    """
    from x402.mechanisms.svm.exact.client import ExactSvmScheme
    from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo

    requirements = PaymentRequirements(
        scheme=str(accepts_entry.get("scheme", "exact")),
        network=str(accepts_entry["network"]),
        asset=str(accepts_entry["asset"]),
        # ``maxAmountRequired`` is the v2 field; older sellers send ``amount``.
        amount=str(accepts_entry.get("maxAmountRequired") or accepts_entry.get("amount") or "0"),
        pay_to=str(accepts_entry["payTo"]),
        max_timeout_seconds=int(accepts_entry.get("maxTimeoutSeconds", 300)),
        # ``extra.feePayer`` is REQUIRED for SVM — the facilitator's
        # fee-payer pubkey. Without it ``ExactSvmScheme`` raises
        # ``ValueError("feePayer is required ...")`` before any RPC call.
        extra=dict(accepts_entry.get("extra") or {}),
    )

    scheme = ExactSvmScheme(signer=signer, rpc_url=rpc_url)
    inner_payload = scheme.create_payment_payload(requirements)

    return PaymentPayload(
        x402_version=2,
        accepted=requirements,
        payload=inner_payload,
        resource=ResourceInfo(url=resource_url) if resource_url else None,
    )


# ---------------------------------------------------------------------------
# The PaidRequester adapter (mirrors _LiveX402PaidRequester surface).
# ---------------------------------------------------------------------------


class LiveSolanaPaidRequester:
    """Solana variant of ``_LiveX402PaidRequester``.

    Same protocol surface (``set_listing_context`` + ``request``), so
    ``_charge_and_fetch`` in ``run.py`` doesn't care which one it has.
    The only behavioral differences:

      1. Picks the Solana entry from ``accepts[]`` (not the first).
      2. Builds the payment payload via ``ExactSvmScheme`` + ed25519
         signing (not EIP-3009 / EvmAccount).
      3. Reads ``tx_signature`` from ``PAYMENT-RESPONSE`` (a real Solana
         transaction signature, base58) on success.

    Per-call hard limit + settled-amount parsing are reused as-is by
    importing ``run.py`` helpers. Method/body resolution (the
    multi-method registry) is identical — paysh providers are mostly
    GET-with-?q=, and POST listings work the same way over Solana.
    """

    def __init__(
        self,
        *,
        payer_private_key_b58: str,
        payer_address: str,
        network: str,
        per_call_hard_limit_usd: Decimal = Decimal("0.10"),
        rpc_url: str | None = None,
    ) -> None:
        self._signer = load_solana_signer(payer_private_key_b58)
        # Defensive: validate the declared address matches the keypair's
        # pubkey. A mismatch would silently sign with the wrong account
        # and either fail at the facilitator or — worse — drain a
        # different wallet than the operator expected.
        signer_address = str(self._signer.address)
        if payer_address and payer_address != signer_address:
            raise ValueError(
                f"GECKO_SOLANA_WALLET_ADDRESS={payer_address!r} does not match "
                f"the public key derived from GECKO_SOLANA_WALLET_PRIVATE_KEY "
                f"({signer_address!r}). Refusing to run — the env is misconfigured."
            )
        self._payer_address = signer_address
        self._network = network
        self._per_call_hard_limit_usd = per_call_hard_limit_usd
        self._rpc_url = rpc_url
        self._current_endpoints: list[Mapping[str, Any]] = []
        self._current_service_id: str = ""

    def set_listing_context(
        self,
        *,
        service_id: str,
        endpoints: Sequence[Mapping[str, Any]],
    ) -> None:
        """Mirrors ``_LiveX402PaidRequester.set_listing_context``."""
        self._current_endpoints = [dict(ep) for ep in endpoints]
        self._current_service_id = service_id

    async def request(
        self,
        *,
        url: str,
        query: str,
        max_cost_usd: float,
        timeout_seconds: float,
    ) -> Any:
        # Lazy imports — keep this module importable in dry-run mode
        # without dragging in httpx / gecko-core.
        import httpx
        from gecko_core.sources.paysh_live import PaidResponse

        # Reuse the EVM buyer's helper functions for header parsing,
        # canonical encoding, and method/body resolution. They are
        # network-agnostic (just byte-level header munging + a registry
        # lookup). ``run.py`` is loaded under several aliases depending
        # on entry point: ``__main__`` (CLI), ``trading_oracle_run``
        # (probe_service), ``trading_oracle_run_v2`` (some tests). Try
        # each in order so this works in every harness without
        # introducing a new sys.path mutation.
        run_mod = (
            sys.modules.get("trading_oracle_run")
            or sys.modules.get("__main__")
            or sys.modules.get("trading_oracle_run_v2")
        )
        if run_mod is None or not hasattr(run_mod, "_build_paid_request_headers"):
            raise RuntimeError(
                "Solana buyer cannot locate the trading-oracle run module. "
                "Expected it under sys.modules as 'trading_oracle_run', "
                "'__main__', or 'trading_oracle_run_v2'."
            )
        _build_paid_request_headers = run_mod._build_paid_request_headers
        _check_advertised_within_limit = run_mod._check_advertised_within_limit
        _extract_payment_required = run_mod._extract_payment_required
        _extract_settled_amount = run_mod._extract_settled_amount
        _is_blocked_endpoint_url = run_mod._is_blocked_endpoint_url
        _substitute_url_template = run_mod._substitute_url_template

        url = _substitute_url_template(url, wallet_address=self._payer_address)

        # Resolve method + body from the per-service registry. paysh
        # providers are mostly GET-with-?q= legacy REST; some specialty
        # listings (chat completions) are POST with a JSON body.
        url, method, body = self._build_request_for_service(
            url=url,
            prompt=query,
            blocklist_check=_is_blocked_endpoint_url,
        )

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            params: dict[str, str] = {"q": query} if method == "GET" else {}
            if method == "GET":
                probe = await client.get(url, params=params)
            else:
                probe = await client.post(
                    url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                )

            if probe.status_code == 200:
                # Free endpoint — degrade gracefully (mirrors EVM path).
                text = probe.text
                content_type = probe.headers.get("content-type", "text/plain")
                resp_body: Any = probe.json() if "json" in content_type else text
                return PaidResponse(
                    status_code=200,
                    cost_usd=0.0,
                    response_body=resp_body,
                    response_text=text,
                    content_type=content_type,
                    tx_signature=None,
                    response_sha=hashlib.sha256(text.encode()).hexdigest(),
                    headers=dict(probe.headers),
                )
            if probe.status_code != 402:
                raise RuntimeError(
                    f"expected 402 challenge from {url!r}, got {probe.status_code}: "
                    f"{probe.text[:256]!r}"
                )

            challenge = _extract_payment_required(probe)
            accepts = challenge.get("accepts") or []
            if not accepts:
                raise RuntimeError(f"402 from {url!r} carried empty accepts[]")

            # Filter to the Solana entry — paysh sellers also advertise
            # Base/Avalanche/X-Layer in the same list.
            chosen = pick_solana_accepts_entry(accepts)
            advertised_usd = _check_advertised_within_limit(
                [chosen],
                self._per_call_hard_limit_usd,
            )
            log.info(
                "x402-svm 402 from %s: advertised max $%.4f network=%s feePayer=%s",
                url,
                float(advertised_usd),
                chosen.get("network"),
                (chosen.get("extra") or {}).get("feePayer"),
            )

            sdk_payload = build_solana_payment_payload(
                accepts_entry=chosen,
                signer=self._signer,
                resource_url=url,
                rpc_url=self._rpc_url,
            )
            paid_headers = _build_paid_request_headers(sdk_payload)

            # Diagnostic — mirrors EVM path. Don't log full signature.
            sig_header = paid_headers["PAYMENT-SIGNATURE"]
            intent_id = f"trading-oracle-svm-{uuid4().hex[:16]}"
            log.info(
                "x402-svm paid retry %s %s: intent=%s sig_len=%d sig_prefix=%r",
                method,
                url,
                intent_id,
                len(sig_header),
                sig_header[:20],
            )

            if method == "GET":
                paid = await client.get(url, params=params, headers=paid_headers)
            else:
                paid = await client.post(
                    url,
                    json=body,
                    headers={**paid_headers, "Content-Type": "application/json"},
                )
            if paid.status_code != 200:
                raise RuntimeError(
                    f"paid {method} {url!r} returned {paid.status_code}: {paid.text[:256]!r}"
                )

            # Pull the on-chain tx signature from PAYMENT-RESPONSE so the
            # downstream economics ledger / audit trail can dedupe + verify
            # via getTransaction. Solana facilitators put a base58 signature
            # in ``transaction``; some put it in ``txHash``.
            tx_signature: str | None = None
            settle_header = (
                paid.headers.get("PAYMENT-RESPONSE")
                or paid.headers.get("X-Payment-Response")
                or paid.headers.get("X-PAYMENT-RESPONSE")
            )
            if settle_header:
                try:
                    decoded = base64.b64decode(settle_header).decode("utf-8")
                    settle = json.loads(decoded)
                    tx_signature = (
                        settle.get("transaction")
                        or settle.get("txHash")
                        or settle.get("transactionHash")
                    )
                except Exception:
                    tx_signature = None

            text = paid.text
            content_type = paid.headers.get("content-type", "text/plain")
            try:
                resp_body = paid.json() if "json" in content_type else text
            except Exception:
                resp_body = text

            settled_usd = _extract_settled_amount(paid.headers, fallback_usd=advertised_usd)
            log.info(
                "x402-svm settled %s: actual=$%.6f advertised_max=$%.4f tx=%s",
                url,
                float(settled_usd),
                float(advertised_usd),
                tx_signature,
            )
            return PaidResponse(
                status_code=200,
                cost_usd=float(settled_usd),
                response_body=resp_body,
                response_text=text,
                content_type=content_type,
                tx_signature=tx_signature,
                response_sha=hashlib.sha256(text.encode()).hexdigest(),
                headers=dict(paid.headers),
            )

    def _build_request_for_service(
        self,
        *,
        url: str,
        prompt: str,
        blocklist_check: Any,
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Resolve (url, method, body) — mirrors EVM path's helper.

        Kept inline (not delegated to ``_LiveX402PaidRequester``) so the
        Solana path doesn't depend on instantiating an EVM requester just
        to reuse its registry lookup. The lookup itself is in
        ``service_call_specs.find_spec_for`` which is network-agnostic.
        """
        from urllib.parse import urlparse

        from service_call_specs import find_spec_for  # type: ignore[import-not-found]

        if self._current_endpoints:
            endpoints_for_lookup: list[Mapping[str, Any]] = [
                ep for ep in self._current_endpoints if not blocklist_check(str(ep.get("url", "")))
            ]
            service_id = self._current_service_id or (urlparse(url).hostname or "")
        else:
            endpoints_for_lookup = [{"url": url, "method": "POST"}]
            service_id = urlparse(url).hostname or ""

        if not endpoints_for_lookup:
            return url, "GET", None

        spec, ep = find_spec_for(service_id, endpoints_for_lookup)
        if spec is None or ep is None or spec.body_builder is None:
            return url, "GET", None
        chosen_url = str(ep.get("url") or url)
        body = spec.body_builder(prompt, {})
        return chosen_url, spec.method, body
