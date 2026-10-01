from abc import abstractmethod
from enum import StrEnum, auto
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

from attrs import define, field, frozen
from cattrs import Converter

from .openai_compat import generate_openai_compat_function_def, make_converter as make_openai_compat_converter


@frozen
class Meta:
    id: str | None = None
    ref_id: str | None = None
    is_summarized: bool = False
    encrypted_content: str | None = None


class MessageType(StrEnum):
    MESSAGE = auto()
    REASONING = auto()
    FUNCTION_OUTPUT = auto()


class MessageRole(StrEnum):
    SYSTEM = auto()
    USER = auto()
    ASSISTANT = auto()


@frozen
class Message:
    content: str
    type: MessageType = MessageType.MESSAGE
    role: MessageRole | None = None

    meta: Meta = field(factory=Meta)


class FunctionCallArgs(dict[str, Any]): ...


@frozen
class FunctionCall:
    name: str
    args: FunctionCallArgs

    meta: Meta = field(factory=Meta)


ConversationItem = Message | FunctionCall


@runtime_checkable
class Provider(Protocol):
    @abstractmethod
    def __call__(self, items: list[ConversationItem], **name_to_fn: Callable) -> list[ConversationItem]: ...


ProviderT = TypeVar('ProviderT', bound=Provider)


@runtime_checkable
class Agent(Protocol):
    provider: Provider
    items: list[ConversationItem]

    @abstractmethod
    def __call__(self): ...


AgentT = TypeVar('AgentT', bound=Agent)


class TinyAgentException(Exception): ...


@define
class LitellmProvider(Provider):
    model_name: str
    api_base: str | None = None
    api_key: str | None = None
    cost: float = 0.0
    n_tokens: int = 0
    reasoning: dict[str, str] = field(factory=dict)

    converter: Converter = field(init=False, factory=make_openai_compat_converter)

    def __call__(self, items: list[ConversationItem], **name_to_fn: Callable) -> list[ConversationItem]:
        import litellm

        litellm_unstructured_input = self.converter.unstructure(items)
        litellm_response = litellm.responses(
            model=self.model_name,
            input=litellm_unstructured_input,
            api_base=self.api_base,
            api_key=self.api_key,
            tools=[generate_openai_compat_function_def(fn, name) for name, fn in name_to_fn.items()],
            parallel_tool_calls=False,
            reasoning=self.reasoning,
        )
        self.cost += litellm_response.usage.cost or 0.0
        self.n_tokens = litellm_response.usage.total_tokens
        litellm_unstructured_output = litellm_response.model_dump()['output']
        return self.converter.structure(litellm_unstructured_output, list[ConversationItem])
