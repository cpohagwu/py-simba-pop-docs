"""Recipes for an age-structured initial_population.csv -- the warm-start
inventory SimbaPopEngine begins from.

NOT part of the installed package (like build_custom_parameters.py). Three
ways to get one, in order of preference -- build_farm_inputs.py picks the
first available:

1. A field census (initial_population.xlsx) or, for a new farm, its planting
   schedule (planting_schedule.xlsx): see adapt_legacy_exports.py.
2. `reconstruct_initial_population` -- from ribbon-tag production records
   alone, for a farm that has only production.xlsx + weather.xlsx:

   * Bunched (post-flowering) plants are OBSERVED: for each ribbon colour, the
     bunches bagged in the last `max_age_weeks_reproductive` weeks minus the
     ones already cut or lost (cuts traced to their bagging week through the
     normalised age_at_cut; losses taken from the oldest standing bunches).
   * Vegetative (pre-flowering) plants are INFERRED through the model's own
     flowering law: every bunch bagged was a vegetative plant, so the weekly
     number of followers selected, u(b), is fitted (non-negative, smooth) so
     that the model's flowering of those followers reproduces the recorded
     bagging. The most recent followers -- selected too recently to have
     flowered yet -- are produced by the model's one-follower-per-mother rule
     from the already-identified mothers. The engine itself is then run
     through the record with those selections and snapshotted at the target
     week, so thermal ages and phase counters are exactly the model's.
   * Only records BEFORE the snapshot week are used (a causal estimate), so a
     Mode A run from it is a genuine forecast.
3. `spin_up_initial_population` -- no records at all: a constant weekly
   selection rate sized to a number of production units (mats), run until
   the age structure is at equilibrium.

Run (from docs/examples/): python -m utils.build_initial_population <farm_dir> [snapshot YYYY-MM-DD | "YYYY S:WW"] [out.csv]
(usually called through build_farm_inputs.py; writes <farm_dir>/initial_population.csv
unless the farm has a census or planting schedule, which take precedence --
then give out.csv to write the reconstruction elsewhere)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import lsq_linear

from py_simba_pop import (
    ColumnMap,
    ParameterSet,
    SimbaPopEngine,
    SuckerSelectionData,
    WeatherProvider,
)
from py_simba_pop.cohorts import ChainKind, CohortChain

# Weeks of kernel history kept before the first production week, so bunches
# bagged early in the record can be explained by followers selected before it.
PRE_RECORD_WEEKS = 60
# Default / minimum weeks of production history before the snapshot week.
DEFAULT_HISTORY_WEEKS = 104
MIN_HISTORY_WEEKS = 90
# A selection week counts as identified by past bagging once this share of its
# (eventual) flowering happened before the snapshot week.
IDENTIFIED_FLOWERED_SHARE = 0.8
# Weeks of climatology appended after the snapshot, so flowering kernels of the
# latest selections are complete without using any weather from after it.
POST_SNAPSHOT_WEEKS = 90


# ---------------------------------------------------------------------------
# Small model helpers
# ---------------------------------------------------------------------------


def _weather_provider(
    weather_df: pd.DataFrame, params: ParameterSet, *, back_weeks: int = 0, forward_weeks: int = 0
) -> WeatherProvider:
    """WeatherProvider on the SAME grid as the farm's weather.csv (week 0 =
    its first date), optionally extended `back_weeks` before the record and
    `forward_weeks` after its last day with a day-of-year climatology of the
    record itself. The extensions are only used to compute flowering kernels
    (of followers selected before the record, or too recently to have
    flowered yet); they are never written anywhere."""
    daily = weather_df[["date", "tmean"]].copy()
    daily["date"] = pd.to_datetime(daily["date"])
    start = daily["date"].min()
    if back_weeks > 0 or forward_weeks > 0:
        climatology = daily.groupby(daily["date"].dt.dayofyear)["tmean"].mean()
        climatology = climatology.reindex(range(1, 367)).interpolate(limit_direction="both")
        smooth = pd.concat([climatology.iloc[-15:], climatology, climatology.iloc[:15]]).rolling(15, center=True).mean()
        smooth = smooth.iloc[15:-15]
        smooth.index = range(1, 367)
        past = pd.date_range(end=start - pd.Timedelta(days=1), periods=7 * back_weeks, freq="D")
        future = pd.date_range(start=daily["date"].max() + pd.Timedelta(days=1), periods=7 * forward_weeks, freq="D")
        daily = pd.concat(
            [
                pd.DataFrame({"date": past, "tmean": smooth.loc[past.dayofyear].to_numpy()}),
                daily,
                pd.DataFrame({"date": future, "tmean": smooth.loc[future.dayofyear].to_numpy()}),
            ],
            ignore_index=True,
        )
    return WeatherProvider.from_params(daily, params, ColumnMap(date="date", tmean="tmean"), run_start_date=start.date())


def _kernel_params(params: ParameterSet) -> ParameterSet:
    """No pruning of the tiny unit cohorts the kernels are computed on."""
    return params.override("general", {"population_epsilon": 0.0})


def flowering_kernel(birth_week: int, weather: WeatherProvider, params: ParameterSet, last_week: int) -> np.ndarray:
    """Share of a cohort selected (born) in `birth_week` that flowers -- i.e.
    is bagged -- in each week birth_week+1 .. last_week, computed with the
    package's own CohortChain mechanics (index 0 = birth_week+1)."""
    chain = CohortChain(ChainKind.PRE_FLOWERING, _kernel_params(params))
    chain.add_new_cohort(1.0e6, birth_week)
    out = np.zeros(max(0, last_week - birth_week))
    for k, week in enumerate(range(birth_week + 1, last_week + 1)):
        if chain.total_population() <= 1e-6:
            break
        rates = chain.calc_rates(weather.thermal_increment(week))
        out[k] = rates.sum_chain_exit / 1.0e6
        chain.integrate(rates, week)
    return out


