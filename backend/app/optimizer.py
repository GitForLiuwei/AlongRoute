from .models import ModeOption, Preferences, Recommendation


MAX_BOARDING_WALK_M = 300


def is_viable(option: ModeOption, prefs: Preferences) -> bool:
    if option.mode not in prefs.modes:
        return False
    if option.mode == "transit":
        return (option.boarding_stop is not None
                and option.boarding_walk_m is not None
                and option.boarding_walk_m <= MAX_BOARDING_WALK_M
                and option.walking_m is not None and option.walking_m <= prefs.max_walking_m
                and option.transfers is not None and option.transfers <= prefs.max_transfers)
    return True


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


def rank_recommendations(recs: list[Recommendation], prefs: Preferences) -> list[Recommendation]:
    if prefs.priority == "cost":
        durations = [o.total_duration_s if o.total_duration_s is not None else r.shared_ride_s + r.stop_duration_s + o.duration_s
                     for r in recs for o in r.options if is_viable(o, prefs)]
        latest = min(durations) + 900 if durations else None
    else:
        latest = None
    for rec in recs:
        viable = [option for option in rec.options if is_viable(option, prefs)]
        rec.options = viable
        if not viable:
            rec.score = 1e9
            continue
        for option in viable:
            if option.total_duration_s is None:
                option.total_duration_s = rec.shared_ride_s + rec.stop_duration_s + option.duration_s
        if latest is not None:
            viable = [option for option in viable if option.total_duration_s <= latest]
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
