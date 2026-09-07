# DynaVal 0.1.0 — local release draft

DynaVal provides a local desktop workflow for correcting selected dataset fields
against source images. This is a release-preparation draft; no GitHub Release,
release signature or notarization has been published. A GitHub Actions workflow
now prepares draft prereleases after macOS and Windows build/test jobs succeed;
its first hosted runs remain to be verified.

## Included

- CSV/TSV, JSONL/NDJSON and JSON-array imports with explicit delimiter/encoding
  recovery, structural checks, exact string preservation and exact JSON numbers.
- Configurable field selection and multiple image columns using HTTP(S), local
  files, native Windows paths or prefixes mapped to already-mounted shares.
- A simplified NiceGUI desktop interface in Ruby Red, Coral Glow, Azure Mist,
  Pearl Aqua and Prussian Blue, with image switching/zoom/pan, visible edits,
  corrections, confirmation, rejection and optional skipping.
- Durable SQLite sessions with immutable source snapshots, seeded saved queues,
  draft autosave, pause/restart continuation and unresolved follow-up passes.
- New corrected-dataset CSV bundles, a latest-decision review log, completion/
  partial-export metadata and optional original-transcription accuracy reporting.
- Native macOS/Windows packaging spec, icons, runtime notices, archive/checksum
  scripts, synthetic demonstration data and automated regression tests.

## Verification

Synthetic automated checks cover import fidelity, deterministic queues, field
semantics, transactional persistence and interruption recovery, session locking,
image/path failure handling, exports, UI interactions and packaging resources.
The opt-in Chromium workflow has passed locally on macOS arm64, including a full
app process restart followed by saved-seed/draft continuation and export.

The final local checks passed: 223 tests with one optional browser test skipped
in the normal suite; the separate Chromium regression passed. Application coverage
is 83% with branch tracking enabled. Repository-wide Ruff checks passed, and mypy passed for
43 source files. The locked Windows all-groups dependency dry run resolved
89 packages; this verifies dependency planning, not a native Windows launch.

The final macOS arm64 `.app` was built on macOS 26.6.2 with Python 3.13.11 and passed:

- `--version` (`0.1.0`, exit 0), HTTP root and all 14 referenced NiceGUI assets.
- Bundled CSS and image-viewer JavaScript equality with the final source.
- Browser-mode timed shutdown and a four-second native launch/shutdown (exit 0).
- The full Chromium workflow against the frozen binary: import, image viewing
  and zoom retention, confirmation, edited draft, pause, binary process restart,
  same seed/current field/draft, correction and dataset/log/manifest/report exports.
  No JavaScript or HTTP errors were observed; the final review screenshot was
  visually checked.

Verified local artifact:

| Item | Value |
| --- | --- |
| ZIP | `dist/verified-release/DynaVal-0.1.0-macos-arm64.zip` |
| Size | 38,743,957 bytes |
| SHA-256 | `98ea0673db84fcd2978da357d1ddc23b1b6426c4c679a52f033c074d9144d8a5` |
| Sidecars | Matching `.zip.sha256` and `.zip.json` files |

The archive contains the complete `.app`, including native icons, UI resources
and collected runtime notices. Its build has local ad-hoc signing, with no
Developer ID signing or notarization configured.

The Windows dependency plan and Windows spec/version/resource branches are ready.
Native Windows builds and clean-machine desktop smoke tests require a Windows
runner. Intel macOS, minimum supported operating-system versions, full manual
native workflows and release signing/notarization are not certified by local
automated or Chromium checks.

## Operating limits

Imports are limited to 100 MiB. References support JPEG, PNG and WebP up to 20 MiB
and 50 megapixels, with bounded timeouts/retries and per-session caching. Image
shares must already be accessible; authenticated provider integrations and share
mounting are outside this release.

Sessions are local to one installation and reviewer. There is no OCR, automatic
validation, cloud synchronization, sampling, Excel/Parquet adapter or session
portability. The input file is preserved; exports apply committed corrections to
a new CSV. CSV retains formula-like text and leading zeros, so import value columns
as text in spreadsheet software. JSON null/missing distinctions are recorded in
the manifest because CSV alone cannot preserve them.

Completion means that the selected fields have been confirmed or corrected. It
does not certify unselected fields or guarantee factual accuracy. Pausing and
reopening uses the saved session, not reimported export CSVs. Abrupt process kills
can lose typing after the latest draft autosave, while committed decisions remain
durable.

## Distribution handoff

Build on each target operating system with:

```sh
uv sync --locked --all-groups
uv run --group build python scripts/build_release.py
```

Distribute the complete macOS `.app` ZIP or Windows onedir ZIP with its `.sha256`
file. Windows requires WebView2 and a suitable .NET Framework runtime. See
[PACKAGING.md](PACKAGING.md) for platform prerequisites, exact commands, the native
smoke checklist and artifact layout. The maintainer owns Actions YAML, project
licensing, target-runner support claims, signing, release metadata and publication.
