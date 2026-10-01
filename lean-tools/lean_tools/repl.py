import json
import signal
import subprocess
from functools import partial
from pathlib import Path
from typing import Any, Callable, Literal

from attrs import define, field
from cattrs import BaseConverter, Converter, override
from cattrs.gen import make_dict_structure_fn, make_dict_unstructure_fn
from cattrs.preconf import wrap


@define
class Position:
    line: int
    column: int
    code_at_line: str | None = field(init=False, default=None)


@define
class Message:
    severity: str
    data: str
    pos: Position
    end_pos: Position | None = None


@define
class Sorry:
    proof_state: int
    goal: str
    pos: Position
    end_pos: Position | None = None


@define
class REPLInput:
    cmd: str
    env: int | None = None


@define
class REPLOutput:
    env: int
    messages: list[Message] = field(factory=list)
    sorries: list[Sorry] = field(factory=list)


@define
class REPLMessage:
    message: str


def configure_converter(converter: BaseConverter):
    make_struct_fn = partial(make_dict_structure_fn, converter=converter, _cattrs_forbid_extra_keys=True)
    make_unstruct_fn = partial(make_dict_unstructure_fn, converter=converter, _cattrs_omit_if_default=True)

    def rename(name: str):
        return override(rename=name)

    converter.register_structure_hook(Position, make_struct_fn(Position))
    converter.register_unstructure_hook(Position, make_unstruct_fn(Position, _cattrs_include_init_false=True))

    converter.register_structure_hook(Message, make_struct_fn(Message, end_pos=rename('endPos')))
    converter.register_unstructure_hook(Message, make_unstruct_fn(Message))

    converter.register_structure_hook(Sorry, make_struct_fn(Sorry, end_pos=rename('endPos'), proof_state=rename('proofState')))
    converter.register_unstructure_hook(Sorry, make_unstruct_fn(Sorry))

    converter.register_structure_hook(REPLInput, make_struct_fn(REPLInput))
    converter.register_unstructure_hook(REPLInput, make_unstruct_fn(REPLInput))

    converter.register_structure_hook(REPLOutput, make_struct_fn(REPLOutput))
    converter.register_unstructure_hook(REPLOutput, make_unstruct_fn(REPLOutput))

    converter.register_structure_hook(REPLMessage, make_struct_fn(REPLMessage))
    converter.register_unstructure_hook(REPLMessage, make_unstruct_fn(REPLMessage))

    return converter


@wrap(Converter)
def make_converter(*args: Any, **kwargs: Any):
    return configure_converter(Converter(*args, **kwargs))


@define
class LeanREPL:
    cwd: Path = field(factory=Path.cwd)
    read_timeout: int = 120
    close_timeout: int = 5
    process: subprocess.Popen = field(init=False)
    converter: BaseConverter = field(init=False, factory=make_converter)
    checks: list[Callable[[str], str | None]] = field(init=False, factory=list)

    @property
    def subprocess_popen(self):
        return partial(
            subprocess.Popen,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            bufsize=1,
            cwd=self.cwd,
        )

    def open(self):
        self.process = self.subprocess_popen(['lake', 'exe', 'repl'])

    def close(self):
        self.process.stdin.close()  # type: ignore[union-attr]
        try:
            self.process.wait(float(self.close_timeout))
        except:
            self.process.kill()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.process.kill()
        except:
            ...

    def restart(self):
        self.close()
        self.open()

    def write(self, repl_input: REPLInput):
        self.process.stdin.write(json.dumps(self.converter.unstructure(repl_input), ensure_ascii=False) + '\n\n')  # type: ignore[union-attr]
        self.process.stdin.flush()  # type: ignore[union-attr]

    def read(self) -> REPLOutput | REPLMessage:
        class REPLTimedOut(Exception): ...

        def timeout(*_, **__):
            raise REPLTimedOut

        output: REPLOutput | REPLMessage
        try:
            signal.signal(signal.SIGALRM, timeout)
            signal.alarm(self.read_timeout)
            output_lines = []
            while True:
                output_lines.append(self.process.stdout.readline())  # type: ignore[union-attr]
                if output_lines[-1] == '\n':
                    break
            raw_lines = ''.join(output_lines)
            if 'PANIC' in raw_lines:
                self.restart()
                output = REPLMessage('The REPL panicked and has been restarted. All envs are lost.')
            else:
                try:
                    output = self.converter.structure(json.loads(raw_lines), REPLOutput | REPLMessage)  # type: ignore[arg-type]
                except:
                    print(raw_lines)
                    raise
        except REPLTimedOut:
            self.restart()
            output = REPLMessage('The REPL timed out and has been restarted. All envs are lost.')
        finally:
            signal.alarm(0)
        return output

    def __call__(self, cmd: str, env: int | None = None):
        """
        Call Lean REPL.

        :param cmd: Lean code to evaluate.
        :param env: If present, must contain a number received in the `env` field of a previous response,
            and causes the command to be run in the existing environment.
            If there is no env field, a new environment is created.
            You can only use `import` commands when you do not specify the `env` field.
        """
        output: REPLOutput | REPLMessage
        check_results: list[str] = list(filter(lambda x: x is not None, [check(cmd) for check in self.checks]))  # type: ignore[arg-type]
        if check_results:
            output = REPLMessage('\n\n'.join(check_results))
        else:
            self.write(REPLInput(cmd=cmd, env=env))
            output = self.read()

        if isinstance(output, REPLOutput):
            lines = cmd.splitlines()
            item: Message | Sorry
            try:
                for item in (*output.messages, *output.sorries):  # type: ignore[assignment]
                    item.pos.code_at_line = lines[item.pos.line - 1]
                    if item.end_pos:
                        item.end_pos.code_at_line = lines[item.end_pos.line - 1]
            except IndexError:
                print(output)
                self.restart()
                output = REPLMessage('The REPL errored out and has been restarted. All envs are lost.')
        return json.dumps(self.converter.unstructure(output), ensure_ascii=False)


@define
class ContainerLeanREPL(LeanREPL):
    image_name: str = 'lean-workspace'
    runtime: Literal['docker', 'podman'] = 'podman'

    def __attrs_post_init__(self):
        try:
            subprocess.run([self.runtime, 'image', 'inspect', self.image_name], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f'Require `{self.image_name}` image.') from e

    def open(self):
        self.process = self.subprocess_popen(
            [
                self.runtime,
                'run',
                '-i',
                '--rm',
                '--network=none',
                self.image_name,
                'lake',
                'exe',
                'repl',
            ]
        )
