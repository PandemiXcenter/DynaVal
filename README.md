# DynaVal

A planned NiceGUI desktop app for correcting datasets against source images.
Import a dataset, connect image columns, choose the fields you want to work on,
then confirm or correct each value. Export a corrected dataset with its original
rows and columns, plus a separate field-by-field review log. An optional report
summarizes original transcription accuracy.

Unfinished datasets can be continued after pausing or closing the app. Saved
sessions retain the same random seed, exact review order, selected fields,
corrections, and current step. Unresolved fields can be revisited in that order.

The initial release targets both macOS and Windows, packaged with PyInstaller.
The maintainer will set up GitHub Actions to build both distributions. Linux is
deferred. There is no application release yet; `main.py` remains a starter stub.

The motivating use case is correcting a historical patient registry, but field
selection and image references remain configurable for other datasets and tasks.

## Development setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
```

Python 3.13 is selected by `.python-version`. Include packaging tools with:

```sh
uv sync --locked --all-groups
```

Check the current starter and environment:

```sh
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy main.py
```

See [PIPELINE.md](PIPELINE.md) for the exact implementation plan, output contracts,
dependency choices, tests to build, and the GitHub Actions packaging handoff.
[AGENTS.md](AGENTS.md) defines the development rules and architecture boundaries.

The local `measles_data_27_08.csv` reference is ignored and is not included in
releases. It uses a `$` delimiter and three network-path image columns; the plan
supports selecting delimiters and mapping those paths to mounted directories.
