# Working on Media to Subs

Use Python 3.11+, `uv`, and standard-library `unittest`. The package must
install and run with its documented dependencies.

The experimental v0.1 scope is frozen. Preparation accepts documentation,
packaging, CI, test improvements and fixes to documented behavior; defer new
features. See [verification boundaries](docs/verification.md).

```sh
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv build
uv run --frozen python scripts/check_distribution.py --wheel dist/media_to_subs-0.1.0-py3-none-any.whl --sdist dist/media_to_subs-0.1.0.tar.gz
```

With uv 0.13.7, run `uv audit --frozen` to check locked runtime dependencies
against known advisories. This contacts the advisory service, not an inference
provider. Review findings; a clean result is not a full security audit.

The prepared GitHub Actions workflow checks macOS/Linux and Python 3.11/3.13,
tests, advisory audit, builds, archive contents and an isolated wheel install.
It has no publication job or provider credentials. Preparing the workflow does
not establish a remote CI result. The archive checker requires a
complete licensed candidate, including license metadata, text and fixture attribution;
see [licensing](docs/licensing.md).

For the standalone model-setup script only (no inference engines or package
installation required):

```sh
python3 -m unittest discover -s tests -p test_model_setup.py -v
python3 scripts/fetch_models.py --list
```

The downloader suite uses loopback HTTP servers and temporary files. It does not
download real models or contact providers. Report the Python/platform used and
separate fixture evidence, built-container evidence, real local inference and
real paid API evidence.

Keep changes small and commit after meaningful checks. Preserve unrelated work.
Tests should reproduce observable behavior: bad timestamps, missing cues, file
collisions, provider failures, cancellation, or unsafe model downloads. Do not
replace a failing behavior check with a mock that merely mirrors the code.

Record third-party dependencies and assets in [the provenance ledger](docs/provenance.md).
Use generated structural fixtures or separately approved redistributable speech.
Do not commit private audio, episode excerpts, keys, weights or deployment files.

Review [privacy](docs/privacy.md) and [release licensing](docs/licensing.md) before
changing providers or export contents. Local mode must never silently fall back
to a remote provider. A new provider needs explicit configuration and timestamp
contract tests; a successful HTTP status alone is not usable transcription.

Project code and contributions use the [MIT license](LICENSE). Retain third-party
notices and the separate CC BY 4.0 terms for the public speech fixture. Never copy
private deployment records, credentials, personal paths or unlicensed media into
contributions. Package/image publication is separate from source contributions.
