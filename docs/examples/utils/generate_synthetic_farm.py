"""Generate two clearly-synthetic banana farms, as the legacy Excel exports a
farm manager keeps week by week, then build their canonical CSVs with
build_farm_inputs.py (exactly the path a real user takes).

  data/synthetic/                          an established 165-ha farm
      weather.xlsx, production.xlsx, sucker_selection.xlsx, initial_population.xlsx
      -> all three modes (A: census only, B: + ribbon bagging, C: + desuckering)
  data/synthetic_new_plantation/           a 170-ha farm planted from scratch
      weather.xlsx, production.xlsx, sucker_selection.xlsx, planting_schedule.xlsx
      -> Mode A from establishment (the setting of Tixier et al. 2004's own
         validation fields), and B/C on the same records

How the "true farm" is made (fixed seed, reproducible):

* Biology = py_simba_pop's own model with Tixier et al. (2004) defaults under
  the per-cohort rate basis: every plant flowers once, is harvested once, and
  keeps one follower.
* The farm's desuckering PRACTICE is not Tixier's calibration but the field
  calibration of Guarin & Ochoa (2012, Table 2; Uraba, Colombia):
  BT = 2080 degree-days, as = 0.171, bs = 5.5, cs = 0.379 -- later and more
  spread-out follower selection, ~1.6 bunches per mat per year. Mode C (fed
  these desuckering records) follows the farm; Modes A/B, which assume
  Tixier's own selection curve, drift in cycle timing -- the reason real
  sucker-selection data matters.
* Size: 1,650 mats/ha. A mat (production unit) is a mother plant plus at most
  one follower, so a census counts ~1.5 standing plants per mat.
* A new farm is established the way agronomists do it: a fraction of the area
  planted each week (~10 ha/week, sized to the packing plant) so fruit is
  harvested every week, never as one synchronized planting.
* Records: bagged bunches = the model's weekly flowering, routed to the week's
  ribbon colour (8-colour rotation); every bagged cohort is then followed
  with binomial draws of the model's own harvest schedule (integer, conserving
  counts), field losses (~1.5%, mostly around harvest) and packing-plant
  rejects (~2.5% of the cut, part of Cortados). Edad counts the bagging week
  as week 1 (the farm_1 convention). Desuckering records split followers into
  nulliparous (mother not flowered yet) and parous (mother already flowered),
  with a 2-week gap near the end (unrecorded weeks -> the engine's fallback).
  The census carries +-2-3% counting noise.

Run (from docs/examples/): python -m utils.generate_synthetic_farm [established|new_plantation ...]
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from py_simba_pop import (
    ParameterSet,
    PopulationInventoryReader,
    RibbonProductionData,
    RibbonProductionReader,
    SimbaPopEngine,
    SuckerSelectionData,
    SuckerSelectionReader,
    WeatherProvider,
)
from py_simba_pop.cohorts import ChainKind, CohortChain
from utils.adapt_legacy_exports import (
    adapt_planting_schedule,
    adapt_weather,
    date_to_grid_week,
    grid_week_index,
    write_csv,
)
from utils.build_farm_inputs import build_farm
from utils.build_initial_population import (
    generation_interval_weeks,
    reconstruct_initial_population,
)

DATA_ROOT = Path(__file__).parent.parent / "data"
SEED = 20260924
# Fixed workbook metadata, for reproducible xlsx files (zip entry times are naive).
XLSX_TIMESTAMP = datetime(2026, 9, 24)  # noqa: DTZ001
PLANTS_PER_HA = 1650
FARM_PRACTICE = {"threshold_dd": 2080.0, "amplitude": 0.171, "peak_weeks": 5.5, "spread": 0.379}
ROTATION = ("Rojo", "Café", "Negro", "Verde", "Azul", "Blanco", "Amarillo", "Morado")  # weekly ribbon order
EXPORT_COLOURS = ("Azul", "Blanco", "Amarillo", "Morado", "Rojo", "Café", "Negro", "Verde")  # sheet column order
FIELDS = ("Racimos Perdidos", "Cortados", "Rechazados", "Edad", "RacimosEmbolsados", "PesoRacimo", "Calibracion", "manos")
LOSS_HAZARD_EARLY, LOSS_HAZARD_HARVEST = 0.0005, 0.003  # weekly field-loss hazard before / from week 8 after bagging
REJECT_SHARE = 0.025
WEATHER_START = date(2016, 1, 4)  # a Monday: grid weeks = ISO weeks
WEATHER_END = date(2025, 12, 24)  # 3 days into a week -> the adapter drops the incomplete week


@dataclass(frozen=True)
class Scenario:
    name: str
    directory: Path
    hectares: float
    record_start: date  # first week of production records (ISO Monday)
    record_end: date
    weather_start: date
    census_date: date | None  # established farm only
    planting_weeks: int | None  # new plantation only
    sucker_gap: tuple[str, ...]  # week labels left blank in sucker_selection.xlsx


ESTABLISHED = Scenario(
    name="established",
    directory=DATA_ROOT / "synthetic",
    hectares=165.0,
    record_start=date(2018, 12, 31),  # 2019 S:01
    record_end=date(2025, 11, 24),  # 2025 S:48
    weather_start=WEATHER_START,
    census_date=date(2018, 12, 31),
    planting_weeks=None,
    sucker_gap=("2025 S:40", "2025 S:41"),
)
NEW_PLANTATION = Scenario(
    name="new_plantation",
    directory=DATA_ROOT / "synthetic_new_plantation",
    hectares=170.0,
    record_start=date(2018, 12, 31),
    record_end=date(2025, 11, 24),
    weather_start=date(2018, 12, 31),
    census_date=None,
    planting_weeks=17,
    sucker_gap=("2025 S:40", "2025 S:41"),
)


# ---------------------------------------------------------------------------
# Weather: one synthetic site, shared by both farms
# ---------------------------------------------------------------------------


def synthetic_daily_weather(rng: np.random.Generator) -> pd.DataFrame:
    """Lowland-tropics climate like the real farms: ~25.6 C mean, a ~1 C
    seasonal swing peaking in May (~74 -> ~88 degree-days/week), AR(1)
    day-to-day noise, and the export's other columns for realism."""
    days = pd.date_range(WEATHER_START, WEATHER_END, freq="D")
    n = len(days)
    doy = days.dayofyear.to_numpy()
    seasonal = 25.6 + 1.0 * np.sin(2 * np.pi * (doy - 44) / 365.25)
    noise = np.zeros(n)
    for i in range(1, n):
        noise[i] = 0.6 * noise[i - 1] + rng.normal(0.0, 0.75)
    tmean = seasonal + noise
    half_range = 3.9 + 0.8 * np.cos(2 * np.pi * (doy - 60) / 365.25) + rng.normal(0, 0.4, n)
    midpoint = tmean + 1.0 + rng.normal(0, 0.3, n)  # the station's daily mean sits ~1 C below the Tmax/Tmin midpoint
    wet = 0.5 + 0.4 * np.sin(2 * np.pi * (doy - 150) / 365.25)
    rain = np.where(rng.random(n) < wet, rng.gamma(0.9, 12.0, n), 0.0)
    frame = pd.DataFrame(
        {
            "Fecha": days.strftime("%m/%d/%Y"),
            "PrecipitacionAcumulada": rain.round(2),
            "Temperatura": tmean.round(6),
            "TemperaturaMaximaPromedio": (midpoint + half_range).round(2),
            "TemperaturaMinimaPromedio": (midpoint - half_range).round(2),
            "HorasLuz": np.clip(6.5 - 0.08 * rain + rng.normal(0, 1.2, n), 0, 11).round(2),
            "HumedadRelativa": np.clip(86 + 0.15 * rain + rng.normal(0, 2.5, n), 60, 100).round(4),
            "RadiacionSolar": np.clip(160 - 2.0 * rain + rng.normal(0, 35, n), 20, 320).round(4),
        }
    )
    frame["UnidadesCalorPromedio"] = (
        (frame["TemperaturaMaximaPromedio"] + frame["TemperaturaMinimaPromedio"]) / 2 - 18
    ).round(4)
    frame["HorasPrecipitacion"] = np.where(rain > 0, np.clip(rain / 3 + rng.normal(0, 1, n), 0.25, 24), 0).round(2)
    # Real-export defects the adapter must handle: blank readings and a missing day.
    blank = frame["Fecha"].isin(["05/14/2020", "05/15/2020", "05/16/2020"])
    frame.loc[blank, ["Temperatura", "TemperaturaMaximaPromedio", "TemperaturaMinimaPromedio", "UnidadesCalorPromedio"]] = np.nan
    return frame[frame["Fecha"] != "08/09/2023"].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Week labels, rotation, ledger
