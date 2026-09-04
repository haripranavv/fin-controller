"""Bounded 'closest subset sum' search via meet-in-the-middle.

Purpose-built for PROJECT_SPEC.md section 8.4's batched settlement
matching: which subset of a bounded candidate list of (id, amount_paisa)
pairs sums closest to a target amount. Not a general knapsack solver — it
returns the top-k closest-sum subsets (not just the single best) so the
caller can detect genuine ambiguity (two subsets that fit almost equally
well) rather than silently picking one, per section 8.7's "stop when...
evidence is too ambiguous".

MAX_ITEMS keeps the search a real O(2^(n/2)) meet-in-the-middle rather than
an approximation — section 8.4: "candidate set must be limited". At
MAX_ITEMS=20 that's at most 2^10 = 1024 subset sums per half: instant.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from itertools import combinations

MAX_ITEMS = 20


@dataclass(frozen=True)
class SubsetSumResult:
    member_ids: tuple[str, ...]
    total: int
    delta: int  # total - target (signed)


def _all_subset_sums(part: list[tuple[str, int]]) -> list[tuple[int, tuple[str, ...]]]:
    out: list[tuple[int, tuple[str, ...]]] = [(0, ())]
    for r in range(1, len(part) + 1):
        for combo in combinations(part, r):
            out.append((sum(c[1] for c in combo), tuple(c[0] for c in combo)))
    return out


def closest_subset_sums(items: list[tuple[str, int]], target: int, k: int = 3) -> list[SubsetSumResult]:
    """Return up to k subsets of `items` with distinct totals, closest to
    `target` first (smallest |delta|). Always includes the empty subset as a
    candidate (sum 0), so "no match" is a representable answer.

    Raises ValueError if len(items) > MAX_ITEMS — the caller should treat
    that as a stop condition (too many candidates), not silently truncate.
    """
    if len(items) > MAX_ITEMS:
        raise ValueError(f"{len(items)} candidates exceeds MAX_ITEMS={MAX_ITEMS}")

    mid = len(items) // 2
    left_sums = _all_subset_sums(items[:mid])
    right_sums = sorted(_all_subset_sums(items[mid:]), key=lambda x: x[0])
    right_values = [s for s, _ in right_sums]

    results: list[SubsetSumResult] = []
    seen_totals: set[int] = set()

    for lsum, lids in left_sums:
        want = target - lsum
        pos = bisect.bisect_left(right_values, want)
        for p in (pos - 1, pos, pos + 1):
            if 0 <= p < len(right_sums):
                rsum, rids = right_sums[p]
                total = lsum + rsum
                if total in seen_totals:
                    continue
                seen_totals.add(total)
                results.append(SubsetSumResult(member_ids=tuple(sorted(lids + rids)), total=total, delta=total - target))

    results.sort(key=lambda r: abs(r.delta))
    return results[:k]
