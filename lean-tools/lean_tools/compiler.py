import subprocess
from functools import partial
from pathlib import Path
from typing import Literal, Mapping

from attrs import define, field


def sanitize_paths(content: str, name_to_path: Mapping[str, str | Path]):
    for name, path in name_to_path.items():
        if isinstance(path, Path):
            content = content.replace(str(path.absolute()), f'${name}')
        if str(path) != '.':
            content = content.replace(str(path), f'${name}')
    return content


@define
class LeanCompiler:
    cwd: Path = field(factory=Path.cwd)
    timeout: int = 120
    flags: tuple[str, ...] = ('-Dlinter.unusedVariables=false',)

    @property
    def subprocess_run(self):
        return partial(
            subprocess.run,
            cwd=self.cwd,
            check=True,
            timeout=self.timeout,
            text=True,
            encoding='utf-8',
            errors='replace',
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def __call__(self, content: str, path: Path):
        path.write_text(content)
        try:
            result, success = self.subprocess_run(['lake', 'env', 'lean', *self.flags, str(path)]).stdout, True
        except subprocess.CalledProcessError as e:  # let other exceptions raise
            result, success = e.stdout, False

        name_to_path = {'SAVE_DIR': path.parent, 'CWD': self.cwd, 'HOME': Path.home()}
        return sanitize_paths(result, name_to_path), success


@define
class ContainerLeanCompiler(LeanCompiler):
    image_name: str = 'lean-workspace'
    runtime: Literal['docker', 'podman'] = 'podman'

    def __attrs_post_init__(self):
        try:
            subprocess.run([self.runtime, 'image', 'inspect', self.image_name], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f'Require `{self.image_name}` image.') from e

    def __call__(self, content: str, path: Path):
        path.write_text(content)
        try:
            result, success = (
                self.subprocess_run(
                    [
                        self.runtime,
                        'run',
                        '--rm',
                        '--network=none',
                        '--workdir=/workspace',
                        '--volume=./:/workspace:ro,z',
                        f'--volume={path}:/save/{path.name}:ro,z',
                        self.image_name,
                        'lake',
                        'env',
                        'lean',
                        *self.flags,
                        f'/save/{path.name}',
                    ]
                ).stdout,
                True,
            )
        except subprocess.CalledProcessError as e:  # let other exceptions raise
            result, success = e.stdout, False

        name_to_path = {'SAVE_DIR': '/save', 'CWD': '/workspace', 'HOME': '/root'}
        return sanitize_paths(result, name_to_path), success
