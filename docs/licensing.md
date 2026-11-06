# Licensing

## Project code: MIT

The project owner has confirmed ownership of the project and ported code/tests
and authorized the MIT license. The full text is in [LICENSE](../LICENSE), with
a project-contributor copyright notice that contains no personal contact details.
Keep that notice and license text when redistributing the code.

## Public speech fixture: CC BY 4.0

The bundled FLEURS speech fixture retains its own CC BY 4.0 terms; it is not
relicensed as MIT. Preserve [source and change attribution](../examples/README.md)
and the [CC BY 4.0 text](../LICENSES/CC-BY-4.0.txt) when redistributing it.
Audio bytes are unmodified; its metadata cache path is normalized for portability.

The package metadata uses `MIT AND CC-BY-4.0` to describe the project's code and
bundled sample together. This does not apply both licenses to every individual
file. Wheel/source archives include the relevant license texts and attribution.

## Dependencies, engines and models

Python dependencies are installed separately and retain their own licenses.
FFmpeg, optional engines and model weights are not relicensed by this project.
No model weights are bundled. See [provenance](provenance.md) and the pinned
sources below. A source release does not certify redistribution of a compiled
container image: review its actual FFmpeg configuration, linked libraries,
engine/transitive notices and selected model terms before publishing such an image.
Never include credentials, saved profiles, private media or local deployment files.

## Reference artifacts

The manifest records pinned URLs, sizes, hashes, and source/license links. Review
the upstream text when distributing an artifact rather than relying on this table.

| Component | Upstream license information |
| --- | --- |
| Included Japanese FLEURS sample | [CC BY 4.0 attribution and source](../examples/README.md) |
| whisper.cpp | [MIT at the pinned engine commit](https://github.com/ggml-org/whisper.cpp/blob/d1be6fde11ac6e0407606b4e42fe72d34add8037/LICENSE) |
| llama.cpp | [MIT at the pinned engine commit](https://github.com/ggml-org/llama.cpp/blob/620d2225cc436ab0b001e3200e590e3b286ef9ab/LICENSE) |
| Whisper small GGML conversion | [Pinned model card: MIT](https://huggingface.co/ggerganov/whisper.cpp/blob/5359861c739e955e79d9a303bcbc70fb988958b1/README.md); [original Whisper code/weights license](https://github.com/openai/whisper/blob/main/LICENSE) |
| Qwen2.5-1.5B-Instruct GGUF | [Pinned model card: Apache-2.0](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/blob/91cad51170dc346986eccefdc2dd33a9da36ead9/README.md) |
| Silero VAD v6.2.0 GGML conversion | [Pinned model card: MIT](https://huggingface.co/ggml-org/whisper-vad/blob/9ffd54a1e1ee413ddf265af9913beaf518d1639b/README.md) |

Python, OS packages, Python build tooling, and all inference-engine dependencies
retain their own licenses. The Docker recipe is a local build recipe, not an audit
of a distributable binary image. No model weights are packaged with this project.
