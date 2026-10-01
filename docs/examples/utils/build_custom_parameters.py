"""Recipe: derive your own harvest-timing parameters from ribbon-tagged
field data, and save them as a plain YAML file `ParameterSet.from_yaml` can
load -- i.e. your own "calibration file."

This is NOT part of the installed py_simba_pop package: py_simba_pop ships
zero statistical-fitting code (see docs/quickstart.rst, "Custom parameter
files"). Every parameter already has a hardcoded Tixier et al. (2004)
default; fitting your own is optional, and this script is a worked,
copy-and-adapt recipe for anyone who wants to build one from their own
field data -- a nonparametric life-table hazard estimation, not
scipy.optimize.curve_fit: a real per-cohort series is typically only 2-4
nonzero weekly bins, too sparse to reliably identify a 3-parameter
continuous curve.

Under the default "cohort" rate basis the fitted harvest histogram is
written as SHARES of the cohort (last entry 1.0 = the remainder), the form
that basis reads; under "current_pool" it is written as hazards.

Run (from docs/examples/): python -m utils.build_custom_parameters [farm_dir] [out.yaml]
e.g. python -m utils.build_custom_parameters data/<farm>
(farm_dir defaults to data/synthetic; out.yaml defaults to
<farm_dir>/custom_params.yaml). Needs the canonical CSVs from
utils/build_farm_inputs.py.
"""

from __future__ import annotations

import math
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from py_simba_pop import (
    ColumnMap,
    ParameterSet,
    PopulationInventoryReader,
    RibbonProductionData,
    RibbonProductionReader,
    SimbaPopEngine,
    SuckerSelectionData,
    SuckerSelectionReader,
    WeatherProvider,
)


@dataclass(frozen=True, slots=True)
class RibbonObservation:
    """One recoverable cohort: bagged in week `bagging_week`, with its
    cut/thermal trajectory known for every week offset up to
    `last_observable_offset` (the right-censoring boundary)."""

    tag: str
    bagging_week: int
    n0: float  # population at bagging
    cut_by_week_offset: Mapping[int, float]
    thermal_by_week_offset: Mapping[int, float]
    last_observable_offset: int


@dataclass(frozen=True, slots=True)
class HazardFit:
    threshold_dd: float
    threshold_dd_sd: float
    n_cohorts: int
    hazard_weeks: list[float]
    hazard_weeks_n_at_risk: list[int]


def load_ribbon_observations(tidy: pd.DataFrame, weather: WeatherProvider) -> list[RibbonObservation]:
    """Decode the standardized tidy (tag, date, bagged, cut, age_at_cut)
    production frame into per-cohort observations.

    Cohort identification: each cut event's own age_at_cut (weeks since
    bagging) back-computes its bagging week (bagging_week = week -
    age_at_cut), cross-
    checked against that tag's actual bagged count at that week -- robust
    because ribbon colors repeat cyclically across years, so color alone
    never identifies a cohort. Rows whose implied bagging week falls before
    the data window are excluded outright: no known n0, can't be
    constructed.
    """
    tidy = tidy.copy()
    tidy["week"] = tidy["date"].map(weather.week_index_for_date)
    indexed = tidy.set_index(["tag", "week"])
    max_week = int(tidy["week"].max())

    bagging_n0: dict[tuple[str, int], float] = {}
    cuts_by_cohort: dict[tuple[str, int], dict[int, float]] = {}

    for (tag, week), row in indexed.iterrows():
        cut = row.get("cut")
        age = row.get("age_at_cut")
        if pd.isna(cut) or pd.isna(age) or cut <= 0:
            continue
        bagging_week = week - int(age)
        if bagging_week < 0:
            continue  # left-censored: bagged before the data window, unrecoverable
        bag_key = (tag, bagging_week)
        if bag_key not in indexed.index:
            continue
        bagged = indexed.loc[bag_key, "bagged"]
        if pd.isna(bagged) or bagged <= 0:
            continue
        offset = week - bagging_week
        if offset < 1:
            continue  # age_at_cut=0 (cut the same week as bagging) has no thermal-age
            # trajectory point (thermal_by_week_offset only ever starts at offset=1,
            # matching the paper's own WH indexing) -- not a recoverable trajectory point.
        bagging_n0[bag_key] = float(bagged)
        cuts_by_cohort.setdefault(bag_key, {})[offset] = float(cut)

    observations: list[RibbonObservation] = []
    for (tag, bagging_week), n0 in bagging_n0.items():
        last_observable_offset = max_week - bagging_week
        if last_observable_offset <= 0:
            continue
        trajectory = weather.thermal_trajectory(bagging_week + 1, bagging_week + last_observable_offset)
        thermal_by_offset = {offset: trajectory[offset - 1] for offset in range(1, last_observable_offset + 1)}
        observations.append(
            RibbonObservation(
                tag=tag,
                bagging_week=bagging_week,
                n0=n0,
                cut_by_week_offset=cuts_by_cohort[(tag, bagging_week)],
                thermal_by_week_offset=thermal_by_offset,
                last_observable_offset=last_observable_offset,
            )
        )
    return observations


