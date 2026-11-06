# syntax=docker/dockerfile:1
# Build from the Media to Subs repository.
FROM python:3.13-slim-bookworm AS wheel
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY LICENSES ./LICENSES
COPY examples/README.md ./examples/README.md
COPY src ./src
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.13-slim-bookworm AS api
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wheel /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels \
    && useradd --uid 10001 --create-home subs \
    && mkdir /output \
    && chown subs:subs /output
USER subs
WORKDIR /output
ENTRYPOINT ["media-to-subs"]
CMD ["--help"]

# Optional CPU-only tools. No model weights are downloaded or included.
FROM debian:bookworm-slim AS engines
ARG WHISPER_COMMIT=d1be6fde11ac6e0407606b4e42fe72d34add8037
ARG LLAMA_COMMIT=620d2225cc436ab0b001e3200e590e3b286ef9ab
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git cmake g++ make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /engines
RUN git init whisper \
    && cd whisper \
    && git fetch --depth 1 https://github.com/ggml-org/whisper.cpp.git "${WHISPER_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${WHISPER_COMMIT}" \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
       -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF -DWHISPER_BUILD_TESTS=OFF \
       -DWHISPER_BUILD_SERVER=OFF -DWHISPER_CURL=OFF \
    && cmake --build build --parallel 2 --target whisper-cli
RUN git init llama \
    && cd llama \
    && git fetch --depth 1 https://github.com/ggml-org/llama.cpp.git "${LLAMA_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${LLAMA_COMMIT}" \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
       -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF -DLLAMA_BUILD_TESTS=OFF \
       -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON -DLLAMA_OPENSSL=OFF \
    && cmake --build build --parallel 2 --target llama-server

FROM api AS local
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=engines /engines/whisper/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=engines /engines/llama/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=engines /engines/whisper/LICENSE /usr/local/share/licenses/whisper.cpp/LICENSE
COPY --from=engines /engines/llama/LICENSE /usr/local/share/licenses/llama.cpp/LICENSE
USER subs

# API-only is the default even when --target is omitted.
FROM api AS default
