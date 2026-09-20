"""Bounded lexical inventory of explicit, literal-named JavaScript test calls.

This is not a JS parser or an evaluator. Unsupported declarations are reported;
comments, strings, regex literals and template text never become test calls.
"""
from collections import Counter
from dataclasses import dataclass
import re


class AnalysisError(ValueError):
    """Selected source cannot be inventoried by the supported lexical subset."""


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.offset = 0
        self.line = 1

    def take(self, count: int = 1) -> str:
        value = self.source[self.offset:self.offset + count]
        self.offset += count
        self.line += value.count("\n")
        return value

    def quoted(self, quote: str, depth: int) -> Token:
        line = self.line
        self.take()
        value = []
        interpolated = False
        while self.offset < len(self.source):
            char = self.take()
            if char == quote:
                return Token("template" if interpolated else "string", "".join(value), line)
            if char in "\r\n" and quote != "`":
                raise AnalysisError("Unterminated string")
            if quote == "`" and char == "$" and self.source[self.offset:self.offset + 1] == "{":
                self.take()
                self.tokens(depth + 1, interpolation=True)
                interpolated = True
            elif char == "\\":
                if self.offset == len(self.source):
                    break
                escaped = self.take()
                if escaped in "\r\n":
                    if escaped == "\r" and self.source[self.offset:self.offset + 1] == "\n":
                        self.take()
                    continue
                if escaped in ("x", "u"):
                    count = 2 if escaped == "x" else 4
                    if escaped == "u" and self.source[self.offset:self.offset + 1] == "{":
                        self.take()
                        end = self.source.find("}", self.offset)
                        if end < 0 or not 1 <= end - self.offset <= 6:
                            raise AnalysisError("Invalid Unicode escape")
                        count = end - self.offset
                        digits = self.take(count)
                        self.take()
                    else:
                        digits = self.take(count)
                    if not re.fullmatch(r"[0-9a-fA-F]+", digits) or len(digits) != count:
                        raise AnalysisError("Invalid character escape")
                    try:
                        value.append(chr(int(digits, 16)))
                    except ValueError:
                        raise AnalysisError("Invalid Unicode escape") from None
                elif escaped.isdigit():
                    if escaped != "0" or self.source[self.offset:self.offset + 1].isdigit():
                        raise AnalysisError("Legacy octal string escapes are unsupported")
                    value.append("\0")
                else:
                    value.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b",
                                  "f": "\f", "v": "\v"}.get(escaped, escaped))
            else:
                value.append(char)
        raise AnalysisError("Unterminated string or template")

    def regex(self) -> None:
        self.take()
        in_class = False
        while self.offset < len(self.source):
            char = self.take()
            if char == "\\":
                self.take()
            elif char in "\r\n":
                break
            elif char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                while self.offset < len(self.source) and self.source[self.offset].isalpha():
                    self.take()
                return
        raise AnalysisError("Unterminated regular expression")

    def tokens(self, depth: int = 0, *, interpolation: bool = False) -> list[Token]:
        if depth > 64:
            raise AnalysisError("Template nesting exceeds the analysis bound")
        result: list[Token] = []
        braces = 0
        parens: list[bool] = []
        regex_allowed = True
        while self.offset < len(self.source):
            char = self.source[self.offset]
            if char.isspace() or char == "\ufeff":
                self.take()
                continue
            if self.source.startswith("//", self.offset) or (self.offset == 0 and self.source.startswith("#!")):
                end = self.source.find("\n", self.offset)
                self.take(len(self.source) - self.offset if end < 0 else end - self.offset)
                continue
            if self.source.startswith("/*", self.offset):
                end = self.source.find("*/", self.offset + 2)
                if end < 0:
                    raise AnalysisError("Unterminated comment")
                self.take(end + 2 - self.offset)
                continue
            line = self.line
            if char in "'\"`":
                result.append(self.quoted(char, depth))
                regex_allowed = False
                continue
            if self.source.startswith(("</", "/>", "<>"), self.offset):
                raise AnalysisError("JSX is outside the supported analysis subset")
            if char == "/" and result and result[-1].kind == "punct" and result[-1].value == "}":
                raise AnalysisError("Ambiguous slash after a closing brace")
            if (char == "/" and result and result[-1].kind == "name"
                    and result[-1].value in ("of", "await", "yield")
                    and not (len(result) > 1 and result[-2].value in (".", "?."))):
                raise AnalysisError("Ambiguous slash after a contextual keyword")
            if char == "/" and regex_allowed:
                self.regex()
                result.append(Token("regex", "", line))
                regex_allowed = False
                continue
            if char == "\\":
                raise AnalysisError("Escaped identifiers are unsupported")
            if char.isalpha() or char in "_$":
                match = re.compile(r"[\w$]+").match(self.source, self.offset)
                value = self.take(len(match.group()))
                property_name = bool(result and result[-1].value in (".", "?."))
                result.append(Token("name", value, line))
                regex_allowed = not property_name and value in {
                    "return", "throw", "case", "delete", "void", "typeof", "new", "in",
                    "instanceof", "of", "yield", "await", "else", "do"}
                continue
            if char.isdigit():
                match = re.compile(r"[\w.]+").match(self.source, self.offset)
                result.append(Token("number", self.take(len(match.group())), line))
                regex_allowed = False
                continue
            if char not in "()[]{}.,;:?~+-*/%&|^!<>=@#":
                raise AnalysisError("Unsupported JavaScript token")
            value = next((op for op in ("...", "=>", "?.", "++", "--", "&&", "||", "??")
                          if self.source.startswith(op, self.offset)), char)
            self.take(len(value))
            if value == "(":
                parens.append(bool(result and result[-1].value in {"if", "while", "for", "with", "switch", "catch"}))
                regex_allowed = True
            elif value == ")":
                regex_allowed = parens.pop() if parens else False
            else:
                regex_allowed = value not in ("]", "}", "++", "--", ".", "?.")
            if value == "{":
                braces += 1
            elif value == "}":
                if interpolation and braces == 0:
                    return result
                braces -= 1
            result.append(Token("punct", value, line))
        if interpolation:
            raise AnalysisError("Unterminated template interpolation")
        return result


