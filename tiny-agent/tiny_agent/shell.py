import subprocess
from functools import partial
from pathlib import Path
from typing import Literal

from attrs import define, field


class ShellException(Exception): ...


class ShellTimedOut(ShellException): ...


class ShellContainerNotFound(ShellException): ...


@define
class POSIXShell:
    cwd: Path = field(factory=Path.cwd)
    timeout: float = 600.0
    max_chars: int = 2**11

    @property
    def subprocess_run(self):
        return partial(
            subprocess.run,
            text=True,
            cwd=self.cwd,
            timeout=self.timeout,
            encoding='utf-8',
            errors='replace',
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def run(self, command: str) -> str:
        return self.subprocess_run(command, shell=True, executable='/bin/sh').stdout

    def __call__(self, command: str) -> str:
        """
        Execute a POSIX shell (sh) command

        :param command: The shell command to execute
        """
        try:
            stdout = self.run(command)
            if len(stdout) > self.max_chars:
                stdout = stdout[: self.max_chars] + '... The output has been truncated.'
            return stdout
        except subprocess.TimeoutExpired as e:
            raise ShellTimedOut(e.stdout) from e


@define
class ContainerPOSIXShell(POSIXShell):
    runtime: Literal['docker', 'podman'] = 'podman'
    read_only: bool = False

    def __attrs_post_init__(self):
        try:
            subprocess.run([self.runtime, 'image', 'inspect', 'busybox'], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise ShellContainerNotFound(f'Require `busybox` image: `{self.runtime} pull busybox`') from e

    def run(self, command: str) -> str:
        return self.subprocess_run(
            [
                self.runtime,
                'run',
                '--rm',
                '--network=none',
                '--workdir=/workspace',
                f'--volume=./:/workspace' + (':ro,z' if self.read_only else ':z'),
                'busybox',
                'sh',
                '-c',
                command,
            ]
        ).stdout
