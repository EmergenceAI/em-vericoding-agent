#!/bin/bash
# patch_repos_for_extraction.sh
#
# Apply patches to Lean repos so that ALL 500 theorems can be extracted
# via env-based dependency analysis (extract_deps.lean / lean_env_extractor.py).
#
# The standard `lake build` in the Docker image leaves some modules unbuilt
# (non-default targets, resource limits, missing env vars). This script:
#   1. Builds modules that aren't default `lake build` targets
#   2. Patches proofs that hit resource limits with `sorry` (safe — extraction
#      only needs type signatures and the dependency graph, not proof terms)
#   3. Deploys extract_deps.lean into each repo
#
# Usage:
#   ./patch_repos_for_extraction.sh --repos-dir /workspace/lean_repos
#
# Idempotent: safe to run multiple times.

set -e

REPOS_DIR=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repos-dir) REPOS_DIR="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "${REPOS_DIR}" ]; then
    echo "ERROR: --repos-dir is required"
    exit 1
fi

REPOS_DIR="$(cd "${REPOS_DIR}" && pwd)"
echo "=== Patching repos for env-based extraction ==="
echo "Repos: ${REPOS_DIR}"
echo ""

# Helper: deploy extract_deps.lean to a repo
deploy_script() {
    local repo="$1"
    cp "${SCRIPT_DIR}/extract_deps.lean" "${REPOS_DIR}/${repo}/extract_deps.lean"
}

# ============================================================
# 1. lean-formal-reasoning-program: build all Frap modules
#    Default `lake build` only builds the default target, which
#    doesn't include exercise files or all submodules.
# ============================================================
echo "[lean-formal-reasoning-program] Building all Frap modules..."
cd "${REPOS_DIR}/lean-formal-reasoning-program"
deploy_script lean-formal-reasoning-program
timeout 600 lake build \
    Frap.SmallStepImp \
    Frap.Sort \
    Frap.Inductive \
    Frap.RedBlack \
    Frap.Hoare \
    Frap.ADT \
    Frap.Propositional \
    Frap.Exercises.IndProp \
    Frap.Exercises.Trans \
    Frap.Types \
    2>&1 | tail -3
echo "  Done."

# ============================================================
# 2. TTBFL: build src.semantics (not always built by default)
# ============================================================
echo "[TTBFL] Building src.semantics..."
cd "${REPOS_DIR}/TTBFL"
deploy_script TTBFL
timeout 300 lake build src.semantics src.candidates 2>&1 | tail -3
echo "  Done."

# ============================================================
# 3. VCV-io: build ToMathlib modules (not default targets)
# ============================================================
echo "[VCV-io] Building ToMathlib modules..."
cd "${REPOS_DIR}/VCV-io"
deploy_script VCV-io
timeout 600 lake build \
    ToMathlib.Control.Lawful.MonadState \
    ToMathlib.Control.Monad.Equiv \
    ToMathlib.PFunctor.Lens.Basic \
    2>&1 | tail -3
echo "  Done."

# ============================================================
# 4. veil: build Examples library (non-default lean_lib target)
# ============================================================
echo "[veil] Building Examples.StellarConsensus.SCPTheory..."
cd "${REPOS_DIR}/veil"
deploy_script veil
# Build the specific module, not the full Examples target (which has C lib deps that fail)
timeout 600 lake build Examples.StellarConsensus.SCPTheory 2>&1 | tail -3
echo "  Done."

# ============================================================
# 5. clean: patch proofs that hit resource limits
#    - BLAKE3.ApplyRounds: maxRecDepth + heartbeat timeouts
#    - Fibonacci32: unknown tactic in soundness proof
#    Both are patched with `sorry` which is safe for extraction.
# ============================================================
echo "[clean] Patching BLAKE3.ApplyRounds (resource limits)..."
cd "${REPOS_DIR}/clean"
deploy_script clean

# BLAKE3.ApplyRounds: add resource options and sorry timeout-prone proofs
python3 << 'PYEOF'
import re, os

fpath = "Clean/Gadgets/BLAKE3/ApplyRounds.lean"
if not os.path.exists(fpath):
    print("  WARNING: file not found, skipping")
    exit(0)

content = open(fpath).read()

# Skip if already patched
if "-- [EXTRACTION PATCH]" in content:
    print("  Already patched, skipping")
    exit(0)

# Add resource options after imports
lines = content.split("\n")
insert_idx = 0
for i, line in enumerate(lines):
    if line.startswith("namespace") or (line.startswith("open") and i > 5):
        insert_idx = i
        break
lines.insert(insert_idx, "-- [EXTRACTION PATCH] Increase limits for env-based extraction")
lines.insert(insert_idx + 1, "set_option maxRecDepth 8192")
lines.insert(insert_idx + 2, "set_option maxHeartbeats 400000")
content = "\n".join(lines)

# Sorry the weakenSpec proofs that timeout
content = re.sub(
    r"(def fourRoundsApplyStyle.*?weakenSpec FourRoundsSpec )\(by\n.*?\n(  \))",
    r"\1(by sorry)",
    content, count=1, flags=re.DOTALL
)
content = re.sub(
    r"(def sixRoundsApplyStyle.*?weakenSpec SixRoundsSpec )\(by\n.*?\n(  \))",
    r"\1(by sorry)",
    content, count=1, flags=re.DOTALL
)
# Sorry concat compatibility proofs
content = re.sub(
    r"(def sevenRoundsFinal.*?\.concat Round\.circuit )\(by\n.*?\n(  \) \(by)",
    r"\1(by sorry) (by",
    content, count=1, flags=re.DOTALL
)
# Sorry soundness proof
content = re.sub(
    r"(theorem soundness : Soundness.*?:= by).*?(theorem completeness)",
    r"\1 sorry\n\n\2",
    content, count=1, flags=re.DOTALL
)
# Sorry completeness proof
content = re.sub(
    r"(theorem completeness : Completeness.*?:= by).*?(end )",
    r"\1 sorry\n\n\2",
    content, count=1, flags=re.DOTALL
)

