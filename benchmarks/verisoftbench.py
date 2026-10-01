import re
import shutil
from contextlib import nullcontext
from enum import StrEnum, auto
from functools import partial
from pathlib import Path
from typing import Annotated, ContextManager, Literal

import polars as pl
import yaml
from attrs import define, evolve, frozen
from cyclopts import App, Parameter, config
from lean_tools.compiler import ContainerLeanCompiler, LeanCompiler
from lean_tools.repl import ContainerLeanREPL, LeanREPL
from rich import panel, print, traceback
from tiny_agent import LitellmProvider, Message, MessageRole
from tiny_agent.iterative import IterativeAgent, limit_call, remind_call_fraction_reached
from tiny_agent.shell import ContainerPOSIXShell, POSIXShell
from tiny_agent.utils import ignore_re

from . import (
    CODE_PROOF_INSTRUCTION,
    LEAN4_INSTRUCTION,
    MSG,
    REPL_INSTRUCTION,
    SEMANTIC_FILTERING_INSTRUCTION,
    SUBMIT_INSTRUCTION,
    Result,
    check_sorry,
    join,
    join_checks,
    print_results,
)

traceback.install(width=None)


SHELL_INSTRUCTION = """
# Shell Instruction
You are provided with a POSIX shell tool to explore the read-only code base, which is mounted at the current directory.
- Use the shell to retrieve relevant Lean4 code. Only POSIX shell commands exist.
- You are provided with a sufficient but limited number of shell calls, so use them efficiently.
- Each command is independent and is executed in a fresh shell environment.
""".strip()

VERISOFTBENCH_SUBMIT_INSTRUCTION = f"""
{SUBMIT_INSTRUCTION}
- You can use anything available in the template, including lemmas and proofs with `sorry`.
- DO NOT use `sorry`, `admit`, or `axiom` for the sections that you need to fill in: {{proof}} and {{proof_aux}}.
- The templating system will only check for `sorry`-like phrases in your provided {{proof}} and {{proof_aux}} and not the whole template.
- You MUST correctly insert `:= by` or equivalent in {{proof}}.
""".strip()

SEMANTIC_MSG = """
Another LLM has filled in the following fields:
- PROOF
- PROOF_AUX

The theorem statement is given as:
```lean4
{thm_stmt}
```

Perform a semantic check to verify whether the LLM has solved the problem.
Be careful of potential loopholes that an LLM may exploit, such as `native_decide + @[implemented_by]`, metaprogramming tricks, etc.
Explain your reasoning before submission.

The LLM is allowed to use anything available in the template, including lemmas and proofs with `sorry`.
However, for the section that the LLM has to fill in, the system forbids `sorry`, `admit`, or `axiom`.
""".strip()


@frozen
class Task:
    local_ctx: str
    thm_stmt: str
    file_path: Path

    proof_aux: str = ''
    proof: str = ''

    def render(self):
        proof = self.proof or '{{proof}}'
        proof_aux = self.proof_aux if self.proof else '{{proof_aux}}'
        return join(
            f'-- Theorem extracted from {self.file_path}',
            self.local_ctx,
            '-- START PROOF AUX --',
            proof_aux,
            '-- START PROOF --',
            self.thm_stmt + '\n' + proof,
        )


@define
class Submit:
    task: Task
    compiler: LeanCompiler
    save_dir: Path
    name: str

    @property
    def template(self):
        return f'```lean4\n{self.task.render()}\n```'

    @property
    def path(self):
        return (self.save_dir / f'{self.name}.lean').resolve()

    @property
    def tmp_path(self):  # save a tmp file if compilation fails
        return self.path.parent / f'{self.path.stem}Tmp{self.path.suffix}'

    def finalize(self, *result_and_success: tuple[str, bool]):
        result, success = join_checks(*result_and_success)
        if success:
            self.tmp_path.rename(self.path)
        return result, success

    def __call__(self, proof: str, proof_aux: str = ''):
        """
        Submit the generated solution. Each argument must be valid Lean 4 code.

        :param proof: The generated Lean 4 solution.
        :param proof_aux: Auxiliary definitions for the solution.
        """
        task = evolve(self.task, proof=proof, proof_aux=proof_aux)
        return self.finalize(check_sorry(join(proof, proof_aux)), self.compiler(task.render(), self.tmp_path))


