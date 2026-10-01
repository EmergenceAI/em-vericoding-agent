import inspect
import json
from typing import Any, Callable

import docstring_parser
from cattr.gen import make_dict_structure_fn, make_dict_unstructure_fn
from cattrs import BaseConverter, Converter, override
from cattrs.preconf import wrap


def configure_converter(converter: BaseConverter):
    from . import ConversationItem, FunctionCall, FunctionCallArgs, Message, MessageRole, MessageType, Meta

    struct_meta = make_dict_structure_fn(Meta, converter, ref_id=override(rename='call_id'))
    unstruct_meta = make_dict_unstructure_fn(Meta, converter, _cattrs_omit_if_default=True, ref_id=override(rename='call_id'))
    converter.register_structure_hook(Meta, struct_meta)
    converter.register_unstructure_hook(Meta, unstruct_meta)

    message_struct_fn = make_dict_structure_fn(Message, converter)
    message_unstruct_fn = make_dict_unstructure_fn(Message, converter, _cattrs_omit_if_default=True)

    @converter.register_structure_hook
    def struct_message(data: dict, data_type: type) -> Message:
        match data:
            case {'content': [{'text': text, 'type': 'input_text'}], 'type': 'message', 'role': 'user' | 'system', **meta}:
                ...
            case {'content': [{'text': text, 'type': 'output_text'}], 'type': 'message', 'role': 'assistant', **meta}:
                ...
            case {'content': [{'text': text, 'type': 'output_text'}], 'type': 'reasoning', **meta}:
                # litellm's strange behavior, which has type output_text instead
                ...
            case {'content': [{'text': text, 'type': 'reasoning_text'}], 'summary': [], 'type': 'reasoning', **meta}:
                ...
            case {'summary': summaries, 'type': 'reasoning', **meta}:
                meta['is_summarized'] = True
                text = '\n\n'.join(summary['text'] for summary in summaries)
            case {'output': [{'text': text, 'type': 'input_text'}], 'type': 'function_call_output', **meta}:
                data = data | {'type': 'function_output'}
            case _:
                raise ValueError(f'Unknown structure: {data}')

        return message_struct_fn(data | {'content': text, 'meta': meta}, data_type)

    @converter.register_unstructure_hook
    def unstruct_message(message: Message):
        data: dict = message_unstruct_fn(message)

        text = data.pop('content')
        match (message.type, message.role, message.meta.is_summarized):
            case (MessageType.MESSAGE, MessageRole.USER | MessageRole.SYSTEM, _):
                data.update({'content': [{'text': text, 'type': 'input_text'}], 'type': 'message'})
            case (MessageType.MESSAGE, MessageRole.ASSISTANT, _):
                data.update({'content': [{'text': text, 'type': 'output_text'}], 'type': 'message'})
            case (MessageType.REASONING, _, False):
                data.update({'content': [{'text': text, 'type': 'reasoning_text'}], 'summary': []})
            case (MessageType.REASONING, _, True):
                data.update({'summary': [{'text': text, 'type': 'summary_text'}]})
                data['meta'].pop('is_summarized')
            case (MessageType.FUNCTION_OUTPUT, _, _):
                data.update({'output': [{'text': text, 'type': 'input_text'}], 'type': 'function_call_output'})
            case _:
                raise ValueError(f'Unknown combination: {message}')

        return data | data.pop('meta', {})

    converter.register_structure_hook(FunctionCallArgs, func=lambda data, _: json.loads(data))
    converter.register_unstructure_hook(FunctionCallArgs, func=json.dumps)

    function_call_struct_fn = make_dict_structure_fn(FunctionCall, converter, args=override(rename='arguments'))
    function_call_unstruct_fn = make_dict_unstructure_fn(FunctionCall, converter, args=override(rename='arguments'))

    @converter.register_structure_hook
    def struct_function_call(data: dict, data_type) -> FunctionCall:
        match data:
            case {'name': _, 'arguments': _, **meta}:
                return function_call_struct_fn(data | {'meta': meta}, data_type)
        raise ValueError('Unknown structure')

    @converter.register_unstructure_hook
    def unstruct_function_call(function_call: FunctionCall) -> dict:
        function_data: dict = function_call_unstruct_fn(function_call)
        return function_data | function_data.pop('meta', {}) | {'type': 'function_call'}

    item_map = {'message': Message, 'reasoning': Message, 'function_call': FunctionCall}
    converter.register_structure_hook(ConversationItem, lambda data, _: converter.structure(data, item_map[data['type']]))

    return converter


@wrap(Converter)
def make_converter(*args: Any, **kwargs: Any):
    return configure_converter(Converter(*args, **kwargs))


default_schema_map = {str: 'string', int: 'integer', float: 'number', bool: 'boolean'}


def generate_openai_compat_function_def(
    fn: Callable,
    fn_name: str,
    schema_map_fn: Callable[[Any], Any] = lambda any_type: default_schema_map.get(any_type),
):
    tool: dict[str, Any] = {'type': 'function', 'name': fn_name}
    fn = fn if inspect.isroutine(fn) else getattr(fn, '__call__')
    doc = docstring_parser.parse(inspect.getdoc(fn))
    if doc.description:
        tool['description'] = doc.description

    tool['parameters'] = {'type': 'object', 'properties': {}, 'required': []}
    params = inspect.signature(fn).parameters
    doc_params = {param.arg_name: param for param in doc.params}
    for name in params.keys():
        prop = {}
        if params[name].annotation is not inspect.Signature.empty and schema_map_fn(params[name].annotation) is not None:
            prop['type'] = schema_map_fn(params[name].annotation)
        if name in doc_params and doc_params[name].description is not None:
            prop['description'] = doc_params[name].description

        if params[name].default is inspect.Signature.empty:
            tool['parameters']['required'].append(name)
        else:
            prop['default'] = params[name].default

        tool['parameters']['properties'][name] = prop

    return tool