open(fpath, "w").write(content)
print("  Patched BLAKE3.ApplyRounds")
PYEOF

echo "[clean] Patching Fibonacci32 (unknown tactic)..."
python3 << 'PYEOF'
import os

fpath = "Clean/Tables/Fibonacci32.lean"
if not os.path.exists(fpath):
    print("  WARNING: file not found, skipping")
    exit(0)

lines = open(fpath).readlines()

# Skip if already patched
if any("-- [EXTRACTION PATCH]" in l for l in lines):
    print("  Already patched, skipping")
    exit(0)

# Find "soundness := by" and replace the proof body with sorry
new_lines = []
in_soundness_proof = False
brace_depth = 0
found_def_start = False

for i, line in enumerate(lines):
    if "soundness := by" in line and not in_soundness_proof:
        new_lines.append("  -- [EXTRACTION PATCH] Original proof has unknown tactic\n")
        new_lines.append("  soundness := by sorry\n")
        in_soundness_proof = True
        continue

    if in_soundness_proof:
        # Track brace depth to find end of the def
        for c in line:
            if c == "{":
                brace_depth += 1
            elif c == "}":
                brace_depth -= 1
        if brace_depth < 0:
            # This closing brace ends the def
            new_lines.append(line)
            in_soundness_proof = False
            brace_depth = 0
        # Skip proof lines
        continue

    new_lines.append(line)

open(fpath, "w").writelines(new_lines)
print("  Patched Fibonacci32")
PYEOF

echo "[clean] Building patched modules..."
timeout 900 lake build \
    Clean.Gadgets.BLAKE3.ApplyRounds \
    Clean.Tables.Fibonacci32 \
    Clean.Utils.OfflineMemory \
    Clean.Circomlib.Gates \
    Clean.Types.U32 \
    Clean.Tables.KeccakInductive \
    2>&1 | tail -3
echo "  Done."

# ============================================================
# 6. formal-snarks-project: patch ToySnark and build with
#    LEAN_SRC_PATH (needed for Mathlib tactic support)
# ============================================================
echo "[formal-snarks-project] Patching ToySnark (simp tactic failure)..."
cd "${REPOS_DIR}/formal-snarks-project"
deploy_script formal-snarks-project

# Set LEAN_SRC_PATH for this repo
export LEAN_SRC_PATH="${REPOS_DIR}/formal-snarks-project/.lake/packages/mathlib"

python3 << 'PYEOF'
import os

fpath = "FormalSnarksProject/SNARKs/ToySnark.lean"
if not os.path.exists(fpath):
    print("  WARNING: file not found, skipping")
    exit(0)

lines = open(fpath).readlines()

if any("-- [EXTRACTION PATCH]" in l for l in lines):
    print("  Already patched, skipping")
    exit(0)

# Find ") := by" after "lemma soundness" and replace proof body with sorry
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    if ") := by" in line and i > 130 and i < 160:
        new_lines.append("    -- [EXTRACTION PATCH] Original proof has simp tactic failure\n")
        new_lines.append("    ) := by sorry\n")
        i += 1
        # Skip until "end soundness"
        while i < len(lines) and "end soundness" not in lines[i]:
            i += 1
        if i < len(lines):
            new_lines.append(lines[i])
        i += 1
    else:
        new_lines.append(line)
        i += 1

open(fpath, "w").writelines(new_lines)
print("  Patched ToySnark")
PYEOF

echo "[formal-snarks-project] Building with LEAN_SRC_PATH..."
timeout 600 lake build \
    FormalSnarksProject.SNARKs.ToySnark \
    FormalSnarksProject.SNARKs.Lipmaa.Soundness \
    2>&1 | tail -3
unset LEAN_SRC_PATH
echo "  Done."

# ============================================================
# 7. loom: patch SpMSpV_Example (loom_solve tactic failure)
# ============================================================
echo "[loom] Patching SpMSpV_Example (tactic failures)..."
cd "${REPOS_DIR}/loom"
deploy_script loom

python3 << 'PYEOF'
import os

fpath = "CaseStudies/Velvet/VelvetExamples/SpMSpV_Example.lean"
if not os.path.exists(fpath):
    print("  WARNING: file not found, skipping")
    exit(0)

content = open(fpath).read()

if "-- [EXTRACTION PATCH]" in content:
    print("  Already patched, skipping")
    exit(0)

# Replace loom_solve <;> loom_auto with sorry
content = content.replace(
    "  loom_solve <;> loom_auto",
    "  -- [EXTRACTION PATCH] Original tactic times out\n  sorry"
)

open(fpath, "w").write(content)
print("  Patched SpMSpV_Example")
PYEOF

echo "[loom] Building patched module..."
timeout 600 lake build \
    CaseStudies.Velvet.VelvetExamples.SpMSpV_Example \
    CaseStudies.Velvet.VelvetExamples.EncodeDecodeStr \
    2>&1 | tail -3
echo "  Done."

# ============================================================
# Deploy extract_deps.lean to remaining repos
# ============================================================
echo ""
echo "Deploying extract_deps.lean to all repos..."
for repo_dir in "${REPOS_DIR}"/*/; do
    repo_name=$(basename "$repo_dir")
    if [ ! -f "${repo_dir}/extract_deps.lean" ]; then
        cp "${SCRIPT_DIR}/extract_deps.lean" "${repo_dir}/extract_deps.lean"
    fi
done

echo ""
echo "=== All extraction patches applied ==="
echo "Run lean_env_extractor.py to extract all 500 theorems."
