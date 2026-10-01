Installation
============

Supported platforms
-------------------

py-simba-pop is published on PyPI as compiled wheels for Python 3.11, 3.12 and
3.13:

.. list-table::
   :header-rows: 1

   * - Operating system
     - Wheel
   * - Linux, x86-64
     - ``manylinux_2_28`` (glibc) and ``musllinux_1_2`` (musl, e.g. Alpine)
   * - macOS
     - ``universal2`` (Intel and Apple silicon)
   * - Windows, x86-64
     - ``win_amd64``

No source distribution is published. On any other platform or Python version,
pip reports that no matching distribution was found.

Install from PyPI
-----------------

.. code-block:: bash

   python -m pip install py-simba-pop

This also installs the two runtime dependencies, pandas and PyYAML.

To keep the package in its own virtual environment:

.. code-block:: bash

   python -m venv .venv
   source .venv/bin/activate      # on Windows: .venv\Scripts\activate
   python -m pip install py-simba-pop

Check which version is installed:

.. code-block:: bash

   python -c "import py_simba_pop; print(py_simba_pop.__version__)"

Upgrade with ``python -m pip install --upgrade py-simba-pop``.

Excel input files
-----------------

Every reader accepts ``.csv`` and ``.xlsx`` files. Reading ``.xlsx`` files
needs openpyxl, which is not installed with the package:

.. code-block:: bash

   python -m pip install py-simba-pop openpyxl

Example notebooks
-----------------

The ``notebooks`` extra installs what the example notebooks and their helper
scripts use (Plotly, openpyxl, SciPy, nbconvert and ipykernel). The notebooks
themselves live in the
`py-simba-pop-docs <https://github.com/cpohagwu/py-simba-pop-docs>`_
repository:

.. code-block:: bash

   python -m pip install "py-simba-pop[notebooks]"
   git clone https://github.com/cpohagwu/py-simba-pop-docs.git
   cd py-simba-pop-docs/docs/examples

Open the notebooks from this folder, in VS Code or any Jupyter front end (for
example ``python -m pip install jupyterlab`` and then ``jupyter lab``), so that
their relative ``data/`` and ``utils/`` paths resolve.

The helper scripts in ``utils/`` are not part of the installed package. Run
them as modules from the same folder:

.. code-block:: bash

   python -m utils.build_farm_inputs data/<farm>          # build a farm's canonical CSV files
   python -m utils.build_custom_parameters data/<farm>    # fit your own harvest parameters
