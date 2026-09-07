Always write modular, clean code.

Always use uv as package manager, script runner, and everything else.

# DynaVal development guidance

## Product and scope

DynaVal is a local, single-reviewer NiceGUI desktop application for correcting
datasets against source images. Producing usable corrected data is the primary
outcome; transcription-accuracy reporting is secondary. Keep the application
generic across datasets and domains. Read [PIPELINE.md](PIPELINE.md) before
implementing a feature; it defines the workflow, data contracts, and delivery order.

The workflow is: import a dataset, select image-reference columns, select fields
to review, configure corrections/skipping, confirm or correct each field, continue
unfinished work with the saved seed/order, and export the corrected dataset plus
a field-by-field review log. The initial release targets both macOS and Windows.
Linux is deferred. The user will set up GitHub Actions to build the packages;
provide reproducible build commands and platform requirements for that workflow.

The patient-registry request in PIPELINE.md is a motivating example, not a fixed
schema. Never hardcode its field list, medical concepts, or review criteria into
the core application. Reviewers choose fields that match their task/expertise.

The current repository is preparation only. `main.py` is still a starter stub.
Do not describe planned screens, tests, or executables as already implemented.

## Tooling

- Use Python 3.13, as selected by `.python-version` and `pyproject.toml`.
- Use `uv sync --locked` to install the normal development environment.
- Use `uv add <package>`, `uv add --dev <package>`, and
  `uv add --group build <package>` for runtime, development, and packaging tools.
- Use `uv run` for Python, pytest, Ruff, mypy, NiceGUI, and PyInstaller commands.
  Do not introduce pip, Poetry, Conda, another lockfile, or bare Python commands.
- Keep `pyproject.toml` and the uv-generated `uv.lock` consistent. Do not hand-edit
  `uv.lock`. Runtime dependencies must not depend on the development/build groups.
- Prefer the standard library for CSV/JSON parsing, SQLite, hashing, and random
  ordering. Add another dependency only for an actual implementation need.

## Architecture

Use a `src/dynaval/` package when implementation begins. Keep the root `main.py`
as a small, freeze-safe launcher. Proposed module responsibilities:

- `domain/`: typed models, review states, value semantics, and queue generation.
  No NiceGUI, filesystem, networking, or database imports.
- `importers/`: format adapters and structural validation; preserve source values.
- `storage/`: SQLite repositories, transactions, schema versions, migrations,
  and operating-system application-data paths.
- `services/`: import, session, media, corrected-dataset export, review-log export,
  and optional accuracy-report use cases. Services enforce
  state transitions and settings independently of the UI.
- `ui/`: NiceGUI pages and reusable image/field/progress components; delegate
  parsing, saving, and decisions to services.
- `app.py`: dependency construction, page registration, and app lifecycle.
- `tests/`: small synthetic fixtures, domain/service tests, persistence and UI
  integration tests. Add modules as they are needed, not as empty scaffolding.

Use explicit dependencies, type annotations, small functions, and narrow
interfaces at I/O boundaries. Prefer composition and simple repositories over
generic frameworks. Do not put business logic in click handlers or global UI
variables. Run blocking parsing, database, and image work off the UI event loop;
serialize database writes and keep connections on their owning worker thread.

## Data and review invariants

- A step is exactly one `(session_id, source_row, src_col)` pair. Source row and
  column indexes are zero-based; UI progress is one-based. `src_col` indexes the
  complete imported schema, not the subset of selected columns.
- Preserve original file bytes and normalized original cell values. Never edit
  the imported dataset in place. CSV values such as `NA`, `FALSE`, leading zeros,
  empty strings, and whitespace are data, not automatic null/type conversions.
- Apply committed corrections as an overlay when exporting a NEW dataset CSV.
  Preserve source row/column order, image references, and unselected values.
  Keep review metadata in a separate log; an unfinished export must identify
  unresolved selected fields through that log and the export summary.
- Column names are data. Preserve dotted names literally; never interpolate
  headers into SQL or render dataset text as executable HTML/Markdown.
- Corrections default to on; skipping defaults to off. Enforce both settings in
  services as well as UI controls. Keep a Mark Invalid action available even
  when editing and skipping are disabled.
- `valid` means the ORIGINAL field was correct. `confirmed` has `valid=true`;
  `corrected` and `rejected` have `valid=false`; `skipped`/`pending` have no verdict
  and export a blank `valid`. Never label a skipped field invalid.
