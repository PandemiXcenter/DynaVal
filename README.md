# DynaVal

A small desktop app for checking datasets against source images and correcting
mistakes.

**Version 0.1.0** · macOS (Apple Silicon) · Windows (x64)

## Features

- Import CSV, TSV, JSONL/NDJSON, and JSON files.
- View multiple source images from URLs, local files, or shared folders.
- Choose fields to review, then confirm or correct them.
- Zoom and pan through source images.
- Save progress and pick up where you left off.
- Export corrected CSVs, review logs, and optional accuracy reports.

Your original dataset stays unchanged. Review progress is saved locally.

## Download

[Releases](https://github.com/PandemiXcenter/DynaVal/releases) ·
[Report an issue](https://github.com/PandemiXcenter/DynaVal/issues)

## Build it yourself

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/PandemiXcenter/DynaVal.git
cd DynaVal
uv sync --locked --all-groups
uv run dynaval
```

To package the app, run this on macOS or Windows:

```sh
uv run --group build python scripts/build_release.py
```

Builds are saved in `dist/releases/`. [Build notes](docs/PACKAGING.md).

## License

[MIT](LICENSE). Free to use, modify, and share, including commercially.