def generation_interval_weeks(weather: WeatherProvider, params: ParameterSet, first_birth_week: int | None = None) -> float:
    """Mean weeks from a plant's own selection (or planting) to the selection
    of its follower -- how often each production unit (mat) produces a new
    plant, and therefore a bunch. Averaged over a year of selection weeks
    (it varies ~2 weeks with the season). Under the "cohort" rate basis a farm
    of M mats selects about M / this many followers every week."""
    first = weather.min_week_index if first_birth_week is None else first_birth_week
    intervals = []
    for birth in range(first, first + 52):
        chain = CohortChain(ChainKind.PRE_FLOWERING, _kernel_params(params))
        chain.add_new_cohort(1.0e6, birth)
        weighted = total = 0.0
        for week in range(birth + 1, weather.max_week_index + 1):
            rates = chain.calc_rates(weather.thermal_increment(week))
            followers = rates.sum_sucker + rates.sum_sucker_parous
            weighted += followers * (week - birth)
            total += followers
            chain.integrate(rates, week)
            if chain.total_population() <= 1e-6 and week - birth > 10:
                break
        if total > 0.999e6:  # this cohort's follower selection is complete within the record
            intervals.append(weighted / total)
    if not intervals:
        raise ValueError("no complete follower selection within the weather record -- check sucker_selection parameters")
    return float(np.mean(intervals))


def _selection_data(weeks: np.ndarray, selections: np.ndarray, weather: WeatherProvider) -> SuckerSelectionData:
    frame = pd.DataFrame(
        {
            "date": [weather.calendar_date_for_week(int(w)) for w in weeks],
            "parous": 0.0,
            "nulliparous": selections,
            "total": selections,
        }
    )
    return SuckerSelectionData(frame, weather)


# ---------------------------------------------------------------------------
# Post-flowering: the per-colour bunch ledger
# ---------------------------------------------------------------------------


