Quick start
===========

This page walks through the public API, from a first simulation to a run
driven by a farm's own field records. The
:doc:`example notebooks <examples/01_running_farm_simulation>` show the same
steps on complete synthetic farms.

A first simulation
------------------

Every parameter has a published Tixier et al. (2004) default, so a weather
file and a starting number of plants are all a first run needs:

.. code-block:: python

   from py_simba_pop import ParameterSet, SimbaPopEngine, WeatherProvider

   params = ParameterSet.from_tixier_defaults()
   weather = WeatherProvider.from_file_and_params("weather.csv", params)  # date, tmean columns

   engine = SimbaPopEngine.from_seed_population(params, weather, seed_population=1000.0)
   output = engine.run(n_weeks=200)
   print(output[["week_index", "date", "Nt", "Ht"]].tail())

``run()`` returns a pandas DataFrame with one row per simulated week (see
`Outputs`_). Calling it again continues from the week the engine has reached.

Weeks are numbered by the weather file: ``week_index`` 0 is the 7-day block
that starts on the first weather date, week 1 the next block, and so on. Every
dated record is placed in the week it falls in, and
``weather.week_index_for_date(d)`` and ``weather.calendar_date_for_week(w)``
convert between the two.

Input files
-----------

Each file type has one standardized schema and its own reader. Every reader
accepts ``.csv`` and ``.xlsx`` files (``.xlsx`` needs openpyxl) and ignores
columns outside its schema. Dates must be calendar dates, for example
``2019-01-07``.

.. list-table::
   :header-rows: 1
   :widths: 18 32 50

   * - File
     - Reader
     - Columns
   * - Weather
     - ``WeatherProvider.from_file_and_params``
     - ``date``, ``tmean`` (daily mean temperature, °C). Alternatively a
       precomputed daily thermal value in ``heat_units``.
   * - Initial population
     - ``PopulationInventoryReader``
     - ``chain`` (``pre_flowering`` or ``post_flowering``), ``population``,
       ``age_weeks`` (whole weeks), and optionally ``tag``, the ribbon tag of
       a post-flowering row.
   * - Production
     - ``RibbonProductionReader``
     - One row per ribbon tag and week: ``tag``, ``date``, ``bagged``,
       ``cut``, ``age_at_cut`` (weeks since bagging, 0 = cut in the bagging
       week); optionally ``field_losses``, ``rejected`` and ``processed``
       (``rejected`` and ``processed`` are part of ``cut``).
   * - Sucker selection
     - ``SuckerSelectionReader``
     - Farm-wide weekly counts: ``date``, ``parous_count`` (followers of
       mothers that have already flowered), ``nulliparous_count`` (followers
       of mothers that have not flowered yet).

Your own column names
---------------------

A weather file with other column names needs a ``ColumnMap``:

.. code-block:: python

   from py_simba_pop import ColumnMap

   columns = ColumnMap(date="day", tmean="t_mean_c")
   weather = WeatherProvider.from_file_and_params("my_weather.csv", params, columns)

``ColumnMap(date="day", heat_units="gdd")`` reads a precomputed daily thermal
value instead of a temperature. The other readers use fixed column names, so
rename those columns before saving the file.

Custom parameter files
----------------------

Already-calibrated values load from a plain YAML file with the same five
sections as the defaults (``general``, ``flowering``, ``sucker_selection``,
``harvest`` and ``mortality_replacement``). Every section and every key must be
present; a missing or unknown key raises ``ParameterError`` naming it. Writing
the defaults out is the easiest way to start one:

.. code-block:: python

   import yaml

   defaults = ParameterSet.from_tixier_defaults()
   sections = ("general", "flowering", "sucker_selection", "harvest", "mortality_replacement")
   with open("my_farm_params.yaml", "w", encoding="utf-8") as fh:
       yaml.safe_dump({name: dict(getattr(defaults, name)) for name in sections}, fh, sort_keys=False)

   params = ParameterSet.from_yaml("my_farm_params.yaml")

To change a few values in code, ``override`` returns a new ``ParameterSet`` and
leaves the original untouched:

.. code-block:: python

   later_harvest = params.override("harvest", {"threshold_dd": 950.0})
   print(later_harvest.harvest["threshold_dd"], params.harvest["threshold_dd"])

py-simba-pop contains no parameter-fitting code. py-simba-pop-tools
(``py_simba_pop_tools.calibration``) fits harvest timing from ribbon-tagged
records; save the result as such a YAML file.

Starting from a field inventory
-------------------------------

An established farm starts from its census, read by
``PopulationInventoryReader``. ``start_week_index`` is the week the engine's
clock starts at, in the weather file's numbering:

.. code-block:: python

   from datetime import date

   from py_simba_pop import PopulationInventoryReader

   inventory = PopulationInventoryReader.read("initial_population.csv").data
   start = weather.week_index_for_date(date(2018, 12, 31))  # the census date

   engine = SimbaPopEngine(params, weather, inventory, start_week_index=start)
   output = engine.run(n_weeks=52)

