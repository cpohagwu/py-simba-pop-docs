"""Adapters for a farm's legacy weekly Excel exports -> the canonical files
py_simba_pop.readers read.

NOT part of the installed py_simba_pop package -- like
build_custom_parameters.py, this is farm-specific conversion code for one
vendor's export vocabulary (Dole's Spanish-language weekly sheets). The
package's readers enforce one standardized schema per file type and never
guess at a farm's own format; solving that here, explicitly, is the point.

One rule ties every file together: **the weather file defines the week grid.**
py_simba_pop numbers weeks as consecutive 7-day blocks starting on the first
weather date (week_index 0), so every other file's weekly records are dated
with the first day of the grid week they fall in:

  weather.xlsx            -> weather.csv             date, tmean
  production.xlsx         -> production.csv          tag, date, bagged, cut, age_at_cut,
                                                     field_losses, rejected
  sucker_selection.xlsx   -> sucker_selection.csv    date, parous_count, nulliparous_count
  initial_population.xlsx -> initial_population.csv  chain, tag, population, age_weeks,
  (or planting_schedule.xlsx)                        snapshot_date

Farm week labels ("2019 S:01", column SemanaAnualDole) are ISO-8601 weeks;
each is placed on the grid by the grid week that holds most of its days (the
week containing its ISO Thursday), so a Monday-Sunday farm week and a
Tuesday-Monday weather grid still line up one-to-one.

Every adapter returns an AdaptResult: the canonical DataFrame plus a list of
plain-language data-quality messages (what was filled, dropped, detected).
See build_farm_inputs.py for the command-line driver.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from py_simba_pop import WeatherProvider

# Longest run of missing/blank daily temperatures filled by interpolation;
# anything longer is a real hole in the record and raises instead.
MAX_FILLED_GAP_DAYS = 14


@dataclass(frozen=True, slots=True)
class AdaptResult:
    """A canonical table plus what happened on the way (for the build report)."""

    data: pd.DataFrame
    messages: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _normalise_text(value: object) -> str:
    """NFC, stripped, runs of whitespace collapsed to one space."""
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def _read_sheet(path: str | Path, sheet: str | int | None = 0) -> pd.DataFrame:
    frame = pd.read_excel(path, sheet_name=0 if sheet is None else sheet)
    frame.columns = [_normalise_text(c) for c in frame.columns]
    return frame


def write_csv(frame: pd.DataFrame, path: str | Path) -> None:
    """UTF-8, no index, LF line endings (stable diffs on every OS)."""
    frame.to_csv(path, index=False, lineterminator="\n", encoding="utf-8")


_WEEK_LABEL = re.compile(r"^\s*(\d{4})\s*S\s*:\s*(\d{1,2})\s*$")


def parse_week_label(label: object) -> tuple[int, int]:
    """'2019 S:01' -> (2019, 1). Raises ValueError for anything else."""
    match = _WEEK_LABEL.match(str(label))
    if not match:
        raise ValueError(f"unrecognised week label {label!r}; expected 'YYYY S:WW'")
    return int(match.group(1)), int(match.group(2))


def grid_week_index(d: date, weather: WeatherProvider) -> int:
    """The grid week holding most of the ISO (Monday-Sunday) week that
    contains date `d` -- i.e. the week containing that ISO week's Thursday."""
    thursday = d + timedelta(days=3 - d.weekday())
    return weather.week_index_for_date(thursday)


def week_label_to_grid_week(label: object, weather: WeatherProvider) -> int:
    year, week = parse_week_label(label)
    return grid_week_index(date.fromisocalendar(year, week, 1), weather)


def date_to_grid_week(d: date, weather: WeatherProvider) -> int:
    """A calendar day (a census or snapshot date) -> the grid week whose START
    is nearest to it: a census taken in a week's first four days describes
    that week's start, one taken later the next week's start."""
    return weather.week_index_for_date(d + timedelta(days=3))


