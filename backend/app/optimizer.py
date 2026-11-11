import math
from decimal import Decimal

from .geometry import haversine_m
from .models import ModeOption, Preferences, Recommendation
from .search_log import event, recommendation_summary

MAX_RECOMMENDATIONS = 6
MAX_BOARDING_WALK_M = 300
TAXI_SECONDS_PER_YUAN = Decimal(108)

# 三档打车偏好权重: (司机绕行秒权重 w_driver, 乘客全程秒权重 w_pax, 每1元折合秒数 sec_per_yuan)
# - balanced (均衡): 1.5 * 司机增加分钟 + 1.0 * 乘客全程分钟 + 1.8 * 车费(元) (1元 = 108秒)
# - time     (省时): 1.5 * 司机增加分钟 + 1.5 * 乘客全程分钟 + 0.8 * 车费(元) (1元 = 48秒)
# - cost     (省费): 1.2 * 司机增加分钟 + 0.8 * 乘客全程分钟 + 3.0 * 车费(元) (1元 = 180秒)
TAXI_WEIGHTS = {
    "balanced": (Decimal("1.5"), Decimal("1.0"), Decimal("108")),
    "time":     (Decimal("1.5"), Decimal("1.5"), Decimal("48")),
    "cost":     (Decimal("1.2"), Decimal("0.8"), Decimal("180")),
}


def execution_key(rec: Recommendation):
    """Same coordinate is insufficient: onward timetables depend on the actual driver trip."""
    return (rec.point.point.csv(), rec.shared_ride_s, rec.stop_duration_s,
            rec.driver_route.duration_s, rec.driver_route.distance_m,
            tuple(p.csv() for p in rec.driver_route.polyline))


def is_viable(option: ModeOption, prefs: Preferences) -> bool:
    if option.mode not in prefs.modes:
        return False
    if option.direct_walk:
        return (option.fare_yuan == 0 and option.walking_m is not None
                and 0 <= option.walking_m <= min(1200, prefs.max_walking_m))
    if option.mode == "transit":
        return (option.boarding_stop is not None
                and option.boarding_walk_m is not None
                and option.boarding_walk_m <= MAX_BOARDING_WALK_M
                and option.walking_m is not None and option.walking_m <= prefs.max_walking_m
                and option.transfers is not None and option.transfers <= prefs.max_transfers)
    return (option.fare_yuan is not None and math.isfinite(option.fare_yuan)
            and 0 <= option.fare_yuan <= prefs.max_taxi_fare_yuan)


def _subway_bonus_minutes(option: ModeOption, priority: str) -> float:
    """Reward subway reliability in balanced and time preferences."""
    if option.direct_walk or option.mode != "transit" or priority == "cost":
        return 0.0
    if option.boarding_kind == "subway":
        return 5.0 if priority == "balanced" else 3.0
    if any(step.kind == "subway" for step in option.transit_steps):
        return 2.5 if priority == "balanced" else 1.5
    return 0.0


def option_score(option: ModeOption, priority: str) -> float:
    time_min = (option.total_duration_s if option.total_duration_s is not None else option.duration_s) / 60
    # 费用未知不能当作零：增加保守惩罚，并在界面显示“待估算”。
    fee_min = option.fare_yuan if option.fare_yuan is not None else 10000
    walking = (option.walking_m or 0) / 1000
    transfers = option.transfers or 0
    bonus = _subway_bonus_minutes(option, priority)
    if priority == "time":
        return time_min - bonus
    if priority == "cost":
        return fee_min * 1000 + time_min + walking * 2 + transfers * 2
    return time_min + fee_min * .55 + walking * 8 + transfers * 5 - bonus


def preferred_option(options: list[ModeOption], priority: str) -> ModeOption:
    best = min(options, key=lambda option: option_score(option, priority))
    if best.direct_walk or best.mode != "transit":
        return best
    # Preserve boarding kind so a shorter curb walk cannot replace a subway entrance.
    duration = best.total_duration_s if best.total_duration_s is not None else best.duration_s
    comparable = [option for option in options if option.mode == "transit"
                  and option.boarding_kind == best.boarding_kind
                  and option.boarding_walk_m is not None
                  and (option.total_duration_s if option.total_duration_s is not None else option.duration_s) <= duration + 120
                  and option.fare_yuan == best.fare_yuan
                  and (option.transfers or 0) <= (best.transfers or 0)
                  and option_score(option, priority) <= option_score(best, priority) + 2]
    return min(comparable, key=lambda option: (option.boarding_walk_m, option_score(option, priority))) if comparable else best


