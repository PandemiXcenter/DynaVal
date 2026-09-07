# Desktop packaging

DynaVal builds on its target operating system with Python 3.13 and the locked uv
environment. macOS and Windows x64 are release targets; Linux is deferred. The
maintainer controls publication and signing. The local build commands below do
not publish a release, configure credentials, or sign it for distribution.

[RELEASE_NOTES.md](RELEASE_NOTES.md) is the v0.1.0 release draft and verification
record to finalize before publication.

## GitHub Actions release workflow

[release.yml](../.github/workflows/release.yml) builds on `macos-15` (Apple Silicon)
and `windows-2025` (x64). Both jobs use uv 0.9.21 and Python 3.13, install the locked
dependencies, run Ruff/mypy/pytest, build with PyInstaller, and run the Chromium
correction/restart/export test against the packaged executable. A failed check
prevents that build from being attached to a release.

Commit and push the workflow and its helper/tests/docs to the default branch first.
To try it without creating a release, open **Actions → Release → Run workflow**,
select the branch, and run it. Download each platform's artifact from the completed
run; the outer Actions ZIP contains the application's ZIP and its sidecars.
Manual runs never create or update a GitHub Release, even when run on a tag.
Actions downloads are retained for 14 days; release attachments persist separately.

To prepare a release:

1. Set the same numeric version in `pyproject.toml` and
   `src/dynaval/__init__.py`, refresh the lock with `uv lock`, and commit/push.
2. Tag that commit with the matching `v` prefix and push the tag, for example:

   ```sh
   git tag -a v0.1.0 -m "DynaVal 0.1.0"
   git push origin v0.1.0
   ```

3. Wait for both platform builds in **Actions → Release**. The workflow verifies
   both ZIPs, checksums and metadata, then creates a **draft prerelease** under
   **Releases**, with all six download files attached.
4. Test the downloads on their native platforms, edit the notes, then click
   **Publish release**. Keep the prerelease checkbox for initial testing builds;
   clear it when the version is ready for a stable release.

Tags must match the project version exactly, such as `v0.1.0`; the current native
version resources use numeric major/minor/patch versions. Use GitHub's prerelease
checkbox for previews. A rerun refreshes files on an existing draft and preserves
edited notes. It refuses to replace downloads on an already published release;
use a new version/tag for that case.

The workflow uses the automatic `GITHUB_TOKEN`; no personal access token is needed.
Only the draft-release job requests `contents: write`. Repository/organization
Actions policy must allow the pinned GitHub actions, Astral's setup-uv action, and
that permission. Build jobs have read-only repository access. Signing credentials
are not configured. Automated browser tests do not replace the native and
clean-machine checks below. The first hosted Windows/macOS runs remain to be
verified after this workflow is pushed.

