# Environment-Based Dependency Extraction

Extract ground-truth dependencies for VeriSoftBench theorems directly from Lean 4's compiled kernel environment, producing richer and more accurate dependency information than static analysis alone.

## Overview

The extraction pipeline has two phases:

**Phase 1 — Kernel extraction:** A Lean 4 script (`extract_deps.lean`) loads each theorem's module environment and outputs a per-constant dependency graph with `type_deps` and `value_deps`. A Python orchestrator (`lean_env_extractor.py`) then walks this graph using a 3-rule algorithm:

- **Rule 1 (Definitions):** Full transitive follow of type + value deps
- **Rule 2 (Local theorems, same module):** Follow all deps; cross-module theorems escalate to Rule 3
- **Rule 3 (Repo theorems, different module):** Follow defs and same-module theorems; cross-module theorems stop (statement-only inclusion)

Post-processing cleans up kernel artifacts:
- Collapses constructors and structure field projections to parent inductive types
- Filters anonymous/auto-generated constants (proof obligations, simp fragments, equation lemmas, elaboration helpers, tactic reflection artifacts)
- Filters kernel machinery (type class instances, unfolding lemmas)

**Phase 2 — Static analysis merge:** The kernel doesn't see macros, notations, or definitions that get elaborated away (structures become projections, abbreviations get inlined). Phase 2 merges in static analysis deps from `verisoftbench.jsonl` not already covered by the env extraction, using suffix-matching dedup to avoid double-counting.

## Prerequisites

A running Docker container with the VeriSoftBench Lean image:

```bash
docker run -d --name verisoftbench-test verisoftbench/lean:latest
```

## Usage

### 1. Apply extraction patches

Some repos need minor patches (resource limits, tactic timeouts) for the extraction script to load their environments successfully:

```bash
docker cp extraction/patch_repos_for_extraction.sh verisoftbench-test:/workspace/
docker cp extraction/extract_deps.lean verisoftbench-test:/workspace/
docker exec verisoftbench-test bash /workspace/patch_repos_for_extraction.sh \
    --repos-dir /workspace/lean_repos
```

### 2. Run extraction

```bash
python3 extraction/lean_env_extractor.py \
    --container verisoftbench-test \
    --verisoftbench data/verisoftbench.jsonl \
    --output data/verisoftbench_env.jsonl
```

This processes all 500 theorems across 23 repos, taking approximately 10-15 minutes.

## Files

| File | Description |
|------|-------------|
| `extract_deps.lean` | Lean 4 script that loads a module environment and outputs dependency graphs. Compatible with Lean 4.8–4.26. |
| `lean_env_extractor.py` | Python orchestrator: runs the Lean script via Docker, applies the 3-rule algorithm, cleans up kernel artifacts, merges static analysis deps, and writes output JSONL. |
| `patch_repos_for_extraction.sh` | Idempotent shell script that applies necessary patches to repos inside the container for successful extraction. |

## Output Format

The output JSONL matches the `verisoftbench.jsonl` schema. Each entry contains:

- `used_repo_defs` / `repo_lemmas` — Definitions and lemmas from other files in the same repo
- `used_local_defs` / `used_local_lemmas` — Definitions and lemmas from the same file
- `used_lib_defs` / `lib_lemmas` — Library dependencies (Mathlib, Std, etc.)

Each repo/local dependency has `name` (fully qualified) and `content` (source text from the repo index when available).

## Lean Script Details

`extract_deps.lean` is designed for broad Lean 4 version compatibility (4.8–4.26):

- Uses `getModuleIdxFor?` + `allImportedModuleNames` instead of `getModuleFor?` (added in 4.25)
- Uses custom `strContains` instead of `String.containsSubstr` (added in 4.25)
- Avoids `Std.HashMap` (API changed in 4.24)
- Uses `IO` monad in `main` (required for `lean --run` to find `main`)

The script reads JSON from stdin (array of `{"module": "...", "name": "..."}`), loads each module's environment once, and outputs a JSON array of dependency records with `stmt_deps`, `proof_deps`, and `dep_graph` (per-constant `type_deps`/`value_deps`).