def taxi_score_seconds(driver_detour_s: int, option: ModeOption, priority: str = "balanced"):
    """Weighted combined driver detour (with piecewise convex penalty above 18 min),
    passenger trip time, and taxi fare in exact Decimal seconds."""
    w_driver, w_pax, sec_per_yuan = TAXI_WEIGHTS.get(priority, TAXI_WEIGHTS["balanced"])
    detour_d = Decimal(driver_detour_s)
    convex_excess = max(Decimal(0), detour_d - Decimal(1080)) * Decimal("0.75")
    return ((detour_d + convex_excess) * w_driver
            + Decimal(option.total_duration_s) * w_pax
            + Decimal(str(option.fare_yuan)) * sec_per_yuan)


def _is_redundant_taxi_cluster(rec: Recommendation, ex: Recommendation) -> bool:
    dist_m = haversine_m(rec.point.point, ex.point.point)
    same_metrics = (rec.driver_detour_s == ex.driver_detour_s
                    and rec.options[0].total_duration_s == ex.options[0].total_duration_s
                    and rec.options[0].fare_yuan == ex.options[0].fare_yuan)
    # Proximity alone cannot erase a driver/passenger/fare tradeoff.
    return same_metrics and (dist_m <= 300 or (
        bool(rec.dropoff_road) and rec.dropoff_road == ex.dropoff_road and dist_m <= 2500))


def _is_severe_overshoot_outlier(rec: Recommendation, best: Recommendation) -> bool:
    """Filter out severe overshoot/backtracking points (e.g. passenger rides far past C toward B,
    spending 35+ min longer and paying 45+ yuan more than the top recommendation)."""
    if rec is best:
        return False
    opt_rec = rec.options[0]
    opt_best = best.options[0]
    fare_rec = opt_rec.fare_yuan or 0
    fare_best = opt_best.fare_yuan or 0
    dur_rec = opt_rec.total_duration_s or 0
    dur_best = opt_best.total_duration_s or 0
    return (dur_rec - dur_best >= 2100 and fare_rec - fare_best >= 45 and fare_rec >= 80)


def _is_walk_home_rec(rec: Recommendation) -> bool:
    return any(o.direct_walk for o in rec.options)


def _semantic_role_tag(rec: Recommendation) -> str:
    if _is_walk_home_rec(rec):
        return "【🚶 ¥0 · 直接步行回家】"
    chosen = next((o for o in rec.options if o.mode == rec.suggested_mode), rec.options[0] if rec.options else None)
    if not chosen:
        return ""
    if rec.suggested_mode == "transit":
        if chosen.boarding_kind == "subway":
            return "【🚇 地铁便捷换乘】"
        return "【🚌 公交经济接驳】"
    fare = chosen.fare_yuan if chosen.fare_yuan is not None else 999
    if rec.driver_detour_s <= 600:
        return "【🚗 司机少绕路首选】"
    if rec.driver_detour_s <= 1320 and 20 <= fare <= 55:
        return "【🏆 司乘双赢拐点】"
    if fare <= 16:
        return "【💰 终点外围低价打车】"
    return "【⚖️ 司乘均衡折中】"


def _assign_recommendation_labels(selected: list[Recommendation], prefs: Preferences) -> None:
    w_driver, w_pax, sec_per_yuan = TAXI_WEIGHTS.get(prefs.priority, TAXI_WEIGHTS["balanced"])
    for index, rec in enumerate(selected):
        role_tag = _semantic_role_tag(rec)
        if _is_walk_home_rec(rec):
            rec.recommendation_label = "¥0 · 直接步行回家"
            opt = rec.options[0]
            walk_m = opt.walking_m or 0
            walk_min = max(1, round((opt.duration_s or 180) / 60))
            rec.reason = (
                f"¥0 · 直接步行回家 {role_tag}：司机增加约{rec.driver_detour_s / 60:.1f}分钟（含停靠），"
                f"下车点距目的地仅约{walk_m}米（步行约{walk_min}分钟），"
                f"乘客全程约{(opt.total_duration_s or 0) / 60:.1f}分钟，后续费用0元；停靠条件待核实。"
            )
            continue
        label = "综合推荐" if index == 0 else f"综合备选{index + 1}"
        old_label = rec.recommendation_label
        rec.recommendation_label = label
        if rec.reason:
            if old_label and rec.reason.startswith(f"{old_label}："):
                body = rec.reason[len(old_label) + 1:]
            elif "：" in rec.reason and rec.reason.startswith("综合"):
                body = rec.reason.split("：", 1)[1]
            else:
                body = rec.reason
            if role_tag and role_tag not in body:
                rec.reason = f"{label}：{role_tag}{body}"
            else:
                rec.reason = f"{label}：{body}"
        else:
            option = rec.options[0]
            rec.reason = (f"{label}：{role_tag}司机增加约{rec.driver_detour_s / 60:.1f}分钟（含停靠），"
                          f"乘客全程约{option.total_duration_s / 60:.1f}分钟，打车预估{option.fare_yuan:.1f}元。"
                          f"当前按{prefs.priority}偏好权重（{w_driver}:{w_pax}:{sec_per_yuan / 60:.1f}）综合比较，分数相同时优先省时；停靠条件待核实。")


