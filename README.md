# Tiny Vericoding Agent

## Overview

The repo is divided into 3 parts:

- `tiny-agent`: A minimal agent that iteratively sends messages and executes tool calls.
- `lean-tools`: Some minimal Lean tools that the benchmarking agent uses.
- `benchmarks`: Benchmarking scripts for VERINA and VeriSoftBench.

Both the `tiny-agent` and `lean-tools` can be installed separately with their own respective `pyproject.toml` for other tasks. `benchmarks` is a wrapper script around the provided functionality of `tiny-agent` or `lean-tools`.

## Setup

**Note**: This instruction is applicable to Linux systems and may not work properly on macOS or Windows.

### Clone the Repository

```sh
git clone --recurse-submodules https://github.com/EmergenceAI/em-vericoding-agent.git
```

Note that the submodules, i.e., VERINA and VeriSoftBench, must be cloned for the remaining setup to work.

### Python Environment

The project uses [uv](https://docs.astral.sh/uv/) to manage the Python environment. To use the locked dependencies, use:

```sh
uv sync --all-groups
```

Activate the environment by:

```sh
source .venv/bin/activate
```



### Lean Environment

The project can use both the local Lean environment and the containerized environment. For now, to set up the containerized environment, you need to set up the local environment first. First, set up [Lean 4](https://lean-lang.org/).

```sh
sh third-party/prepare.sh
```

**Note**: The Python environment needs to be activated before running the above script. The script can be invoked again in case any failure happens. The script will build the required dependencies for both VERINA and VeriSoftBench locally. This will take about an hour or more depending on the network speed.

Most of the setup script reuses the provided scripts by VeriSoftBench and some additional custom patching to accommodate the REPL dependency and ensure compilation. The script will also patch all ground truth proofs in VeriSoftBench (Aristotle) and replace them with `sorry` to ensure no leakage. Therefore, it is crucial to ensure the local setup succeeds first before building the image, since otherwise the proof may be leaked during solving.

To build the container, first install [Podman](https://podman.io/). You may need to restart the terminal after installing Podman and reactivate the Python environment. Then run:

```sh
podman build -t lean-workspace .
podman pull busybox
```

This will take about half an hour with a good network connection and create an image named `lean-workspace` using the provided `Containerfile`. The image is about 66 GB.

To avoid some warnings that might break the runs

```
mkdir -p ~/.config/containers
cat >> ~/.config/containers/containers.conf <<'EOF'
[engine]
cgroup_manager = "cgroupfs"
EOF
```

## Usage

**Note**: Running the REPL can consume substantial memory, especially for VeriSoftBench. Most of this memory is inactive and can be swapped to disk. On Linux, enable a large disk swap space, e.g., at least 256 GB, before running. It can be difficult to run multiple instances with 32 GB of RAM or less. The instructions for enabling the swap space can be different on different systems, but it is a pretty standard feature, so you can ask a coding agent to help enable this. You should see something like this after enabling swap and running `swapon --show`:

```sh
NAME       TYPE      SIZE
/swapfile  file      256G
```

To run the task without the container, use:

```sh
python -m benchmarks.verina --task-name verina_basic_1
python -m benchmarks.verisoftbench --task-id 0
```

**Note**: The REPL has read-write-execute capability, so it can affect your system in the worst case. If you decide to run the task without a container, use it at your own risk. Always monitor the task run.

To use the container image, add `--image-name lean-workspace`:

```sh
python -m benchmarks.verina --task-name verina_basic_1 --image-name lean-workspace
python -m benchmarks.verisoftbench --task-id 0 --image-name lean-workspace
```

Configuration can be specified either by command-line arguments, e.g., `--model-name openai/gpt-5.4`, or by creating a `config.yaml` at the repository root. To see all options, use the `--help` flag. An example configuration is given:

- `gpt-5.4`:

```yaml
model-name: openai/gpt-5.4
reasoning:
  effort: xhigh
  summary: auto
timeout: 600
run-name: gpt-5.4
```

- `claude-opus-4.6`:

```yaml
model-name: anthropic/claude-opus-4-6
reasoning:
  effort: max
timeout: 600
run-name: opus-4.6
```

You may want to increase the `timeout` if the `shell` and `repl` repeatedly time out for multiple consecutive calls on some systems. In our case, `timeout: 300` is sufficient, but this could be system-specific.

To add the API key, you can use either an environment variable (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`) or put it directly in the above `config.yaml` as `api-key: KEY`.

To count the runs after finishing everything, use:

```sh
python -m benchmarks.verina count --run-name gpt-5.4
python -m benchmarks.verisoftbench count --run-name gpt-5.4
```

Note: `passed` means the runs compiled successfully. `failed` means the agent failed to solve the task. `crashed` means there are unexpected errors during the solving process, e.g., network problems.