SUPPRESSIONS = {"skip", "todo", "only", "failing", "fails", "skipIf", "runIf"}
MODIFIERS = SUPPRESSIONS | {"serial", "concurrent", "each", "for"}
HOOKS = {"before", "after", "beforeEach", "afterEach", "always"}


def context_markers(tokens: list[Token], pairs: dict[int, int], start: int, end: int) -> Counter:
    """Recognize skip/todo on a callback's simple first parameter or Mocha's this."""
    cursor = start
    callback = start
    while cursor < end:
        if tokens[cursor].value == "," and cursor + 1 < end:
            callback = cursor + 1
        cursor = pairs[cursor] + 1 if cursor in pairs else cursor + 1
    cursor = callback
    if tokens[cursor].value == "async":
        cursor += 1
    function = tokens[cursor].value == "function"
    if function:
        cursor += 1
        if cursor < end and tokens[cursor].kind == "name":
            cursor += 1
    names = {"this"} if function else set()
    if cursor < end and tokens[cursor].value == "(":
        closing = pairs[cursor]
        if not function and (closing + 1 >= end or tokens[closing + 1].value != "=>"):
            return Counter()
        if cursor + 1 < closing and tokens[cursor + 1].kind == "name":
            names.add(tokens[cursor + 1].value)
        cursor = closing + 1
    elif cursor + 1 < end and tokens[cursor].kind == "name" and tokens[cursor + 1].value == "=>":
        names.add(tokens[cursor].value)
        cursor += 1
    else:
        return Counter()
    result: Counter = Counter()
    for index in range(cursor, end - 2):
        if tokens[index].kind != "name" or tokens[index].value not in names:
            continue
        if index and tokens[index - 1].value in (".", "?."):
            continue
        member = tokens[index + 2]
        if member.value not in ("skip", "todo"):
            continue
        if tokens[index + 1].value in (".", "?.") and member.kind == "name":
            result[member.value] += 1
        elif (tokens[index + 1].value == "[" and member.kind == "string"
              and index + 3 < end and tokens[index + 3].value == "]"):
            result[member.value] += 1
    return result


