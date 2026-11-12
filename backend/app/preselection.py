"""Bounded candidate allocation; heuristic coverage is not an optimality claim."""
from .candidates import interleave_groups
from .search_log import event


MAX_CANDIDATES = 24
SURFACE_RESERVE = 6


def candidate_key(candidate):
    return (candidate.place.point.csv(), candidate.approach.csv() if candidate.approach else "",
            candidate.exit_place.point.csv() if candidate.exit_place else "")


def candidate_trace(candidate):
    return dict(candidate_key=candidate_key(candidate), route_index=candidate.route_index,
                region=candidate.region, direction=candidate.direction,
                sample_tier=candidate.sample_tier, origins=candidate.origins,
                source=candidate.source, discovery_group=candidate.discovery_group,
                hub_kind=candidate.hub_kind)


def select_candidates(primary, surface, limit=MAX_CANDIDATES, surface_reserve=SURFACE_RESERVE):
    """Preserve primary order, reserve surface coverage, and backfill unused slots.

    Only identical coordinates AND exit constraints merge. Road sides/levels are
    never inferred equivalent from proximity. Mutated records are request-local.
    """
    unique = {}
    pools = [[], []]
    for pool_index, candidates in enumerate((primary, surface)):
        for candidate in candidates:
            key = candidate_key(candidate)
            event("candidate_generated", **candidate_trace(candidate))
            if key in unique:
                existing = unique[key]
                existing.modes = tuple(dict.fromkeys((*existing.modes, *candidate.modes)))
                existing.origins = sorted(set(existing.origins + candidate.origins))
                # Same physical point can satisfy both source pools; never spend two slots.
                if existing not in pools[pool_index]:
                    pools[pool_index].append(existing)
                event("candidate_preselection", decision="merged", **candidate_trace(candidate))
            else:
                unique[key] = candidate
                pools[pool_index].append(candidate)
    reserve = min(max(0, surface_reserve), max(0, limit), len(pools[1]))
    reserved = pools[1][:reserve]
    reserved_keys = {candidate_key(c) for c in reserved}
    primary_only = [c for c in pools[0] if candidate_key(c) not in reserved_keys]
    chosen_primary = primary_only[:max(0, limit - reserve)]
    selected = interleave_groups([chosen_primary, reserved])
    selected_keys = {candidate_key(c) for c in selected}
    for candidate in interleave_groups(pools):
        if len(selected) >= limit:
            break
        if candidate_key(candidate) not in selected_keys:
            selected.append(candidate)
            selected_keys.add(candidate_key(candidate))
    old_keys = {candidate_key(c) for c in pools[0][:limit]}
    for key, candidate in unique.items():
        event("candidate_preselection", decision="selected" if key in selected_keys else "quota_limit",
              selected_by_primary_only=key in old_keys, **candidate_trace(candidate))
    event("preselection_summary", generated=len(primary) + len(surface), unique=len(unique),
          selected=len(selected), limit=limit, surface_reserved=reserve,
          primary_only_displaced=[key for key in sorted(old_keys - selected_keys)])
    return selected, len(unique) > len(selected)
