"""Build every canonical input file for one farm directory from the legacy
Excel exports a farm manager keeps -- the files notebooks 01-03 read.

    <farm_dir>/weather.xlsx            (required)  -> weather.csv
    <farm_dir>/production.xlsx         (required)  -> production.csv
    <farm_dir>/sucker_selection.xlsx   (optional)  -> sucker_selection.csv   (Mode C; else
                                                      production.xlsx's Desbunche columns, if any)
    <farm_dir>/initial_population.xlsx (optional)  -> initial_population.csv
      or planting_schedule.xlsx        (optional)     (census > planting plan >
      or neither                                       reconstruction from production)

With only weather.xlsx + production.xlsx and no desuckering records (a Mode
A/B farm) the initial
population is reconstructed from the production records
(build_initial_population.reconstruct_initial_population). Every output is
read back through py_simba_pop's own readers and an engine is constructed at
the snapshot week as a smoke check; data-quality findings are printed.

Run (from docs/examples/):
    python -m utils.build_farm_inputs data/<farm> [--snapshot "2021 S:01"] [--edad-offset 0|1]
    python -m utils.build_farm_inputs --all
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from py_simba_pop import (
    ParameterSet,
    PopulationInventoryReader,
    RibbonProductionData,
    RibbonProductionReader,
    SimbaPopEngine,
    SuckerSelectionData,
    SuckerSelectionReader,
)
from utils.adapt_legacy_exports import (
    AdaptResult,
    adapt_inventory,
    adapt_legacy_sucker_selection_columns,
    adapt_planting_schedule,
    adapt_production,
    adapt_sucker_selection,
    adapt_weather,
    load_weather,
    snapshot_week_index,
    write_csv,
)
from utils.build_initial_population import reconstruct_initial_population

DATA_ROOT = Path(__file__).parent.parent / "data"


def _report(title: str, result: AdaptResult | None = None, lines: list[str] | None = None) -> None:
    print(f"  {title}")
    for line in (result.messages if result is not None else []) + (lines or []):
        print(f"      - {line}")


def build_farm(
    farm_dir: Path, params: ParameterSet, snapshot: str | None = None, edad_offset: int | None = None
) -> dict:
    """Writes the canonical CSVs for `farm_dir` and returns a summary dict.
    `snapshot` only applies to a reconstruction; `edad_offset` overrides the
    detected Edad convention (0: weeks since bagging, 1: bagging week = 1)."""
    print(f"\n=== {farm_dir}")
    weather_result = adapt_weather(farm_dir / "weather.xlsx")
    write_csv(weather_result.data, farm_dir / "weather.csv")
    _report("weather.xlsx -> weather.csv", weather_result)
    weather = load_weather(farm_dir / "weather.csv", params)

    production_result = adapt_production(farm_dir / "production.xlsx", weather, edad_offset=edad_offset)
    write_csv(production_result.data, farm_dir / "production.csv")
    _report("production.xlsx -> production.csv", production_result)

    sucker_path = farm_dir / "sucker_selection.csv"
    if (farm_dir / "sucker_selection.xlsx").exists():
        sucker_result = adapt_sucker_selection(farm_dir / "sucker_selection.xlsx", weather)
        source = "sucker_selection.xlsx"
    else:
        sucker_result = adapt_legacy_sucker_selection_columns(farm_dir / "production.xlsx", weather)
        source = "production.xlsx (Desbunche columns)"
    if sucker_result is not None and len(sucker_result.data):
        write_csv(sucker_result.data, sucker_path)
        _report(f"{source} -> sucker_selection.csv", sucker_result)
    else:
        if sucker_path.exists():
            sucker_path.unlink()
        _report("no sucker-selection records -> Mode B only (engine uses the model's own selection)")

    production = production_result.data
    if (farm_dir / "initial_population.xlsx").exists():
        inventory = adapt_inventory(
            farm_dir / "initial_population.xlsx", weather, age_offset=production_result.details["edad_offset"]
        )
        population, how = inventory.data, "initial_population.xlsx (census)"
        messages = inventory.messages
    elif (farm_dir / "planting_schedule.xlsx").exists():
        plan = adapt_planting_schedule(farm_dir / "planting_schedule.xlsx", weather)
        population, how, messages = plan.data, "planting_schedule.xlsx (new plantation)", plan.messages
    else:
        weather_df = weather_result.data
        week = snapshot_week_index(snapshot, weather) if snapshot else None
        population, rebuilt = reconstruct_initial_population(production, weather_df, params, week)
        how = "reconstructed from production records"
        messages = [
            (f"snapshot: start of grid week {rebuilt['snapshot_week_index']} ({rebuilt['snapshot_date']}), "
            "using only records before it"),
            (f"bunches standing: {rebuilt['standing_bunches']:,.0f} (later cuts traced to them: "
            f"{rebuilt['later_cuts_of_standing']:,.0f}, plus field losses of any age in those weeks: "
            f"{rebuilt['later_field_losses_all']:,.0f}; closed cohorts dropped: {rebuilt['closed_unrecorded']:,.0f})"),
            (f"vegetative plants inferred: {rebuilt['pre_flowering_plants']:,.0f} (bagging fit R2 weekly "
            f"{rebuilt['bagging_fit_r2_weekly']:.3f}, 8-week {rebuilt['bagging_fit_r2_8_week']:.3f}; "
            f"smoothing range {rebuilt['smoothing_range_pre_total'][0]:,.0f}-{rebuilt['smoothing_range_pre_total'][1]:,.0f})"),
            (f"followers selected per bagged bunch: {rebuilt['selections_per_bagged_bunch']:.3f} "
            f"(one follower per mother gives ~1); implied production units ~{rebuilt['implied_mats']:,.0f} mats"),
        ]
    if snapshot and how != "reconstructed from production records":
        messages.append(f"--snapshot {snapshot!r} not used: the initial population comes from {how.split()[0]}")
    write_csv(population, farm_dir / "initial_population.csv")
    _report(f"{how} -> initial_population.csv", lines=messages)

    summary = _check_outputs(farm_dir, params, production_result)
    _report("checks", lines=summary.pop("lines"))
    return summary


def _check_outputs(farm_dir: Path, params: ParameterSet, production_result: AdaptResult) -> dict:
    """Round-trips every output through the package readers and asserts the
    invariants the notebooks rely on."""
    weather = load_weather(farm_dir / "weather.csv", params)
    weather_df = pd.read_csv(farm_dir / "weather.csv")
    assert weather_df["tmean"].notna().all(), "weather.csv has blank tmean"
    assert len(weather_df) % 7 == 0, "weather.csv must hold whole weeks only"

    production = RibbonProductionReader.read(farm_dir / "production.csv").data
    for d in production["date"]:
        assert weather.calendar_date_for_week(weather.week_index_for_date(d)) == d, f"{d} is not a grid date"
    wide_totals = production_result.data.groupby("tag")[["bagged", "cut"]].sum()
    csv_totals = production.groupby("tag")[["bagged", "cut"]].sum()
    assert (wide_totals - csv_totals).abs().to_numpy().max() < 1e-6, "per-colour totals changed on the way"
    # Independent of the adapter's column matching: every raw column ending in the
    # field name (any spelling) must land in production.csv.
    raw = pd.read_excel(farm_dir / "production.xlsx")
    raw = raw[raw.iloc[:, 0].notna()]
    for field, column in (("racimosembolsados", "bagged"), ("cortados", "cut")):
        matching = [c for c in raw.columns if " ".join(str(c).split()).casefold().endswith(field)]
        raw_total = raw[matching].apply(pd.to_numeric, errors="coerce").sum().sum()
        assert abs(raw_total - production[column].sum()) < 1e-6, f"{column}: production.xlsx and .csv totals differ"
    ribbon = RibbonProductionData(production, weather)

    sucker = None
    if (farm_dir / "sucker_selection.csv").exists():
        sucker = SuckerSelectionData(SuckerSelectionReader.read(farm_dir / "sucker_selection.csv").data, weather)

    population = PopulationInventoryReader.read(farm_dir / "initial_population.csv").data
    snapshot_date = pd.to_datetime(pd.read_csv(farm_dir / "initial_population.csv")["snapshot_date"].iloc[0]).date()
    start = weather.week_index_for_date(snapshot_date)
    engine = SimbaPopEngine(
        params,
        weather,
        population,
        start_week_index=start,
        ribbon_production=ribbon,
        sucker_selection=sucker,
        tags=list(ribbon.tags()),
    )
    engine.run(4)
    last_production = int(max(weather.week_index_for_date(d) for d in production["date"]))
    last_usable = min(weather.max_week_index, last_production)
    by_chain = population.groupby("chain")["population"].sum()
    return {
        "farm": farm_dir.name,
        "start_week_index": start,
        "last_usable_week": last_usable,
        "mode_c": sucker is not None,
        "lines": [
            "readers accept every file; engine built at the snapshot week and stepped 4 weeks",
            (f"simulation window: grid weeks {start}..{last_usable} ({last_usable - start + 1} weeks); "
            f"modes available: {'A, B, C' if sucker is not None else 'A, B'}"),
            (f"initial population: {by_chain.get('pre_flowering', 0):,.0f} vegetative + "
            f"{by_chain.get('post_flowering', 0):,.0f} bunched plants"),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("farm_dirs", nargs="*", type=Path)
    parser.add_argument("--all", action="store_true", help="every folder under data/ with a production.xlsx")
    parser.add_argument("--snapshot", help='reconstruction snapshot: a date "YYYY-MM-DD" or a week "YYYY S:WW"')
    parser.add_argument(
        "--edad-offset",
        type=int,
        choices=(0, 1),
        help="the farm's Edad convention if it can't be detected: 0 = weeks since bagging, 1 = bagging week is week 1",
    )
    args = parser.parse_args()
    farm_dirs = list(args.farm_dirs)
    if args.all:
        farm_dirs += sorted(p.parent for p in DATA_ROOT.glob("*/production.xlsx"))
    if not farm_dirs:
        parser.error("give one or more farm directories, or --all")
    params = ParameterSet.from_tixier_defaults()
    for farm_dir in farm_dirs:
        build_farm(farm_dir, params, args.snapshot, args.edad_offset)


if __name__ == "__main__":
    main()