def reconstruct_post_flowering(
    production: pd.DataFrame, weather: WeatherProvider, snapshot_week: int, max_age_weeks: int
) -> tuple[pd.DataFrame, dict]:
    """Standing bunches per (colour, bagging week) at the START of
    `snapshot_week`, from records strictly before it. `production` is the
    canonical production.csv table (age_at_cut = weeks since bagging).
    Rejected bunches are part of the cut and are not subtracted again."""
    prod = production.copy()
    prod["week"] = pd.to_datetime(prod["date"]).dt.date.map(weather.week_index_for_date)
    past = prod[prod["week"] < snapshot_week]
    cut_ages = past.loc[past["cut"].fillna(0) > 0, "age_at_cut"]
    p99_age = float(cut_ages.quantile(0.99)) if len(cut_ages) else float(max_age_weeks)
    track_from = snapshot_week - max_age_weeks - 20  # older cohorts tracked too, so they absorb their own cuts/losses

    rows, closed, unmatched_cut = [], 0.0, 0.0
    for tag, group in past[past["week"] >= track_from].groupby("tag"):
        by_week = group.set_index("week")
        cohorts: dict[int, dict] = {}  # bagging week -> {"bagged", "left", "group"}
        groups: list[list[int]] = []
        group_last_cut: dict[int, int] = {}
        for week in range(track_from, snapshot_week):
            row = by_week.loc[week] if week in by_week.index else None
            if row is not None:
                cut, age = row.get("cut"), row.get("age_at_cut")
                if pd.notna(cut) and cut > 0:
                    implied = week - int(age)
                    target = _nearest_group(groups, implied, before=week)
                    if target is None:
                        if implied > track_from + 2:  # earlier bagging is simply outside the window
                            unmatched_cut += cut
                    else:
                        group_last_cut[target] = week
                        unmatched_cut += _take_oldest_first(cohorts, groups[target], float(cut))
                loss = row.get("field_losses")
                if pd.notna(loss) and loss > 0:
                    live = sorted(b for b, c in cohorts.items() if b < week and c["left"] > 0)
                    _take_oldest_first(cohorts, live, float(loss))
                bagged = row.get("bagged")
                if pd.notna(bagged) and bagged > 0:
                    if groups and week - groups[-1][-1] <= 2:
                        groups[-1].append(week)
                    else:
                        groups.append([week])
                    cohorts[week] = {"bagged": float(bagged), "left": float(bagged), "group": len(groups) - 1}

        for bagging_week, cohort in cohorts.items():
            age = snapshot_week - bagging_week
            if cohort["left"] < 0.5 or age > max_age_weeks:
                continue
            members = groups[cohort["group"]]
            group_bagged = sum(cohorts[b]["bagged"] for b in members)
            group_left = sum(cohorts[b]["left"] for b in members)
            last_cut = group_last_cut.get(cohort["group"])
            finished = last_cut is not None and last_cut <= snapshot_week - 2 and group_left <= 0.1 * group_bagged
            if age > p99_age or finished:
                closed += cohort["left"]
                continue
            rows.append({"chain": "post_flowering", "tag": tag, "population": cohort["left"], "age_weeks": age})

    standing = pd.DataFrame(rows, columns=["chain", "tag", "population", "age_weeks"])
    future = prod[(prod["week"] >= snapshot_week) & (prod["week"] < snapshot_week + int(p99_age) + 1)]
    future_cut = future.loc[future["week"] - future["age_at_cut"] < snapshot_week, "cut"].sum()
    report = {
        "standing_bunches": float(standing["population"].sum()),
        "closed_unrecorded": float(closed),
        "unmatched_cut": float(unmatched_cut),
        # Diagnostics only (never used to build the inventory): later cuts traced
        # back to bunches bagged before the snapshot, and all field losses in
        # those weeks (losses carry no age, so they include younger bunches too).
        "later_cuts_of_standing": float(future_cut),
        "later_field_losses_all": float(future["field_losses"].sum()),
        "p99_cut_age": p99_age,
    }
    return standing, report


def _nearest_group(groups: list[list[int]], implied_week: int, *, before: int, tolerance: int = 2) -> int | None:
    best, best_distance = None, tolerance + 1
    for index, weeks in enumerate(groups):
        if weeks[0] >= before:
            continue
        distance = 0 if weeks[0] <= implied_week <= weeks[-1] else min(abs(implied_week - w) for w in weeks)
        if distance < best_distance:
            best, best_distance = index, distance
    return best


