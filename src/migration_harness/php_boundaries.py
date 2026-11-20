from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
import re

from .execution import check_cancelled


_MAX_BYTES = 4 * 1024 * 1024
_OPEN = re.compile(r"<\?php(?=\s|$)", re.IGNORECASE)
_TOKENS = re.compile(
    r"/\*|//|\#(?!\[)|<<<|<\?|\?>|[\x27\x22`{}()[\];]|"
    r"[a-zA-Z_\u0080-\U0010ffff][\w\u0080-\U0010ffff]*"
)
_LINE_END = re.compile(r"\r\n|\r|\n")
_BYTE_LINE_END = re.compile(rb"\r\n|\r|\n")
_IDENTIFIER = re.compile(r"[a-zA-Z_\u0080-\U0010ffff][\w\u0080-\U0010ffff]*")
_DECLARATION_PREFIX = re.compile(
    r"[ \t]*(?:(?:public|protected|private|static|final|abstract)[ \t]+)*", re.IGNORECASE,
)


@dataclass(frozen=True)
class PhpFunction:
    name: str
    start_line: int
    end_line: int


def function_boundaries(
    path: Path, encoding: str, *, through_line: int | None = None,
) -> tuple[tuple[PhpFunction, ...], str]:
    with path.open("rb") as stream:
        data = stream.read(_MAX_BYTES)
        bounded = bool(stream.read(1))
    partial = False
    if bounded:
        if through_line is None:
            return (), "static scan byte limit"
        # Start at the file's opening tag, never from an unknown lexical state.
        boundary = next(islice(_BYTE_LINE_END.finditer(data), through_line - 1, None), None)
        if boundary is None or boundary.end() == len(data):
            return (), "static scan byte limit before requested lines"
        data = data[:boundary.end()]
        partial = True
    text = data.decode(encoding)
    opening = _OPEN.match(text, int(text.startswith("\ufeff")))
    if opening is None:
        return (), "not a standalone PHP document"
    try:
        tokens = _tokens(text, opening.end())
        return _functions(text, tokens, partial=partial), (
            "named functions, bounded source prefix only" if partial else "named functions, lexical boundaries only"
        )
    except ValueError as exc:
        return (), str(exc)


def _tokens(text: str, offset: int) -> list[tuple[str, int, int]]:
    tokens = []
    quotes = {
        quote: re.compile(re.escape(quote) + r"(?:\\[\s\S]|[^\\" + re.escape(quote) + r"])*" + re.escape(quote))
        for quote in ("'", '"', "`")
    }
    while match := _TOKENS.search(text, offset):
        word = match[0]
        start, offset = match.span()
        if len(tokens) % 256 == 0:
            check_cancelled()
        if word == "/*":
            end = text.find("*/", offset)
            if end < 0:
                raise ValueError("unterminated PHP block comment")
            offset = end + 2
        elif word in ("//", "#"):
            line_end = _LINE_END.search(text, offset)
            offset = line_end.start() if line_end else len(text)
            if "?>" in text[start:offset]:
                raise ValueError("mixed PHP and markup")
        elif word in quotes:
            quoted = quotes[word].match(text, start)
            if quoted is None:
                raise ValueError("unterminated PHP string")
            offset = quoted.end()
            if word in ('"', "`") and "{" in quoted[0] and "$" in quoted[0]:
                raise ValueError("complex PHP interpolation")
        elif word == "<<<":
            raise ValueError("heredoc/nowdoc or unsupported PHP syntax")
        elif word in ("<?", "?>"):
            if word == "?>" and not text[offset:].strip():
                break
            raise ValueError("mixed PHP and markup")
        else:
            if len(tokens) >= 262_144:
                raise ValueError("static scan token limit")
            tokens.append((word, start, offset))
    return tokens


def _functions(text: str, tokens: list[tuple[str, int, int]], *, partial: bool = False) -> tuple[PhpFunction, ...]:
    pairs: dict[int, int] = {}
    stack: list[int] = []
    matching = {")": "(", "]": "[", "}": "{"}
    for index, (token, _, _) in enumerate(tokens):
        if token in "([{":
            stack.append(index)
        elif token in matching:
            if not stack or tokens[stack[-1]][0] != matching[token]:
                raise ValueError("unbalanced PHP delimiters")
            pairs[stack.pop()] = index
    if stack and not partial:
        raise ValueError("unbalanced PHP delimiters")
    line_starts = [0, *(end.end() for end in _LINE_END.finditer(text))]
    functions = []
    covered_until = 0
    for index, (token, offset, end) in enumerate(tokens[:-1]):
        if offset < covered_until or token.lower() != "function":
            continue
        line = bisect_right(line_starts, offset)
        if not _DECLARATION_PREFIX.fullmatch(text[line_starts[line - 1]:offset]):
            continue
        name, name_start, _ = tokens[index + 1]
        if not _IDENTIFIER.fullmatch(name) or not re.fullmatch(r"\s*&?\s*", text[end:name_start]):
            continue
        opening = index + 2
        if opening >= len(tokens) or tokens[opening][0] != "(":
            continue
        if opening not in pairs:
            break
        body = pairs[opening] + 1
        while body < len(tokens) and tokens[body][0] not in ("{", ";", "}", "function"):
            body += 1
        if body == len(tokens) or tokens[body][0] != "{":
            continue
        if body not in pairs:
            break
        _, closing, covered_until = tokens[pairs[body]]
        end_line = bisect_right(line_starts, closing)
        following = line_starts[end_line] if end_line < len(line_starts) else len(text)
        if not text[covered_until:following].strip():
            functions.append(PhpFunction(name, line, end_line))
    return tuple(functions)
