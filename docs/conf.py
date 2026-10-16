"""Sphinx configuration for the py-simba-pop documentation."""

import json
import os
import re

# -- Project information -----------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_CITATION = os.path.join(_HERE, os.pardir, "CITATION.cff")


def _citation_version() -> str:
    """The release number, read from CITATION.cff (its single home in this repo)."""
    with open(_CITATION, encoding="utf-8") as fh:
        text = fh.read()
    # Anchored at the line start, so it never matches "cff-version:".
    match = re.search(r"""^version:\s*["']?([^"'\s#]+)""", text, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"no top-level 'version:' field in {_CITATION}")
    return match.group(1)


project = "py-simba-pop"
copyright = "2026, Collins Patrick Ohagwu"
author = "Collins Patrick Ohagwu"
release = _citation_version()
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.duration",
    "sphinx.ext.doctest",
    "nbsphinx",
]

templates_path = ["_templates"]
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "**/.ipynb_checkpoints",
    "examples/data",
]

# The committed notebook outputs are the documentation: never re-run them.
nbsphinx_execute = "never"
# No require.js: plotly.js must define the global `Plotly`, not register as an AMD module.
nbsphinx_requirejs_path = ""

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

# The plotly.js release bundled with plotly.py 6.6.0, which produced the committed figures.
PLOTLY_JS_VERSION = "3.4.0"
html_js_files = [(f"https://cdn.plot.ly/plotly-{PLOTLY_JS_VERSION}.min.js", {"charset": "utf-8"})]

# -- Plotly figures in notebooks ---------------------------------------------
# The notebooks store each Plotly figure only as application/vnd.plotly.v1+json,
# which nbsphinx cannot display. At build time, give every such output a
# text/html twin (a div plus a Plotly.newPlot call). The .ipynb files on disk
# are never modified.

_PLOTLY_MIME = "application/vnd.plotly.v1+json"


def _plotly_html(figure: dict, div_id: str) -> str:
    layout = figure.get("layout", {})
    height = f"{layout['height']}px" if layout.get("height") else "525px"
    width = f"{layout['width']}px" if layout.get("width") else "100%"
    config = {"responsive": True, **figure.get("config", {})}
    spec = json.dumps({"data": figure.get("data", []), "layout": layout, "config": config})
    spec = spec.replace("</", "<\\/")  # never close the <script> element early
    return (
        f'<div id="{div_id}" class="plotly-graph-div" style="height:{height}; width:{width};"></div>\n'
        f'<script type="text/javascript">(function () {{ var fig = {spec}; '
        f'Plotly.newPlot("{div_id}", fig.data, fig.layout, fig.config); }})();</script>'
    )


def _add_plotly_html(app, docname, source):
    if not docname.startswith("examples/") or not source[0].lstrip().startswith("{"):
        return
    notebook = json.loads(source[0])
    count = 0
    for cell in notebook.get("cells", []):
        for output in cell.get("outputs", []):
            data = output.get("data", {})
            if _PLOTLY_MIME in data and "text/html" not in data:
                count += 1
                div_id = f"{docname.replace('/', '-')}-fig-{count}"
                data["text/html"] = _plotly_html(data[_PLOTLY_MIME], div_id)
    if count:
        source[0] = json.dumps(notebook)


def setup(app):
    app.connect("source-read", _add_plotly_html)