def _take_oldest_first(cohorts: dict[int, dict], weeks: list[int], amount: float) -> float:
    """Removes `amount` from the listed cohorts, oldest first; returns what
    could not be removed (more recorded than was standing)."""
    for week in sorted(weeks):
        if amount <= 0:
            break
        take = min(amount, cohorts[week]["left"])
        cohorts[week]["left"] -= take
        amount -= take
    return max(0.0, amount)


# ---------------------------------------------------------------------------
# Pre-flowering: selections inferred through the flowering law
# ---------------------------------------------------------------------------


def fit_weekly_selections(
    bagging: pd.Series,
    weather_df: pd.DataFrame,
    params: ParameterSet,
    snapshot_week: int,
    *,
    smoothing: float = 1.0,
) -> dict:
    """Non-negative, smooth weekly follower selections u(b) whose model
    flowering best reproduces the recorded weekly bagging before
    `snapshot_week`. `bagging` is indexed by grid week (all colours summed).
    Weeks with no bagging at all (e.g. a holiday pause) are left out of the
    fit rather than read as zero flowering. `weather_df` must end before the
    snapshot week (reconstruct_initial_population cuts it there); the kernels
    continue on its climatology."""
    first_week = int(bagging.index.min())
    weather_ext = _weather_provider(
        weather_df, params, back_weeks=PRE_RECORD_WEEKS, forward_weeks=POST_SNAPSHOT_WEEKS
    )
    fit_weeks = np.array([w for w in range(first_week, snapshot_week) if bagging.get(w, 0.0) > 0.0])
    if len(fit_weeks) < 26:
        raise ValueError("fewer than 26 weeks of bagging before the snapshot week -- choose a later snapshot")
    births = np.arange(first_week - PRE_RECORD_WEEKS, snapshot_week - 26)
    kernels = np.zeros((len(fit_weeks), len(births)))
    flowered_by_snapshot = np.zeros(len(births))
    horizon = snapshot_week + 80
    if horizon > weather_ext.max_week_index:
        raise ValueError("the weather extension does not reach the kernel horizon")
    for j, birth in enumerate(births):
        kernel = flowering_kernel(int(birth), weather_ext, params, min(horizon, weather_ext.max_week_index))
        offsets = fit_weeks - birth - 1
        valid = (offsets >= 0) & (offsets < len(kernel))
        kernels[valid, j] = kernel[offsets[valid]]
        flowered_by_snapshot[j] = kernel[: max(0, snapshot_week - 1 - birth)].sum() / max(kernel.sum(), 1e-12)

    y = bagging.reindex(fit_weeks).to_numpy(dtype=float)
    scale = y.mean()

    def solve(weight: float) -> np.ndarray:
        diff = np.diff(np.eye(len(births)), axis=0)
        lam = weight * float((kernels**2).sum(axis=0).mean())
        design = np.vstack([kernels, np.sqrt(lam) * diff])
        target = np.concatenate([y / scale, np.zeros(len(births) - 1)])
        return lsq_linear(design, target, bounds=(0.0, np.inf), method="bvls").x * scale

    u = solve(smoothing)
    fitted = kernels @ u
    residual = y - fitted
    r2 = 1.0 - (residual**2).sum() / ((y - y.mean()) ** 2).sum()
    rolled = pd.Series(y).rolling(8).sum().dropna(), pd.Series(fitted).rolling(8).sum().dropna()
    r2_8 = 1.0 - ((rolled[0] - rolled[1]) ** 2).sum() / ((rolled[0] - rolled[0].mean()) ** 2).sum()
    identified = births[flowered_by_snapshot >= IDENTIFIED_FLOWERED_SHARE]
    last_identified = int(identified.max()) if len(identified) else int(births[0])
    sensitivity = {w: solve(w) for w in (smoothing / 3.0, smoothing * 3.0)}
    return {
        "births": births,
        "selections": u,
        "last_identified_birth": last_identified,
        "r2_weekly": float(r2),
        "r2_8_week": float(r2_8),
        "selections_per_bagged": float(u[(births >= first_week) & (births <= last_identified)].mean() / y.mean()),
        "sensitivity": sensitivity,
    }