def fit_reproductive_parameters(
    observations: Sequence[RibbonObservation],
    *,
    onset_epsilon: float = 0.01,
    min_at_risk: int = 20,
    max_hazard_bins: int = 12,
    exhaustion_epsilon: float = 0.01,
    min_trailing_hazard: float = 0.02,
) -> HazardFit:
    """Nonparametric life-table fit for harvest.threshold_dd / hazard_weeks.

    Stage 1 (threshold_dd): each cohort's first harvest week brackets the
    threshold -- its thermal age that week is >= HT, and the week before it
    was < HT. The estimate is the value inside the largest number of those
    (lower, upper] brackets (interval censoring; averaging the first-harvest
    thermal age itself would overshoot by about half a week of heat units).

    Stage 2 (hazard_weeks): pools numerator and denominator SEPARATELY
    across cohorts before dividing (the standard discrete-time life-table
    estimator), reindexing each cohort's cut series onto a WH-equivalent bin
    using the same Eq19'/19'' recursion the live model uses, driven by the
    fitted threshold. Stops extending once the pooled at-risk count drops
    below `min_at_risk`, drops trailing bins where (almost) nothing is cut
    any more, then forces the last kept bin to 1.0 (full exhaustion,
    matching Tixier's own convention).

    A cohort stops contributing once its own remaining population drops
    below `exhaustion_epsilon` of n0, and no cohort contributes past
    `max_hazard_bins` regardless -- this dataset's field losses aren't
    tagged per-cohort/age, so a naive "remaining = n0 - cumulative_cut"
    never reaches exactly 0 for a cohort with unattributed loss; without
    these caps its hazard-bin counter would keep incrementing for the
    cohort's entire observable life.
    """
    if not observations:
        raise ValueError("no usable cohort observations to fit from")

    onset_thermal_durations: list[float] = []
    brackets: list[tuple[float, float]] = []
    for obs in observations:
        if not obs.cut_by_week_offset:
            continue
        cumulative = 0.0
        for offset in sorted(obs.cut_by_week_offset):
            cumulative += obs.cut_by_week_offset[offset]
            if cumulative / obs.n0 >= onset_epsilon:
                upper = obs.thermal_by_week_offset[offset]
                lower = obs.thermal_by_week_offset.get(offset - 1, 0.0)
                onset_thermal_durations.append(upper)
                brackets.append((lower, upper))
                break

    if not onset_thermal_durations:
        raise ValueError("no cohort ever reached the onset epsilon fraction; cannot fit threshold_dd")

    threshold_dd = _most_consistent_threshold(brackets)
    threshold_dd_sd = statistics.pstdev(onset_thermal_durations) if len(onset_thermal_durations) > 1 else 0.0

    numerators: dict[int, float] = {}
    denominators: dict[int, float] = {}

    for obs in observations:
        wh = 0
        remaining = obs.n0
        for offset in range(1, obs.last_observable_offset + 1):
            thermal_age = obs.thermal_by_week_offset[offset]
            wh = 0 if thermal_age < threshold_dd else wh + 1
            if wh == 0:
                continue
            if wh > max_hazard_bins:
                break
            harvested = obs.cut_by_week_offset.get(offset, 0.0)
            numerators[wh] = numerators.get(wh, 0.0) + harvested
            denominators[wh] = denominators.get(wh, 0.0) + remaining
            remaining = max(0.0, remaining - harvested)
            if remaining <= exhaustion_epsilon * obs.n0:
                break

    if not denominators:
        raise ValueError("no cohort ever crossed the fitted threshold; cannot fit hazard_weeks")

    hazard_weeks: list[float] = []
    n_at_risk: list[int] = []
    for wh in range(1, max(denominators) + 1):
        at_risk = denominators.get(wh, 0.0)
        if at_risk < min_at_risk:
            break
        harvested = numerators.get(wh, 0.0)
        hazard_weeks.append(min(1.0, harvested / at_risk))
        n_at_risk.append(round(at_risk))

    if not hazard_weeks:
        raise ValueError(
            f"no hazard bin reached the minimum at-risk sample size ({min_at_risk}); "
            "supply more data or lower min_at_risk"
        )
    while len(hazard_weeks) > 1 and hazard_weeks[-1] < min_trailing_hazard:
        hazard_weeks.pop()  # only unattributed losses left at risk, nothing cut any more
        n_at_risk.pop()
    hazard_weeks[-1] = 1.0  # force full exhaustion in the last supported bin

    return HazardFit(
        threshold_dd=threshold_dd,
        threshold_dd_sd=threshold_dd_sd,
        n_cohorts=len(onset_thermal_durations),
        hazard_weeks=hazard_weeks,
        hazard_weeks_n_at_risk=n_at_risk,
    )


