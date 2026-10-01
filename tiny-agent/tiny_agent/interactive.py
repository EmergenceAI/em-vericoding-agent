from functools import cached_property
from pathlib import Path
from typing import Generic

from attrs import define, evolve, field
from cattrs.preconf.pyyaml import PyyamlConverter, make_converter as make_pyyaml_converter
from rich import prompt
from statemachine import Event, State, StateChart

from . import Agent, ConversationItem, FunctionCall, Message, MessageRole, MessageType, ProviderT
from .iterative import DEFAULT_PATH, DEFAULT_UNKNOWN_TOOL_PROMPT, IterativeAgentConsole
from .shell import POSIXShell, ShellTimedOut


class InteractiveAgentConsole(IterativeAgentConsole):
    user_prompt_prefix: str = '\n[bold red]>[/] '

    @cached_property
    def prompt(self):
        class CustomPrompt(prompt.Prompt):
            prompt_suffix: str = ''

        return CustomPrompt(self.user_prompt_prefix, console=self)

    @cached_property
    def confirm(self):
        return prompt.Confirm(self.user_prompt_prefix + 'Confirm?', console=self)

    def print_items(self, items: list[ConversationItem]):
        for item in items:
            match item:
                case Message(role=MessageRole.USER):
                    self.print(f'{self.user_prompt_prefix}{item.content}')
                case Message():
                    self.print_message(item)
                case FunctionCall():
                    self.print_function_call(item)


class InteractiveAgentFlow(StateChart['InteractiveAgent[ProviderT]']):
    catch_errors_as_events = False

    initial = State(initial=True)
    requesting = State()
    responding = State()
    confirming = State()
    using_shell = State()

    def __init__(self, agent: 'InteractiveAgent'):
        super().__init__(model=agent, state_field='state')

    @property
    def agent(self):
        return self.model

    def require_shell(self):
        item = self.agent.items[-1]
        if not isinstance(item, FunctionCall):
            return False
        return True

    @requesting.enter
    def request(self):
        try:
            user_input = self.agent.console.prompt()
        except EOFError:
            exit()
        self.agent.items.append(Message(content=user_input, role=MessageRole.USER))

    @responding.enter
    def response(self):
        with self.agent.console.wait():
            items = self.agent.provider(self.agent.items, shell=self.agent.shell)

        messages = list(filter(lambda item: isinstance(item, Message), items))
        self.agent.console.print_items(messages)

        function_calls = list(filter(lambda item: isinstance(item, FunctionCall), items))
        match function_calls:
            case []:
                pass
            case [function_call]:
                self.agent.console.print_function_call(function_call)
            case _:
                raise ValueError('Only 1 function call is allowed at any time')

        self.agent.items.extend(messages)
        self.agent.items.extend(function_calls)

    @confirming.enter
    def confirm(self):
        item: FunctionCall = self.agent.items[-1]
        if not self.agent.console.confirm():
            message = Message(
                self.agent.shell_cancelled_prompt,
                type=MessageType.FUNCTION_OUTPUT,
                meta=evolve(item.meta, id=None),
            )
            self.agent.console.print_message(message)
            self.agent.items.append(message)

    @using_shell.enter
    def use_shell(self):
        function_call: FunctionCall = self.agent.items[-1]
        match function_call.name:
            case 'shell':
                try:
                    result = self.agent.shell(**function_call.args)
                except ShellTimedOut as e:
                    result = '\n\n'.join([self.agent.shell_timed_out_prompt, str(e)])
                except KeyboardInterrupt:
                    result = self.agent.shell_cancelled_prompt
                    self.agent.console.line()  # clear the ctrl+c line
            case _:
                result = self.agent.unknown_tool_prompt
        message = Message(content=result, type=MessageType.FUNCTION_OUTPUT, meta=evolve(function_call.meta, id=None))
        self.agent.console.print_message(message)
        self.agent.items.append(message)

    def before_transition(self):
        self.model.save()

    step = Event(
        initial.to(requesting)
        | requesting.to(responding)
        | responding.to(confirming, cond=require_shell)
        | responding.to(requesting, unless=require_shell)
        | confirming.to(using_shell, cond=require_shell)
        | confirming.to(requesting, unless=require_shell)
        | using_shell.to(responding)
    )


DEFAULT_SHELL_CANCELLED_PROMPT = 'The user cancelled the operation.'
DEFAULT_SHELL_TIMED_OUT_PROMPT = 'Your last command timed out.'


@define
class InteractiveAgent(Agent, Generic[ProviderT]):
    provider: ProviderT
    items: list[ConversationItem] = field(factory=list)

    path: Path = DEFAULT_PATH
    shell: POSIXShell = POSIXShell(timeout=5.0)
    state: str | None = None  # hold the current flow state, can be used to restore the flow

    shell_cancelled_prompt: str = DEFAULT_SHELL_CANCELLED_PROMPT
    shell_timed_out_prompt: str = DEFAULT_SHELL_TIMED_OUT_PROMPT
    unknown_tool_prompt: str = DEFAULT_UNKNOWN_TOOL_PROMPT

    console: InteractiveAgentConsole = field(init=False, factory=InteractiveAgentConsole)
    converter: PyyamlConverter = field(init=False, factory=make_pyyaml_converter)
    flow: InteractiveAgentFlow = field(init=False)

    def __attrs_post_init__(self):
        self.console.print_items(self.items)
        self.flow = InteractiveAgentFlow(self)

    def __call__(self):
        while True:
            self.flow.step()

    def save(self):
        self.path.write_text(self.converter.dumps(self))