def reconstruct_pre_flowering(
    bagging: pd.Series,
    weather_df: pd.DataFrame,
    params: ParameterSet,
    snapshot_week: int,
    *,
    smoothing: float = 1.0,
) -> tuple[pd.DataFrame, dict]:
    """Vegetative cohorts at the START of `snapshot_week`: the engine is run
    from the record start with the fitted, identified selections as
    sucker-selection data; after the last identified week it falls back to
    the model's own one-follower-per-mother selection. Its snapshot at the
    snapshot week is exact model state (thermal age, counters)."""
    fit = fit_weekly_selections(bagging, weather_df, params, snapshot_week, smoothing=smoothing)
    weather = _weather_provider(weather_df, params)
    run_params = params.override("sucker_selection", {"carrying_capacity": None})

    def pre_state(selections: np.ndarray) -> pd.DataFrame:
        start = max(weather.min_week_index, int(fit["births"][0]))
        keep = (fit["births"] >= start) & (fit["births"] <= fit["last_identified_birth"])
        engine = SimbaPopEngine(
            run_params,
            weather,
            0.0,
            start_week_index=start,
            sucker_selection=_selection_data(fit["births"][keep], selections[keep], weather),
        )
        engine.run(snapshot_week - start)
        snapshot = engine.snapshot_cohorts()
        pre = snapshot[(snapshot["chain"] == "pre_flowering") & (snapshot["population"] >= 0.5)]
        return pd.DataFrame(
            {"chain": "pre_flowering", "tag": np.nan, "population": pre["population"], "age_weeks": pre["age_weeks"]}
        ).sort_values("age_weeks").reset_index(drop=True)

    state = pre_state(fit["selections"])
    totals = [pre_state(u)["population"].sum() for u in fit["sensitivity"].values()]
    report = {
        "pre_flowering_plants": float(state["population"].sum()),
        "bagging_fit_r2_weekly": fit["r2_weekly"],
        "bagging_fit_r2_8_week": fit["r2_8_week"],
        "selections_per_bagged_bunch": fit["selections_per_bagged"],
        "last_identified_selection_week": fit["last_identified_birth"],
        "smoothing_range_pre_total": (float(min(totals)), float(max(totals))),
        "mean_weekly_selections": float(fit["selections"][-52:].mean()),
    }
    return state, report


def reconstruct_initial_population(
    production: pd.DataFrame,
    weather_df: pd.DataFrame,
    params: ParameterSet,
    snapshot_week_index: int | None = None,
    *,
    smoothing: float = 1.0,
) -> tuple[pd.DataFrame, dict]:
    """The farm's standing plants at the start of `snapshot_week_index`
    (default: DEFAULT_HISTORY_WEEKS after its first production week), from its
    canonical production.csv and weather.csv tables only. Returns
    (`chain, tag, population, age_weeks, snapshot_date` table, report dict).
    Rebuild it whenever `params` change -- the inferred vegetative part
    depends on the flowering law."""
    weather = _weather_provider(weather_df, params)
    prod = production.copy()
    prod["week"] = pd.to_datetime(prod["date"]).dt.date.map(weather.week_index_for_date)
    first_week = int(prod["week"].min())
    if snapshot_week_index is None:
        snapshot_week_index = first_week + DEFAULT_HISTORY_WEEKS
    if snapshot_week_index - first_week < MIN_HISTORY_WEEKS:
        raise ValueError(
            f"snapshot week {snapshot_week_index} leaves only {snapshot_week_index - first_week} weeks of production "
            f"history (minimum {MIN_HISTORY_WEEKS}) -- the vegetative plants cannot be inferred reliably"
        )
    if snapshot_week_index > weather.max_week_index + 1:
        raise ValueError("the weather record ends more than a week before the snapshot week")
    # Causal: nothing from the snapshot week on -- neither production nor weather.
    past_weather_df = weather_df[
        pd.to_datetime(weather_df["date"]).dt.date < weather.calendar_date_for_week(snapshot_week_index)
    ]
    prod = prod[prod["week"] < snapshot_week_index]

    bagging = prod.groupby("week")["bagged"].sum(min_count=1).fillna(0.0)
    bagging = bagging.reindex(range(first_week, int(prod["week"].max()) + 1), fill_value=0.0)
    max_age = int(params.general["max_age_weeks_reproductive"])
    post, post_report = reconstruct_post_flowering(production, weather, snapshot_week_index, max_age)
    pre, pre_report = reconstruct_pre_flowering(
        bagging, past_weather_df, params, snapshot_week_index, smoothing=smoothing
    )

    data = pd.concat([pre, post], ignore_index=True)
    data["age_weeks"] = data["age_weeks"].astype(int)
    data["snapshot_date"] = weather.calendar_date_for_week(snapshot_week_index)
    interval = generation_interval_weeks(
        _weather_provider(past_weather_df, params, forward_weeks=POST_SNAPSHOT_WEEKS), params
    )
    report = {
        "snapshot_week_index": snapshot_week_index,
        "snapshot_date": data["snapshot_date"].iloc[0],
        **pre_report,
        **post_report,
        "generation_interval_weeks": interval,
        "implied_mats": pre_report["mean_weekly_selections"] * interval,
        "parameters": params.source,
    }
    return data[["chain", "tag", "population", "age_weeks", "snapshot_date"]], report