def _most_consistent_threshold(brackets: Sequence[tuple[float, float]]) -> float:
    """The threshold inside the most (lower, upper] brackets; the midpoint of
    the best-supported stretch when several values tie."""
    events = sorted([(lower, 1) for lower, _ in brackets] + [(upper, -1) for _, upper in brackets])
    best_count, best_range, count = -1, (0.0, 0.0), 0
    for i, (value, step) in enumerate(events[:-1]):
        count += step
        nxt = events[i + 1][0]
        if nxt > value and count > best_count:
            best_count, best_range = count, (value, nxt)
    return 0.5 * (best_range[0] + best_range[1])


@dataclass(frozen=True, slots=True)
class AggregateCurveMatch:
    """NOT a parameter fit -- a similarity score between a predicted and an
    observed weekly aggregate series. flowering.*/sucker_selection.* cannot
    be fit the same way harvest.* is: there is no per-cohort/age attribution
    for a desuckering event (no Edad-equivalent), and unlike harvest, sucker
    output is explicitly non-depleting, so there is no "remaining
    population" trajectory to build a life-table from at all."""

    best_lag_weeks: int
    correlation_at_best_lag: float
    rmse_at_best_lag: float
    predicted_total: float
    observed_total: float
    total_ratio: float  # predicted_total / observed_total


def evaluate_aggregate_curve_match(
    predicted: pd.Series,
    observed: pd.Series,
    *,
    max_lag_weeks: int = 12,
) -> AggregateCurveMatch:
    """Cross-correlation-style scan over lags in
    [-max_lag_weeks, +max_lag_weeks] (in weeks) for the lag that maximizes
    Pearson r between `predicted` shifted by that lag and `observed`, plus
    RMSE and total-mass ratio at that lag. Both series must be indexed by
    (integer) week; they are reindexed onto the union of both series' week
    ranges, with gaps filled as 0.0, before lag-scanning.
    """
    predicted = predicted.astype(float)
    observed = observed.astype(float)
    week_start = int(min(predicted.index.min(), observed.index.min()))
    week_end = int(max(predicted.index.max(), observed.index.max()))
    weeks = range(week_start, week_end + 1)
    predicted = predicted.reindex(weeks).fillna(0.0)
    observed = observed.reindex(weeks).fillna(0.0)

    best_lag = 0
    best_corr = float("-inf")
    best_rmse = float("nan")
    for lag in range(-max_lag_weeks, max_lag_weeks + 1):
        shifted = predicted.shift(lag).to_numpy()
        obs = observed.to_numpy()
        mask = ~pd.isna(shifted)
        if mask.sum() < 2:
            continue
        p, o = shifted[mask], obs[mask]
        if p.std() == 0.0 or o.std() == 0.0:
            continue
        corr = float(pd.Series(p).corr(pd.Series(o)))
        if math.isnan(corr) or corr <= best_corr:
            continue
        best_corr = corr
        best_lag = lag
        best_rmse = float(((p - o) ** 2).mean() ** 0.5)

    if best_corr == float("-inf"):
        raise ValueError(
            "no overlapping, non-constant window found across any lag in "
            f"[-{max_lag_weeks}, {max_lag_weeks}]; try a wider max_lag_weeks "
            "or check that both series have genuine variation"
        )

    predicted_total = float(predicted.sum())
    observed_total = float(observed.sum())
    total_ratio = predicted_total / observed_total if observed_total else float("nan")

    return AggregateCurveMatch(
        best_lag_weeks=best_lag,
        correlation_at_best_lag=best_corr,
        rmse_at_best_lag=best_rmse,
        predicted_total=predicted_total,
        observed_total=observed_total,
        total_ratio=total_ratio,
    )


