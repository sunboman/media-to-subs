# Dependencies and asset provenance

This ledger records the model, engine and speech-fixture sources used by Media to
Subs. License selection and public-release checks are described in
[licensing.md](licensing.md).

## Code origin and ownership

The standalone project was created through selective adaptation of media,
subtitle, Whisper/timing, translation and validation behavior and regression
tests from the owner's SRT Translator application. The source was reviewed during the standalone extraction. The owner has confirmed ownership
of the ported code/tests and authorized MIT licensing. No application runtime
dependency is introduced. Third-party assets retain their own terms.

The Python runtime dependency is `requests`, with locked transitive dependencies
`certifi`, `charset-normalizer`, `idna`, and `urllib3`. They are separately
installed distributions, not vendored source. See the current metadata labels,
advisory checks and artifact boundaries in [licensing](licensing.md).

## Packaging and model setup

The model downloader, `scripts/fetch_models.py`, uses Python's standard library.
Its integrity and download-recovery tests live in `tests/test_model_setup.py`.

`models-manifest.json` metadata was checked against the upstream Hugging Face
repository APIs at the listed immutable revisions. Existing reference artifacts
were hashed read-only and matched the pinned SHA256/size; no model was downloaded
during that verification. Model cards provide the listed upstream license labels.
Hashes establish byte identity, not transcription or translation quality.

Optional Docker engines use full upstream commits:

- whisper.cpp `d1be6fde11ac6e0407606b4e42fe72d34add8037` (v1.9.5).
- llama.cpp `620d2225cc436ab0b001e3200e590e3b286ef9ab` (installed reference build 11669).

Record any future pin update with its source URL, revision, byte size/hash,
license/source review and a meaningful local compatibility check. Never include
private model paths or test speech in a release provenance record.

## Redistributable speech fixture

Only `examples/fleurs-ja-validation-0.wav` and `examples/fleurs-fixture.json` were
copied from the coordinator's approved extraction of official `google/fleurs`,
Japanese validation row 0, converted parquet revision
`168de341b3db6859a9bac1c50a2ef5e3b47647e0`. Audio was copied byte-for-byte.
The official dataset card identifies CC-BY-4.0; [fixture attribution](../examples/README.md)
records source, license, changes, size and hash. No full parquet or private
macOS-generated speech was copied.
