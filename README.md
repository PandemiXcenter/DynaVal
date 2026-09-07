# DynaVal

A local NiceGUI desktop app for correcting datasets against source images. Import
a dataset, select image columns and the fields you want to review, then confirm
or correct each value. Export a corrected dataset with its original shape and a
separate field-by-field review log. Transcription-accuracy reporting is optional.

Pause and continue unfinished work after restarting the app. Saved sessions retain
the original source snapshot, seed, exact review order, selected fields, committed
corrections and saved draft. Follow-up passes revisit unresolved fields in the
same order. The original input file is never changed.

The application is generic across datasets and domains. Its initial distribution
targets are macOS and Windows x64; Linux is deferred. The
[release workflow](.github/workflows/release.yml) builds both platforms and prepares
a draft release from a matching version tag. No public release has been published.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from
the repository root:

```sh
uv sync --locked
uv run python main.py
```

Python 3.13 is selected by `.python-version`. The installed entry point also works:

```sh
uv run dynaval
```

The default opens a native desktop window and binds the server to loopback only.
For browser-based development, start the local server and open its displayed URL:

```sh
uv run python main.py --browser --port 8080
```

Use `--data-dir <directory>` to isolate smoke-test sessions, cache and logs from
your normal application data. Do not place real sessions inside the repository.
Windows native mode needs WebView2 and .NET; see [packaging prerequisites](docs/PACKAGING.md).

## Try a synthetic dataset

```sh
uv run python scripts/create_demo.py /path/to/new/dynaval-demo
```

On Windows, a new path such as `C:\Temp\dynaval-demo` works. The generator creates
`dataset.csv` and two fictional source cards in `images/`. It never overwrites an
existing directory. Select `image` as the reference column and any of `name`,
`age`, or `label` to review. The first record's age deliberately needs correcting
from `37` to `38`. For an uploaded dataset, set the image folder to the demo folder;
the native file chooser preserves that base directory automatically.

## Workflow

1. Import CSV/TSV, JSONL/NDJSON, or a JSON array of objects. Preview the parsed
   schema and records. Set the delimiter or encoding explicitly when needed.
2. Select one or more image-reference columns. Use HTTP(S) URLs, local/relative
   paths, native Windows drives/UNC paths, or mapped already-mounted shares.
3. Select fields to correct. Corrections default to on and skipping to off.
   All rows are shuffled with a saved seed; fields remain grouped by source row.
4. Confirm unchanged originals, save a correction, mark an original invalid, or
   skip when enabled. All selected reference images must load before a verdict.
   Edited drafts remain visible and are autosaved separately from decisions.
5. Pause and continue the saved session, or finish the pass and revisit unresolved
   fields. Export complete or partial results whenever needed.

Each export is a new folder containing `corrected_dataset.csv`, `review_log.csv`
and `manifest.json`; optionally include `accuracy_report.csv`. CSVs use UTF-8 with
a BOM, comma delimiters and standard quoting. The corrected dataset preserves
original source order, every column and unselected values. Only committed
corrections replace cells; unsaved drafts never enter an export.

`valid` describes the **original** field: confirmed originals are `true`,
corrected/rejected originals are `false`, and skipped/pending fields are blank.
`status`, `is_corrected` and `effective_value` make accepted replacements explicit,
including an intentional empty-string correction. Confirmed and corrected fields
are resolved; rejected, skipped and pending fields remain unresolved. Completion
applies only to selected fields, not to the accuracy of every dataset column.

## Local data and limits

Sessions use SQLite and immutable source snapshots in the operating system's
per-user application-data directory. Image caches and rotating operational logs
are separate. Dataset records, private image URLs and image contents are not logged.

| Platform | Sessions | Cache | Logs |
| --- | --- | --- | --- |
| macOS | `~/Library/Application Support/DynaVal/sessions` | `~/Library/Caches/DynaVal` | `~/Library/Logs/DynaVal` |
| Windows | `%LOCALAPPDATA%\DynaVal\sessions` | `%LOCALAPPDATA%\DynaVal\Cache` | `%LOCALAPPDATA%\DynaVal\Logs` |

Session continuation is local to one installation. Exported CSVs are deliverables,
not checkpoints; continue through the saved session. A moved original input does
not prevent resume because its snapshot is preserved. Image shares must already
be accessible through the operating system; DynaVal does not mount or authenticate
them. Cached images can work offline; uncached remote images need their source.

Imports are limited to 100 MiB. JPEG, PNG and WebP references are limited to 20 MiB
and 50 megapixels, with a 30-second load timeout, at most two transient retries
and three HTTP(S) redirects. The image cache has a 256 MiB target per session;
the current/next pinned row can temporarily exceed that target. There is no OCR,
automatic validation, multi-reviewer synchronization, Excel or Parquet adapter.

CSV preserves literal strings, including leading zeros, whitespace, `NA`, and
formula-like text. Import value columns as **text** in spreadsheet software.
JSON null and missing keys become blank dataset CSV cells; their original kinds
remain identified in the manifest unless replaced by committed corrections.

The ignored `measles_data_27_08.csv` is private local reference material. Do not
copy it into tests/docs or bundle it in releases. All checked-in tests and the demo
use synthetic records.

## Verify and package

```sh
uv sync --locked --all-groups
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy main.py src/dynaval
uv run pytest
uv run pytest --cov=dynaval --cov-report=term-missing
```

The automated suite covers parsing fidelity, review semantics, persistence,
failure recovery, image loading, exports, packaging helpers and NiceGUI simulated
users. Ordinary tests need no native window, private dataset/share or real network
service. Playwright is a development-only dependency for optional real-browser
regressions, skipped by default:

```sh
uv run playwright install chromium
uv run pytest --run-browser tests/test_browser.py
```

The browser regression launches an isolated local app, verifies correction and
resume across process restart, and writes screenshots/traces under
`output/playwright/`. It uses synthetic data and does not certify native webview
behavior.

Build on each target operating system with the locked packaging group:

```sh
uv run --group build python scripts/build_release.py
```

This builds `packaging/DynaVal.spec`, archives the complete `.app` or Windows
directory, and writes SHA-256 checksums and build metadata. See
[docs/PACKAGING.md](docs/PACKAGING.md) for exact commands, platform requirements,
the synthetic native smoke test, icons, notices and release handoff. The maintainer
controls signing/notarization, release publication and supported-OS claims.
After pushing the workflow to the default branch, use **Actions → Release → Run
workflow** for a build-only test. Pushing a tag such as `v0.1.0` builds both targets
and attaches verified downloads to a draft prerelease; publication stays manual.
See the [step-by-step release instructions](docs/PACKAGING.md#github-actions-release-workflow).
The [v0.1.0 release draft](docs/RELEASE_NOTES.md) records included behavior and
the remaining distribution verification.

The final macOS arm64 executable built and passed version, bundled HTTP/assets,
timed native launch/exit and full Chromium correction/pause/process-restart/
resume/export checks locally. The normal suite passed 223 tests with one optional
browser case skipped; that Chromium case also passed separately against source
and the frozen executable. Application coverage is 83% with branch tracking.
Windows dependencies/spec branches are ready; native Windows verification still
requires a Windows runner. Full manual native and clean-machine workflows remain
release checks. These results do not certify untested operating systems or
architectures.

[PIPELINE.md](PIPELINE.md) records the workflow, versioned output contracts and
acceptance criteria. [AGENTS.md](AGENTS.md) defines development rules and boundaries.
