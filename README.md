# py-simba-pop

[![PyPI](https://img.shields.io/pypi/v/py-simba-pop.svg)](https://pypi.org/project/py-simba-pop/)
[![Python](https://img.shields.io/pypi/pyversions/py-simba-pop.svg)](https://pypi.org/project/py-simba-pop/)
[![Documentation](https://readthedocs.org/projects/py-simba-pop/badge/?version=latest)](https://py-simba-pop.readthedocs.io/en/latest/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**py-simba-pop** is a Python implementation of the **SIMBA-POP** cohort model
described by Tixier, Malézieux & Dorel (2004, *Ecological Modelling*
180:407–417), including the flowering-rate correction of Ochoa (2010) and the
cohort-basis reading of Guarín & Ochoa (2012). The engineering and the
data-driven extensions (observed ribbon/bagging routing, Modes B/C) are by the
author. Not affiliated with or endorsed by CIRAD or the original authors.

One `SimbaPopEngine` drives every
simulation, every input file type has exactly one standardized schema whichever
farm the data comes from, and a bare seed population and a full field inventory
are passed in the same way. Real ribbon-tag bagging and farm-wide
sucker-selection records can drive the model directly; whenever a record is
missing, the engine falls back to the model's own prediction and says so in an
explicit notice, never silently.

## Installation

```bash
pip install py-simba-pop
```

Compiled wheels are published for Python 3.11–3.13 on Linux (x86-64), macOS
(universal2) and Windows (x86-64). Reading `.xlsx` input files also needs
`openpyxl`. See the
[installation guide](https://py-simba-pop.readthedocs.io/en/latest/installation.html)
for details.

## Quick Start

The Jupyter notebooks in [`docs/examples/`](docs/examples/) show how to run,
initialize and validate the model on two synthetic farms:

```bash
git clone https://github.com/cpohagwu/py-simba-pop-docs.git
cd py-simba-pop-docs/docs/examples
pip install "py-simba-pop[notebooks]"
```

- [01_running_farm_simulation.ipynb](docs/examples/01_running_farm_simulation.ipynb):
  a farm simulation, end to end.
- [02_initial_population_structure_and_growth_limits.ipynb](docs/examples/02_initial_population_structure_and_growth_limits.ipynb):
  the ways to build an initial population, and how the farm keeps its scale.
- [03_validating_farm_simulation.ipynb](docs/examples/03_validating_farm_simulation.ipynb):
  three ways to drive the engine, scored against the recorded harvest.

The `data/` folder next to the notebooks holds the synthetic farms' canonical
CSV files. Notebook 02 also uses py-simba-pop-tools, for the initial-population
reconstruction.

## Documentation

User guides and the rendered notebooks are hosted on
[Read the Docs](https://py-simba-pop.readthedocs.io).

## Citation

If you reference this model or package in an academic publication, please use
the "Cite this repository" button in the GitHub sidebar of this repository, or
refer to the [`CITATION.cff`](CITATION.cff) file.

## License

Distributed under the Apache License 2.0. See [`LICENSE`](LICENSE) for more
information.
