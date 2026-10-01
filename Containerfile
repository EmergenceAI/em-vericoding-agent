FROM debian:bookworm-slim

RUN apt update && apt install -y curl git unzip
RUN curl https://elan.lean-lang.org/elan-init.sh -sSf | sh -s -- -y

ENV PATH="/root/.local/bin:/root/.elan/bin:${PATH}"

WORKDIR /workspace

COPY lean-toolchain /workspace/
COPY lakefile.toml /workspace/
COPY lake-manifest.json /workspace/

COPY AgentWorkspace/VeriSoftBenchRepos /workspace/AgentWorkspace/VeriSoftBenchRepos

RUN lake update && lake build --allow-empty && echo '{"cmd": ""}' | lake exe repl
RUN for d in /workspace/AgentWorkspace/VeriSoftBenchRepos/*/; do (cd "$d" && echo "Building: $(pwd)" && lake update && lake build && echo '{"cmd": ""}' | lake exe repl); done
