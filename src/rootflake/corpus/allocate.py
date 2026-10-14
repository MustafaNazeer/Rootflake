"""Floor plus proportional allocation and shortfall reallocation (ADR 0006, Decision 4).

Quotas are exact fractions, so the result never depends on floating point rounding.
"""

from fractions import Fraction

FLOOR = 12
TOTAL = 150
DEV_TOTAL = 50


def largest_remainder(total: int, weights: dict[str, int], caps: dict[str, int]) -> dict[str, int]:
    """Largest remainder with iterative capping; ties go to the name first in code point order."""
    if total < 0:
        raise ValueError(f"total must not be negative, got {total}")
    result = {name: 0 for name in weights}
    active = {name for name, weight in weights.items() if weight > 0 and caps[name] > 0}
    while active and total > 0:
        weight_sum = sum(weights[name] for name in active)
        quotas = {name: Fraction(total * weights[name], weight_sum) for name in active}
        capped = {name for name in active if quotas[name] >= caps[name]}
        if capped:
            for name in capped:
                result[name] = caps[name]
                total -= caps[name]
            active -= capped
            continue
        for name in active:
            result[name] = int(quotas[name])
        left = total - sum(result[name] for name in active)
        order = sorted(active, key=lambda name: (-(quotas[name] - int(quotas[name])), name))
        for name in order[:left]:
            result[name] += 1
        break
    return result


def plan(
    eligible_prs: dict[str, int], floor: int = FLOOR, total: int = TOTAL, dev_total: int = DEV_TOTAL
) -> dict[str, tuple[int, int]]:
    """Planned (dev, test) counts per stratum from each stratum's eligible PR count.

    ADR 0006 defines the dev step only when the strata can fill the whole corpus; with fewer
    eligible PRs it would put the slots into dev first, so that case is refused.
    """
    if sum(eligible_prs.values()) < total:
        raise ValueError(f"{sum(eligible_prs.values())} eligible PRs cannot fill {total} slots")
    floors = {name: min(floor, count) for name, count in eligible_prs.items()}
    above = {name: count - floors[name] for name, count in eligible_prs.items()}
    extra = largest_remainder(max(total - sum(floors.values()), 0), above, above)
    sizes = {name: floors[name] + extra[name] for name in eligible_prs}
    dev = largest_remainder(dev_total, sizes, sizes)
    return {name: (dev[name], sizes[name] - dev[name]) for name in eligible_prs}


def reallocate(lost: int, remaining: dict[str, int]) -> dict[str, int]:
    """Lost slots of one split, shared by later strata's unexamined eligible PRs."""
    return largest_remainder(lost, remaining, remaining)
