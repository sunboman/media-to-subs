# Changelog

## Unreleased — experimental v0.1.0

Initial experimental source release. Project code is MIT licensed; the public
speech fixture retains CC BY 4.0 attribution and terms.

### Added

- Convert one local media file into validated Chinese SRT, using existing
  subtitles or independently selected local/API speech transcription and translation.
- Translate through installed Codex, Claude Code, OpenCode or Antigravity CLIs,
  with capability checks, bounded execution and response validation.
- Install once, save settings with `setup`, and inspect dependencies with `doctor`.
- Export timed source SRT and apply an optional glossary.
- Protect media, sidecars and existing outputs; validate complete translations
  before publication and clean up owned resources on cancellation.
- Prepare macOS/Linux CI and distribution-content checks for release preparation.

### Changed

- Normalize public fixture metadata and retain only the verification details
  needed to understand compatibility and quality limits.

### Limitations

- Chinese output only; one local file per invocation.
- Recorded real-speech success is limited to short Japanese samples. Small local
  models have known semantic errors; structurally valid SRT is not a quality guarantee.
- Claude, OpenCode and hosted STT/LLM API paths lack successful live acceptance.
  Those paths remain experimental.
- Antigravity compatibility is restricted to the inspected CLI 1.4.12.
- Windows, Linux/amd64, broader GPU behavior, Korean real speech and long-form
  quality remain unverified. Recent installer/agent changes lack fresh Linux proof.