def snapshot_week_index(value: str | date, weather: WeatherProvider) -> int:
    """A census/snapshot given as a date ('2021-01-05') or a farm week label
    ('2021 S:01') -> the grid week_index whose START the snapshot describes."""
    if isinstance(value, date):
        return date_to_grid_week(value, weather)
    text = str(value).strip()
    if _WEEK_LABEL.match(text):
        return week_label_to_grid_week(text, weather)
    return date_to_grid_week(date.fromisoformat(text), weather)


def _week_labels_to_grid(labels: pd.Series, weather: WeatherProvider, *, require_consecutive: bool) -> pd.Series:
    parsed = labels.map(parse_week_label)
    if parsed.duplicated().any():
        dupes = sorted(set(labels[parsed.duplicated(keep=False)]))
        raise ValueError(f"duplicate week label(s) {dupes[:5]}")
    weeks = labels.map(lambda label: week_label_to_grid_week(label, weather))
    if require_consecutive:
        steps = weeks.diff().dropna()
        if not (steps == 1).all():
            bad = labels[weeks.diff().fillna(1) != 1].tolist()
            raise ValueError(f"week labels are not consecutive ISO weeks; first break(s) at {bad[:5]}")
    return weeks.astype(int)


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeatherExportColumns:
    date: str = "Fecha"
    tmean: str = "Temperatura"  # daily MEAN temperature -- the model's only input (Eq 16'')
    station: str = "EstacionClima"
    date_format: str = "%m/%d/%Y"
    # UnidadesCalorPromedio is deliberately ignored: in these exports it is
    # (Tmax + Tmin)/2 - 18, not Eq 16'' (Temperatura - 14).


def _nan_runs(values: pd.Series) -> list[tuple[int, int]]:
    """(start position, length) of every run of NaNs."""
    runs: list[tuple[int, int]] = []
    start = None
    for i, missing in enumerate(values.isna().to_numpy()):
        if missing and start is None:
            start = i
        elif not missing and start is not None:
            runs.append((start, i - start))
            start = None
    if start is not None:
        runs.append((start, len(values) - start))
    return runs


def adapt_weather(path: str | Path, columns: WeatherExportColumns = WeatherExportColumns()) -> AdaptResult:
    """Daily weather export -> `date, tmean`, one row per calendar day, no
    gaps, whole weeks only. Short gaps (<= MAX_FILLED_GAP_DAYS consecutive
    missing or blank days) are filled by linear interpolation; blank days at
    either end are trimmed; the trailing incomplete week is dropped, so every
    grid week sums exactly 7 days of thermal time."""
    raw = _read_sheet(path)
    missing = [c for c in (columns.date, columns.tmean) if c not in raw.columns]
    if missing:
        raise ValueError(f"{path}: missing weather column(s) {missing}; found {list(raw.columns)}")
    messages: list[str] = []

    if columns.station in raw.columns:
        stations = raw[columns.station].dropna().map(_normalise_text).unique()
        if len(stations) > 1:
            raise ValueError(f"{path}: {len(stations)} weather stations {list(stations)} -- export one station")

    if pd.api.types.is_datetime64_any_dtype(raw[columns.date]):
        dates = pd.to_datetime(raw[columns.date])
    else:
        dates = pd.to_datetime(raw[columns.date].astype(str).str.strip(), format=columns.date_format)
    daily = pd.DataFrame({"date": dates.dt.normalize(), "tmean": pd.to_numeric(raw[columns.tmean], errors="coerce")})

    duplicated = int(daily["date"].duplicated().sum())
    if duplicated:
        daily = daily.groupby("date", as_index=False)["tmean"].mean()
        messages.append(f"averaged {duplicated} duplicate date row(s)")
    daily = daily.sort_values("date").set_index("date")

    full_index = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
    absent_days = len(full_index) - len(daily)
    daily = daily.reindex(full_index)
    blank_days = int(daily["tmean"].isna().sum()) - absent_days
    if absent_days:
        messages.append(f"{absent_days} calendar day(s) absent from the export")
    if blank_days:
        messages.append(f"{blank_days} day(s) with a blank {columns.tmean}")

    valid = daily["tmean"].notna()
    first, last = valid.idxmax(), valid[::-1].idxmax()
    trimmed = int((daily.index < first).sum() + (daily.index > last).sum())
    if trimmed:
        messages.append(f"trimmed {trimmed} blank day(s) at the ends of the record")
    daily = daily.loc[first:last]

    runs = _nan_runs(daily["tmean"])
    if runs:
        longest = max(length for _, length in runs)
        if longest > MAX_FILLED_GAP_DAYS:
            start, length = max(runs, key=lambda r: r[1])
            raise ValueError(
                f"{path}: {length} consecutive days without {columns.tmean} from "
                f"{daily.index[start].date()} -- too long to interpolate (limit {MAX_FILLED_GAP_DAYS})"
            )
        gap_days = sum(length for _, length in runs)
        messages.append(f"filled {gap_days} day(s) in {len(runs)} gap(s) by linear interpolation (longest {longest})")
        suspicious = _outliers_beside_gaps(daily["tmean"], runs)
        if suspicious:
            messages.append(
                "check day(s) beside a gap that sit > 4 SD from their seasonal norm: "
                + ", ".join(f"{d} ({t:.2f} C)" for d, t in suspicious)
            )
        daily["tmean"] = daily["tmean"].interpolate(method="linear", limit_area="inside")

    remainder = len(daily) % 7
    if remainder:
        messages.append(f"dropped the last {remainder} day(s): an incomplete final week")
        daily = daily.iloc[: len(daily) - remainder]

    out = pd.DataFrame({"date": daily.index.date, "tmean": daily["tmean"].round(6).to_numpy()})
    messages.insert(
        0, f"{len(out)} days, {out['date'].iloc[0]} .. {out['date'].iloc[-1]} ({len(out) // 7} whole weeks)"
    )
    return AdaptResult(data=out, messages=messages)


