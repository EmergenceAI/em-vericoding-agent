import shutil
from abc import ABC, abstractmethod
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from typing import Annotated, ContextManager, Literal

import yaml
from attrs import define, evolve, frozen
from cyclopts import App, Parameter, config
from lean_tools.compiler import ContainerLeanCompiler, LeanCompiler
from lean_tools.repl import ContainerLeanREPL, LeanREPL
from rich import panel, print, traceback
from tiny_agent import LitellmProvider, Message, MessageRole
from tiny_agent.iterative import IterativeAgent, limit_call, remind_call_fraction_reached
from tiny_agent.utils import ignore_re
from verina.baseline.generate import create_placeholder
from verina.dataset.dataset import BenchmarkData, load_benchmark_data_from_task_file
from verina.dataset.template import LeanGenerationTaskTemplate

from . import (
    CODE_PROOF_INSTRUCTION,
    LEAN4_INSTRUCTION,
    MSG,
    REPL_INSTRUCTION,
    SEMANTIC_FILTERING_INSTRUCTION,
    SUBMIT_INSTRUCTION,
    Result,
    check_imports,
    check_sorry,
    forbid_bare_mathlib,
    forbid_io,
    forbid_unecessary_imports,
    join,
    join_checks,
    print_results,
)

traceback.install(width=None)


VERINA_SUBMIT_INSTRUCTION = f"""
{SUBMIT_INSTRUCTION}
- DO NOT import Std or Init.
- DO NOT use `set_option` in the import.
- Be careful of indentations. The first line of `code` or `proof` submission MUST be indented.
""".strip()

SEMANTIC_MSG = """
Another LLM has filled in the following fields:
- code_imports
- proof_imports
- code_aux
- proof_aux
- code
- proof

Perform a semantic check to verify whether the LLM has solved the problem.
Be careful of potential loopholes that an LLM may exploit, such as `native_decide + @[implemented_by]`, metaprogramming tricks, etc.
Explain your reasoning before submission.
""".strip()


@frozen
class Task:
    data: BenchmarkData

    code_imports: str = ''
    code_aux: str = ''
    code: str = ''

    proof_imports: str = ''
    proof_aux: str = ''
    proof: str = ''

    def render(self, include_code: bool, include_proof: bool):
        template = LeanGenerationTaskTemplate(self.data.signature)
        renders: list[str] = [template.render_imports(self.data.lean_data.task_imports, 'task')]
        if include_code:
            code_imports = self.code_imports if self.code else create_placeholder('code_imports')
            renders.append(template.render_imports(code_imports, 'code'))
        if include_proof:
            proof_imports = self.proof_imports if self.proof else create_placeholder('proof_imports')
            renders.append(template.render_imports(proof_imports, 'proof'))
        renders += [
            template.render_aux(self.data.lean_data.task_aux, 'task'),
            template.render_aux(self.data.lean_data.solution_aux, 'solution'),
            template.render_aux(self.data.lean_data.precond_aux, 'precond'),
            template.render_precond(self.data.lean_data.precond),
            template.render_aux(self.data.lean_data.postcond_aux, 'postcond'),
            template.render_postcond(self.data.lean_data.postcond),
        ]
        if include_code:
            code_aux = self.code_aux if self.code else create_placeholder('code_aux')
            renders.append(template.render_aux(code_aux, 'code'))
            renders.append(template.render_code(self.code or create_placeholder('code')))
        if include_proof:
            proof_aux = self.proof_aux if self.proof else create_placeholder('proof_aux')
            renders.append(template.render_aux(proof_aux, 'proof'))
            renders.append(template.render_proof(self.proof or create_placeholder('proof')))
        return '\n'.join(renders)


@define
class Submit(ABC):
    task: Task
    compiler: LeanCompiler
    save_dir: Path

    name: str = 'main'

    @property
    def path(self):
        return self.save_dir / f'{self.name.capitalize()}.lean'

    @property
    def tmp_path(self):  # save a tmp file if compilation fails
        return self.path.parent / f'{self.path.stem}Tmp{self.path.suffix}'

    @property
    @abstractmethod
    def lean(self) -> str: ...

    @property
    def template(self):
        return join('`task_description`', self.task.data.description, '`task_template`', f'```lean4\n{self.lean}```')

    def finalize(self, *result_and_success: tuple[str, bool]):
        result, success = join_checks(*result_and_success)
        if success:
            self.tmp_path.rename(self.path)
        return result, success

    @abstractmethod
    def __call__(self, *args, **kwargs) -> tuple[str, bool]: ...


