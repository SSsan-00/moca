"""Bounded typed PHP control-flow normalization. Never loads or executes PHP."""

from __future__ import annotations

from dataclasses import dataclass
import re

from migration_harness.context import php_boundaries


_MAX_FILE = 1024 * 1024
_MAX_RANGE = 16 * 1024
_MAX_TOKENS = 2_048
_MAX_DEPTH = 32
_MAX_EXPRESSION_OPERATORS = 64
_MIN_INT, _MAX_INT = -(2**31), 2**31 - 1
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_VARIABLE = re.compile(r"\$[A-Za-z_][A-Za-z_0-9]*")
_INTEGER = re.compile(r"(?:0|[1-9][0-9]{0,9})")
_PHYSICAL_LINE = re.compile(r"\r\n|\r|\n")
_STRICT_TYPES = re.compile(r"declare[ \t\r\n]*\([ \t\r\n]*strict_types[ \t\r\n]*=[ \t\r\n]*[01][ \t\r\n]*\)[ \t\r\n]*;",
                           re.IGNORECASE)
_TYPES = frozenset({"int", "bool"})
_RESERVED = frozenset("""
__halt_compiler abstract and array as break callable case catch class clone const continue declare default die
do echo else elseif empty enddeclare endfor endforeach endif endswitch endwhile enum eval exit extends final
fn for foreach function global goto if implements include include_once instanceof insteadof interface isset
list match namespace new or print private protected public readonly require require_once return static switch
throw trait try unset use var while xor yield int float bool string null true false void iterable object
resource numeric never mixed self parent
""".split())
_SPECIAL_VARIABLES = frozenset("""
$this $GLOBALS $_SERVER $_GET $_POST $_FILES $_COOKIE $_SESSION $_REQUEST $_ENV
""".split())
_TOKEN = re.compile(
    r"[ \t\r\n]+|/\*|//|\#(?!\[)|\$[A-Za-z_][A-Za-z_0-9]*|[A-Za-z_][A-Za-z_0-9]*|"
    r"[0-9]+|===|!==|&&|\|\||<=|>=|[!<>=(){}:;,\-]"
)
_PRECEDENCE = {"||": 1, "&&": 2, "===": 3, "!==": 3, "<": 4, "<=": 4, ">": 4, ">=": 4}
_OPERATORS = {"===": "==", "!==": "!="}


class UnsupportedPhp(ValueError):
    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"PHP line {line}: {message}" if line is not None else message)


@dataclass(frozen=True)
class Token:
    text: str
    line: int


def _function_range(source: str, name: str, start: int, end: int) -> str:
    # Reuse the bounded lexical pass to reject occurrences inside comments, strings and markup.
    opening = php_boundaries._OPEN.match(source, int(source.startswith("\ufeff")))
    if opening is None:
        raise UnsupportedPhp("expected a standalone PHP document")
    try:
        tokens = php_boundaries._tokens(source, opening.end())
        functions = php_boundaries._functions(source, tokens)
    except ValueError as error:
        raise UnsupportedPhp(str(error)) from error
    name_key = name.lower()
    candidates = [item for item in functions if item.name.lower() == name_key]
    if len(candidates) != 1 or (candidates[0].start_line, candidates[0].end_line) != (start, end):
        raise UnsupportedPhp("function name and exact source range do not uniquely match", start)

    depth = 0
    declarations = 0
    found_top_level = False
    for index, (token, offset, _) in enumerate(tokens):
        word = token.lower()
        if word in {"namespace", "__halt_compiler"}:
            raise UnsupportedPhp("namespace or compiler directives are outside the supported subset")
        if word == "declare" and (index != 0 or _STRICT_TYPES.match(source, offset) is None):
            raise UnsupportedPhp("only an initial strict_types declaration is supported")
        if word == "function" and index + 1 < len(tokens) and tokens[index + 1][0].lower() == name_key:
            declarations += 1
            if depth == 0 and (index == 0 or tokens[index - 1][0] in {";", "}"}):
                found_top_level = True
        if token in {"{", "(", "["}:
            depth += 1
        elif token in {"}", ")", "]"}:
            depth -= 1
    if declarations != 1 or not found_top_level:
        raise UnsupportedPhp("expected one unconditional top-level function", start)
    return "\n".join(_PHYSICAL_LINE.split(source)[start - 1:end])