# ---------------------------------------------------------------------------


def week_label(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return f"{year} S:{week:02d}"


def colour_for_week(week_start: date) -> str:
    """8-colour weekly rotation; 2019 S:01 is Rojo (as on farm_1)."""
    return ROTATION[((week_start - date(2018, 12, 31)).days // 7) % len(ROTATION)]


def harvest_hazards(bagging_week: int, weather: WeatherProvider, params: ParameterSet, horizon: int = 30) -> list[float]:
    """Weekly harvest hazard of a cohort bagged in `bagging_week`, week by
    week after bagging, from the model's own post-flowering mechanics."""
    chain = CohortChain(ChainKind.POST_FLOWERING, params.override("general", {"population_epsilon": 0.0}))
    chain.add_new_cohort(1.0, bagging_week)
    hazards = []
    for week in range(bagging_week + 1, min(bagging_week + horizon, weather.max_week_index) + 1):
        before = chain.total_population()
        rates = chain.calc_rates(weather.thermal_increment(week))
        hazards.append(rates.sum_chain_exit / before if before > 1e-12 else 1.0)
        chain.integrate(rates, week)
        if chain.total_population() <= 1e-12:
            break
    return hazards


def simulate_ledger(
    bagging: dict[int, int], weather: WeatherProvider, params: ParameterSet, rng: np.random.Generator
) -> tuple[pd.DataFrame, dict[int, dict]]:
    """Follows every bagged cohort with binomial draws: each week first the
    model's harvest hazard (cut), then a small field-loss hazard, exactly the
    engine's order. Returns weekly per-colour records and the cohorts (for the
    census)."""
    cohorts: dict[int, dict] = {}
    events = []
    for week in sorted(bagging):
        n = int(bagging[week])
        if n <= 0:
            continue
        colour = colour_for_week(weather.calendar_date_for_week(week))
        cohorts[week] = {"colour": colour, "bagged": n, "history": []}
        left = n
        for k, hazard in enumerate(harvest_hazards(week, weather, params), start=1):
            if left <= 0:
                break
            cut = int(rng.binomial(left, min(1.0, hazard)))
            left -= cut
            loss_hazard = LOSS_HAZARD_HARVEST if k >= 8 else LOSS_HAZARD_EARLY
            lost = int(rng.binomial(left, loss_hazard)) if left > 0 else 0
            left -= lost
            rejected = int(rng.binomial(cut, REJECT_SHARE)) if cut > 0 else 0
            cohorts[week]["history"].append((week + k, cut, lost))
            events.append({"week": week + k, "colour": colour, "cut": cut, "lost": lost, "rejected": rejected, "age": k})
    ev = pd.DataFrame(events)
    ev["age_x_cut"] = ev["age"] * ev["cut"]
    weekly = ev.groupby(["week", "colour"], as_index=False)[["cut", "lost", "rejected", "age_x_cut"]].sum()
    weekly["Edad"] = np.where(weekly["cut"] > 0, np.rint(weekly["age_x_cut"] / weekly["cut"].clip(lower=1)) + 1, np.nan)
    return weekly.drop(columns="age_x_cut"), cohorts


def standing_bunches(cohorts: dict[int, dict], week: int) -> list[tuple[str, int, int]]:
    """(colour, weeks since bagging, bunches) at the START of `week`."""
    out = []
    for bagging_week, cohort in cohorts.items():
        if bagging_week >= week:
            continue
        removed = sum(cut + lost for w, cut, lost in cohort["history"] if w < week)
        left = cohort["bagged"] - removed
        if left > 0:
            out.append((cohort["colour"], week - bagging_week, left))
    return out


def production_sheet(
    weekly: pd.DataFrame, bagging: dict[int, int], weather: WeatherProvider, first_week: int, last_week: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """The wide weekly export: SemanaAnualDole + 8 fields per colour."""
    rows = []
    by_key = {(r.week, r.colour): r for r in weekly.itertuples()}
    for week in range(first_week, last_week + 1):
        monday = weather.calendar_date_for_week(week)
        row: dict[str, object] = {"SemanaAnualDole": week_label(monday)}
        season = np.sin(2 * np.pi * (monday.timetuple().tm_yday - 120) / 365.25)
        for colour in EXPORT_COLOURS:
            values = dict.fromkeys(FIELDS, np.nan)
            if colour_for_week(monday) == colour and bagging.get(week, 0) > 0:
                values["RacimosEmbolsados"] = bagging[week]
            record = by_key.get((week, colour))
            if record is not None:
                if record.lost > 0:
                    values["Racimos Perdidos"] = record.lost
                if record.cut > 0:
                    values["Cortados"] = record.cut
                    values["Rechazados"] = record.rejected
                    values["Edad"] = record.Edad
                    values["PesoRacimo"] = round(30.0 + 1.2 * season + rng.normal(0, 1.0), 6)
                    values["Calibracion"] = round(45.5 + rng.normal(0, 0.4), 6)
                    values["manos"] = round(8.6 + rng.normal(0, 0.3), 6)
            for field_name in FIELDS:
                row[f"{colour} {field_name}"] = values[field_name]
        rows.append(row)
    return pd.DataFrame(rows)


def _write_xlsx(path: Path, sheets: dict[str, pd.DataFrame]) -> None:
    """Workbook timestamps and zip entry times are fixed, so regenerating a
    farm reproduces its xlsx files byte for byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet, frame in sheets.items():
            frame.to_excel(writer, sheet_name=sheet, index=False)
    # openpyxl stamps the save time into docProps/core.xml and every zip entry.
    stamp = XLSX_TIMESTAMP.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "docProps/core.xml":
                data = re.sub(rb"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp, data)
            fixed = zipfile.ZipInfo(info.filename, date_time=XLSX_TIMESTAMP.timetuple()[:6])
            target.writestr(fixed, data, compress_type=zipfile.ZIP_DEFLATED)
    path.write_bytes(buffer.getvalue())


# ---------------------------------------------------------------------------
# One scenario
# ---------------------------------------------------------------------------


def generate(scenario: Scenario, daily_weather: pd.DataFrame, rng: np.random.Generator) -> None:
    out = scenario.directory
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*"):
        if stale.is_file():
            stale.unlink()
    print(f"\n##### generating {scenario.name} -> {out}")

    raw_weather = daily_weather[pd.to_datetime(daily_weather["Fecha"]) >= pd.Timestamp(scenario.weather_start)]
    _write_xlsx(out / "weather.xlsx", {"data": raw_weather.reset_index(drop=True)})
    weather_df = adapt_weather(out / "weather.xlsx").data
    write_csv(weather_df, out / "weather.csv")
    defaults = ParameterSet.from_tixier_defaults()
    truth_params = defaults.override("sucker_selection", FARM_PRACTICE, source="farm_practice_guarin_ochoa_2012")
    weather = WeatherProvider.from_file_and_params(str(out / "weather.csv"), truth_params)
    first_week = grid_week_index(scenario.record_start, weather)
    last_week = grid_week_index(scenario.record_end, weather)
    mats = scenario.hectares * PLANTS_PER_HA
    interval = generation_interval_weeks(weather, truth_params)

    # ---- the true farm's weekly dynamics
    flowering: dict[int, float] = {}
    nulliparous: dict[int, float] = {}
    parous: dict[int, float] = {}
    snapshots: dict[int, pd.DataFrame] = {}
    if scenario.planting_weeks is None:
        # Phase 1: constant selection inflow sized to the farm (an equilibrium
        # age structure); phase 2: the farm's own endogenous practice.
        burn_in = first_week - 52
        rate = mats / interval
        selections = pd.DataFrame(
            {
                "date": [weather.calendar_date_for_week(w) for w in range(burn_in)],
                "parous": 0.0,
                "nulliparous": rate,
                "total": rate,
            }
        )
        phase1 = SimbaPopEngine(truth_params, weather, 0.0, sucker_selection=SuckerSelectionData(selections, weather))
        phase1.run(burn_in)
        engine = SimbaPopEngine(truth_params, weather, phase1.snapshot_cohorts(), start_week_index=burn_in)
        ledger_from = burn_in
    else:
        plan = planting_plan(scenario, weather, rng)
        _write_xlsx(out / "planting_schedule.xlsx", {"plan_de_siembra": plan})
        planted = adapt_planting_schedule(out / "planting_schedule.xlsx", weather)
        start = planted.details["snapshot_week_index"]
        engine = SimbaPopEngine(truth_params, weather, planted.data, start_week_index=start)
        ledger_from = start
    census_week = date_to_grid_week(scenario.census_date, weather) if scenario.census_date else None
    while engine.current_week_index <= min(last_week, weather.max_week_index):
        week = engine.current_week_index
        if week == census_week:
            snapshots[week] = engine.snapshot_cohorts()
        if scenario.planting_weeks is None and week == first_week + 104:
            snapshots[week] = engine.snapshot_cohorts()  # for the reconstruction self-check
        record = engine.step()
        flowering[week], nulliparous[week], parous[week] = record.FLt, record.sucker_output_pre, record.sucker_output_post

    # ---- records
    bagging = {w: int(np.rint(v)) for w, v in flowering.items() if w >= ledger_from}
    weekly, cohorts = simulate_ledger(bagging, weather, truth_params, rng)
    production = production_sheet(weekly, bagging, weather, first_week, last_week, rng)
    _write_xlsx(out / "production.xlsx", {"data": production})

    first_selection = min((w for w in nulliparous if nulliparous[w] + parous[w] >= 0.5 and w >= first_week), default=first_week)
    sucker_rows = []
    for week in range(first_selection, last_week + 1):
        label = week_label(weather.calendar_date_for_week(week))
        blank = label in scenario.sucker_gap
        sucker_rows.append(
            {
                "SemanaAnualDole": label,
                "DesbuncheMatasParidas": np.nan if blank else int(np.rint(parous.get(week, 0.0))),
                "DesbuncheMatasSinParir": np.nan if blank else int(np.rint(nulliparous.get(week, 0.0))),
            }
        )
    _write_xlsx(out / "sucker_selection.xlsx", {"data": pd.DataFrame(sucker_rows)})

    if census_week is not None:
        census, summary = census_sheets(snapshots[census_week], cohorts, census_week, scenario, weather, mats, rng)
        _write_xlsx(out / "initial_population.xlsx", {"inventario": census, "resumen": summary})

    print(
        f"  truth: {mats:,.0f} mats, generation interval {interval:.1f} weeks "
        f"({52 / interval:.2f} bunches/mat/year), practice {FARM_PRACTICE}"
    )
    build_farm(out, defaults)
    self_check(scenario, out, defaults, bagging, snapshots, cohorts, first_week, last_week)


def planting_plan(scenario: Scenario, weather: WeatherProvider, rng: np.random.Generator) -> pd.DataFrame:
    """~10 ha a week, lots of 8-12 ha, summing to the farm's area."""
    sizes = rng.uniform(8.0, 12.0, scenario.planting_weeks)
    sizes = np.round(sizes / sizes.sum() * scenario.hectares, 2)
    sizes[-1] = round(scenario.hectares - sizes[:-1].sum(), 2)
    first = grid_week_index(scenario.record_start, weather)
    return pd.DataFrame(
        {
            "SemanaAnualDole": [week_label(weather.calendar_date_for_week(first + i)) for i in range(len(sizes))],
            "Lote": [f"L{i + 1:02d}" for i in range(len(sizes))],
            "Hectareas": sizes,
            "PlantasSembradas": np.rint(sizes * PLANTS_PER_HA).astype(int),
        }
    )


def census_sheets(
    snapshot: pd.DataFrame,
    cohorts: dict[int, dict],
    census_week: int,
    scenario: Scenario,
    weather: WeatherProvider,
    mats: float,
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The field census at the start of `census_week`: vegetative plants by
    weeks since selection (from the true farm) and bunched plants by ribbon
    colour and weeks since bagging (from the bunch ledger, so it agrees with
    production.xlsx), each counted with a small error. Ages count the current
    week as week 1, like Edad."""
    census_day = weather.calendar_date_for_week(census_week).strftime("%m/%d/%Y")
    rows = []
    pre = snapshot[snapshot["chain"] == "pre_flowering"]
    for age, population in zip(pre["age_weeks"], pre["population"], strict=True):
        counted = int(np.rint(population * (1 + rng.normal(0, 0.025))))
        if counted > 0:
            rows.append({"FechaInventario": census_day, "Estado": "SinParir", "Cinta": None, "EdadSemanas": int(age) + 1, "Plantas": counted})
    for colour, age, bunches in sorted(standing_bunches(cohorts, census_week)):
        counted = int(np.rint(bunches * (1 + rng.normal(0, 0.02))))
        if counted > 0:
            rows.append({"FechaInventario": census_day, "Estado": "Parida", "Cinta": colour, "EdadSemanas": age + 1, "Plantas": counted})
    census = pd.DataFrame(rows)
    by_stage = census.groupby("Estado")["Plantas"].sum()
    summary = pd.DataFrame(
        {
            "Concepto": [
                "FechaInventario",
                "Hectareas",
                "Matas (unidades de produccion)",
                "Plantas sin parir (incl. hijos seleccionados)",
                "Plantas paridas (racimos en campo)",
                "Plantas en pie",
            ],
            "Valor": [
                census_day,
                scenario.hectares,
                int(mats),
                int(by_stage.get("SinParir", 0)),
                int(by_stage.get("Parida", 0)),
                int(by_stage.sum()),
            ],
        }
    )
    return census, summary


# ---------------------------------------------------------------------------
# Self-checks: the records and the pipeline reproduce the true farm
# ---------------------------------------------------------------------------


def _r2(observed: np.ndarray, predicted: np.ndarray) -> float:
    return float(1.0 - ((observed - predicted) ** 2).sum() / ((observed - observed.mean()) ** 2).sum())


def self_check(
    scenario: Scenario,
    out: Path,
    defaults: ParameterSet,
    bagging: dict[int, int],
    snapshots: dict[int, pd.DataFrame],
    cohorts: dict[int, dict],
    first_week: int,
    last_week: int,
) -> None:
    print("  self-checks")
    weather = WeatherProvider.from_file_and_params(str(out / "weather.csv"), defaults)
    production = RibbonProductionReader.read(out / "production.csv").data
    ribbon = RibbonProductionData(production, weather)
    sucker = SuckerSelectionData(SuckerSelectionReader.read(out / "sucker_selection.csv").data, weather)
    population = PopulationInventoryReader.read(out / "initial_population.csv").data
    start = weather.week_index_for_date(
        pd.to_datetime(pd.read_csv(out / "initial_population.csv")["snapshot_date"].iloc[0]).date()
    )
    end = min(last_week, weather.max_week_index)
    engine = SimbaPopEngine(
        defaults, weather, population, start_week_index=start, ribbon_production=ribbon, sucker_selection=sucker,
        tags=list(ribbon.tags()),
    )
    output = engine.run(end - start + 1).set_index("week_index")
    production["week"] = production["date"].map(weather.week_index_for_date)
    cut = production.groupby("week")["cut"].sum().reindex(output.index, fill_value=0.0)
    bag = production.groupby("week")["bagged"].sum().reindex(output.index, fill_value=0.0)
    harvesting = output.index[cut.to_numpy() > 0]
    window = output.index[output.index >= harvesting.min()]
    r2_cut = _r2(cut.loc[window].to_numpy(), output.loc[window, "Ht"].to_numpy())
    ratio = output.loc[window, "Ht"].sum() / cut.loc[window].sum()
    flowering_window = window[window <= end - 45]
    r2_fl = _r2(bag.loc[flowering_window].to_numpy(), output.loc[flowering_window, "FLt"].to_numpy())
    print(f"      Mode C Ht vs recorded Cortados: r2 {r2_cut:.4f}, total ratio {ratio:.4f}")
    print(f"      Mode C mechanistic flowering vs recorded bagging: r2 {r2_fl:.4f}")
    assert r2_cut >= 0.99 and abs(ratio - 1) <= 0.02, "Mode C does not reproduce the recorded harvest"
    assert r2_fl >= 0.98, "Mode C flowering does not reproduce the recorded bagging"
    after_first = cut.loc[harvesting.min():end]
    assert (after_first > 0).all(), "a week without harvest after the first harvest week"

    # One follower per mother keeps the number of production units constant, so
    # the long-run bagging rate is mats / generation interval.
    mats = scenario.hectares * PLANTS_PER_HA
    expected = mats / generation_interval_weeks(weather, defaults.override("sucker_selection", FARM_PRACTICE))
    recent = pd.Series(bagging).loc[end - 103 : end].mean()
    print(f"      bagging per week over the last 2 years: {recent:,.0f} vs mats / generation interval {expected:,.0f}")
    assert abs(recent / expected - 1) < 0.04, "the true farm does not keep its number of mats"

    if scenario.planting_weeks is None:
        check_week = first_week + 104
        weather_df = pd.read_csv(out / "weather.csv")
        rebuilt, report = reconstruct_initial_population(pd.read_csv(out / "production.csv"), weather_df, defaults, check_week)
        truth_pre = snapshots[check_week]
        truth_pre = truth_pre.loc[truth_pre["chain"] == "pre_flowering", "population"].sum()
        truth_post = pd.DataFrame(standing_bunches(cohorts, check_week), columns=["tag", "age", "n"]).groupby("tag")["n"].sum()
        got_post = rebuilt[rebuilt["chain"] == "post_flowering"].groupby("tag")["population"].sum()
        post_error = ((got_post.reindex(truth_post.index, fill_value=0) - truth_post).abs() / truth_post).max()
        got_pre = rebuilt.loc[rebuilt["chain"] == "pre_flowering", "population"].sum()
        print(
            f"      reconstruction from production at week {check_week}: vegetative {got_pre:,.0f} vs true "
            f"{truth_pre:,.0f} ({got_pre / truth_pre - 1:+.2%}); bunches per colour max error {post_error:.2%}; "
            f"followers per bagged bunch {report['selections_per_bagged_bunch']:.3f}"
        )
        assert abs(got_pre / truth_pre - 1) < 0.05, "reconstructed vegetative stock is off"
        assert post_error < 0.05, "reconstructed bunches per colour are off"


def main() -> None:
    scenarios = {scenario.name: scenario for scenario in (ESTABLISHED, NEW_PLANTATION)}
    names = sys.argv[1:] or list(scenarios)
    unknown = [name for name in names if name not in scenarios]
    if unknown:
        raise SystemExit(f"unknown scenario(s) {unknown}; choose from {list(scenarios)}")
    rng = np.random.default_rng(SEED)
    daily = synthetic_daily_weather(rng)
    for name, scenario in scenarios.items():
        if name in names:
            generate(scenario, daily, np.random.default_rng([SEED, len(scenario.name)]))


if __name__ == "__main__":
    main()
