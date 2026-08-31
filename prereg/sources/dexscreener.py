"""Claims about newly listed tokens, from public data only.

THE DEFINITION, FIXED
---------------------
Two calls, and each resolves from the same public endpoint the claim was made
from, with no judgement left over:

  call=rug    within the horizon, the token's pooled liquidity in USD falls to
              20% or less of its value at claim time, OR the token stops
              returning any pair at all.
  call=holds  it does not.

Liquidity is read from the pair with the deepest pool, at
`GET https://api.dexscreener.com/latest/dex/tokens/<address>`.

This definition is deliberately boring, entirely mechanical, and frozen. A
record is worth exactly as much as the stability of the rule it was scored
under; a definition that gets adjusted after the results come in is worth
nothing, and anybody replaying the room would be able to see it move.

WHAT THE MODEL IS
-----------------
A handful of weighted, documented rules over public fields. It is not the
private pipeline that motivated this project and it does not pretend to be. Its
one virtue is that a reader can check every step of it, which is worth more here
than a better model nobody can audit.

It abstains. Publishing a call on every token would score well against a base
rate where most new tokens fail while carrying almost no information, and the
Brier score would show that. A claim only goes out when the model lands away
from the middle, and the confidence it publishes is the number it actually
computed, never rounded up.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from prereg.agent import ClaimDraft
from prereg.record import Claim

log = logging.getLogger("prereg.dexscreener")

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"

# The fraction of claim-time liquidity below which we call it a rug. Frozen.
RUG_LIQUIDITY_FRACTION = 0.20

# Chains we are willing to be scored on. Settlement uses the same endpoint for
# all of them, so this list is about having enough listings to be worth reading,
# not about capability.
CHAINS = ("solana", "base", "ethereum", "bsc", "arbitrum")

MIN_LIQUIDITY_USD = 5_000.0
MAX_PAIR_AGE_HOURS = 48.0
DEFAULT_HORIZON = timedelta(hours=72)

# Above this the model calls a rug, below its mirror it calls a hold, and in
# between it says nothing.
CALL_RUG_AT = 0.70
CALL_HOLDS_AT = 0.30

Fetch = Callable[[str], Any]


def http_json(url: str, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers={
        "User-Agent": "prereg/0.1 (+https://github.com/Bornoz/prereg)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


@dataclass(frozen=True)
class Snapshot:
    """The public state of a token at one moment, and the basis of a claim."""

    chain: str
    address: str
    symbol: str
    liquidity_usd: float
    fdv: float
    age_hours: float
    buys_24h: int
    sells_24h: int
    volume_24h: float
    price_change_24h: float

    def bundle(self) -> bytes:
        """Exactly what the evidence digest in the claim commits to."""
        return json.dumps({
            "chain": self.chain, "address": self.address, "symbol": self.symbol,
            "liquidity_usd": round(self.liquidity_usd, 2), "fdv": round(self.fdv, 2),
            "age_hours": round(self.age_hours, 3),
            "buys_24h": self.buys_24h, "sells_24h": self.sells_24h,
            "volume_24h": round(self.volume_24h, 2),
            "price_change_24h": round(self.price_change_24h, 4),
            "definition": "rug = liquidity <= 20% of this value, or no pair, within the horizon",
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def deepest_pair(payload: Any) -> dict[str, Any] | None:
    pairs = (payload or {}).get("pairs") or []
    with_liquidity = [
        p for p in pairs
        if isinstance(p, dict) and isinstance(p.get("liquidity"), dict)
        and _number(p["liquidity"].get("usd")) > 0
    ]
    if not with_liquidity:
        return None
    return max(with_liquidity, key=lambda p: _number(p["liquidity"].get("usd")))


def snapshot(chain: str, address: str, fetch: Fetch = http_json) -> Snapshot | None:
    try:
        payload = fetch(TOKEN_URL + urllib.parse.quote(address, safe=""))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        # A source that cannot see is a source with nothing to say. It never
        # becomes a message.
        log.info("no snapshot for %s/%s: %s", chain, address[:12], exc)
        return None

    pair = deepest_pair(payload)
    if pair is None:
        return None

    created = _number(pair.get("pairCreatedAt"))
    age_hours = _age_hours(created)
    txns = (pair.get("txns") or {}).get("h24") or {}
    return Snapshot(
        chain=str(pair.get("chainId") or chain),
        address=address,
        symbol=str((pair.get("baseToken") or {}).get("symbol") or "?")[:16],
        liquidity_usd=_number((pair.get("liquidity") or {}).get("usd")),
        fdv=_number(pair.get("fdv")),
        age_hours=age_hours,
        buys_24h=int(_number(txns.get("buys"))),
        sells_24h=int(_number(txns.get("sells"))),
        volume_24h=_number((pair.get("volume") or {}).get("h24")),
        price_change_24h=_number((pair.get("priceChange") or {}).get("h24")),
    )


def rug_probability(snap: Snapshot) -> tuple[float, list[str]]:
    """P(liquidity collapses within the horizon), with its reasons.

    Every term is listed so the arithmetic can be checked against the snapshot
    that the claim's evidence digest commits to. The weights are judgement, and
    the record will say soon enough whether the judgement was any good.
    """
    score = 0.45  # most freshly listed tokens do not survive; this is the prior
    why: list[str] = []

    ratio = snap.fdv / snap.liquidity_usd if snap.liquidity_usd > 0 else 0.0
    if ratio > 60:
        score += 0.22
        why.append(f"fdv/liq {ratio:.0f}x")
    elif ratio > 25:
        score += 0.12
        why.append(f"fdv/liq {ratio:.0f}x")
    elif ratio < 8:
        score -= 0.12
        why.append(f"fdv/liq only {ratio:.1f}x")

    if snap.liquidity_usd < 15_000:
        score += 0.14
        why.append(f"thin pool ${snap.liquidity_usd:,.0f}")
    elif snap.liquidity_usd > 150_000:
        score -= 0.16
        why.append(f"deep pool ${snap.liquidity_usd:,.0f}")

    trades = snap.buys_24h + snap.sells_24h
    if trades < 60:
        score += 0.10
        why.append(f"only {trades} trades")
    elif snap.sells_24h > snap.buys_24h * 1.8 and trades > 200:
        score += 0.12
        why.append(f"sells {snap.sells_24h} vs buys {snap.buys_24h}")
    elif snap.buys_24h > snap.sells_24h * 1.3 and trades > 400:
        score -= 0.10
        why.append("buy-weighted flow")

    churn = snap.volume_24h / snap.liquidity_usd if snap.liquidity_usd > 0 else 0.0
    if churn > 25:
        score += 0.10
        why.append(f"churn {churn:.0f}x pool")

    if snap.price_change_24h < -55:
        score += 0.10
        why.append(f"down {snap.price_change_24h:.0f}% already")

    return _clamp(score), why


class DexScreenerSource:
    """Turns the public new-listings feed into drafts the agent may sign."""

    def __init__(
        self,
        fetch: Fetch = http_json,
        horizon: timedelta = DEFAULT_HORIZON,
        chains: tuple[str, ...] = CHAINS,
        limit: int = 3,
    ) -> None:
        self.fetch = fetch
        self.horizon = horizon
        self.chains = chains
        self.limit = limit
        self._seen: set[str] = set()

    def pending(self) -> list[ClaimDraft]:
        try:
            profiles = self.fetch(PROFILES_URL)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            log.info("listings feed unavailable: %s", exc)
            return []
        if not isinstance(profiles, list):
            return []

        drafts: list[ClaimDraft] = []
        for entry in profiles:
            if len(drafts) >= self.limit:
                break
            if not isinstance(entry, dict):
                continue
            chain = str(entry.get("chainId") or "")
            address = str(entry.get("tokenAddress") or "")
            if chain not in self.chains or not address or address in self._seen:
                continue
            self._seen.add(address)

            draft = self.consider(chain, address)
            if draft is not None:
                drafts.append(draft)
        return drafts

    def consider(self, chain: str, address: str) -> ClaimDraft | None:
        snap = snapshot(chain, address, self.fetch)
        if snap is None:
            return None
        if snap.liquidity_usd < MIN_LIQUIDITY_USD:
            return None  # too thin to have a meaningful "80% gone" threshold
        if snap.age_hours > MAX_PAIR_AGE_HOURS:
            return None  # not a new listing any more

        probability, why = rug_probability(snap)
        if CALL_HOLDS_AT < probability < CALL_RUG_AT:
            log.info("abstaining on %s: p=%.2f is too close to the middle",
                     snap.symbol, probability)
            return None

        rug = probability >= CALL_RUG_AT
        return ClaimDraft(
            domain="dex-liquidity",
            subject=f"{snap.chain}:{snap.address}",
            call="rug" if rug else "holds",
            confidence=probability if rug else 1.0 - probability,
            horizon=self.horizon,
            evidence=snap.bundle(),
            text=(
                f"{snap.symbol} liq ${snap.liquidity_usd:,.0f} age {snap.age_hours:.0f}h; "
                + ", ".join(why[:4])
            )[:600],
        )


class DexScreenerResolver:
    """Settles a claim by reading the same endpoint it was made from."""

    def __init__(
        self,
        fetch: Fetch = http_json,
        evidence: Callable[[str], bytes | None] | None = None,
    ) -> None:
        self.fetch = fetch
        # The claim carries only a digest, so the claim-time measurement has to
        # come back from wherever the agent kept it.
        self.evidence = evidence or (lambda _claim_id: None)

    def resolve(self, claim: Claim) -> tuple[str, str, str] | None:
        from prereg.record import now

        expired = claim.deadline <= now()
        _domain, _, address = claim.subject.partition(":")
        snap = snapshot(_domain, address, self.fetch)

        if snap is None:
            # No pair at all. That is a collapse under the definition, and it is
            # decidable immediately -- there is nothing left to wait for.
            rugged = True
            proof = "no-pair"
            detail = "no pair returned for the token"
        else:
            baseline = self._baseline(claim)
            if baseline is None:
                if not expired:
                    return None
                return ("void", "no-baseline",
                        "claim-time liquidity was not recoverable, so the "
                        "threshold cannot be applied honestly")
            rugged = snap.liquidity_usd <= baseline * RUG_LIQUIDITY_FRACTION
            proof = f"liq-{snap.liquidity_usd:.0f}-of-{baseline:.0f}"
            detail = (
                f"liquidity ${snap.liquidity_usd:,.0f} against ${baseline:,.0f} at claim "
                f"({snap.liquidity_usd / baseline:.0%})"
            )
            # A "holds" call is only safe once the horizon is over; a "rug" call
            # can be settled the moment the threshold is crossed.
            if not rugged and not expired:
                return None

        hit = rugged if claim.call == "rug" else not rugged
        return ("hit" if hit else "miss", proof, detail)

    def _baseline(self, claim: Claim) -> float | None:
        """Claim-time liquidity, read back out of the stored evidence bundle."""
        raw = self.evidence(claim.id)
        if raw is None:
            return None
        from prereg.record import evidence_digest

        # The bundle only counts if it is the one the claim committed to.
        if evidence_digest(raw) != claim.evidence:
            log.warning("evidence for %s does not match its digest", claim.id)
            return None
        try:
            value = float(json.loads(raw)["liquidity_usd"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        return value if value > 0 else None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _age_hours(created_ms: float) -> float:
    import time

    if created_ms <= 0:
        return 0.0
    return max(0.0, (time.time() * 1000 - created_ms) / 3_600_000)


def _clamp(value: float) -> float:
    return round(max(0.02, min(0.98, value)), 2)