A cohort's ``age_weeks`` fixes the week it started in, and that week must lie
inside the weather record. ``start_week_index >= max(age_weeks)`` is always
safe when the weather starts at week 0. A start week that is too early raises
``CohortChainError``, and the message names the smallest ``start_week_index``
that works.

Driving the engine with field records
-------------------------------------

Ribbon-tag production records and farm-wide sucker-selection records are read
with their own readers and wrapped against the same ``WeatherProvider`` the
engine uses, so their dates map to the same week numbers:

.. code-block:: python

   from py_simba_pop import (
       RibbonProductionData,
       RibbonProductionReader,
       SuckerSelectionData,
       SuckerSelectionReader,
   )

   production = RibbonProductionReader.read("production.csv")
   production.notices.emit_warnings()  # e.g. an optional column that is absent
   ribbon = RibbonProductionData(production.data, weather)

   selection = SuckerSelectionReader.read("sucker_selection.csv")
   sucker = SuckerSelectionData(selection.data, weather)

   engine = SimbaPopEngine(
       params,
       weather,
       inventory,
       start_week_index=start,
       ribbon_production=ribbon,
       sucker_selection=sucker,
       tags=list(ribbon.tags()),
   )
   output = engine.run(n_weeks=104)

Each reader returns a result with ``.data`` (the validated table) and
``.notices`` (data-quality notes); ``.notices.emit_warnings()`` re-emits them as
``SimbaPopDataNotice`` warnings. Both record types are optional. On a week the
records do not cover, the engine uses the model's own prediction instead and
records a notice. The recorded cut is never fed back into the simulation: the
weekly harvest ``Ht`` is always the model's prediction, so it can be scored
against the records.

Outputs
-------

``engine.run()`` and ``engine.get_output()`` return one farm-wide row per
simulated week:

.. list-table::
   :header-rows: 1
   :widths: 32 68

   * - Column
     - Meaning
   * - ``week_index``, ``date``
     - The simulated week and the calendar date it starts on.
   * - ``Nt``
     - All standing plants, ``Nt_pre + Nt_post``.
   * - ``Nt_pre``
     - Plants that have not flowered yet (vegetative).
   * - ``Nt_post``
     - Flowered plants with a bunch still on the plant.
   * - ``Ht``
     - Bunches harvested this week.
   * - ``FLt``
     - The model's flowering prediction for this week.
   * - ``St``
     - The model's follower (sucker) selection prediction,
       ``sucker_output_pre + sucker_output_post``.
   * - ``sucker_output_pre``
     - Predicted followers selected for mothers that have not flowered yet.
   * - ``sucker_output_post``
     - Predicted followers selected for mothers that have already flowered.
   * - ``Dt``
     - Plants that died this week.
   * - ``sucker_input_used``
     - Followers that actually started new vegetative cohorts this week: the
       recorded total when there is one, otherwise the prediction.
   * - ``flowering_routed_to_unassigned``
     - Flowering placed on the reserved ``__unassigned__`` tag because no
       ribbon record covered the week.
   * - ``notice_count``
     - Notices emitted this week.
   * - ``pruned_population_this_week``
     - Plants dropped because their cohort passed the maximum age or became
       negligibly small.

``engine.get_tag_output()`` breaks the bunched plants down by ribbon tag, one
row per week and tag (including ``__unassigned__``): ``week_index``, ``date``,
``tag``, ``Nt`` (standing bunches), ``Ht`` (harvest), ``Xt`` (field losses),
``bagged_input`` (bunches that started the tag's new cohort that week), and the
recorded ``cut_observed`` and ``age_at_cut_observed`` for comparison. Summed
over tags each week, its ``Nt`` and ``Ht`` equal the farm-wide ``Nt_post`` and
``Ht``.

``engine.snapshot_cohorts()`` returns the current inventory with the
initial-population columns (``chain``, ``tag``, ``population``,
``age_weeks``), so a later run can resume from it:

.. code-block:: python

   tag_output = engine.get_tag_output()
   snapshot = engine.snapshot_cohorts()
   resumed = SimbaPopEngine(params, weather, snapshot, start_week_index=engine.current_week_index)

Notices
-------

Every substitution the engine makes, for example a week without sucker-selection
records, is appended to ``engine.notices``, a plain list of ``Notice`` objects:

.. code-block:: python

   for n in engine.notices:
       where = "" if n.week_index is None else f" (week_index {n.week_index})"
       print(f"[{n.kind.value}] {n.subject}{where}: {n.message}")

``n.kind`` is ``NoticeKind.FALLBACK_ESTIMATE`` when the model's own prediction
was used instead, or ``NoticeKind.FEATURE_DISABLED`` when there is no substitute
and the quantity is treated as absent.

Next steps
----------

- :doc:`examples/01_running_farm_simulation`: a farm simulation, end to end.
- :doc:`examples/02_initial_population_structure_and_growth_limits`: the ways to
  build an initial population, and how the farm keeps its scale.
- :doc:`examples/03_validating_farm_simulation`: three ways to drive the
  engine, scored against the recorded harvest.