def rank_taxi_recommendations(recs: list[Recommendation], prefs: Preferences) -> list[Recommendation]:
    """Rank feasible trips by priority-weighted driver delay + passenger full trip + taxi fare."""
    candidates = []
    for original in recs:
        if not 0 <= original.driver_detour_s <= prefs.max_detour_minutes * 60:
            event("taxi_filtered", candidate_id=original.id, reason="detour_limit",
                  driver_detour_s=original.driver_detour_s)
            continue
        if ("(公交站)" in original.point.name
                and not _is_walk_home_rec(original)
                and not any(o.mode == "transit" and is_viable(o, prefs) for o in original.options)):
            event("taxi_filtered", candidate_id=original.id, reason="bus_stop_taxi_only")
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
        option = min(options, key=lambda item: (taxi_score_seconds(original.driver_detour_s, item, prefs.priority),
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

    ordered = sorted(frontier, key=lambda rec: (
        taxi_score_seconds(rec.driver_detour_s, rec.options[0], prefs.priority),
        rec.driver_detour_s + rec.options[0].total_duration_s,
        rec.options[0].fare_yuan, rec.driver_detour_s, rec.id))

    selected = []
    for rec in ordered:
        if any(_is_redundant_taxi_cluster(rec, ex) for ex in selected):
            event("taxi_filtered", candidate_id=rec.id, reason="duplicate_nearby_cluster")
            continue
        if len(selected) >= 3 and _is_severe_overshoot_outlier(rec, selected[0]):
            event("taxi_filtered", candidate_id=rec.id, reason="severe_overshoot_outlier")
            continue
        selected.append(rec)
        if len(selected) == MAX_RECOMMENDATIONS:
            break

    if len(selected) >= 3 and selected[0].driver_detour_s >= 1440:
        knee_idx = next(
            (i for i in range(1, len(selected))
             if selected[i].driver_detour_s <= selected[0].driver_detour_s - 480
             and (selected[i].options[0].fare_yuan if selected[i].options[0].fare_yuan is not None else 999) <= 55),
            None,
        )
        if knee_idx is not None and knee_idx > 1:
            knee_rec = selected.pop(knee_idx)
            selected.insert(1, knee_rec)

    _, _, sec_per_yuan = TAXI_WEIGHTS.get(prefs.priority, TAXI_WEIGHTS["balanced"])
    priority_label = {"balanced": "均衡", "time": "省时间", "cost": "省费用"}.get(prefs.priority, "均衡")
    for index, rec in enumerate(selected):
        option = rec.options[0]
        rec.score = float(taxi_score_seconds(rec.driver_detour_s, option, prefs.priority) / 60)
        rec.recommendation_label = "综合推荐" if index == 0 else f"综合备选{index + 1}"
        rec.reason = (f"{rec.recommendation_label}：司机增加约{rec.driver_detour_s / 60:.1f}分钟（含停靠），"
                      f"乘客全程约{option.total_duration_s / 60:.1f}分钟，打车预估{option.fare_yuan:.1f}元。"
                      f"当前按{priority_label}偏好综合比较，分数相同时优先省时；停靠条件待核实。")
    _assign_recommendation_labels(selected, prefs)
    event("taxi_shortlist", recommendations=[recommendation_summary(rec) for rec in selected],
          remaining_frontier_ids=[rec.id for rec in ordered], seconds_per_yuan=int(sec_per_yuan))
    return selected


def has_mode_minimum(recs: list[Recommendation], prefs: Preferences) -> bool:
    """A mixed search needs two independent cards for each requested vehicle mode."""
    minimum = 2 if len(prefs.modes) > 1 else 1
    return all(len({execution_key(r) for r in recs
                    if r.suggested_mode == mode and not r.options[0].direct_walk}) >= minimum
               for mode in prefs.modes)


def rank_recommendations(recs: list[Recommendation], prefs: Preferences) -> list[Recommendation]:
    """Interleave independently ranked mode cards without merging nearby dropoffs.

    IDs identify the original candidate for diagnostic replay. A displayed card
    is identified by (id, suggested_mode), including two modes at the same point.
    """
    taxis = rank_taxi_recommendations(recs, prefs) if "taxi" in prefs.modes else []
    transits = []
    if "transit" in prefs.modes:
        transit_recs = [r.model_copy(deep=True) for r in recs if any(o.mode == "transit" for o in r.options)]
        transit_prefs = prefs.model_copy(update={"modes": ["transit"]})
        transits = _rank_transit_recommendations(transit_recs, transit_prefs)
        if len(prefs.modes) > 1 and sum(not preferred_option(r.options, prefs.priority).direct_walk for r in transits) < 2:
            # A soft time preference must not hide the only useful second option.
            transits = _rank_transit_recommendations(
                [r.model_copy(deep=True) for r in recs if any(o.mode == "transit" for o in r.options)],
                transit_prefs, apply_time_budget=False)
        for rec in transits:
            rec.options = [preferred_option(rec.options, prefs.priority)]
    ordered, seen = [], set()
    for index in range(max(len(taxis), len(transits))):
        for group in (taxis, transits):
            if index >= len(group):
                continue
            rec = group[index]
            key = (execution_key(rec), rec.suggested_mode)
            if key not in seen:
                seen.add(key)
                ordered.append(rec)
    # Reserve two vehicle cards per requested mode before filling remaining slots.
    # A direct walk is useful but cannot stand in for a taxi or a transit trip.
    reserved = set()
    if len(prefs.modes) > 1:
        for mode in prefs.modes:
            matches = [i for i, r in enumerate(ordered)
                       if r.suggested_mode == mode and not r.options[0].direct_walk]
            reserved.update(matches[:2])
    chosen = set(reserved)
    for index in range(len(ordered)):
        if len(chosen) >= MAX_RECOMMENDATIONS:
            break
        chosen.add(index)
    selected = [r for index, r in enumerate(ordered) if index in chosen]
    _assign_recommendation_labels(selected, prefs)
    return selected


def _rank_transit_recommendations(recs: list[Recommendation], prefs: Preferences, *, apply_time_budget=True) -> list[Recommendation]:
    max_detour_s = prefs.max_detour_minutes * 60
    if prefs.priority == "cost" and apply_time_budget:
        durations = [o.total_duration_s if o.total_duration_s is not None else r.shared_ride_s + r.stop_duration_s + o.duration_s
                     for r in recs if 0 <= r.driver_detour_s <= max_detour_s
                     for o in r.options if is_viable(o, prefs)]
        latest = min(durations) + 900 if durations else None
    else:
        latest = None
    for rec in recs:
        if not 0 <= rec.driver_detour_s <= max_detour_s:
            event("transit_filtered", candidate_id=rec.id, reason="detour_limit",
                  driver_detour_s=rec.driver_detour_s)
            rec.options = []
            rec.score = 1e9
            continue
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
        rec.score = round(option_score(best, prefs.priority) + rec.driver_detour_s / 60 * (0.35 if prefs.priority in ("time", "cost") else 1), 2)
        rec.reason = (f"司机预计增加 {rec.driver_detour_s / 60:.1f} 分钟，乘客全程约 "
                      f"{(best.total_duration_s + 59) // 60} 分钟，"
                      f"后续建议{'打车' if best.mode == 'taxi' else '公共交通'}；"
                      "但停靠合法性尚未核实。")
        if best.mode == "transit" and not best.direct_walk:
            rec.reason += f" 到实际站点或入口步行{best.boarding_walk_m}米，优先选择接近站点的位置。"
    # Surface buses cannot dominate subway options merely through shorter walks.
    def dimensions(rec):
        option = preferred_option(rec.options, prefs.priority)
        return (1 if option.boarding_kind == "bus" else 0, rec.driver_detour_s,
                option.total_duration_s, option.fare_yuan if option.fare_yuan is not None else 1e9,
                option.walking_m if option.walking_m is not None else 1e9,
                option.transfers if option.transfers is not None else 1e9, option.boarding_walk_m)
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
    if prefs.priority == "cost":
        ordered = sorted((r for r in viable_recs if r.id not in dominated), key=lambda r: (
            preferred_option(r.options, prefs.priority).fare_yuan
            if preferred_option(r.options, prefs.priority).fare_yuan is not None else 1e9,
            preferred_option(r.options, prefs.priority).total_duration_s, r.driver_detour_s))
    else:
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
                     and (option := preferred_option(rec.options, prefs.priority)).boarding_kind == first_option.boarding_kind
                     and option.fare_yuan == first_option.fare_yuan
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
        duplicate_key = (family, dimensions(rec))
        if duplicate_key in families:
            continue
        families.add(duplicate_key)
        selected.append(rec)
        if len(selected) == MAX_RECOMMENDATIONS:
            break
    _assign_recommendation_labels(selected, prefs)
    return selected