# ---------------------------------------------------------------------------
# No records at all
# ---------------------------------------------------------------------------


def spin_up_initial_population(
    weather: WeatherProvider, params: ParameterSet, mats: float, *, weeks: int = 104
) -> pd.DataFrame:
    """A farm of `mats` production units in steady state, with no records:
    select mats / generation-interval followers every week for `weeks` weeks
    from the start of the weather record, then snapshot. Post-flowering plants
    land in UNASSIGNED_TAG (no colour is known). Snapshot date = the week after."""
    interval = generation_interval_weeks(weather, params)
    start = weather.min_week_index
    if start + weeks > weather.max_week_index:
        raise ValueError(f"weather covers fewer than {weeks} weeks")
    rate = mats / interval
    selection_weeks = np.arange(start, start + weeks)
    engine = SimbaPopEngine(
        params.override("sucker_selection", {"carrying_capacity": None}),
        weather,
        0.0,
        start_week_index=start,
        sucker_selection=_selection_data(selection_weeks, np.full(weeks, rate), weather),
    )
    engine.run(weeks)
    snapshot = engine.snapshot_cohorts()
    snapshot = snapshot[snapshot["population"] >= 0.5]
    data = snapshot[["chain", "tag", "population", "age_weeks"]].copy()
    data.loc[data["chain"] == "pre_flowering", "tag"] = np.nan
    data["snapshot_date"] = weather.calendar_date_for_week(engine.current_week_index)
    return data.sort_values(["chain", "age_weeks"]).reset_index(drop=True)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    from utils.adapt_legacy_exports import snapshot_week_index as parse_snapshot

    farm_dir = Path(sys.argv[1])
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else farm_dir / "initial_population.csv"
    preferred = [name for name in ("initial_population.xlsx", "planting_schedule.xlsx") if (farm_dir / name).exists()]
    if preferred and len(sys.argv) <= 3:
        raise SystemExit(
            f"{farm_dir} has {preferred[0]}, which build_farm_inputs.py turns into initial_population.csv; "
            "give an output path as the third argument to write the reconstruction elsewhere"
        )
    params = ParameterSet.from_tixier_defaults()
    weather_df = pd.read_csv(farm_dir / "weather.csv")
    production = pd.read_csv(farm_dir / "production.csv")
    weather = _weather_provider(weather_df, params)
    snapshot = parse_snapshot(sys.argv[2], weather) if len(sys.argv) > 2 else None
    data, report = reconstruct_initial_population(production, weather_df, params, snapshot)
    for key, value in report.items():
        print(f"  {key}: {value}")
    data.to_csv(out_path, index=False, lineterminator="\n")
    print(f"Wrote {out_path} ({len(data)} cohorts)")


if __name__ == "__main__":
    main()
