#!/bin/sh

parent=$(dirname "$file")
cd $parent

# build for outer dir first, this is for verina
lake update && lake exe cache get && lake build --allow-empty
echo '{"cmd": ""}' | lake exe repl

# using the same setup as VeriSoftBench Aristotle subset
bash third-party/VeriSoftBench/scripts/setup_repos.sh \
    --clone --config-dir third-party/VeriSoftBench/build_config \
    --output-dir AgentWorkspace/VeriSoftBenchRepos \
    --repos ArkLib,clean,iris-lean,juvix-lean,LeanExprEvaluator,lean-formal-reasoning-program,LeroyCompilerVerificationCourse,loom,pcf-lean,VCV-io,veil \
    --skip-if-built

# reset changes before applying patches
for d in AgentWorkspace/VeriSoftBenchRepos/*; do
   git -C "$d" restore .;
   git -C "$d" clean -fd; 
done

# prior tag fails to compile
git -C AgentWorkspace/VeriSoftBenchRepos/veil checkout v4.24.0 

# setup_repos.sh will surely fail, so run the last command here
bash third-party/VeriSoftBench/scripts/docker_repo_patches.sh \
    --repos-dir AgentWorkspace/VeriSoftBenchRepos \
    --config-dir third-party/VeriSoftBench/build_config

# the ' in the name cause problems, so we rename it
[ -e "AgentWorkspace/VeriSoftBenchRepos/loom/Loom/MonadAlgebras/NonDetT'" ] && mv "AgentWorkspace/VeriSoftBenchRepos/loom/Loom/MonadAlgebras/NonDetT'" AgentWorkspace/VeriSoftBenchRepos/loom/Loom/MonadAlgebras/NonDetTPrime
perl -pi -e "s/NonDetT'/NonDetTPrime/g" AgentWorkspace/VeriSoftBenchRepos/loom/Loom/MonadAlgebras/NonDetTPrime/Extract.lean

# our patching
python third-party/patch_verisoftbench.py
