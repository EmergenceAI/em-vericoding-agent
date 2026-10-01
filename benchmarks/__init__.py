import re

from attrs import define
from rich import print

LEAN4_INSTRUCTION = """
# Lean 4 Instruction
You are an expert in Lean 4 programming and theorem proving.
Your solution should:
- Be well-documented with comments if necessary.
- Follow Lean 4 best practices and use appropriate Lean 4 syntax and features.
- DO NOT use Lean 3 syntax or features.
- DO NOT use catch-all generic imports like `import Mathlib`. Only import what you need.

Hint:
- Use a[i]! instead of a[i] when a is an array or a list when necessary.
- To use `lemma` instead of `theorem`, you need to use `import Mathlib.Tactic.Lemma`.
""".strip()

SUBMIT_INSTRUCTION = """
# Submission Instruction
You are provided with a `submit` tool to submit the final solution.
The tool's signature should match the fields in the template if a template is provided.
""".strip()

CODE_PROOF_INSTRUCTION = """
# Solution Instruction
You are an expert in Lean 4 programming and theorem proving.
- First, create an informal natural language step-by-step code that addresses the problem. You MUST completely solve the problem in natural language first.
- Then, create the formal solution using the informal solution. You MUST complete the solution before using the `submit` tool.
- Decompose the solution into smaller units when possible.
""".strip()

REPL_INSTRUCTION = """
# REPL Instruction
You are provided with a Lean interactive REPL to assist with your task.
- You MUST use the REPL to make sure that your Lean code compiles before submission.
- The REPL does not import any libraries or include any code by default. You need to import the library or define the code to use it.
- You are provided with a sufficient but limited number of REPL calls, so use it efficiently.
- You should use the REPL to check if some functions, theorems, or imports exist.
- If the REPL only returns `{'env': _}`, it means that the code compiles.
- You MUST use the correct `env` (returned by REPL) if you wish to continue with some previous code.
- To enter tactic mode, issue a command containing a `sorry`, which will return the proof goals.
- If you exceed max calls, wrap up your work and submit anyway. You MUST explain your unfinished submission and details of the failure.

Hint:
- Always append `set_option linter.unusedVariables false` after import to avoid unnecessary linter warnings.
""".strip()

SEMANTIC_FILTERING_INSTRUCTION = """
# Semantic verification
You are an expert in Lean 4 programming and theorem proving.

Another LLM has solved the problem using Lean 4, and the solution is successfully compiled.
The solution has already been checked for phrases like `sorry`, `admit`, or `axiom`.
Your goal is to perform a semantic check to verify whether the model successfully solves the goal without exploiting language loopholes.
Use the `submit` tool to submit your final answer.
""".strip()

MSG = 'Solve the problem using the provided template and submit the solution.'


def join(*strings):
    return '\n\n'.join(strings)


def check_sorry(content: str):
    results = []
    for phrase in ['sorry', 'admit', 'axiom']:
        if phrase in content:
            results.append(f'`{phrase}` is not allowed')
    if results:
        return join(*results), False
    return '', True


def check_imports(content: str):
    if 'set_option' in content:
        return '`set_option` is not allowed in imports', False
    return '', True


def join_checks(*result_and_success: tuple[str, bool]):
    results, successes = zip(*result_and_success)
    return join(*filter(None, results)), all(successes)


def forbid_bare_mathlib(cmd: str):
    if 'import Mathlib' in [line.strip() for line in cmd.splitlines()]:
        return 'Bare `import Mathlib` is not allowed.'


def forbid_io(cmd: str):
    if re.search(r'\bIO\b', cmd):
        return '`IO` is not allowed.'


def forbid_unecessary_imports(cmd: str):
    results = []
    for lib in ['Lean', 'Std', 'Init']:
        import_str = f'import {lib}'
        if import_str in cmd:
            results.append(f'`{import_str}` is not allowed.')
    if results:
        return join(*results)


@define
class Result:
    name: str
    passed: bool | None = None
    semantic: bool | None = None
    n_tokens: int = 0
    n_calls: int = 0


def print_results(results: list[Result]):
    print(f'{len(results)=}')
    passed_results = list(filter(lambda result: result.passed is True, results))
    print(f'{len(passed_results)=}', passed_results)
    failed_semantic_results = list(filter(lambda result: result.semantic is False, passed_results))
    print(f'{len(failed_semantic_results)=}', failed_semantic_results)
    failed_results = list(filter(lambda result: result.passed is False, results))
    print(f'{len(failed_results)=}', failed_results)
    crashed_results = list(filter(lambda result: result.passed is None, results))
    print(f'{len(crashed_results)=}', crashed_results)

    print(f'Pass rate: {len(passed_results) / len(results) * 100:#.2f}%')
    passed_semantic_results = list(filter(lambda result: result.semantic is True, passed_results))
    print(f'Semantic pass rate: {len(passed_semantic_results) / len(results) * 100:#.2f}%')

    call_counts, token_counts = [result.n_calls for result in results], [result.n_tokens for result in results]
    print(f'Avg calls per problem {sum(call_counts) / len(call_counts):#.2f}')
    print(f'Avg tokens per problem {sum(token_counts) / len(token_counts):#.2f}')