def hazards_to_shares(hazards: Sequence[float]) -> list[float]:
    """Life-table hazards -> the per-cohort shares the "cohort" rate basis
    reads (share_j = h_j x prod(1 - h_i, i < j)); the last entry stays 1.0,
    meaning "whatever remains"."""
    shares, remaining = [], 1.0
    for hazard in hazards[:-1]:
        shares.append(remaining * hazard)
        remaining *= 1.0 - hazard
    return [*shares, 1.0]


def _load_weather(farm_dir: Path, params: ParameterSet) -> WeatherProvider:
    """Standardized readers only care about the CANONICAL schema (date,
    tmean) -- the column NAMES in a given farm's own file are the one thing
    still caller-supplied (a trivial rename, not a reshape; see
    py_simba_pop.readers.weather_reader's docstring)."""
    candidates = (
        (farm_dir / "weather.csv", ColumnMap()),
        (farm_dir / "weather.xlsx", ColumnMap(date="Fecha", tmean="Temperatura")),
    )
    for path, columns in candidates:
        if path.exists():
            return WeatherProvider.from_file_and_params(path, params, columns)
    raise FileNotFoundError(f"no weather.csv or weather.xlsx found in {farm_dir}")


def main() -> None:
    farm_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "data/synthetic")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else farm_dir / "custom_params.yaml"
    defaults = ParameterSet.from_tixier_defaults()
    weather = _load_weather(farm_dir, defaults)

    prod_result = RibbonProductionReader.read(farm_dir / "production.csv")
    prod_result.notices.emit_warnings()
    ribbon_df = prod_result.data

    available_weeks = weather.max_week_index
    production_weeks = ribbon_df["date"].map(weather.week_index_for_date)
    if (production_weeks > available_weeks).any():
        print(
            f"  Note: production data reaches week {int(production_weeks.max())} but weather only "
            f"covers up to week {available_weeks}; truncating to the weeks weather covers."
        )
        ribbon_df = ribbon_df[production_weeks <= available_weeks].reset_index(drop=True)

    observations = load_ribbon_observations(ribbon_df, weather)
    fit = fit_reproductive_parameters(observations)

    print(f"Fitted from {fit.n_cohorts} recoverable cohorts in {farm_dir}:")
    print(f"  harvest.threshold_dd = {fit.threshold_dd:.1f} (SD={fit.threshold_dd_sd:.1f})")
    print(f"  harvest hazards (life table) = {[round(h, 3) for h in fit.hazard_weeks]}")
    cohort_basis = defaults.general["rate_basis"] == "cohort"
    harvest_values = hazards_to_shares(fit.hazard_weeks) if cohort_basis else fit.hazard_weeks
    print(
        f"  harvest.hazard_weeks as written ({'shares, rate_basis=cohort' if cohort_basis else 'hazards'}) = "
        f"{[round(h, 3) for h in harvest_values]}"
    )
    print(
        "  (compare to Tixier's own French West Indies defaults: "
        "threshold_dd=900, hazard_weeks=[0.2, 0.625, 1.0])"
    )

    sucker_selection_path = farm_dir / "sucker_selection.csv"
    has_pre_flowering_signal = sucker_selection_path.exists()
    flowering_comment = "this dataset has no pre-flowering lineage; stays at Tixier defaults"
    sucker_comment = "same reason"

    if has_pre_flowering_signal:
        flowering_comment = (
            "this dataset's pre-flowering signal is a farm-wide aggregate with no per-cohort/"
            "per-plant age attribution (no age_at_cut-equivalent for desuckering events) -- not "
            "identifiable by the same nonparametric life-table method used for harvest.*; see "
            "evaluate_aggregate_curve_match for validation-only comparison instead"
        )
        sucker_comment = flowering_comment

        ribbon_data = RibbonProductionData(ribbon_df, weather)
        sel_result = SuckerSelectionReader.read(sucker_selection_path)
        sucker_selection = SuckerSelectionData(sel_result.data, weather)

        end_week = int(ribbon_df["date"].map(weather.week_index_for_date).max())
        inventory_path = farm_dir / "initial_population.csv"
        inventory = PopulationInventoryReader.read(inventory_path).data
        snapshot_date = pd.to_datetime(pd.read_csv(inventory_path)["snapshot_date"].iloc[0]).date()
        start_week = weather.week_index_for_date(snapshot_date)
        engine = SimbaPopEngine(
            defaults,
            weather,
            inventory,
            start_week_index=start_week,
            ribbon_production=ribbon_data,
            sucker_selection=sucker_selection,
            tags=list(ribbon_data.tags()),
        )
        farm_output = engine.run(end_week - start_week + 1)
        tag_output = engine.get_tag_output()
        pooled_bagged = tag_output.groupby("week_index")["bagged_input"].sum()

        comparisons = (
            (
                "predicted FLt vs. pooled bagged_input (flowering)",
                farm_output.set_index("week_index")["FLt"],
                pooled_bagged,
            ),
            (
                "predicted sucker_output_post (parous) vs. real parous_count",
                farm_output.set_index("week_index")["sucker_output_post"],
                sel_result.data.assign(week_index=sel_result.data["date"].map(weather.week_index_for_date))
                .set_index("week_index")["parous"],
            ),
            (
                "predicted sucker_output_pre (nulliparous) vs. real nulliparous_count",
                farm_output.set_index("week_index")["sucker_output_pre"],
                sel_result.data.assign(week_index=sel_result.data["date"].map(weather.week_index_for_date))
                .set_index("week_index")["nulliparous"],
            ),
        )
        print("\nPre-flowering signal detected -- validation-only comparisons (NOT parameter fits):")
        for label, predicted, observed in comparisons:
            try:
                match = evaluate_aggregate_curve_match(predicted, observed)
            except ValueError:
                # Under the legacy "current_pool" rate basis the predicted parous
                # series is identically 0 (post-flowering thermal age resets and
                # HT=900 < BT=2180).
                print(
                    f"  {label}: no correlation computable (predicted or observed series is "
                    f"constant) -- predicted_total={float(predicted.sum()):.1f}, "
                    f"observed_total={float(observed.sum()):.1f}"
                )
                continue
            print(
                f"  {label}: best_lag={match.best_lag_weeks}w, "
                f"corr={match.correlation_at_best_lag:.3f}, rmse={match.rmse_at_best_lag:.1f}, "
                f"total_ratio={match.total_ratio:.3f}"
            )

    custom_parameters = {
        "general": dict(defaults.general),
        "flowering": dict(defaults.flowering),  # see flowering_comment above
        "sucker_selection": dict(defaults.sucker_selection),  # see sucker_comment above
        "harvest": {"threshold_dd": fit.threshold_dd, "hazard_weeks": harvest_values},
        "mortality_replacement": dict(defaults.mortality_replacement),
    }
    with out_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(custom_parameters, fh, sort_keys=False)
        fh.write(f"\n# flowering: {flowering_comment}\n# sucker_selection: {sucker_comment}\n")
    print(f"\nWrote {out_path}")
    print(f"Load it with: ParameterSet.from_yaml({str(out_path)!r})")


if __name__ == "__main__":
    main()
