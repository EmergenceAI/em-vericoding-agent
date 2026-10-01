from pathlib import Path
from typing import Annotated

from cattrs.preconf.pyyaml import make_converter
from cyclopts import App, Parameter, config
from rich import traceback
from statemachine.contrib.diagram import formatter

from . import LitellmProvider, Message, MessageRole
from .interactive import DEFAULT_PATH, InteractiveAgent, InteractiveAgentFlow
from .utils import ignore_re

traceback.install(width=None)

app = App(config=[config.Yaml('config.yaml', allow_unknown=True), config.Env(prefix='TINY_')])


@app.command
def resume(path: Path = DEFAULT_PATH):
    basic_agent = make_converter().loads(path.read_text(), InteractiveAgent[LitellmProvider])
    basic_agent()


@app.command
def visualization(path: Path):
    path.write_text(formatter.render(InteractiveAgentFlow, path.suffix.lstrip('.')))


@app.default()
def cli(
    provider: Annotated[LitellmProvider, Parameter(parse=ignore_re('cost', 'n_tokens'), name='*')],
    prompt: str = 'You are a helpful assistant.',
):
    basic_agent = InteractiveAgent(provider=provider, items=[Message(prompt, role=MessageRole.SYSTEM)])
    basic_agent()


app()