@define
class SubmitCode(Submit):
    name: str = 'code'

    @property
    def lean(self):
        return self.task.render(include_code=True, include_proof=False)

    def __call__(self, code: str, code_imports: str = '', code_aux: str = ''):
        """
        Submit the generated solution. Each argument must be valid Lean 4 code.

        :param code: The generated Lean 4 solution.
        :param code_imports: Imports required by the solution.
        :param code_aux: Auxiliary definitions for the solution.
        """

        task = evolve(self.task, code=code, code_imports=code_imports, code_aux=code_aux)
        content = task.render(include_code=True, include_proof=False)
        return self.finalize(
            check_imports(code_imports),
            check_sorry(join(code_imports, code_aux, code)),
            self.compiler(content, self.tmp_path),
        )


@define
class SubmitProof(SubmitCode):
    name: str = 'proof'

    def __attrs_post_init__(self):
        if not self.task.code:
            raise ValueError('Expect code.')

    @property
    def lean(self):
        return self.task.render(include_code=True, include_proof=True)

    def __call__(self, proof: str, proof_imports: str = '', proof_aux: str = ''):
        """
        Submit the generated solution. Each argument must be valid Lean 4 code.

        :param proof: The generated Lean 4 solution.
        :param proof_imports: Imports required by the solution.
        :param proof_aux: Auxiliary definitions for the solution.
        """
        task = evolve(self.task, proof=proof, proof_imports=proof_imports, proof_aux=proof_aux)
        content = task.render(include_code=True, include_proof=True)
        return self.finalize(
            check_imports(proof_imports),
            check_sorry(join(proof_imports, proof_aux, proof)),
            self.compiler(content, self.tmp_path),
        )


@define
class SubmitAll(Submit):
    @property
    def lean(self):
        return self.task.render(include_code=True, include_proof=True)

    def __call__(
        self,
        code: str,
        proof: str,
        code_imports: str = '',
        proof_imports: str = '',
        code_aux: str = '',
        proof_aux: str = '',
    ):
        """
        Submit the generated solution. Each argument must be valid Lean 4 code.

        :param code: The generated Lean 4 code.
        :param proof: The generated Lean 4 proof.
        :param code_imports: Imports required by the code.
        :param proof_imports: Imports required by the proof.
        :param code_aux: Auxiliary definitions for the code.
        :param proof_aux: Auxiliary definitions for the proof.
        """
        task = evolve(
            self.task,
            code=code,
            proof=proof,
            code_imports=code_imports,
            proof_imports=proof_imports,
            code_aux=code_aux,
            proof_aux=proof_aux,
        )
        content = task.render(include_code=True, include_proof=True)
        return self.finalize(
            check_imports(join(code_imports, proof_imports)),
            check_sorry(join(code_imports, proof_imports, code_aux, proof_aux, code, proof)),
            self.compiler(content, self.tmp_path),
        )


def load_dataset(data_dir: Path) -> list[BenchmarkData]:
    task_dirs = sorted(
        [d for d in data_dir.iterdir() if d.is_dir()],
        key=lambda d: int(d.stem.split('_')[-1]),
    )
    return [load_benchmark_data_from_task_file(task_dir, task_dir / 'task.json') for task_dir in task_dirs]


def run_agent(
    provider: LitellmProvider,
    max_checks: int,
    repl: LeanREPL,
    max_repl_calls: int,
    submit_fn: Submit,
):
    system_prompts = [LEAN4_INSTRUCTION, CODE_PROOF_INSTRUCTION, VERINA_SUBMIT_INSTRUCTION]
    if max_repl_calls:
        system_prompts += [REPL_INSTRUCTION]

    agent = IterativeAgent(
        provider=provider,
        items=[
            Message(content=join(*system_prompts), role=MessageRole.SYSTEM),
            Message(content=submit_fn.template, role=MessageRole.USER),
            Message(content=MSG, role=MessageRole.USER),
        ],
        max_checks=max_checks,
        path=submit_fn.save_dir / f'{submit_fn.name}.yaml',
    )
    agent.submit = submit_fn
    context: ContextManager = nullcontext()
    if max_repl_calls:
        context = repl
        repl.checks.extend([forbid_bare_mathlib, forbid_io, forbid_unecessary_imports])
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
        agent.console.save_html(str(submit_fn.save_dir / f'{submit_fn.name}.html'))


