from typing import Union, TypeVar, Callable, Generic, Optional

T = TypeVar("T")


def parse_template_parts(s: str, converter: Callable[[str], T], interpret_escapes: bool = True) -> list[Union[str, T]]:
    """
    Split template string `s` into a list of literal strings and T parts.
    Expressions are delimited by `${{` ... `}}`.

    Returns: list[str | T]
    """
    res: list[Union[str, T]] = []
    n = len(s)
    last = 0

    while True:
        pos = s.find("${{", last)
        if pos == -1:
            break
        end = s.find("}}", pos + 3)
        if end == -1:
            # no closing -> stop and treat remainder as literal
            break

        # count backslashes immediately before pos
        bs = 0
        k = pos - 1
        while k >= 0 and s[k] == "\\":
            bs += 1
            k -= 1

        # prefix is everything from `last` up to the first of those backslashes
        prefix = s[last: pos - bs]

        expr_text = s[pos + 3: end]
        token_literal = s[pos: end + 2]  # the whole `${{...}}`

        if not interpret_escapes:
            if prefix:
                res.append(prefix)
            res.append(converter(expr_text))
            last = end + 2
            continue

        # interpret escapes according to rules
        if bs == 0:
            # normal expression
            if prefix:
                res.append(prefix)
            res.append(converter(expr_text))
        elif bs == 1:
            # single backslash -> escape token, drop the backslash
            if prefix:
                res.append(prefix)
            res.append(token_literal)
        else:
            # bs >= 2
            kept = bs - 1
            kept_bs = "\\" * kept
            if bs % 2 == 0:
                # even -> keep (bs-1) backslashes, then expression
                if prefix or kept_bs:
                    res.append(prefix + kept_bs)
                res.append(converter(expr_text))
            else:
                # odd -> keep (bs-1) backslashes, token treated as literal appended together
                res.append(prefix + kept_bs + token_literal)

        last = end + 2

    # append the remainder
    if last < n:
        rest = s[last:]
        if rest:
            res.append(rest)

    return res

class FriendlyException(Exception):
    """Friendly exceptions are used to hide stacktraces from user."""
    pass


class LateInit(Generic[T]):
    def __init__(self):
        self._value: Optional[T] = None
        self._value_set: bool = False

    def __get__(self, instance, owner) -> T:
        if not self._value_set:
            raise AttributeError(
                "LateInit variable accessed before initialization")
        return self._value  # type: ignore

    def __set__(self, instance, value: T) -> None:
        self._value = value
        self._value_set = True

    def __delete__(self, instance) -> None:
        self._value = None
        self._value_set = False
