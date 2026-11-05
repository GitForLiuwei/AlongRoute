import math
from decimal import Decimal

from .models import ModeOption, Preferences, Recommendation
from .search_log import event, recommendation_summary


MAX_BOARDING_WALK_M = 300
TAXI_SECONDS_PER_YUAN = 180


def is_viable(option: ModeOption, prefs: Preferences) -> bool:
    if option.mode not in prefs.modes:
        return False
    if option.mode == "transit":
        return (option.boarding_stop is not None
                and option.boarding_walk_m is not None
                and option.boarding_walk_m <= MAX_BOARDING_WALK_M
                and option.walking_m is not None and option.walking_m <= prefs.max_walking_m
                and option.transfers is not None and option.transfers <= prefs.max_transfers)
    return (option.fare_yuan is not None and math.isfinite(option.fare_yuan)
            and 0 <= option.fare_yuan <= prefs.max_taxi_fare_yuan)


def option_score(option: ModeOption, priority: str) -> float:
    time_min = (option.total_duration_s if option.total_duration_s is not None else option.duration_s) / 60
    # 费用未知时不能当作零：增加保守惩罚，并在界面显示“待估算”。
    fee_min = option.fare_yuan if option.fare_yuan is not None else 10000
    walking = (option.walking_m or 0) / 1000
    transfers = option.transfers or 0
    if priority == "time":
        return time_min
    if priority == "cost":
        return fee_min * 1000 + time_min + walking * 2 + transfers * 2
    return time_min + fee_min * .55 + walking * 8 + transfers * 5


def preferred_option(options: list[ModeOption], priority: str) -> ModeOption:
    best = min(options, key=lambda option: option_score(option, priority))
    if best.mode != "transit":
        return best
    # Preserve the chosen time/cost preference, then prefer a closer real entrance.
    duration = best.total_duration_s if best.total_duration_s is not None else best.duration_s
    comparable = [option for option in options if option.mode == "transit"
                  and option.boarding_walk_m is not None
                  and (option.total_duration_s if option.total_duration_s is not None else option.duration_s) <= duration + 120
                  and option.fare_yuan == best.fare_yuan
                  and (option.transfers or 0) <= (best.transfers or 0)
                  and option_score(option, priority) <= option_score(best, priority) + 2]
    return min(comparable, key=lambda option: (option.boarding_walk_m, option_score(option, priority))) if comparable else best


def taxi_score_seconds(driver_detour_s: int, option: ModeOption):
    """One yuan costs three combined driver/passenger minutes; compare exact decimals."""
    return (Decimal(driver_detour_s + option.total_duration_s)
            + Decimal(str(option.fare_yuan)) * TAXI_SECONDS_PER_YUAN)