def _outliers_beside_gaps(tmean: pd.Series, runs: list[tuple[int, int]]) -> list[tuple[date, float]]:
    doy = tmean.index.dayofyear
    norm = tmean.groupby(doy).transform("mean").rolling(15, center=True, min_periods=1).mean()
    resid = tmean - norm
    sd = resid.std()
    if not sd or np.isnan(sd):
        return []
    flagged = []
    for start, length in runs:
        for pos in [*range(max(0, start - 3), start), *range(start + length, min(len(tmean), start + length + 3))]:
            value = tmean.iloc[pos]
            if not np.isnan(value) and abs(resid.iloc[pos]) > 4 * sd:
                flagged.append((tmean.index[pos].date(), float(value)))
    return flagged


def load_weather(weather_csv: str | Path, params) -> WeatherProvider:
    """The WeatherProvider every other adapter aligns to (grid = first date)."""
    return WeatherProvider.from_file_and_params(str(weather_csv), params)


# ---------------------------------------------------------------------------
# Production (wide, per ribbon colour) -> tidy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductionExportColumns:
    """Column suffixes of the wide sheet: '<Colour> <field>' per ribbon colour.
    Only the four counts (+ Edad) matter to the model; PesoRacimo,
    Calibracion and manos (bunch weight, grade, hands) are not used."""

    week: str = "SemanaAnualDole"
    bagged: str = "RacimosEmbolsados"
    cut: str = "Cortados"
    age_at_cut: str = "Edad"
    field_losses: str = "Racimos Perdidos"
    rejected: str = "Rechazados"
    parous: str = "DesbuncheMatasParidas"  # legacy, per-colour broadcast sucker selection
    nulliparous: str = "DesbuncheMatasSinParir"
    unused: tuple[str, ...] = ("PesoRacimo", "Calibracion", "manos")  # known, not modelled

    def count_fields(self) -> dict[str, str]:
        return {
            "bagged": self.bagged,
            "cut": self.cut,
            "age_at_cut": self.age_at_cut,
            "field_losses": self.field_losses,
            "rejected": self.rejected,
        }


