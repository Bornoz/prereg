"""Where claims come from, and how they are settled.

A source proposes; the agent decides what to sign. Nothing in here reaches the
network on Technocore's side, so a broken source degrades to an agent with
nothing to say, which is a state the loop already handles correctly.
"""

from __future__ import annotations

from prereg.agent import ClaimDraft, ClaimSource, OutcomeResolver
from prereg.record import Claim


class Chain:
    """Several sources behind one, asked in order until the cycle's budget is met."""

    def __init__(self, *sources: ClaimSource) -> None:
        self.sources = sources

    def pending(self) -> list[ClaimDraft]:
        drafts: list[ClaimDraft] = []
        for source in self.sources:
            try:
                drafts.extend(source.pending())
            except Exception:  # noqa: BLE001 - one broken source must not silence the rest
                continue
        return drafts


class Router:
    """Sends a claim to the resolver that owns its domain."""

    def __init__(self, **by_domain: OutcomeResolver) -> None:
        self.by_domain = by_domain

    def resolve(self, claim: Claim) -> tuple[str, str, str] | None:
        resolver = self.by_domain.get(claim.domain.replace("-", "_"))
        if resolver is None:
            return None
        try:
            return resolver.resolve(claim)
        except Exception:  # noqa: BLE001 - an unresolvable claim stays open
            return None