def rank_taxi_recommendations(recs: list[Recommendation], prefs: Preferences) -> list[Recommendation]:
    """Rank feasible trips by driver delay + passenger full trip + fare at 3 min/yuan."""
    candidates = []
    for original in recs:
        if not 0 <= original.driver_detour_s <= prefs.max_detour_minutes * 60:
            event("taxi_filtered", candidate_id=original.id, reason="detour_limit",
                  driver_detour_s=original.driver_detour_s)
            continue
        options = []
        for option in original.options:
            if option.mode != "taxi" or not is_viable(option, prefs):
                continue
            option = option.model_copy(deep=True)
            if option.total_duration_s is None:
                option.total_duration_s = original.shared_ride_s + original.stop_duration_s + option.duration_s
            options.append(option)
        if not options:
            if any(option.mode == "taxi" for option in original.options):
                event("taxi_filtered", candidate_id=original.id, reason="fare_limit_or_unknown",
                      limit_yuan=prefs.max_taxi_fare_yuan)
            continue
        option = min(options, key=lambda item: (taxi_score_seconds(original.driver_detour_s, item),
                                                item.total_duration_s, item.fare_yuan))
        rec = original.model_copy(deep=True)
        rec.options = [option]
        rec.suggested_mode = "taxi"
        candidates.append(rec)

    def dimensions(rec):
        return (rec.driver_detour_s, rec.options[0].total_duration_s,
                Decimal(str(rec.options[0].fare_yuan)))

    frontier = []
    for rec in candidates:
        values = dimensions(rec)
        better = next((other for other in candidates if other is not rec
                       and all(a <= b for a, b in zip(dimensions(other), values))
                       and any(a < b for a, b in zip(dimensions(other), values))), None)
        if better is not None:
            event("taxi_filtered", candidate_id=rec.id, reason="dominated",
                  by_candidate_id=better.id, driver_detour_s=rec.driver_detour_s,
                  passenger_total_s=rec.options[0].total_duration_s,
                  fare_yuan=rec.options[0].fare_yuan)
        else:
            frontier.append(rec)

    # Exact equality at one yuan / three minutes favors saving time.
    ordered = sorted(frontier, key=lambda rec: (
        taxi_score_seconds(rec.driver_detour_s, rec.options[0]),
        rec.driver_detour_s + rec.options[0].total_duration_s,
        rec.options[0].fare_yuan, rec.driver_detour_s, rec.id))
    selected = ordered[:3]
    for index, rec in enumerate(selected):
        option = rec.options[0]
        rec.score = float(taxi_score_seconds(rec.driver_detour_s, option) / 60)
        rec.recommendation_label = "综合推荐" if index == 0 else f"综合备选{index + 1}"
        rec.reason = (f"{rec.recommendation_label}：司机增加约{rec.driver_detour_s / 60:.1f}分钟（含停靠），"
                      f"乘客全程约{option.total_duration_s / 60:.1f}分钟，打车预估{option.fare_yuan:.1f}元。"
                      "按每1元折合双方合计3分钟比较，分数相同时优先省时；停靠条件待核实。")
    event("taxi_shortlist", recommendations=[recommendation_summary(rec) for rec in selected],
          remaining_frontier_ids=[rec.id for rec in ordered], seconds_per_yuan=TAXI_SECONDS_PER_YUAN)
    return selected


def rank_recommendations(recs: list[Recommendation], prefs: Preferences) -> list[Recommendation]:
    taxis = rank_taxi_recommendations(recs, prefs) if "taxi" in prefs.modes else []
    if "transit" not in prefs.modes:
        return taxis
    transit_recs = [r.model_copy(deep=True) for r in recs if any(o.mode == "transit" for o in r.options)]
    transit_prefs = prefs.model_copy(update={"modes": ["transit"]})
    transits = _rank_transit_recommendations(transit_recs, transit_prefs)
    if "taxi" not in prefs.modes:
        return transits
    # Independent objectives have no shared numeric score. Interleave the two
    # shortlists, merging modes at the same point while keeping its first profile.
    selected, by_point = [], {}
    for index in range(max(len(taxis), len(transits))):
        for group in (taxis, transits):
            if index >= len(group):
                continue
            rec = group[index]
            key = rec.point.point.csv()
            if key in by_point:
                existing = by_point[key]
                existing.options.extend(o for o in rec.options if o.mode not in {x.mode for x in existing.options})
            elif len(selected) < 3:
                by_point[key] = rec
                selected.append(rec)
    # The shortlist chooses a suggested mode, not the only available mode at P.
    # Keep other individually viable options available in the detail panel.
    for rec in selected:
        for original in recs:
            if original.point.point.csv() != rec.point.point.csv():
                continue
            for option in original.options:
                if is_viable(option, prefs) and option.mode not in {o.mode for o in rec.options}:
                    option = option.model_copy(deep=True)
                    if option.total_duration_s is None:
                        option.total_duration_s = rec.shared_ride_s + rec.stop_duration_s + option.duration_s
                    if option.mode == "taxi" and option.fare_yuan is not None and (not math.isfinite(option.fare_yuan) or option.fare_yuan < 0):
                        option.fare_yuan = None
                    rec.options.append(option)
    return selected


