# Configuration examples and speech fixture

The TOML profiles use the foundation config fields. Relative model paths are
relative to each TOML file, so `../models` points to the repository-root model
directory. Set `source_language = "ko"` for Korean speech; the examples use Japanese.
Keys come from `MEDIA_TO_SUBS_STT_API_KEY` and `MEDIA_TO_SUBS_LLM_API_KEY`, separately.
See [setup](../docs/setup.md) for current integration and API availability limits.

## FLEURS Japanese fixture attribution

`fleurs-ja-validation-0.wav` and the row data in `fleurs-fixture.json` are from
**FLEURS: Few-shot Learning Evaluation of Universal Representations of Speech**,
Google / Alexis Conneau and the FLEURS authors (2022), under
[Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).
Source: [official google/fleurs dataset card](https://huggingface.co/datasets/google/fleurs),
[paper and authors](https://arxiv.org/abs/2205.12446). No endorsement is implied.

This is one extracted Japanese (`ja_jp`) validation row (row 0, ID 1519), renamed
for clarity. The audio bytes are unmodified float WAV. The JSON includes upstream
row metadata plus extraction provenance/hash fields; its `path` is normalized
from an upstream absolute cache path to the bundled relative filename
`fleurs-ja-validation-0.wav`. The original filename remains in
`audio_original_path`. Transcript, dataset revision, license and hash attribution
are unchanged. This metadata-only path normalization does not alter the audio;
no audio editing, resampling, normalization or generated speech was applied.

- Converted parquet revision: `168de341b3db6859a9bac1c50a2ef5e3b47647e0`.
- [Source parquet file](https://huggingface.co/datasets/google/fleurs/blob/168de341b3db6859a9bac1c50a2ef5e3b47647e0/ja_jp/validation/0000.parquet).
- Original audio filename: `10009621509886822059.wav`.
- Duration: 11.1 seconds; mono, 16,000 Hz, float32 little-endian PCM.
- WAV bytes: 710,458.
- WAV SHA256: `9627b8f5d46806e1a433b443d692fdfb24f26a35c1a47eba26291475fc643d7b`.
- Full parquet SHA256: `4636065a2eb7d1bb07007d3193092eb1856cf101360831c61d6dfa611e579071` (source record; the parquet is not included).

The fixture's CC BY 4.0 terms are independent of this project's unresolved
source-code license. Preserve this attribution/license link when redistributing
it and identify any later changes. The source card describes read speech; a
single short sample does not establish accuracy for conversation, films or Korean.

## Reproduce the local sample

After installing the engines and the three manifest models, run from the repository:

```sh
mkdir -p work
uv run --frozen media-to-subs examples/fleurs-ja-validation-0.wav \
  --config examples/local-local.toml --glossary examples/fleurs-glossary.json \
  --output work/fleurs.zh.srt --save-source work/fleurs.source.srt
```

Choose new output paths for another run, or use `--overwrite` deliberately.
`fleurs-glossary.json` supplies a Chinese translation for the specialist term
“ギャップイヤーコース”. It is a small authored example, not dataset metadata.
The reference small models can misrecognize words; compare the generated source
with the original text in `fleurs-fixture.json`. See [observed results](../docs/verification.md).
