import inspect
import warnings
from functools import wraps
from pathlib import Path
from typing import Callable, Generic

from attrs import define, evolve, field
from cattrs.preconf.pyyaml import PyyamlConverter, make_converter
from rich import console, markdown, panel
from statemachine import Event, State, StateChart

from . import Agent, ConversationItem, FunctionCall, Message, MessageRole, MessageType, ProviderT, TinyAgentException
from .shell import ShellTimedOut


class IterativeAgentConsole(console.Console):
    def wait(self):
        return self.status('[bold]Waiting...')

    def print_message(self, message: Message):
        content, role = message.content, message.role or ''
        self.print(panel.Panel(markdown.Markdown(content), title=role.capitalize()))

    def print_function_call(self, function_call: FunctionCall):
        formatted_args = '\n\n'.join(f'[bold]{k}[/]\n{v}' for k, v in function_call.args.items())
        self.print(panel.Panel(f'[red]Execute [bold]{function_call.name}[/bold] with arguments:[/red]\n\n{formatted_args}'))

    def print_items(self, items: list[ConversationItem]):
        for item in items:
            match item:
                case Message():
                    self.print_message(item)
                case FunctionCall():
                    self.print_function_call(item)


class MaxCallExceeded(TinyAgentException): ...


def limit_call(fn: Callable | None = None, *, max_calls: int):
    def decorator(fn):
        count = 0

        @wraps(fn if inspect.isroutine(fn) else fn.__call__)
        def wrapped(*args, **kwargs):
            nonlocal count
            if count >= max_calls:
                raise MaxCallExceeded()
            count += 1
            return fn(*args, **kwargs)

        return wrapped

    if fn is None:
        return decorator
    return decorator(fn)


class IterativeAgentFlow(StateChart['IterativeAgent[ProviderT]']):
    catch_errors_as_events = False

    initial = State(initial=True)
    working = State()
    using_function = State()
    checking = State()
    final = State(final=True)

    step = Event(
        initial.to(working)
        | working.to(using_function, cond='require_function')
        | working.to(checking, cond='require_checking')
        | working.to(final, unless=['require_function', 'require_checking'])
        | checking.to(working, cond='require_working')
        | using_function.to(working)
        | checking.to(final, unless='require_working')
    )

    def __init__(self, agent: 'IterativeAgent'):
        super().__init__(model=agent, state_field='state')

    @property
    def agent(self):
        return self.model

    def require_function(self):
        item = self.agent.items[-1]
        if not isinstance(item, FunctionCall) or item.name == 'submit':
            return False
        return True

    def require_checking(self):
        item = self.agent.items[-1]
        if not isinstance(item, FunctionCall) or item.name != 'submit':
            return False
        n_checks = len(list(filter(lambda item: isinstance(item, FunctionCall) and item.name == 'submit', self.agent.items)))
        return n_checks <= self.agent.max_checks

    def require_working(self):
        item = self.agent.items[-1]
        if not isinstance(item, Message):
            return False
        return True

    @working.enter
    def work(self):
        items = [item for hook in self.agent.hooks for item in hook(self.agent.items)]
        self.agent.console.print_items(items)
        self.agent.items.extend(items)

        with self.agent.console.wait():
            items = self.agent.provider(self.agent.items, submit=self.agent.submit, **self.agent.name_to_fn)
        messages = list(filter(lambda item: isinstance(item, Message), items))
        self.agent.console.print_items(messages)

        function_calls = list(filter(lambda item: isinstance(item, FunctionCall), items))
        match function_calls:
            case []:
                pass
            case [function_call]:
                self.agent.console.print_function_call(function_call)
            case [function_call, *_]:
                function_calls = [function_call]
                warnings.warn('The agent tried to call multiple tools. Only the first call is kept.')
                self.agent.console.print_function_call(function_call)

        self.agent.items.extend(messages)
        self.agent.items.extend(function_calls)

    @using_function.enter
    def use_function(self):
        function_call: FunctionCall = self.agent.items[-1]
        if not function_call.name in self.agent.name_to_fn:
            result = self.agent.unknown_tool_prompt
        else:
            with self.agent.console.wait():
                try:
                    result = self.agent.name_to_fn[function_call.name](**function_call.args)
                except Exception as e:
                    if not type(e) in self.agent.exception_cls_to_msg:
                        raise
                    result = self.agent.exception_cls_to_msg[type(e)]

        if len(result) > self.agent.max_fn_output_chars:
            result = result[: self.agent.max_fn_output_chars] + '... Output has been truncated.'

        message = Message(content=result, type=MessageType.FUNCTION_OUTPUT, meta=evolve(function_call.meta, id=None))
        self.agent.console.print_message(message)
        self.agent.items.append(message)

    @checking.enter
    def check(self):
        function_call: FunctionCall = self.agent.items[-1]
        with self.agent.console.wait():
            result, success = self.agent.submit(**function_call.args)
        if success:
            return

        messages = [Message(result, type=MessageType.FUNCTION_OUTPUT, meta=evolve(function_call.meta, id=None))]
        messages.append(Message(self.agent.submission_failed_prompt, role=MessageRole.USER))
        self.agent.console.print_items(messages)
        self.agent.items.extend(messages)

    def before_transition(self):
        self.agent.save()


