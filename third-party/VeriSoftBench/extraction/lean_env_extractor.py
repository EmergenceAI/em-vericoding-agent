#!/usr/bin/env python3
"""
Environment-based context extraction for VeriSoftBench.

Uses Lean 4's compiled environment (via extract_deps.lean) to get ground-truth
dependencies directly from the kernel, then merges with static analysis output
to produce comprehensive dependency records.

## Architecture

The extraction is a two-phase pipeline:

**Phase 1 — Env extraction (ground truth from kernel):**
  The Lean script (extract_deps.lean) loads each theorem's module environment
  and outputs a per-constant dependency graph with `type_deps` and `value_deps`.
  Python then walks this graph using the benchmark spec's 3-rule algorithm:

    - Rule 1 (Definitions): Full transitive follow of type+value deps.
    - Rule 2 (Local theorems, same module): Follow all; defs→R1, local thms→R2, repo thms→R3.
    - Rule 3 (Repo theorems, different module): Follow defs→R1, same-module thms→R3,
      cross-module thms→STOP (record but don't recurse).

  Post-processing cleans up the raw kernel output:
    - Collapses constructors and structure field projections to parent inductives
    - Filters anonymous/auto-generated constants (proof obligations, simp fragments,
      equation lemmas, match expressions, elaboration helpers, private defs)
    - Filters kernel machinery (type class instances, .eq_def unfolding lemmas)

**Phase 2 — Static analysis merge:**
  The kernel doesn't see everything: macros/notations, definitions that get
  elaborated away (structures→projections, abbreviations→inlined), etc.
  Phase 2 merges in static analysis deps not already covered by env, using
  suffix-matching dedup to avoid double-counting qualified name variants.

## Usage

    python3 extraction/lean_env_extractor.py \\
        --container verisoftbench-test \\
        --verisoftbench data/verisoftbench.jsonl \\
        --output data/verisoftbench_env.jsonl

## Prerequisites

    # Start container and apply patches
    docker run -d --name verisoftbench-test verisoftbench/lean:latest
    docker cp extraction/patch_repos_for_extraction.sh verisoftbench-test:/workspace/
    docker cp extraction/extract_deps.lean verisoftbench-test:/workspace/
    docker exec verisoftbench-test bash /workspace/patch_repos_for_extraction.sh \\
        --repos-dir /workspace/lean_repos
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

DOCKER_LEAN_REPOS_DIR = "/workspace/lean_repos"
DOCKER_EXTRACTION_DIR = "/workspace/extraction"

LIBRARY_PREFIXES = frozenset([
    "Init", "Std", "Lean", "Mathlib", "Aesop", "Qq", "ProofWidgets",
    "Batteries", "ImportGraph", "LeanSearchClient", "Plausible",
    "Cli", "Lake",
])


def is_library_module(module: str) -> bool:
    """Check if a module name belongs to a known library."""
    root = module.split(".")[0]
    return root in LIBRARY_PREFIXES


class LeanEnvExtractor:
    """Orchestrates Lean environment-based dependency extraction via Docker."""

    # Known theorem name corrections: benchmark name -> actual Lean constant name.
    # These arise when the benchmark's name extraction disagrees with the kernel.
    THM_NAME_CORRECTIONS: dict[str, str] = {
        "U32.ByteVector.bitwise_componentwise": "U32.bitwise_componentwise",
    }

    def __init__(
        self,
        docker_container: str,
        verisoftbench_path: Path,
        repo_index_dir: str,
        output_path: Path,
    ):
        self.container = docker_container
        self.verisoftbench_path = verisoftbench_path
        self.repo_index_dir = repo_index_dir  # path inside container
        self.output_path = output_path

        # Load verisoftbench entries
        self.entries: list[dict] = []
        with open(verisoftbench_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    self.entries.append(json.loads(line))
        log.info("Loaded %d entries from %s", len(self.entries), verisoftbench_path)

        # Cache for repo index data (loaded lazily)
        self._repo_index_cache: dict[str, dict] = {}

    # Repos that need LEAN_SRC_PATH set for Mathlib tactic support
    LEAN_SRC_PATH_REPOS: dict[str, str] = {
        "formal-snarks-project": ".lake/packages/mathlib",
    }

    def _docker_exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        input_data: str | None = None,
        timeout: int = 300,
        env_vars: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a command inside the Docker container."""
        docker_cmd = ["docker", "exec"]
        if input_data is not None:
            docker_cmd.append("-i")
        if env_vars:
            for k, v in env_vars.items():
                docker_cmd.extend(["-e", f"{k}={v}"])
        if cwd:
            docker_cmd.extend(["-w", cwd])
        docker_cmd.append(self.container)
        docker_cmd.extend(cmd)

        return subprocess.run(
            docker_cmd,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def deploy_script(self, repo_name: str) -> None:
        """Copy extract_deps.lean into the repo directory inside the container."""
        script_path = Path(__file__).parent / "extract_deps.lean"
        dest = f"{self.container}:{DOCKER_LEAN_REPOS_DIR}/{repo_name}/extract_deps.lean"
        result = subprocess.run(
            ["docker", "cp", str(script_path), dest],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("Failed to deploy script to %s: %s", repo_name, result.stderr)

    def _load_repo_index(self, repo_name: str) -> dict:
        """Load and cache repo index JSON from the container."""
        if repo_name in self._repo_index_cache:
            return self._repo_index_cache[repo_name]

        idx_path = f"{self.repo_index_dir}/{repo_name}.json"
        result = self._docker_exec(["cat", idx_path], timeout=30)
        if result.returncode != 0:
            log.warning("Could not load repo index for %s: %s", repo_name, result.stderr)
            self._repo_index_cache[repo_name] = {}
            return {}

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning("Invalid JSON in repo index for %s", repo_name)
            data = {}

        self._repo_index_cache[repo_name] = data
        return data

    def _lookup_source_text(self, repo_name: str, const_name: str) -> str | None:
        """Look up declaration source text from the repo index."""
        index = self._load_repo_index(repo_name)
        if not index:
            return None

        # Search in both definitions_by_file and theorems_by_file
        for section in ("definitions_by_file", "theorems_by_file"):
            file_map = index.get(section, {})
            for _file, decls in file_map.items():
                for decl in decls:
                    if const_name in decl.get("aliases", []) or decl.get("name") == const_name:
                        return decl.get("text")
        return None

    def _rel_path_to_module(self, rel_path: str, repo_name: str = "") -> str:
        """Convert 'Foo/Bar/Baz.lean' to 'Foo.Bar.Baz', handling repo quirks."""
        if rel_path.endswith(".lean"):
            rel_path = rel_path[:-5]
        module = rel_path.replace("/", ".")

        # iris-lean: source files are under src/ but src is not a module prefix
        if repo_name == "iris-lean" and module.startswith("src."):
            module = module[4:]

        # lean-mlir: duplicate directory structure (Blase/Blase/... → Blase....)
        if repo_name == "lean-mlir":
            if module.startswith("Blase.Blase."):
                module = module.replace("Blase.Blase.", "Blase.", 1)
            elif module.startswith("LeanMLIR.LeanMLIR."):
                module = module.replace("LeanMLIR.LeanMLIR.", "LeanMLIR.", 1)

        return module

    def _extract_module(
        self,
        repo_name: str,
        module: str,
        thm_names: list[str],
        timeout: int = 300,
    ) -> list[dict]:
        """Extract dependencies for theorems in a single module."""
        # Apply known name corrections
        corrected = {n: self.THM_NAME_CORRECTIONS.get(n, n) for n in thm_names}
        lean_input = [{"module": module, "name": corrected[n]} for n in thm_names]
        input_json = json.dumps(lean_input)
        repo_dir = f"{DOCKER_LEAN_REPOS_DIR}/{repo_name}"

        # Build env vars for repos that need them
        env_vars = None
        if repo_name in self.LEAN_SRC_PATH_REPOS:
            src_path = f"{repo_dir}/{self.LEAN_SRC_PATH_REPOS[repo_name]}"
            env_vars = {"LEAN_SRC_PATH": src_path}

        try:
            result = self._docker_exec(
                ["lake", "env", "lean", "--run", "extract_deps.lean"],
                cwd=repo_dir,
                input_data=input_json,
                timeout=timeout,
                env_vars=env_vars,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "  %s/%s: timed out after %ds (%d theorems)",
                repo_name, module, timeout, len(thm_names),
            )
            return [
                {"name": n, "module": module, "error": f"timeout after {timeout}s"}
                for n in thm_names
            ]

        if result.returncode != 0:
            log.warning(
                "  %s/%s: lean failed (exit %d): %s",
                repo_name, module, result.returncode, result.stderr[:500],
            )
            return [
                {"name": n, "module": module, "error": f"lean exit {result.returncode}"}
                for n in thm_names
            ]

        try:
            stdout = result.stdout
            json_start = stdout.find("[")
            if json_start == -1:
                raise json.JSONDecodeError("No JSON array found", stdout, 0)
            results = json.loads(stdout[json_start:])
            # Map corrected names back to original names
            reverse_map = {v: k for k, v in corrected.items() if k != v}
            for r in results:
                orig_name = reverse_map.get(r.get("name", ""))
                if orig_name:
                    r["name"] = orig_name
            return results
        except json.JSONDecodeError:
            log.warning(
                "  %s/%s: invalid JSON: %s",
                repo_name, module, result.stdout[:500],
            )
            return [
                {"name": n, "module": module, "error": "invalid JSON output"}
                for n in thm_names
            ]

    def extract_repo(self, repo_name: str, theorems: list[dict]) -> list[dict]:
        """
        Extract dependencies for all theorems in a repo, one module at a time.

        Returns list of raw extraction results from the Lean script.
        """
        self.deploy_script(repo_name)

        # Group theorems by module
        module_groups: dict[str, list[str]] = defaultdict(list)
        for thm in theorems:
            module = self._rel_path_to_module(thm["rel_path"], repo_name)
            module_groups[module].append(thm["thm_name"])

        log.info(
            "Extracting %d theorems from %s (%d modules)",
            len(theorems), repo_name, len(module_groups),
        )

        all_results: list[dict] = []
        for i, (module, thm_names) in enumerate(sorted(module_groups.items()), 1):
            log.info(
                "  [%d/%d] %s (%d theorems)",
                i, len(module_groups), module, len(thm_names),
            )
            results = self._extract_module(repo_name, module, thm_names)
            all_results.extend(results)

        return all_results

    @staticmethod
    def _apply_three_rule_algorithm(
        target_module: str,
        stmt_deps: list[str],
        proof_deps: list[str],
        dep_graph: dict[str, dict],
    ) -> set[str]:
        """
        Walk the dep_graph applying the benchmark spec's 3-rule algorithm.

        Rules:
          Rule 1 (Definitions): Include type+value. Recurse into defs via Rule 1,
                  theorems via Rule 2 (local) or Rule 3 (repo).
          Rule 2 (Local theorems, same module): Include type+value.
                  Defs → Rule 1. Local theorems → Rule 2. Repo theorems → Rule 3.
          Rule 3 (Repo theorems, different module): Include type+value.
                  Defs → Rule 1. Same-module theorems → Rule 3.
                  Different-module theorems → STOP (record but don't recurse).

        Library constants are always recorded but never recursed into.
        Returns the set of constant names that should be included.
        """

        def _info(name: str) -> dict | None:
            return dep_graph.get(name)

        def _is_lib(info: dict) -> bool:
            return not info.get("is_repo", False)

        def _is_theorem(info: dict) -> bool:
            return info.get("kind") == "theorem"

        def _module(info: dict) -> str:
            return info.get("module", "")

        included: set[str] = set()
        # Stack items: (constant_name, rule) where rule is 1, 2, or 3
        stack: list[tuple[str, int]] = []

        # Seed: direct deps of the target theorem
        # Statement deps and proof deps both get followed; the target theorem
        # itself is treated like a local theorem (Rule 2) for determining
        # how to recurse its deps.
        all_direct = list(dict.fromkeys(stmt_deps + proof_deps))  # dedup, preserve order
        for dep in all_direct:
            info = _info(dep)
            if info is None:
                continue
            if _is_lib(info):
                included.add(dep)
                continue
            # Determine initial rule for this dep based on what it is
            if not _is_theorem(info):
                stack.append((dep, 1))  # definition → Rule 1
            elif _module(info) == target_module:
                stack.append((dep, 2))  # local theorem → Rule 2
            else:
                stack.append((dep, 3))  # repo theorem → Rule 3

        visited: set[tuple[str, int]] = set()

        while stack:
            name, rule = stack.pop()
            if (name, rule) in visited:
                continue
            visited.add((name, rule))
            included.add(name)

            info = _info(name)
            if info is None or _is_lib(info):
                continue

            # Gather deps to follow: type_deps + value_deps
            children = info.get("type_deps", []) + info.get("value_deps", [])

            for child in children:
                child_info = _info(child)
                if child_info is None:
                    continue
                if _is_lib(child_info):
                    included.add(child)
                    continue

                child_is_thm = _is_theorem(child_info)
                child_module = _module(child_info)

                if not child_is_thm:
                    # Definitions always get Rule 1 regardless of parent rule
                    if (child, 1) not in visited:
                        stack.append((child, 1))
                else:
                    # Theorem — rule depends on parent rule and location
                    if rule == 1:
                        # Rule 1 (def) encountering a theorem:
                        # local → Rule 2, repo → Rule 3
                        if child_module == target_module:
                            if (child, 2) not in visited:
                                stack.append((child, 2))
                        else:
                            if (child, 3) not in visited:
                                stack.append((child, 3))
                    elif rule == 2:
                        # Rule 2 (local theorem) encountering a theorem:
                        # local → Rule 2, repo → Rule 3
                        if child_module == target_module:
                            if (child, 2) not in visited:
                                stack.append((child, 2))
                        else:
                            if (child, 3) not in visited:
                                stack.append((child, 3))
                    elif rule == 3:
                        # Rule 3 (repo theorem) encountering a theorem:
                        # same module as THIS constant → Rule 3
                        # different module → STOP (include but don't recurse)
                        this_module = _module(info)
                        if child_module == this_module:
                            if (child, 3) not in visited:
                                stack.append((child, 3))
                        else:
                            # Statement-only: include but don't recurse
                            included.add(child)

        return included

    # Prefixes that identify macro/notation/syntax declarations in static analysis output.
    _MACRO_PREFIXES = (
        "scoped ", "macro", "notation", "syntax",
        "infixl", "infixr", "infix:", "prefix", "postfix",
    )

    @classmethod
    def _is_macro_or_notation(cls, name: str) -> bool:
        """Check if a name is a macro, notation, or syntax declaration."""
        return any(name.startswith(p) for p in cls._MACRO_PREFIXES)

    @staticmethod
    def _is_kernel_machinery(name: str) -> bool:
        """Check if a constant is kernel machinery a proof author wouldn't reference.

        This includes type class instances (instFoo, Foo.instBar) and
        auto-generated unfolding lemmas (.eq_def).
        """
        parts = name.split(".")
        last = parts[-1] if parts else ""
        # Type class instances: instFoo or Foo.instBar
        if last.startswith("inst") and len(last) > 4 and last[4].isupper():
            return True
        # .eq_def unfolding lemmas
        if last == "eq_def":
            return True
        return False

    @staticmethod
    def _is_anonymous_dep(name: str) -> bool:
        """Check if a constant name is an anonymous/auto-generated proof obligation."""
        # Anonymous proof terms: Foo.bar._proof_1 or Foo.bar.proof_1
        if re.search(r"\.(_)?proof_\d", name):
            return True
        # Simp lemma fragments: Foo.bar._simp_1_2
        if "._simp_" in name:
            return True
        # Equation lemmas: Foo.bar.eq_1 (but not e.g. Foo.eq_of_bar)
        parts = name.rsplit(".", 1)
        if len(parts) == 2:
            last = parts[1]
            if last.startswith("eq_") and last[3:].isdigit():
                return True
            # match_N: elaborated match expressions
            if re.match(r"match_\d+$", last):
                return True
        # Auto-params: Foo._autoParam, Foo._auto.N
        if "._autoParam" in name or "._auto." in name:
            return True
        # Hygiene-renamed: Foo._hyg.123
        if "._hyg." in name:
            return True
        # Internal elaboration helpers
        if "._unary" in name or "._binary" in name or "._mutual" in name:
            return True
        # Auxiliary definitions: Foo.bar.aux
        if name.endswith(".aux"):
            return True
        # Simp congruence lemmas: Foo.congr_simp
        if ".congr_simp" in name:
            return True
        # Tactic reflection artifacts: ._cert_def_, ._expr_def_, ._reflection_def_
        if "._cert_def_" in name or "._expr_def_" in name or "._reflection_def_" in name:
            return True
        # Private internal definitions: _private.Foo.Bar
        if name.startswith("_private."):
            return True
        return False

    def format_entry(self, original: dict, raw_deps: dict | None) -> dict:
        """Produce an entry matching the verisoftbench.jsonl schema.

        Phase 1: Apply 3-rule algorithm to env dep_graph, collapse structure
        fields, filter anonymous/auto-generated constants, classify into
        repo/local/library buckets, and look up source text from repo index.

        Phase 2: Merge in static analysis deps (macros, elaborated-away defs)
        not already covered by env, using suffix-matching dedup.
        """
        # Start with fields copied from original
        entry: dict[str, Any] = {
            "id": original["id"],
            "thm_name": original["thm_name"],
            "thm_stmt": original["thm_stmt"],
            "lean_root": original["lean_root"],
            "rel_path": original["rel_path"],
            "imports": original["imports"],
        }

        thm_module = self._rel_path_to_module(original["rel_path"], original.get("lean_root", ""))
        repo_name = original["lean_root"]

        # Initialize dependency lists
        used_lib_defs: list[dict] = []
        used_repo_defs: list[dict] = []
        lib_lemmas: list[dict] = []
        repo_lemmas: list[dict] = []
        used_local_defs: list[dict] = []
        used_local_lemmas: list[dict] = []

        # --- Phase 1: Env extraction (ground truth from kernel) ---
        # These are fully-qualified names with accurate kind/module info.
        env_names: set[str] = set()  # all env dep names (for dedup with static)

        if raw_deps and "error" not in raw_deps:
            dep_graph = raw_deps.get("dep_graph", {})
            stmt_deps = raw_deps.get("stmt_deps", [])
            proof_deps = raw_deps.get("proof_deps", [])

            # Apply the 3-rule algorithm to determine which deps to include
            included = self._apply_three_rule_algorithm(
                thm_module, stmt_deps, proof_deps, dep_graph
            )

            # Post-process: collapse constructors → parent inductives,
            # and filter out anonymous proof obligations / auto-generated fragments.
            cleaned: set[str] = set()
            for dep_name in included:
                dep_info = dep_graph.get(dep_name, {})
                kind = dep_info.get("kind", "def")

                # Collapse constructors and structure fields to their
                # parent inductive type.  In the kernel, structure fields
                # appear as def (projections) or theorem (proof fields),
                # e.g. DCast.dcast → DCast, DCast.dcast_id → DCast.
                if "." in dep_name:
                    parent = dep_name.rsplit(".", 1)[0]
                    if kind == "constructor" or (
                        parent in dep_graph
                        and dep_graph[parent].get("kind") == "inductive"
                    ):
                        cleaned.add(parent)
                        continue

                # Filter anonymous proof obligations and auto-generated fragments
                if self._is_anonymous_dep(dep_name):
                    continue

                # Filter kernel machinery that a proof author wouldn't reference
                if self._is_kernel_machinery(dep_name):
                    continue

                cleaned.add(dep_name)

            for dep_name in cleaned:
                dep_info = dep_graph.get(dep_name, {})
                dep_module = dep_info.get("module", "")
                kind = dep_info.get("kind", "def")
                is_repo = dep_info.get("is_repo", False)
                is_theorem = kind == "theorem"

                env_names.add(dep_name)

                if not is_repo or is_library_module(dep_module):
                    lib_entry = {"name": dep_name, "module": dep_module}
                    if is_theorem:
                        lib_lemmas.append(lib_entry)
                    else:
                        used_lib_defs.append(lib_entry)
                elif dep_module == thm_module:
                    source_text = self._lookup_source_text(repo_name, dep_name)
                    local_entry = {"name": dep_name, "content": source_text or dep_name}
                    if is_theorem:
                        used_local_lemmas.append(local_entry)
                    else:
                        used_local_defs.append(local_entry)
                else:
                    source_text = self._lookup_source_text(repo_name, dep_name)
                    repo_entry = {"name": dep_name, "content": source_text or dep_name}
                    if is_theorem:
                        repo_lemmas.append(repo_entry)
                    else:
                        used_repo_defs.append(repo_entry)

        # --- Phase 2: Merge in static analysis deps ---
        # The static analysis finds things the kernel doesn't see:
        # macros/notations, definitions that got elaborated away, etc.
        # We add them if they aren't already covered by the env extraction.
        #
        # Build a suffix index from env names for dedup: if static has "Foo.bar"
        # and env already has "Capless.Foo.bar", they're the same constant.
        env_suffix_index: set[str] = set()
        for name in env_names:
            env_suffix_index.add(name)
            parts = name.split(".")
            for j in range(1, len(parts)):
                env_suffix_index.add(".".join(parts[j:]))

        # Track all names already added (across all fields) to prevent duplicates
        seen_names: set[str] = set(env_names)

        static_repo_local = [
            ("used_repo_defs", used_repo_defs),
            ("repo_lemmas", repo_lemmas),
            ("used_local_defs", used_local_defs),
            ("used_local_lemmas", used_local_lemmas),
        ]
        for field, target_list in static_repo_local:
            for d in original.get(field, []):
                name = d.get("name", "")
                # Skip if already covered by env (exact or suffix match)
                if name in env_suffix_index:
                    continue
                # Skip duplicates within static merge
                if name in seen_names:
                    continue
                # Skip parse artifacts: empty, universe-annotated, placeholder
                if not name or ".{" in name or name == "...":
                    continue
                # Accept macros/notations unconditionally
                # Accept qualified names (likely real declarations)
                # Accept bare names only if they have content (actual source text)
                accepted = False
                if self._is_macro_or_notation(name):
                    accepted = True
                elif " " in name:
                    # Multi-word entries that aren't macros — likely infix/prefix
                    # notations not caught by _is_macro_or_notation
                    if any(kw in name for kw in ("infix", "prefix", "postfix", "=>")):
                        accepted = True
                elif "." in name or d.get("content", "") != name:
                    # Qualified name, or has real content (not just the name echoed back)
                    accepted = True
                if accepted:
                    target_list.append(d)
                    seen_names.add(name)

        entry["used_lib_defs"] = used_lib_defs
        entry["used_repo_defs"] = used_repo_defs
        entry["lib_lemmas"] = lib_lemmas
        entry["repo_lemmas"] = repo_lemmas
        entry["used_local_defs"] = used_local_defs
        entry["used_local_lemmas"] = used_local_lemmas

        # Copy remaining fields from original
        entry["local_ctx"] = original.get("local_ctx", "")
        entry["target_theorem"] = original.get("target_theorem", "")
        entry["ground_truth_proof"] = original.get("ground_truth_proof", "")
        entry["nesting_depth"] = original.get("nesting_depth", 0)
        entry["transitive_dep_count"] = original.get("transitive_dep_count", 0)
        entry["subset_aristotle"] = original.get("subset_aristotle", False)
        entry["category"] = original.get("category", "")

        # Add metadata about extraction method
        if raw_deps and "error" in raw_deps:
            entry.setdefault("metadata", {})["env_extraction_error"] = raw_deps["error"]

        return entry

    def run_all(self) -> None:
        """Process all theorems grouped by repo and write output JSONL."""
        # Group entries by repo
        repo_groups: dict[str, list[dict]] = defaultdict(list)
        for entry in self.entries:
            repo_groups[entry["lean_root"]].append(entry)

        log.info(
            "Processing %d theorems across %d repos",
            len(self.entries), len(repo_groups),
        )

        # Build a lookup from thm_name -> raw extraction result
        all_raw: dict[str, dict] = {}
        failed_repos: list[str] = []

        for repo_name, theorems in sorted(repo_groups.items()):
            try:
                raw_results = self.extract_repo(repo_name, theorems)
                for r in raw_results:
                    name = r.get("name", "")
                    all_raw[name] = r
                log.info(
                    "  %s: extracted %d/%d theorems",
                    repo_name, len(raw_results), len(theorems),
                )
            except Exception:
                log.exception("Failed to extract %s", repo_name)
                failed_repos.append(repo_name)

        # Format and write output
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with open(self.output_path, "w") as f:
            for entry in self.entries:
                raw = all_raw.get(entry["thm_name"])
                formatted = self.format_entry(entry, raw)
                f.write(json.dumps(formatted) + "\n")
                written += 1

        log.info("Wrote %d entries to %s", written, self.output_path)
        if failed_repos:
            log.warning("Failed repos: %s", ", ".join(failed_repos))

        # Summary stats
        has_deps = sum(1 for e in self.entries if e["thm_name"] in all_raw and "error" not in all_raw[e["thm_name"]])
        has_errors = sum(1 for e in self.entries if e["thm_name"] in all_raw and "error" in all_raw[e["thm_name"]])
        missing = len(self.entries) - len(all_raw)
        log.info(
            "Summary: %d extracted, %d errors, %d missing",
            has_deps, has_errors, missing,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Environment-based dependency extraction for VeriSoftBench.",
    )
    parser.add_argument(
        "--container",
        default="vsb-dev",
        help="Docker container name (default: vsb-dev)",
    )
    parser.add_argument(
        "--verisoftbench",
        type=Path,
        required=True,
        help="Path to verisoftbench.jsonl (host path)",
    )
    parser.add_argument(
        "--repo-index-dir",
        default="/workspace/repo_index",
        help="Repo index directory path inside the container",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for verisoftbench_env.jsonl (host path)",
    )
    args = parser.parse_args()

    extractor = LeanEnvExtractor(
        docker_container=args.container,
        verisoftbench_path=args.verisoftbench,
        repo_index_dir=args.repo_index_dir,
        output_path=args.output,
    )
    extractor.run_all()


if __name__ == "__main__":
    main()
