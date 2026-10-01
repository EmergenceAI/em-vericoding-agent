# %%
import subprocess
from enum import StrEnum, auto
from pathlib import Path

import polars as pl
import yaml  # type: ignore[import-untyped]
from rich import print

current_dir = Path(__file__).parent

data_path = current_dir / 'VeriSoftBench' / 'data' / 'verisoftbench.jsonl'
repos_path = current_dir.parent / 'AgentWorkspace' / 'VeriSoftBenchRepos'

df = pl.read_ndjson(data_path)
aristotle_df = df.filter(pl.col('subset_aristotle') == True)
print(aristotle_df['lean_root'].unique().to_list())

# %%

with open(current_dir / 'verisoftbench_dataset_patch.yaml') as file:
    dataset_patch: dict[str, dict[str, str]] = yaml.safe_load(file)
with open(current_dir / 'verisoftbench_file_patch.yaml') as file:
    file_patch: dict[str, list[dict[str, str]]] = yaml.safe_load(file)


class Col(StrEnum):
    LEAN_ROOT = auto()
    REL_PATH = auto()
    THM_NAME = auto()
    GROUND_TRUTH_PROOF = auto()


for i, row in enumerate(aristotle_df.iter_rows(named=True)):
    thm_name: str = row[Col.THM_NAME]
    if thm_name in dataset_patch:
        row = row | dataset_patch[thm_name]

    file_path: Path = repos_path / row[Col.LEAN_ROOT] / row[Col.REL_PATH]
    content = file_path.read_text()

    proof = row[Col.GROUND_TRUTH_PROOF]
    if content.find(proof) == -1:
        print(f'Skipped {thm_name} in {file_path}. May have been patched.')
        continue
    patched = content.replace(proof, ':= by sorry\n')
    file_path.write_text(patched)

for i, (file_rel_path, patches) in enumerate(file_patch.items()):
    file_path: Path = repos_path / file_rel_path  # type: ignore[no-redef]
    if not file_path.exists():
        file_path.touch()

    content = file_path.read_text()
    for patch in patches:
        content = content.replace(patch['before'], patch['after'])
    file_path.write_text(content)

# %%

lakefile_lean_str = """
require REPL from git "https://github.com/leanprover-community/repl" @ "{rev}"
"""

lakefile_toml_str = """
[[require]]
name = "REPL"
scope = "leanprover-community"
rev = "{rev}"
"""


def get_rev(repo_path: Path):
    return (repo_path / 'lean-toolchain').read_text().split(':')[1].strip()


for repo_path in repos_path.iterdir():
    rev = get_rev(repo_path)
    print(f'{repo_path=} with {rev=}')

    toml_path, toml_str = repo_path / 'lakefile.toml', lakefile_toml_str.format(rev=rev)
    if toml_path.exists():
        if toml_str in toml_path.read_text():
            continue
        with open(toml_path, 'a') as file:
            file.write(toml_str)
        continue

    lean_path, lean_str = repo_path / 'lakefile.lean', lakefile_lean_str.format(rev=rev)
    if lean_path.exists():
        if lean_str in lean_path.read_text():
            continue
        with open(lean_path, 'a') as file:
            file.write(lean_str)

for repo_path in repos_path.iterdir():
    print(f'Building {repo_path=}')
    subprocess.run(['lake', 'update'], cwd=repo_path)
    subprocess.run(['lake', 'build'], cwd=repo_path)
    subprocess.run('echo \'{"cmd": ""}\' | lake exe repl', cwd=repo_path, shell=True)