- Store a separate status, correction, original value, and effective value.
  An empty-string correction is a real correction; distinguish it from no
  correction with `status`/`is_corrected`. Editing a draft is not a decision.
- Review all rows in seeded shuffled order, grouping selected fields within each
  row in source-column order. Persist the full queue and seed before review;
  never regenerate the queue on resume. No train/test split or sampling in v1.
- Continue a saved dataset with its original seed, queue, selected fields, edits,
  and cursor. A seed alone is not a checkpoint. On reimport of matching source
  bytes, offer existing sessions before creating a new one; never silently merge.
- `confirmed` and `corrected` resolve a selected field. `rejected`, `skipped`, and
  `pending` remain unresolved for dataset correction. Offer an explicit follow-up
  pass over unresolved fields in their existing queue order, retaining earlier
  decisions as history. Do not reshuffle, restart, or duplicate export-log rows.
- Completion applies only to selected fields. Do not call an entire dataset
  fully corrected when unselected or unresolved fields remain unreviewed.
- Save each decision and cursor advance in one transaction before changing the
  visible step. Duplicate click/retry delivery must not create duplicate results.
- Persist pause state and drafts; recover committed work after an unexpected
  close. NiceGUI browser/session storage is not the source of truth.
- Missing images, network failures, parse failures, and failed saves are errors,
  never automatic validation outcomes. Report an actionable recovery path.

## Reference images and local files

Support multiple reference columns containing HTTP(S) URLs, local files, or
mounted-share paths. On Windows, support native drive and UNC paths through the
current user's existing filesystem access. On macOS, UNC paths require a mapping
to an already mounted local directory. Do not mount shares or obtain credentials
implicitly. Keep the original path and mapping separate.

Load images asynchronously with bounded downloads and a per-session cache. Show
loading/error states and allow retry, image switching, zoom, pan, and reset.
Store sessions using `platformdirs`, outside the source tree, application bundle,
and PyInstaller extraction directory. Restrict local media serving to the active
session's registered files; do not expose arbitrary filesystem paths.

`measles_data_27_08.csv` is an ignored local reference, not a redistributable test
fixture. It contains 1,115 rows, 43 columns, a `$` delimiter, and three UNC image
columns. Do not commit it, copy its records into tests/docs, or bundle it in a
release. Use synthetic fixtures matching structural cases. Never log full
records, private image URLs, or image contents.

## Verification and packaging

Commands available after dependency setup:

```sh
uv sync --locked --all-groups
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy main.py
```

Once the implementation package and tests exist, also run:

```sh
uv run mypy src/dynaval
uv run pytest
uv run pytest --cov=dynaval --cov-report=term-missing
```

Run tests appropriate to the change. Prioritize import fidelity, deterministic
ordering, correction/skip semantics, atomic writes, duplicate submissions,
resume after process interruption, unresolved follow-up passes, image failures,
and corrected-dataset/review-log export round trips. Verify optional accuracy
counts independently from correction-completion counts.
Use temporary directories and mocked HTTP; tests must not require the reference
dataset, private shares, real network services, or a native desktop window.
Use NiceGUI's testing helpers for UI integration and a separate native smoke test.

Build with the locked `build` dependency group. Use NiceGUI's `nicegui-pack`
wrapper around PyInstaller, starting with `--onedir --windowed` builds: a macOS
`.app` and a Windows folder containing `DynaVal.exe` and its bundled dependencies.
Keep `multiprocessing.freeze_support()` first inside the launcher main guard,
native configuration at import scope where needed, and `reload=False` for release.
Bind the app to loopback. Include required NiceGUI assets and platform hooks.
Verify launches, image access, pause/reopen with the same seed, both CSV exports,
and clean exit on BOTH platforms; a successful build alone is not sufficient.
Include Windows WebView2/.NET prerequisites in the packaging handoff. Implement
filesystem paths, per-user data directories, and session locking for both systems.

Build on the target operating system and architecture. macOS and Windows are
required release targets; report each platform's actual verification separately.
Do not claim untested architectures, signing, or notarization support. Follow the
build handoff in PIPELINE.md; the user owns GitHub Actions setup. Keep README and
PIPELINE accurate
about implemented behavior, defaults, limitations, and checks actually run.