def inventory(source: str, functions: tuple[str, ...]) -> tuple[Counter, Counter]:
    tokens = Lexer(source).tokens()
    if len(tokens) > 200_000:
        raise AnalysisError("Source exceeds the token analysis bound")
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind != "punct":
            continue
        if token.value in ("(", "[", "{"):
            stack.append(index)
            if len(stack) > 128:
                raise AnalysisError("Source exceeds the delimiter nesting bound")
        elif token.value in (")", "]", "}"):
            if not stack or tokens[stack[-1]].value != {")": "(", "]": "[", "}": "{"}[token.value]:
                raise AnalysisError("Unbalanced delimiters")
            pairs[stack.pop()] = index
    if stack:
        raise AnalysisError("Unbalanced delimiters")
    declarations: Counter = Counter()
    suppressions: Counter = Counter()
    for index, token in enumerate(tokens):
        if token.kind != "name" or token.value not in functions:
            continue
        if index and tokens[index - 1].value in (".", "?.", "function"):
            continue
        cursor = index + 1
        modifiers = []
        while cursor < len(tokens):
            if tokens[cursor].value in (".", "?.") and cursor + 1 < len(tokens):
                member = tokens[cursor + 1]
                if member.kind != "name":
                    raise AnalysisError("Unsupported test member")
                modifiers.append(member.value)
                cursor += 2
            elif (tokens[cursor].value == "[" and cursor + 2 < len(tokens)
                  and tokens[cursor + 1].kind == "string" and tokens[cursor + 2].value == "]"):
                modifiers.append(tokens[cursor + 1].value)
                cursor += 3
            elif tokens[cursor].value == "[":
                raise AnalysisError("Dynamic test members are unsupported")
            else:
                break
            if modifiers[-1] in ("each", "for", "skipIf", "runIf") and cursor < len(tokens):
                if tokens[cursor].value == "(":
                    cursor = pairs[cursor] + 1
                elif tokens[cursor].kind in ("string", "template"):
                    cursor += 1  # Tagged table syntax.
        if set(modifiers) & HOOKS:
            continue
        modes = set(modifiers) & SUPPRESSIONS
        key = ("suite" if token.value in ("describe", "suite") else "test", "<unbound>")
        if cursor < len(tokens) and tokens[cursor].value == "(":
            if set(modifiers) - MODIFIERS:
                raise AnalysisError("Unsupported test modifier")
            end = pairs[cursor]
            if (cursor + 2 >= len(tokens) or tokens[cursor + 1].kind != "string"
                    or tokens[cursor + 2].value not in (",", ")")):
                raise AnalysisError("Test declarations require literal string titles")
            key = (key[0], tokens[cursor + 1].value)
            declarations[key] += 1
            for mode, count in context_markers(tokens, pairs, cursor + 1, end).items():
                suppressions[(*key, mode)] += count
            # Inspect only a literal second-argument options object, never callback bodies.
            option = cursor + 3
            if option < end and tokens[option].value == "{":
                stop = pairs[option]
                option += 1
                while option < stop:
                    field = tokens[option]
                    if field.value == "...":
                        raise AnalysisError("Spread test options require runtime analysis")
                    if field.value == "[":
                        if (option + 2 >= stop or tokens[option + 1].kind != "string"
                                or tokens[option + 2].value != "]"):
                            raise AnalysisError("Dynamic test option keys are unsupported")
                        field = tokens[option + 1]
                        option += 2
                    if (field.value in ("skip", "todo", "only") and option + 2 < stop
                            and tokens[option + 1].value == ":"):
                        value = tokens[option + 2]
                        if not (value.kind == "name" and value.value == "false"
                                and tokens[option + 3].value in (",", "}")):
                            modes.add(field.value)
                    # Skip each field's expression, including nested literal objects.
                    while option < stop and tokens[option].value != ",":
                        option = pairs[option] + 1 if option in pairs else option + 1
                    option += 1
        for mode in modes:
            suppressions[(*key, mode)] += 1
    return declarations, suppressions
