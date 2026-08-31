import json
from datetime import timedelta

from prereg import record
from prereg.sources.dexscreener import (
    CALL_HOLDS_AT,
    CALL_RUG_AT,
    DexScreenerResolver,
    DexScreenerSource,
    Snapshot,
    deepest_pair,
    rug_probability,
    snapshot,
)
from prereg.store import Store


def pair(liquidity=50_000, fdv=400_000, age_hours=6, buys=500, sells=400,
         volume=200_000, change=5.0, chain="base", symbol="TEST"):
    import time

    return {
        "chainId": chain,
        "baseToken": {"symbol": symbol},
        "liquidity": {"usd": liquidity},
        "fdv": fdv,
        "pairCreatedAt": int((time.time() - age_hours * 3600) * 1000),
        "txns": {"h24": {"buys": buys, "sells": sells}},
        "volume": {"h24": volume},
        "priceChange": {"h24": change},
    }


def fetcher(mapping):
    def fetch(url):
        for needle, payload in mapping.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected url {url}")

    return fetch


def snap(**kw):
    base = dict(chain="base", address="0xabc", symbol="TEST", liquidity_usd=50_000,
                fdv=400_000, age_hours=6, buys_24h=500, sells_24h=400,
                volume_24h=200_000, price_change_24h=5.0)
    base.update(kw)
    return Snapshot(**base)


# -- the model ------------------------------------------------------------


def test_the_deepest_pool_wins_and_empty_pools_are_ignored():
    payload = {"pairs": [pair(liquidity=100), {"liquidity": None}, pair(liquidity=9_000)]}
    assert deepest_pair(payload)["liquidity"]["usd"] == 9_000
    assert deepest_pair({"pairs": [{"liquidity": None}]}) is None
    assert deepest_pair({}) is None


def test_a_thin_pool_under_a_huge_valuation_reads_as_a_rug():
    probability, why = rug_probability(snap(liquidity_usd=9_000, fdv=900_000, buys_24h=10, sells_24h=8))
    assert probability >= CALL_RUG_AT
    assert any("fdv/liq" in reason for reason in why)


def test_a_deep_pool_with_buy_weighted_flow_reads_as_holding():
    probability, _why = rug_probability(
        snap(liquidity_usd=400_000, fdv=2_000_000, buys_24h=900, sells_24h=300)
    )
    assert probability <= CALL_HOLDS_AT


def test_the_probability_never_reaches_certainty():
    for extreme in (snap(liquidity_usd=1, fdv=10**9, buys_24h=0, sells_24h=0,
                         volume_24h=10**9, price_change_24h=-99),
                    snap(liquidity_usd=10**8, fdv=10**8, buys_24h=10**5, sells_24h=1)):
        probability, _ = rug_probability(extreme)
        assert 0.0 < probability < 1.0


# -- the source -----------------------------------------------------------


def test_a_middling_token_produces_no_claim_at_all():
    # Abstention is the behaviour that keeps the record informative: calling
    # every listing would track the base rate and say nothing.
    source = DexScreenerSource(fetch=fetcher({
        "token-profiles": [{"chainId": "base", "tokenAddress": "0xmid"}],
        "0xmid": {"pairs": [pair(liquidity=60_000, fdv=1_000_000, buys=300, sells=280)]},
    }))
    probability, _ = rug_probability(snap(liquidity_usd=60_000, fdv=1_000_000,
                                          buys_24h=300, sells_24h=280))
    assert CALL_HOLDS_AT < probability < CALL_RUG_AT
    assert source.pending() == []


def test_a_bad_listing_becomes_a_rug_draft():
    source = DexScreenerSource(fetch=fetcher({
        "token-profiles": [{"chainId": "base", "tokenAddress": "0xbad"}],
        "0xbad": {"pairs": [pair(liquidity=8_000, fdv=900_000, buys=12, sells=9,
                                 change=-70)]},
    }))
    drafts = source.pending()
    assert len(drafts) == 1
    assert drafts[0].call == "rug"
    assert drafts[0].subject == "base:0xbad"
    assert drafts[0].domain == "dex-liquidity"
    assert drafts[0].confidence >= CALL_RUG_AT
    assert json.loads(drafts[0].evidence)["liquidity_usd"] == 8_000


def test_pools_too_thin_to_measure_are_skipped():
    source = DexScreenerSource(fetch=fetcher({
        "token-profiles": [{"chainId": "base", "tokenAddress": "0xdust"}],
        "0xdust": {"pairs": [pair(liquidity=900, fdv=900_000)]},
    }))
    assert source.pending() == []


def test_listings_that_are_no_longer_new_are_skipped():
    source = DexScreenerSource(fetch=fetcher({
        "token-profiles": [{"chainId": "base", "tokenAddress": "0xold"}],
        "0xold": {"pairs": [pair(liquidity=8_000, fdv=900_000, age_hours=500)]},
    }))
    assert source.pending() == []