def simple_submit() -> tuple[str, bool]:
    """Call to signal completion."""
    return 'Completed.', True


DEFAULT_PATH = Path('agent.yaml')
DEFAULT_UNKNOWN_TOOL_PROMPT = 'Unknown tool.'
DEFAULT_SUBMISSION_FAILED_PROMPT = 'Your previous submision failed. You only have a limited number of submissions.'
DEFAULT_EXCEPTION_CLS_TO_MSG = {
    MaxCallExceeded: 'You have exceeded the maxium number of calls for this tool.',
    ShellTimedOut: 'The shell timed out.',
}


@define
class IterativeAgent(Agent, Generic[ProviderT]):
    provider: ProviderT
    items: list[ConversationItem]

    max_checks: int = 0
    path: Path | None = DEFAULT_PATH

    submission_failed_prompt: str = DEFAULT_SUBMISSION_FAILED_PROMPT
    unknown_tool_prompt: str = DEFAULT_UNKNOWN_TOOL_PROMPT
    max_fn_output_chars: int = 2**14

    submit: Callable[..., tuple[str, bool]] = field(init=False, default=simple_submit)
    name_to_fn: dict[str, Callable] = field(init=False, factory=dict)
    hooks: list[Callable[[list[ConversationItem]], list[ConversationItem]]] = field(init=False, factory=list)
    exception_cls_to_msg: dict[type[Exception], str] = field(init=False, factory=lambda: DEFAULT_EXCEPTION_CLS_TO_MSG)
    console: IterativeAgentConsole = field(init=False, factory=lambda: IterativeAgentConsole(record=True))
    converter: PyyamlConverter = field(init=False, factory=make_converter)
    flow: IterativeAgentFlow = field(init=False)

    def __attrs_post_init__(self):
        self.console.print_items(self.items)
        self.flow = IterativeAgentFlow(self)

    @property
    def submission(self):
        function_calls: list[FunctionCall] = list(filter(lambda item: isinstance(item, FunctionCall), self.items))  # type: ignore[arg-type]
        if not function_calls:
            return None
        submissions = list(filter(lambda fc: fc.name == 'submit', function_calls))
        if not submissions:
            return None
        return submissions[-1].args

    def __call__(self):
        while not self.flow.is_terminated:
            self.flow.step()
        self.save()

    def save(self):
        if self.path is None:
            return
        self.path.write_text(self.converter.dumps(self))


def remind_call_fraction_reached(
    items: list[ConversationItem],
    name: str,
    max_calls: int,
    call_fraction: float,
) -> list[ConversationItem]:
    n_calls = len(list(filter(lambda item: isinstance(item, FunctionCall) and item.name == name, items)))
    if n_calls != round(max_calls * call_fraction):
        return []
    return [Message(f'You have used {call_fraction * 100:#.1f}% of the available calls for `{name}`', role=MessageRole.USER)]


def remind_every(items: list[ConversationItem], name: str, n_remind_calls: int, reminder: str) -> list[ConversationItem]:
    n_calls = len(list(filter(lambda item: isinstance(item, FunctionCall) and item.name == name, items)))
    if n_calls == 0 or n_calls % n_remind_calls != 0:
        return []
    return [Message(reminder, role=MessageRole.USER)]


def remind_post_call(items: list[ConversationItem], name: str, reminder: str) -> list[ConversationItem]:
    item = items[-1]
    if not isinstance(item, Message) or item.type != MessageType.FUNCTION_OUTPUT:
        return []
    item = items[-2]
    if not isinstance(item, FunctionCall) or item.name != name:
        return []
    return [Message(reminder, role=MessageRole.USER)]