def detect_tags(columns: Sequence[str], vocab: ProductionExportColumns = ProductionExportColumns()) -> list[str]:
    """Every ribbon colour with at least one '<Colour> <count field>' column,
    in sheet order (so a one-off group such as 'Mecate' is kept, not lost)."""
    suffixes = sorted({f" {s}" for s in vocab.count_fields().values()}, key=len, reverse=True)
    tags: list[str] = []
    for column in columns:
        for suffix in suffixes:
            if column.endswith(suffix) and len(column) > len(suffix):
                tag = column[: -len(suffix)].strip()
                if tag not in tags:
                    tags.append(tag)
                break
    return tags


def _canonical_production_headers(
    columns: Sequence[str], vocab: ProductionExportColumns
) -> tuple[list[str], list[str]]:
    """Field suffixes matched case-insensitively (whitespace is already
    collapsed), so 'Negro cortados' is still Negro's Cortados. Returns the
    canonical headers and every column that fits no known field."""
    fields = sorted(
        [*vocab.count_fields().values(), vocab.parous, vocab.nulliparous, *vocab.unused], key=len, reverse=True
    )
    headers: list[str] = []
    unrecognised: list[str] = []
    for column in columns:
        folded = column.casefold()
        if folded == vocab.week.casefold():
            headers.append(vocab.week)
            continue
        for suffix in fields:
            if folded.endswith(" " + suffix.casefold()) and len(column) > len(suffix) + 1:
                headers.append(f"{column[: -len(suffix)].strip()} {suffix}")
                break
        else:
            headers.append(column)
            unrecognised.append(column)
    return headers, unrecognised