app = App(config=config.Yaml('config.yaml', allow_unknown=True))

file_dir = Path(__file__).parent
workspace_dir = file_dir.parent / 'AgentWorkspace' / 'verina'
verina_dir = file_dir.parent / 'third-party' / 'verina'


@app.default
def cli(
    provider: Annotated[LitellmProvider, Parameter(parse=ignore_re('cost', 'n_tokens'), name='*')],
    task_name: str,
    dataset_path: Path = verina_dir / 'datasets' / 'verina',
    workspace: Path = workspace_dir,
    run_name: str = 'default',
    timeout: int = 120,
    cwd: Path = Path('.'),
    max_checks: int = 2,
    max_repl_calls: int = 100,
    joint: bool = True,
    runtime: Literal['docker', 'podman'] = 'podman',
    image_name: str | None = None,
):
    dataset = load_dataset(dataset_path)
    task_data = next(filter(lambda data: data.data_id == task_name, dataset))
    task = Task(task_data)

    result_dir = workspace / task_data.data_id / run_name
    shutil.rmtree(result_dir, ignore_errors=True)
    result_dir.mkdir(parents=True)
    print(f'[bold blue]Task: {task_data.data_id}\nDir: {result_dir}[/]')

    if image_name:
        compiler = ContainerLeanCompiler(cwd=cwd, timeout=timeout, runtime=runtime, image_name=image_name)
        repl = ContainerLeanREPL(cwd=cwd, runtime=runtime, image_name=image_name)
    else:
        compiler, repl = LeanCompiler(cwd=cwd, timeout=timeout), LeanREPL(cwd=cwd)
    run = partial(run_agent, provider=provider, max_checks=max_checks, repl=repl, max_repl_calls=max_repl_calls)

    if joint:
        submit_all = SubmitAll(task=task, compiler=compiler, save_dir=result_dir)
        run(submit_fn=submit_all)
    else:
        submit_code = SubmitCode(task=task, compiler=compiler, save_dir=result_dir)
        submission, success = run(submit_fn=submit_code)
        if not success:
            return

        task = evolve(
            task,
            code_imports=submission.get('code_imports', ''),
            code_aux=submission.get('code_aux', ''),
            code=submission['code'],
        )
        submit_proof = SubmitProof(task=task, compiler=compiler, save_dir=result_dir)
        run(submit_fn=submit_proof)


@app.command
def count(workspace: Path = workspace_dir, run_name: str = 'default', difficulty: Literal['basic', 'advanced'] | None = None):
    task_dirs = sorted(filter(Path.is_dir, workspace.iterdir()), key=lambda p: (len(p.name), p.name))

    results: list[Result] = []

    for task_dir in task_dirs:
        if difficulty and difficulty not in task_dir.name:
            continue

        for run_name_dir in sorted(filter(Path.is_dir, task_dir.iterdir())):
            if run_name_dir.name != run_name:
                continue

            result = Result(name=task_dir.name)
            file_paths = sorted(map(lambda p: p.name, run_name_dir.iterdir()))

            semantic_file = run_name_dir / 'semantic.txt'
            if semantic_file.exists():
                result.semantic = semantic_file.read_text() == 'pass'

            if 'MainTmp.lean' in file_paths:
                result.passed = False
            elif 'Main.lean' in file_paths:
                result.passed = True

            run = yaml.safe_load((run_name_dir / 'main.yaml').read_text())
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
    task_name: str,
    workspace: Path = workspace_dir,
    run_name: str = 'default',
):
    result_dir = workspace / task_name / run_name
    file_path = result_dir / 'Main.lean'
    agent = IterativeAgent(
        provider=provider,
        items=[
            Message(content=SEMANTIC_FILTERING_INSTRUCTION, role=MessageRole.SYSTEM),
            Message(content=f'```lean4\n{file_path.read_text()}\n```', role=MessageRole.USER),
            Message(content=SEMANTIC_MSG, role=MessageRole.USER),
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