def _rank_transit_recommendations(recs: list[Recommendation], prefs: Preferences) -> list[Recommendation]:
    if prefs.priority == "cost":
        durations = [o.total_duration_s if o.total_duration_s is not None else r.shared_ride_s + r.stop_duration_s + o.duration_s
                     for r in recs for o in r.options if is_viable(o, prefs)]
        latest = min(durations) + 900 if durations else None
    else:
        latest = None
    for rec in recs:
        viable = [option for option in rec.options if is_viable(option, prefs)]
        if len(viable) != len(rec.options):
            event("transit_filtered", candidate_id=rec.id, reason="mode_or_boarding_walking_transfer_limits",
                  removed_options=len(rec.options) - len(viable))
        rec.options = viable
        if not viable:
            rec.score = 1e9
            continue
        for option in viable:
            if option.total_duration_s is None:
                option.total_duration_s = rec.shared_ride_s + rec.stop_duration_s + option.duration_s
        if latest is not None:
            viable = [option for option in viable if option.total_duration_s <= latest]
            if len(viable) != len(rec.options):
                event("transit_filtered", candidate_id=rec.id, reason="cost_time_budget", latest_duration_s=latest)
            rec.options = viable
            if not viable:
                rec.score = 1e9
                continue
        best = preferred_option(viable, prefs.priority)
        rec.suggested_mode = best.mode
        rec.score = round(option_score(best, prefs.priority) + rec.driver_detour_s / 60 * (0 if prefs.priority in {"time", "cost"} else 1), 2)
        rec.reason = (f"司机预计增加 {rec.driver_detour_s / 60:.1f} 分钟，乘客全程约 "
                      f"{(best.total_duration_s + 59) // 60} 分钟，"
                      f"后续建议{'打车' if best.mode == 'taxi' else '公共交通'}；"
                      "但停靠合法性尚未核实。")
        if best.mode == "transit":
            rec.reason += f" 到实际站台或入口步行{best.boarding_walk_m}米，优先选择接近站点的位置。"
    # Remove candidates that are worse in every measured dimension.
    def dimensions(rec):
        option = preferred_option(rec.options, prefs.priority)
        return (option.total_duration_s, option.fare_yuan, option.walking_m,
                option.transfers, rec.driver_detour_s, option.boarding_walk_m)
    viable_recs = [r for r in recs if r.score < 1e9]
    dominated = set()
    for rec in viable_recs:
        values = dimensions(rec)
        if any(value is None for value in values):
            continue
        for other in viable_recs:
            if rec is other:
                continue
            comparison = dimensions(other)
            if all(value is not None for value in comparison) and all(a <= b for a, b in zip(comparison, values)) and any(a < b for a, b in zip(comparison, values)):
                dominated.add(rec.id)
                event("transit_filtered", candidate_id=rec.id, reason="dominated", by_candidate_id=other.id)
                break
    ordered = sorted((r for r in viable_recs if r.id not in dominated), key=lambda r: (r.score, r.driver_detour_s))
    # Within two score-minutes / two actual minutes at the same fare, prioritize
    # verified access distance. Taxi candidates keep their independent ordering.
    remaining, ordered = ordered, []
    while remaining:
        first = remaining[0]
        first_option = preferred_option(first.options, prefs.priority)
        group = [first]
        if first_option.mode == "transit":
            group = [rec for rec in remaining if rec.suggested_mode == "transit"
                     and (option := preferred_option(rec.options, prefs.priority)).fare_yuan == first_option.fare_yuan
                     and option.total_duration_s <= first_option.total_duration_s + 120
                     and rec.score <= first.score + 2
                     and (option.transfers or 0) <= (first_option.transfers or 0)]
            group.sort(key=lambda rec: (preferred_option(rec.options, prefs.priority).boarding_walk_m, rec.score, rec.driver_detour_s))
        ordered.extend(group)
        ids = {id(rec) for rec in group}
        remaining = [rec for rec in remaining if id(rec) not in ids]
    selected, families = [], set()
    for rec in ordered:
        best = preferred_option(rec.options, prefs.priority)
        # Keep different platforms/entrances; collapse repeated versions of one itinerary.
        family = (best.mode, best.boarding_stop.point.csv(), best.details) if best.boarding_stop else (best.mode, rec.point.point.csv())
        if family in families:
            continue
        families.add(family)
        selected.append(rec)
        if len(selected) == 3:
            break
    return selected