def run_agent(
    provider: LitellmProvider,
    max_checks: int,
    repl: LeanREPL,
    max_repl_calls: int,
    shell: POSIXShell,
    max_shell_calls: int,
    submit_fn: Submit,
):
    system_prompts = [LEAN4_INSTRUCTION, CODE_PROOF_INSTRUCTION, VERISOFTBENCH_SUBMIT_INSTRUCTION]
    if max_repl_calls:
        system_prompts += [REPL_INSTRUCTION]
    if max_shell_calls:
        system_prompts += [SHELL_INSTRUCTION]

    agent = IterativeAgent(
        provider=provider,
        items=[
            Message(content=join(*system_prompts), role=MessageRole.SYSTEM),
            Message(content=submit_fn.template, role=MessageRole.USER),
            Message(content=MSG, role=MessageRole.USER),
        ],
        max_checks=max_checks,
        path=submit_fn.save_dir / 'result.yaml',
    )
    agent.submit = submit_fn
    if max_shell_calls:
        agent.name_to_fn['shell'] = limit_call(shell, max_calls=max_shell_calls)
    context: ContextManager = nullcontext()
    if max_repl_calls:
        context = repl
        agent.name_to_fn['repl'] = limit_call(repl, max_calls=max_repl_calls)
        agent.hooks.extend(
            partial(remind_call_fraction_reached, name='repl', max_calls=max_repl_calls, call_fraction=frac)
            for frac in [0.5, 0.75, 0.9]
        )

    try:
        with context:
            agent()
        if agent.submission is None:
            raise RuntimeError

        with agent.console.wait():
            result, success = submit_fn(**agent.submission)
        agent.console.print(panel.Panel(result, title='Stdout'))
        agent.console.print(panel.Panel(f'[bold {"green" if success else "red"}]Compiled: {success}'))
        return agent.submission, success
    finally:
        agent.console.save_html(str(submit_fn.save_dir / 'result.html'))


class Col(StrEnum):
    LEAN_ROOT = auto()
    REL_PATH = auto()
    IMPORTS = auto()
    THM_NAME = auto()
    THM_STMT = auto()


app = App(config=config.Yaml('config.yaml', allow_unknown=True))

file_dir = Path(__file__).parent
workspace_dir = file_dir.parent / 'AgentWorkspace' / 'VeriSoftBench'
repos_dir = file_dir.parent / 'AgentWorkspace' / 'VeriSoftBenchRepos'
verisoftbench_dir = file_dir.parent / 'third-party' / 'VeriSoftBench'
patch_path = file_dir.parent / 'third-party' / 'verisoftbench_dataset_patch.yaml'