def _region_tokens(text: str, first_line: int) -> list[Token]:
    tokens = []
    offset, line = 0, first_line
    while offset < len(text):
        match = _TOKEN.match(text, offset)
        if match is None:
            raise UnsupportedPhp("unsupported expression, literal or PHP token", line)
        token = match[0]
        finish = match.end()
        if token == "/*":
            comment_end = text.find("*/", finish)
            if comment_end < 0:
                raise UnsupportedPhp("unterminated block comment", line)
            finish = comment_end + 2
        elif token in {"//", "#"}:
            line_end = text.find("\n", finish)
            finish = len(text) if line_end < 0 else line_end
        elif not token.isspace():
            if len(tokens) >= _MAX_TOKENS:
                raise UnsupportedPhp("function token budget exceeded", line)
            tokens.append(Token(token, line))
        line += text.count("\n", offset, finish)
        offset = finish
    return tokens + [Token("", line)]


def _all_paths_return(body: list[dict]) -> bool:
    return any(statement["op"] == "return" or (
        _all_paths_return(statement["then"]) and _all_paths_return(statement["else"])
    ) for statement in body)


class _FunctionParser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0
        self.args: dict[str, tuple[int, str]] = {}
        self.return_type = ""
        self.operators = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def pop(self) -> Token:
        token = self.current
        if not token.text:
            raise UnsupportedPhp("incomplete function", token.line)
        self.index += 1
        return token

    def accept(self, text: str) -> Token | None:
        return self.pop() if self.current.text.lower() == text else None

    def require(self, text: str) -> Token:
        token = self.accept(text)
        if token is None:
            raise UnsupportedPhp(f"expected {text!r} in the supported subset", self.current.line)
        return token

    def scalar_type(self) -> str:
        token = self.pop()
        kind = token.text.lower()
        if kind not in _TYPES:
            raise UnsupportedPhp("expected native int or bool type", token.line)
        return kind

    def depth(self, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise UnsupportedPhp("nesting budget exceeded", self.current.line)

    def function(self, expected_name: str) -> tuple[list[str], str, list[dict]]:
        self.require("function")
        name = self.pop()
        if name.text.lower() != expected_name.lower():
            raise UnsupportedPhp("declaration did not match paired function", name.line)
        self.require("(")
        if self.current.text != ")":
            while True:
                kind = self.scalar_type()
                variable = self.pop()
                if not _VARIABLE.fullmatch(variable.text) or variable.text in self.args or variable.text in _SPECIAL_VARIABLES:
                    raise UnsupportedPhp("expected a unique ordinary parameter", variable.line)
                self.args[variable.text] = (len(self.args), kind)
                if len(self.args) > 8:
                    raise UnsupportedPhp("parameter budget exceeded", variable.line)
                if not self.accept(",") or self.current.text == ")":
                    break
        self.require(")")
        self.require(":")
        self.return_type = self.scalar_type()
        body = self.block(0)
        if self.current.text:
            raise UnsupportedPhp("additional syntax outside the paired function body", self.current.line)
        if not _all_paths_return(body):
            raise UnsupportedPhp("not all branches have an explicit return", self.current.line)
        return [kind for _, kind in self.args.values()], self.return_type, body

    def block(self, depth: int) -> list[dict]:
        self.depth(depth)
        self.require("{")
        body = []
        while not self.accept("}"):
            if token := self.accept("if"):
                body.append(self.conditional(token, depth))
            elif token := self.accept("return"):
                body.append(self.return_value(token))
            else:
                raise UnsupportedPhp("only if/elseif/else and return statements are supported", self.current.line)
        return body

    def branch(self, depth: int) -> list[dict]:
        if token := self.accept("return"):
            return [self.return_value(token)]
        return self.block(depth)

    def conditional(self, keyword: Token, depth: int) -> dict:
        self.depth(depth)
        self.require("(")
        test, kind = self.typed_expression()
        if kind != "bool":
            raise UnsupportedPhp("condition does not have native bool type", keyword.line)
        self.require(")")
        yes = self.branch(depth + 1)
        no = []
        if token := self.accept("elseif"):
            no = [self.conditional(token, depth + 1)]
        elif self.accept("else"):
            if token := self.accept("if"):
                no = [self.conditional(token, depth + 1)]
            else:
                no = self.branch(depth + 1)
        return {"op": "if", "line": keyword.line, "test": test, "then": yes, "else": no}

    def return_value(self, keyword: Token) -> dict:
        value, kind = self.typed_expression()
        if kind != self.return_type:
            raise UnsupportedPhp("return type is not guaranteed without coercion", keyword.line)
        self.require(";")
        return {"op": "return", "line": keyword.line, "value": value}

    def typed_expression(self) -> tuple[list, str]:
        self.operators = 0
        return self.expression(1, 0)

    def operator(self) -> Token:
        token = self.pop()
        self.operators += 1
        if self.operators > _MAX_EXPRESSION_OPERATORS:
            raise UnsupportedPhp("expression operator budget exceeded", token.line)
        return token

    def expression(self, minimum: int, depth: int) -> tuple[list, str]:
        self.depth(depth)
        left, left_type = self.operand(depth + 1)
        previous = 0
        while (precedence := _PRECEDENCE.get(self.current.text, 0)) >= minimum:
            operator = self.operator()
            if precedence >= 3 and previous == precedence:
                raise UnsupportedPhp("chained comparison requires parentheses", operator.line)
            right, right_type = self.expression(precedence + 1, depth + 1)
            if operator.text in {"&&", "||"}:
                valid = left_type == right_type == "bool"
            elif operator.text in {"===", "!=="}:
                valid = left_type == right_type
            else:
                valid = left_type == right_type == "int"
            if not valid:
                raise UnsupportedPhp("operands are not proven compatible without coercion", operator.line)
            left, left_type = [_OPERATORS.get(operator.text, operator.text), left, right], "bool"
            previous = precedence
        return left, left_type

    def operand(self, depth: int) -> tuple[list, str]:
        self.depth(depth)
        token = self.operator() if self.current.text == "!" else self.pop()
        if token.text == "(":
            value = self.expression(1, depth + 1)
            self.require(")")
            return value
        if token.text == "!":
            value, kind = self.operand(depth + 1)
            if kind != "bool":
                raise UnsupportedPhp("negation requires a native bool expression", token.line)
            return ["not", value], "bool"
        if token.text in self.args:
            index, kind = self.args[token.text]
            return ["arg", index], kind
        if token.text.lower() in {"true", "false"}:
            return ["bool", token.text.lower() == "true"], "bool"
        literal = self.pop() if token.text == "-" else token
        if _INTEGER.fullmatch(literal.text):
            value = int(literal.text) * (-1 if token.text == "-" else 1)
            if _MIN_INT <= value <= _MAX_INT:
                return ["int", value], "int"
            raise UnsupportedPhp("literal outside signed int32", literal.line)
        raise UnsupportedPhp("expected a typed parameter or literal; dynamic values are not supported", token.line)


def normalize_php(source: str, *, name: str, start_line: int, end_line: int) -> dict:
    if (not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) or name.lower() in _RESERVED
            or type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line):
        raise UnsupportedPhp("invalid explicit function or source range")
    if not isinstance(source, str):
        raise UnsupportedPhp("expected decoded PHP source text")
    if len(source) > _MAX_FILE:
        raise UnsupportedPhp("source byte budget exceeded")
    try:
        size = len(source.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise UnsupportedPhp("source contains invalid Unicode") from error
    if size > _MAX_FILE:
        raise UnsupportedPhp("source byte budget exceeded")
    text = _function_range(source, name, start_line, end_line)
    if len(text.encode("utf-8")) > _MAX_RANGE:
        raise UnsupportedPhp("function byte budget exceeded", start_line)
    params, return_type, body = _FunctionParser(_region_tokens(text, start_line)).function(name)
    return {"name": name, "start_line": start_line, "end_line": end_line,
            "params": params, "return_type": return_type, "body": body}
