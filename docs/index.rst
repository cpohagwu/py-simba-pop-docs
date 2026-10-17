py-simba-pop
============

**py-simba-pop** is a Python implementation of the **SIMBA-POP** cohort model
described by Tixier, Malézieux & Dorel (2004, *Ecological Modelling*
180:407–417), including the flowering-rate correction of Ochoa (2010) and the
cohort-basis reading of Guarín & Ochoa (2012). The engineering and the
data-driven extensions (observed ribbon/bagging routing, Modes B/C) are by the
author. Not affiliated with or endorsed by CIRAD or the original authors.

py-simba-pop simulates a banana farm week by
week from daily temperature and reports, among other outputs, the number of
bunches harvested each week.

- One ``SimbaPopEngine`` drives every simulation.
- Every parameter has a published default, so a first run needs only a weather
  file. Your own calibrated values load from a plain YAML file.
- Every input file type has exactly one standardized schema, whichever farm the
  data comes from.
- Real ribbon-tag bagging and farm-wide sucker-selection records can drive the
  model directly. Whenever a record is missing, the engine falls back to the
  model's own prediction and records an explicit notice.

.. toctree::
   :maxdepth: 2
   :caption: Getting started

   installation
   quickstart

.. toctree::
   :maxdepth: 1
   :caption: Example notebooks

   examples/01_running_farm_simulation
   examples/02_initial_population_structure_and_growth_limits
   examples/03_validating_farm_simulation

The notebooks run on two synthetic farms, in ``docs/examples/`` of the
`py-simba-pop-docs <https://github.com/cpohagwu/py-simba-pop-docs>`_
repository; notebook 02 also uses py-simba-pop-tools.