@app.default
def cli(
    provider: Annotated[LitellmProvider, Parameter(parse=ignore_re('cost', 'n_tokens'), name='*')],
    task_id: int,
    dataset_path: Path = verisoftbench_dir / 'data' / 'verisoftbench.jsonl',
    lean_repos_dir: Path = repos_dir,
    workspace: Path = workspace_dir,
    patch_path: Path = patch_path,
    run_name: str = 'default',
    timeout: int = 120,
    max_checks: int = 2,
    max_repl_calls: int = 100,
    max_shell_calls: int = 50,
    runtime: Literal['docker', 'podman'] = 'podman',
    image_name: str | None = None,
):
    df = pl.read_ndjson(dataset_path).filter(pl.col('subset_aristotle') == True)
    row = df.row(task_id, named=True)
    thm_name = row[Col.THM_NAME]

    patch = yaml.safe_load(patch_path.read_text())
    if thm_name in patch:
        print(f'Patching {thm_name=}')
        row = row | patch[thm_name]

    result_dir = workspace / f'task_{task_id}' / run_name
    shutil.rmtree(result_dir, ignore_errors=True)
    result_dir.mkdir(parents=True)
    print(f'[bold blue]{thm_name} of {row[Col.LEAN_ROOT]}\nDir: {result_dir}[/]')

    file_path: Path = lean_repos_dir / row[Col.LEAN_ROOT] / row[Col.REL_PATH]
    file_content = file_path.read_text()

    lake_build_dir: Path = lean_repos_dir / row[Col.LEAN_ROOT] / '.lake' / 'build' / 'lib' / 'lean'
    build_path: Path = lake_build_dir / row[Col.REL_PATH].strip('src/')
    build_path = build_path.with_suffix('.olean')
    assert build_path.exists(), f'Expect {build_path=}.'

    match = re.search(re.escape(row[Col.THM_STMT]) + r'\s*' + re.escape(':= by sorry'), file_content)
    if not match:
        raise ValueError(f'Cannot find {row[Col.THM_STMT]=} in {file_path=}.')
    idx = match.start()
    local_ctx = file_content[:idx]  # VeriSoftBench local_ctx field in row is processed and not usable
    task = Task(local_ctx=local_ctx, file_path=row[Col.REL_PATH], thm_stmt=row[Col.THM_STMT])

    cwd = lean_repos_dir / row[Col.LEAN_ROOT]
    if image_name:
        compiler = ContainerLeanCompiler(cwd=cwd, timeout=timeout, runtime=runtime, image_name=image_name)
        repl = ContainerLeanREPL(cwd=cwd, runtime=runtime, image_name=image_name)
    else:
        compiler, repl = LeanCompiler(cwd=cwd, timeout=timeout), LeanREPL(cwd=cwd)
    shell = ContainerPOSIXShell(cwd=cwd, read_only=True, timeout=timeout // 2)

    submit = Submit(task=task, compiler=compiler, save_dir=result_dir, name=thm_name.replace('.', '__'))
    run_agent(
        provider=provider,
        max_checks=max_checks,
        repl=repl,
        max_repl_calls=max_repl_calls,
        shell=shell,
        max_shell_calls=max_shell_calls,
        submit_fn=submit,
    )


@app.command
def count(workspace: Path = workspace_dir, run_name: str = 'default'):
    task_dirs = sorted(filter(Path.is_dir, workspace.iterdir()), key=lambda p: (len(p.name), p.name))

    results: list[Result] = []

    for task_dir in task_dirs:
        for run_name_dir in sorted(filter(Path.is_dir, task_dir.iterdir())):
            if run_name_dir.name != run_name:
                continue

            result = Result(name=task_dir.name)
            file_paths = sorted(map(lambda p: p.name, run_name_dir.iterdir()))

            semantic_file = run_name_dir / 'semantic.txt'
            if semantic_file.exists():
                result.semantic = semantic_file.read_text() == 'pass'

            if any('Tmp.lean' in file_path for file_path in file_paths):
                result.passed = False
            elif any('.lean' in file_path for file_path in file_paths):
                result.passed = True

            run = yaml.safe_load((run_name_dir / 'result.yaml').read_text())
            result.n_tokens = run['provider']['n_tokens']
            for item in run['items']:
                if 'name' in item and item['name'] == 'repl':
                    result.n_calls += 1
            results.append(result)

    print_results(results)


def semantic_check(solved: bool = True):
    """Submit the answer."""
    return '', solved


@app.command
def semantic(
    provider: Annotated[LitellmProvider, Parameter(parse=ignore_re('cost', 'n_tokens'), name='*')],
    task_id: int,
    dataset_path: Path = verisoftbench_dir / 'data' / 'verisoftbench.jsonl',
    workspace: Path = workspace_dir,
    run_name: str = 'default',
):
    result_dir = workspace / f'task_{task_id}' / run_name

    df = pl.read_ndjson(dataset_path).filter(pl.col('subset_aristotle') == True)
    row = df.row(task_id, named=True)
    thm_name = row[Col.THM_NAME]

    patch = yaml.safe_load(patch_path.read_text())
    if thm_name in patch:
        print(f'Patching {thm_name=}')
        row = row | patch[thm_name]

    file_path = result_dir / f'{thm_name.replace(".", "__")}.lean'
    agent = IterativeAgent(
        provider=provider,
        items=[
            Message(content=SEMANTIC_FILTERING_INSTRUCTION, role=MessageRole.SYSTEM),
            Message(content=f'```lean4\n{file_path.read_text()}\n```', role=MessageRole.USER),
            Message(content=SEMANTIC_MSG.format(thm_stmt=row[Col.THM_STMT]), role=MessageRole.USER),
        ],
        path=result_dir / 'semantic.yaml',
    )
    agent.submit = semantic_check
    agent()
    if agent.submission is None:
        raise RuntimeError
    _, solved = semantic_check(**agent.submission)
    (result_dir / 'semantic.txt').write_text('pass' if solved else 'fail')


if __name__ == '__main__':
    app()