def test_chains_we_cannot_settle_on_are_skipped():
    source = DexScreenerSource(fetch=fetcher({
        "token-profiles": [{"chainId": "robinhood", "tokenAddress": "0xelse"}],
    }))
    assert source.pending() == []


def test_the_same_token_is_only_ever_proposed_once():
    calls = {"token-profiles": [{"chainId": "base", "tokenAddress": "0xbad"}],
             "0xbad": {"pairs": [pair(liquidity=8_000, fdv=900_000, buys=12, sells=9)]}}
    source = DexScreenerSource(fetch=fetcher(calls))
    assert len(source.pending()) == 1
    assert source.pending() == []


def test_a_dead_feed_produces_silence_not_an_error():
    source = DexScreenerSource(fetch=fetcher({"token-profiles": OSError("down")}))
    assert source.pending() == []

    source = DexScreenerSource(fetch=fetcher({
        "token-profiles": [{"chainId": "base", "tokenAddress": "0xgone"}],
        "0xgone": OSError("timeout"),
    }))
    assert source.pending() == []


def test_a_snapshot_of_a_delisted_token_is_none():
    assert snapshot("base", "0xgone", fetcher({"0xgone": {"pairs": []}})) is None


# -- settlement -----------------------------------------------------------


def claim_for(store, claim_id="aaaaaaaaaaaa", call="rug", liquidity=50_000, hours=72):
    bundle = snap(liquidity_usd=liquidity).bundle()
    store.save_evidence(claim_id, bundle)
    return record.Claim(
        id=claim_id, domain="dex-liquidity", subject="base:0xabc", call=call, confidence=0.8,
        deadline=record.now() + timedelta(hours=hours),
        evidence=record.evidence_digest(bundle), text="because",
    )


def test_a_vanished_pair_settles_a_rug_call_as_a_hit(tmp_path):
    store = Store(tmp_path)
    claim = claim_for(store)
    resolver = DexScreenerResolver(fetcher({"0xabc": {"pairs": []}}), store.evidence)
    outcome, proof, _detail = resolver.resolve(claim)
    assert outcome == "hit"
    assert proof == "no-pair"


def test_collapsed_liquidity_settles_a_rug_call_as_a_hit(tmp_path):
    store = Store(tmp_path)
    claim = claim_for(store, liquidity=50_000)
    resolver = DexScreenerResolver(
        fetcher({"0xabc": {"pairs": [pair(liquidity=5_000)]}}), store.evidence
    )
    outcome, _proof, detail = resolver.resolve(claim)
    assert outcome == "hit"
    assert "10%" in detail


def test_liquidity_just_above_the_line_is_not_a_rug(tmp_path):
    store = Store(tmp_path)
    claim = claim_for(store, liquidity=50_000, hours=-1)
    resolver = DexScreenerResolver(
        fetcher({"0xabc": {"pairs": [pair(liquidity=11_000)]}}), store.evidence
    )
    assert resolver.resolve(claim)[0] == "miss"


def test_a_holds_call_is_not_settled_before_its_deadline(tmp_path):
    store = Store(tmp_path)
    claim = claim_for(store, call="holds", hours=72)
    resolver = DexScreenerResolver(
        fetcher({"0xabc": {"pairs": [pair(liquidity=50_000)]}}), store.evidence
    )
    assert resolver.resolve(claim) is None


def test_a_holds_call_that_survived_the_horizon_is_a_hit(tmp_path):
    store = Store(tmp_path)
    claim = claim_for(store, call="holds", hours=-1)
    resolver = DexScreenerResolver(
        fetcher({"0xabc": {"pairs": [pair(liquidity=50_000)]}}), store.evidence
    )
    assert resolver.resolve(claim)[0] == "hit"


def test_evidence_that_does_not_match_its_digest_is_refused(tmp_path):
    store = Store(tmp_path)
    claim = claim_for(store, hours=-1)
    store.save_evidence(claim.id, b'{"liquidity_usd": 999999}')  # tampered
    resolver = DexScreenerResolver(
        fetcher({"0xabc": {"pairs": [pair(liquidity=50_000)]}}), store.evidence
    )
    outcome, proof, _detail = resolver.resolve(claim)
    assert outcome == "void"
    assert proof == "no-baseline"


def test_a_missing_bundle_leaves_an_open_claim_open(tmp_path):
    store = Store(tmp_path)
    claim = claim_for(store, hours=72)
    (tmp_path / "evidence" / f"{claim.id}.json").unlink()
    resolver = DexScreenerResolver(
        fetcher({"0xabc": {"pairs": [pair(liquidity=50_000)]}}), store.evidence
    )
    assert resolver.resolve(claim) is None


def test_the_evidence_bundle_round_trips_through_the_store(tmp_path):
    store = Store(tmp_path)
    bundle = snap().bundle()
    store.save_evidence("0123456789ab", bundle)
    assert store.evidence("0123456789ab") == bundle
    assert store.evidence("ffffffffffff") is None