References: [GitHub runner platforms](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[manual workflow runs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
and [draft releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).

## Build and archive

Run the same commands from the repository root on macOS and Windows (PowerShell):

```sh
uv sync --locked --all-groups
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy main.py src/dynaval
uv run pytest
uv run --group build python scripts/build_release.py
```

Optional real Chromium workflow verification is available separately:

```sh
uv run playwright install chromium
uv run pytest --run-browser tests/test_browser.py
```

It is skipped by ordinary pytest, uses an isolated local app and synthetic data,
and saves screenshots/traces under `output/playwright/`. It does not replace the
native desktop checks below.

After a native build, run the same workflow against the frozen executable:

```sh
uv run pytest --run-browser tests/test_browser.py --app-executable dist/DynaVal.app/Contents/MacOS/DynaVal
```

On Windows, pass `--app-executable dist/DynaVal/DynaVal.exe` instead. This verifies
the frozen server and its bundled UI resources, including the custom image viewer,
using Chromium; native webview behavior remains a separate smoke test.

The release script invokes the locked PyInstaller environment using the checked-in
`packaging/DynaVal.spec`. It rebuilds local `build/` and `dist/DynaVal*` outputs,
archives the complete application, and writes a SHA-256 checksum and JSON build
metadata under `dist/releases/`. An existing release ZIP or sidecar is never
overwritten; use `--output <new-directory>` for another archive of the same version.

| Target | Complete bundle | Release archive |
| --- | --- | --- |
| macOS, actual native build architecture | `dist/DynaVal.app` | `DynaVal-<version>-macos-<arch>.zip` |
| Windows x64 | `dist/DynaVal/` containing `DynaVal.exe` and `_internal/` | `DynaVal-<version>-windows-x86_64.zip` |

macOS arch names are `arm64` or `x86_64`, based on the build interpreter. Build and
verify each architecture separately. The script does not produce universal2 or
cross-compile Windows from macOS. A Windows ZIP must contain the entire `DynaVal`
directory. Never distribute the standalone `.exe` from this onedir build.

To rebuild only the native bundle:

```sh
uv run --group build pyinstaller --clean --noconfirm packaging/DynaVal.spec
```

To archive an existing build after native verification or maintainer-managed
signing, without rebuilding:

```sh
uv run --group build python scripts/build_release.py --skip-build --output dist/verified-release
```

The macOS ZIP uses Apple's `ditto` to retain framework symlinks, executable modes,
and resource metadata. Windows uses ZIP with all supporting files. Checksums name
the exact archive, while the JSON file records version, host, architecture, Python,
and the remaining desktop-verification requirement. A successful archive is not
evidence that a native smoke test has passed.

The spec was derived from the installed NiceGUI `nicegui-pack --onedir --windowed`
wrapper and retains its NiceGUI resource collection. It explicitly includes the
application's CSS and image-viewer JavaScript, pywebview JavaScript/native hooks,
platform backend imports, version resources and icons. It analyzes the source
package without bundling the repository, tests, private datasets or sessions.
See [PyInstaller's spec/build options](https://pyinstaller.org/en/stable/usage.html).

## Assets and notices

`packaging/create_icons.py` draws the sheet/check icon in the application palette;
the resulting PNG, `.icns` and `.ico` are checked in. Regenerate them with:

```sh
uv run python packaging/create_icons.py
```

Version metadata comes from `pyproject.toml`. The Windows executable has file and
product versions; the macOS bundle uses `org.dynaval.desktop` and the same version.
Keep the application version in `src/dynaval/__init__.py` aligned when releasing.

Each spec build collects installed runtime dependency license/notice files and
metadata into `build/packaging-assets/licenses`, then includes them under the
bundle's `licenses/` resource directory. The dependency walk follows platform and
extra markers, starting only from `[project].dependencies`; it does not enumerate
the development/build environment. Vendored license files present in runtime
distributions are included. The notice index identifies packages with no standalone
license file in their installed wheel. Review those metadata notices and the
bundled interpreter/native-library licensing when preparing a public release;
automated collection is not a substitute for that distribution review.

DynaVal is licensed under [MIT](../LICENSE). The spec includes its license under
`licenses/LICENSE` alongside the separate dependency notices. Python distributions
also include the license through `pyproject.toml`. Release signing/notarization and
final release notes remain maintainer choices. No Developer ID or Windows
Authenticode signing is configured.
PyInstaller may apply the ad-hoc macOS signature needed to run locally; that is
not Developer ID signing or notarization.

## Platform prerequisites and support claims

End users do not need Python or uv. On Windows, DynaVal requests pywebview's
EdgeChromium backend. Install Microsoft's
[WebView2 Evergreen Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)
if absent. The locked pywebview WinForms implementation checks for .NET Framework
4.6.2 or later for this backend; use a supported Windows installation with the
framework available (for example 4.8/4.8.1 where supported). The Python dependencies
`pythonnet` and `clr-loader` and pywebview's WebView2 loader/managed DLLs are bundled
through their platform hooks. The WebView2 browser runtime and .NET Framework are
OS prerequisites, not installed by DynaVal. See
[pywebview's installation guidance](https://pywebview.flowrl.com/guide/installation.html).

On macOS the app uses the native WebKit backend and bundled PyObjC bridges. Image
shares must already be mounted by the user. Windows drive and UNC access uses
the current user's existing filesystem access; the app does not mount shares or
request network credentials.

Minimum supported macOS and Windows versions are **not yet certified**. Record the
runner OS and run clean-install desktop verification before declaring minimums.
Do not infer DynaVal support from Python, PyInstaller or pywebview minimums alone.
The final macOS arm64 application was built on macOS 26.6.2 with Python 3.13.11.
Its version command, HTTP root/assets, source/bundled CSS/viewer equality, timed
browser exit and four-second native launch/exit smoke all passed. The complete
Chromium correction/restart/resume/export workflow also passed against the frozen
binary, and its final review screenshot was visually checked. These checks do not
certify older macOS, Intel macOS, Windows, or a clean-machine/full manual native
workflow. Record native results separately for every released target. Exact local
artifact details and remaining checks are in [RELEASE_NOTES.md](RELEASE_NOTES.md).

## Synthetic desktop smoke test

Generate local demonstration records without using private material:

```sh
uv run python scripts/create_demo.py /path/to/new/dynaval-demo
```

On Windows, use a new local path such as `C:\Temp\dynaval-demo`. The generator
creates `dataset.csv` and two source cards in `images/`, using fictional names and
generic name/age/label fields. The first record deliberately has `37` in the CSV
and `38` on its card. Choose `image` as the reference column. A drag-and-drop import
requires the demo directory as its relative-image base.

For each native bundle on a clean test machine:

1. Launch without Python/uv and import the demo. Verify icon, clean window,
   image display/switching, zoom, pan, reset and native file/directory dialogs.
2. Select fields; confirm an original, correct the deliberate age error, try an
   explicit empty replacement, and test rejection/skipping with their settings.
3. Pause with an edit draft, close and reopen. Continue with the original seed,
   saved queue, corrections and next step. Repeat after a forced process close,
   distinguishing committed work from unsaved keystrokes.
4. Finish the pass and continue unresolved fields without reshuffling. Export a
   partial and completed bundle, including optional accuracy reporting. Check the
   corrected dataset's original shape/order and every selected review-log row.
5. Exercise local paths, configured mounted-prefix mappings and a controlled
   HTTP image source. Test a missing image and offline cached-image recovery.
6. Close cleanly and confirm no orphan server/native processes remain. Move the
   app and verify that per-user sessions remain accessible and writable.

Keep native results, signing status, OS/architecture and limitations in the release
notes. The final handoff to Actions is the locked check/build commands, complete
ZIPs, their `.sha256` files and platform-specific smoke-test evidence.