def read_wide_production(
    path: str | Path, weather: WeatherProvider, vocab: ProductionExportColumns = ProductionExportColumns()
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """The wide sheet with canonical headers plus a `week_index` column (grid),
    its ribbon colours, and the columns that match no known field."""
    wide = _read_sheet(path)
    headers, unrecognised = _canonical_production_headers(list(wide.columns), vocab)
    duplicated = sorted({h for h in headers if headers.count(h) > 1})
    if duplicated:
        raise ValueError(f"{path}: columns {duplicated} appear more than once (after normalising their spelling)")
    wide.columns = headers
    if vocab.week not in wide.columns:
        raise ValueError(f"{path}: no {vocab.week!r} column; found {list(wide.columns)[:6]}...")
    wide = wide[wide[vocab.week].notna()].reset_index(drop=True)
    wide["week_index"] = _week_labels_to_grid(wide[vocab.week], weather, require_consecutive=True)
    return wide, detect_tags(wide.columns, vocab), unrecognised


def _long_counts(wide: pd.DataFrame, tags: Sequence[str], vocab: ProductionExportColumns) -> pd.DataFrame:
    parts = []
    for tag in tags:
        part = pd.DataFrame({"tag": tag, "week_index": wide["week_index"]})
        for name, suffix in vocab.count_fields().items():
            column = f"{tag} {suffix}"
            part[name] = pd.to_numeric(wide[column], errors="coerce") if column in wide.columns else np.nan
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def detect_edad_offset(long: pd.DataFrame, *, min_share: float = 0.90, min_margin: float = 0.50) -> tuple[int, dict]:
    """Farms differ in how they count a bunch's age: weeks since bagging
    (offset 0) or with the bagging week counted as week 1 (offset 1). Picks
    the offset under which the most cut volume maps back onto a week in which
    that same colour was actually bagged, among cuts whose implied bagging
    week is inside the record. Raises if neither offset is clearly right."""
    first_week = int(long["week_index"].min())
    bagged_weeks = {
        tag: set(group.loc[group["bagged"].fillna(0) > 0, "week_index"].astype(int)) for tag, group in long.groupby("tag")
    }
    cuts = long[(long["cut"].fillna(0) > 0) & long["age_at_cut"].notna()]
    share: dict[int, float] = {}
    for offset in (0, 1):
        implied = cuts["week_index"] - (cuts["age_at_cut"] - offset)
        inside = implied >= first_week
        matched = [int(b) in bagged_weeks.get(t, set()) for t, b in zip(cuts["tag"], implied, strict=True)]
        matched = pd.Series(matched, index=cuts.index) & inside
        denominator = cuts.loc[inside, "cut"].sum()
        share[offset] = float(cuts.loc[matched, "cut"].sum() / denominator) if denominator else 0.0
    best = max(share, key=share.get)
    other = 1 - best
    if share[best] < min_share or share[best] - share[other] < min_margin:
        raise ValueError(
            f"cannot tell how Edad counts weeks: {share[0]:.1%} of cut volume matches a bagging week with "
            f"age = Edad, {share[1]:.1%} with age = Edad - 1. Pass edad_offset= explicitly "
            "(python -m utils.build_farm_inputs --edad-offset 0 or 1)."
        )
    return best, share


def adapt_production(
    path: str | Path,
    weather: WeatherProvider,
    *,
    vocab: ProductionExportColumns = ProductionExportColumns(),
    edad_offset: int | None = None,
) -> AdaptResult:
    """Wide weekly sheet -> tidy `tag, date, bagged, cut, age_at_cut,
    field_losses, rejected` (one row per colour per week with anything
    reported -- loss-only weeks included). `age_at_cut` becomes weeks since
    bagging. `processed` is not in these exports and is left out (the reader
    then notes it as not modeled). Rows dated beyond the weather record are
    kept but reported (they cannot be simulated)."""
    wide, tags, unrecognised = read_wide_production(path, weather, vocab)
    if not tags:
        raise ValueError(f"{path}: no '<colour> {vocab.bagged}'/'{vocab.cut}' column groups found")
    long = _long_counts(wide, tags, vocab)
    counts = ["bagged", "cut", "field_losses", "rejected"]
    long = long[long[counts].notna().any(axis=1)].copy()

    negative = (long[counts + ["age_at_cut"]] < 0).any(axis=1)
    if negative.any():
        raise ValueError(f"{path}: negative counts on {int(negative.sum())} row(s)")
    missing_age = (long["cut"].fillna(0) > 0) & long["age_at_cut"].isna()
    if missing_age.any():
        raise ValueError(f"{path}: {int(missing_age.sum())} row(s) with {vocab.cut} but no {vocab.age_at_cut}")

    shares = None
    if edad_offset is None:
        edad_offset, shares = detect_edad_offset(long)
    long["age_at_cut"] = long["age_at_cut"] - edad_offset
    if (long["age_at_cut"] < 0).any():
        raise ValueError(f"{path}: {vocab.age_at_cut} - {edad_offset} is negative on some rows")

    rejected_over_cut = (long["rejected"].fillna(0) > long["cut"].fillna(0)).sum()
    long = long.sort_values(["week_index", "tag"]).reset_index(drop=True)
    long.insert(1, "date", long["week_index"].map(weather.calendar_date_for_week))

    messages = [
        (f"{len(wide)} weeks ({wide[vocab.week].iloc[0]} .. {wide[vocab.week].iloc[-1]}), {len(tags)} colours "
        f"{tags}, {len(long)} tidy rows"),
        "Edad counts the bagging week as week 1 (age_at_cut = Edad - 1)"
        if edad_offset == 1
        else "Edad is weeks since bagging (age_at_cut = Edad)",
    ]
    if shares is not None:
        messages.append(f"Edad convention check: {shares[edad_offset]:.1%} of in-record cut volume matches its bagging week")
    totals = long[counts].sum()
    messages.append(
        "totals: " + ", ".join(f"{name} {totals[name]:,.0f}" for name in counts)
        + f" (rejected are part of cut; {int(rejected_over_cut)} row(s) with rejected > cut)"
    )
    before = int((long["week_index"] < weather.min_week_index).sum())
    if before:
        messages.append(f"{before} row(s) dated before the weather record starts -- kept, but cannot be simulated")
    beyond = int((long["week_index"] > weather.max_week_index).sum())
    if beyond:
        messages.append(f"{beyond} row(s) dated after the weather record ends -- kept, but cannot be simulated")
    if unrecognised:
        messages.append(f"{len(unrecognised)} column(s) match no known field and were not used: {unrecognised[:8]}")
    zero_weeks = sorted(
        set(wide["week_index"]) - set(long.loc[long["bagged"].fillna(0) > 0, "week_index"])
    )
    if zero_weeks:
        messages.append(f"{len(zero_weeks)} week(s) with no bagging recorded: {zero_weeks[:8]}")

    data = long[["tag", "date", "bagged", "cut", "age_at_cut", "field_losses", "rejected"]]
    details = {"edad_offset": edad_offset, "tags": tags, "week_labels": wide[[vocab.week, "week_index"]]}
    return AdaptResult(data=data, messages=messages, details=details)


def adapt_legacy_sucker_selection_columns(
    path: str | Path, weather: WeatherProvider, vocab: ProductionExportColumns = ProductionExportColumns()
) -> AdaptResult | None:
    """Older exports broadcast farm-wide desuckering counts into every
    colour group ('<Colour> DesbuncheMatasParidas' / '...SinParir'). Returns
    them as sucker_selection rows, or None if the sheet has none. The value
    must be identical across colours on a row (it is one farm-wide count)."""
    wide, tags, _ = read_wide_production(path, weather, vocab)
    series = {}
    for name, suffix in (("parous_count", vocab.parous), ("nulliparous_count", vocab.nulliparous)):
        cols = [f"{t} {suffix}" for t in tags if f"{t} {suffix}" in wide.columns]
        if not cols:
            continue
        values = wide[cols].apply(pd.to_numeric, errors="coerce")
        spread = (values.max(axis=1) - values.min(axis=1)).fillna(0)
        if (spread > 1e-9).any():
            raise ValueError(f"{path}: {suffix} differs between colours on some weeks -- not a farm-wide count")
        series[name] = values.max(axis=1)
    if not series:
        return None
    data = pd.DataFrame({"date": wide["week_index"].map(weather.calendar_date_for_week)})
    for name in ("parous_count", "nulliparous_count"):
        data[name] = series.get(name, np.nan)
    data = data[data[["parous_count", "nulliparous_count"]].notna().any(axis=1)].reset_index(drop=True)
    return AdaptResult(data=data, messages=[f"{len(data)} weeks of sucker selection taken from production columns"])


# ---------------------------------------------------------------------------
# Sucker selection (farm-wide, weekly)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuckerSelectionExportColumns:
    week: str = "SemanaAnualDole"
    parous: str = "DesbuncheMatasParidas"  # followers selected on mothers that already flowered
    nulliparous: str = "DesbuncheMatasSinParir"  # ... on mothers that have not flowered yet


def adapt_sucker_selection(
    path: str | Path, weather: WeatherProvider, vocab: SuckerSelectionExportColumns = SuckerSelectionExportColumns()
) -> AdaptResult:
    """Weekly desuckering sheet -> `date, parous_count, nulliparous_count`.
    A week with both counts blank is dropped (unrecorded -- the engine then
    falls back to its own prediction, with a notice); blank is never read as
    zero."""
    raw = _read_sheet(path)
    missing = [c for c in (vocab.week, vocab.parous, vocab.nulliparous) if c not in raw.columns]
    if missing:
        raise ValueError(f"{path}: missing column(s) {missing}")
    raw = raw[raw[vocab.week].notna()].reset_index(drop=True)
    weeks = _week_labels_to_grid(raw[vocab.week], weather, require_consecutive=False)
    data = pd.DataFrame(
        {
            "date": weeks.map(weather.calendar_date_for_week),
            "parous_count": pd.to_numeric(raw[vocab.parous], errors="coerce"),
            "nulliparous_count": pd.to_numeric(raw[vocab.nulliparous], errors="coerce"),
        }
    )
    blank = data[["parous_count", "nulliparous_count"]].isna().all(axis=1)
    data = data[~blank].reset_index(drop=True)
    messages = [
        f"{len(data)} weeks of sucker selection ({raw[vocab.week].iloc[0]} .. {raw[vocab.week].iloc[-1]})",
        f"totals: parous {data['parous_count'].sum():,.0f}, nulliparous {data['nulliparous_count'].sum():,.0f}",
    ]
    if blank.any():
        messages.append(f"{int(blank.sum())} unrecorded week(s) left out: {raw.loc[blank, vocab.week].tolist()}")
    return AdaptResult(data=data, messages=messages)


# ---------------------------------------------------------------------------
# Initial population: a field census, or a new plantation's planting schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InventoryExportColumns:
    """A farm census of standing plants on one date: vegetative plants (mothers
    not yet flowered and selected followers) by weeks since selection/planting,
    and bunched mothers by ribbon colour and weeks since bagging."""

    sheet: str = "inventario"
    date: str = "FechaInventario"
    stage: str = "Estado"
    tag: str = "Cinta"
    age: str = "EdadSemanas"
    plants: str = "Plantas"
    not_flowered: str = "SinParir"
    flowered: str = "Parida"
    date_format: str = "%m/%d/%Y"


def adapt_inventory(
    path: str | Path,
    weather: WeatherProvider,
    *,
    age_offset: int = 0,
    vocab: InventoryExportColumns = InventoryExportColumns(),
) -> AdaptResult:
    """Census sheet -> `chain, tag, population, age_weeks, snapshot_date`.
    The census describes the farm at the START of its grid week S (before that
    week's bagging, harvest and losses); `age_offset` is the farm's age-counting
    convention (1 if the week of bagging/selection counts as week 1, as for
    production Edad)."""
    raw = _read_sheet(path, vocab.sheet)
    missing = [c for c in (vocab.date, vocab.stage, vocab.age, vocab.plants) if c not in raw.columns]
    if missing:
        raise ValueError(f"{path}: missing census column(s) {missing}")
    if pd.api.types.is_datetime64_any_dtype(raw[vocab.date]):
        dates = pd.to_datetime(raw[vocab.date])
    else:
        dates = pd.to_datetime(raw[vocab.date].astype(str).str.strip(), format=vocab.date_format)
    census_dates = dates.dt.date.unique()
    if len(census_dates) != 1:
        raise ValueError(f"{path}: expected one census date, found {sorted(census_dates)}")
    snapshot_week = date_to_grid_week(census_dates[0], weather)

    stage = raw[vocab.stage].map(_normalise_text)
    chain = stage.map({vocab.not_flowered: "pre_flowering", vocab.flowered: "post_flowering"})
    if chain.isna().any():
        raise ValueError(f"{path}: unknown {vocab.stage} value(s) {sorted(set(stage[chain.isna()]))}")
    tag = raw[vocab.tag].map(lambda v: np.nan if pd.isna(v) or not _normalise_text(v) else _normalise_text(v)) \
        if vocab.tag in raw.columns else pd.Series(np.nan, index=raw.index)
    tag = tag.where(chain == "post_flowering", np.nan)
    age_weeks = pd.to_numeric(raw[vocab.age], errors="coerce") - age_offset
    if age_weeks.isna().any() or (age_weeks < 1).any() or (age_weeks != age_weeks.round()).any():
        raise ValueError(
            f"{path}: {vocab.age} must be a whole number >= {1 + age_offset} on every row (census at the start of a week)"
        )
    plants = pd.to_numeric(raw[vocab.plants], errors="coerce")
    uncounted = raw[vocab.plants].isna()
    bad = ~uncounted & (plants.isna() | (plants < 0) | (plants != plants.round()))
    if bad.any():
        raise ValueError(
            f"{path}: {vocab.plants} must be a whole, non-negative count; rows {list(raw.index[bad][:5])} are "
            f"{raw.loc[bad, vocab.plants].tolist()[:5]}"
        )
    data = (
        pd.DataFrame(
            {
                "chain": chain,
                "tag": tag,
                "population": plants.fillna(0.0).astype(float),
                "age_weeks": age_weeks.astype(int),
            }
        )
        .groupby(["chain", "tag", "age_weeks"], dropna=False, as_index=False)["population"]
        .sum()
    )
    data = data[data["population"] > 0][["chain", "tag", "population", "age_weeks"]]
    data = data.sort_values(["chain", "tag", "age_weeks"], na_position="first").reset_index(drop=True)
    data["snapshot_date"] = weather.calendar_date_for_week(snapshot_week)
    by_chain = data.groupby("chain")["population"].sum()
    messages = [
        (f"census of {census_dates[0]} = state at the start of grid week {snapshot_week} "
        f"({data['snapshot_date'].iloc[0]})"),
        (f"{by_chain.get('pre_flowering', 0):,.0f} plants not yet flowered + {by_chain.get('post_flowering', 0):,.0f} "
        f"bunched = {data['population'].sum():,.0f} standing plants"),
    ]
    if uncounted.any():
        messages.append(f"{int(uncounted.sum())} row(s) with a blank {vocab.plants} count left out")
    return AdaptResult(data=data, messages=messages, details={"snapshot_week_index": snapshot_week})


@dataclass(frozen=True, slots=True)
class PlantingScheduleColumns:
    """A new plantation's planting plan ("plan de siembra"): plants set out
    per lot and week. Tixier et al. (2004) measure FT and BT from planting,
    so a planting week is exactly a pre-flowering cohort's birth week."""

    sheet: str = "plan_de_siembra"
    week: str = "SemanaAnualDole"
    lot: str = "Lote"
    hectares: str = "Hectareas"
    plants: str = "PlantasSembradas"


def adapt_planting_schedule(
    path: str | Path, weather: WeatherProvider, vocab: PlantingScheduleColumns = PlantingScheduleColumns()
) -> AdaptResult:
    """Planting plan -> `initial_population.csv` rows: one pre-flowering cohort
    per planting week, aged at the start of the week after the last planting
    (the simulation's first week), so a staggered establishment -- a fraction
    of the area each week, sized to the packing plant -- is represented exactly
    instead of one synchronized seed."""
    raw = _read_sheet(path, vocab.sheet)
    missing = [c for c in (vocab.week, vocab.plants) if c not in raw.columns]
    if missing:
        raise ValueError(f"{path}: missing planting column(s) {missing}")
    raw = raw[raw[vocab.week].notna()].reset_index(drop=True)
    parsed = raw[vocab.week].map(parse_week_label)
    weeks = raw[vocab.week].map(lambda label: week_label_to_grid_week(label, weather)).astype(int)
    if weeks.min() < weather.min_week_index:
        raise ValueError(f"{path}: planting starts before the weather record -- extend weather.xlsx back")
    plants = pd.to_numeric(raw[vocab.plants], errors="coerce")
    if plants.isna().any() or (plants < 0).any():
        raise ValueError(f"{path}: {vocab.plants} must be a non-negative count on every planted lot")
    per_week = plants.groupby(weeks).sum()
    start_week = int(per_week.index.max()) + 1
    data = pd.DataFrame(
        {
            "chain": "pre_flowering",
            "tag": np.nan,
            "population": per_week.to_numpy(dtype=float),
            "age_weeks": (start_week - per_week.index.to_numpy()).astype(int),
        }
    ).sort_values("age_weeks").reset_index(drop=True)
    data["snapshot_date"] = weather.calendar_date_for_week(start_week)
    hectares = pd.to_numeric(raw[vocab.hectares], errors="coerce").sum() if vocab.hectares in raw.columns else np.nan
    first_label = f"{parsed.iloc[0][0]} S:{parsed.iloc[0][1]:02d}"
    messages = [
        (f"{plants.sum():,.0f} plants ({hectares:,.1f} ha, {len(raw)} lots) planted over {len(per_week)} weeks "
        f"from {first_label}; simulation starts at grid week {start_week} ({data['snapshot_date'].iloc[0]})"),
    ]
    return AdaptResult(data=data, messages=messages, details={"snapshot_week_index": start_week})
