import re


def ignore_re(*fields: str) -> re.Pattern[str]:
    if not fields:
        return re.compile(r'.+')
    excluded = '|'.join(re.escape(field) for field in fields)
    return re.compile(rf'^(?!(?:{excluded})$).+')
