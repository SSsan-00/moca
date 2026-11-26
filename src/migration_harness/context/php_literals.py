"""Read-only helpers for migrating PHP literals and simple C# identifiers.

This is not a PHP parser: identify executable statements and control flow first.
Dynamic strings and encoding-dependent byte escapes require source inspection.
PHP semantics: https://www.php.net/manual/en/language.types.string.php
The file is also copied into the agent workspace, so use only the standard library.
"""

from collections.abc import Iterable
import json
from pathlib import Path
import re


# An escaped quote belongs to the token; a lazy quote-to-quote regex loses data.
STRING_LITERAL = r"""(?:'(?:\\[\s\S]|[^'\\])*'|"(?:\\[\s\S]|[^"\\])*")"""


_CSHARP_RESERVED = frozenset("""
abstract as base bool break byte case catch char checked class const continue decimal default delegate
do double else enum event explicit extern false finally fixed float for foreach goto if implicit in int
interface internal is lock long namespace new null object operator out override params private protected
public readonly ref return sbyte sealed short sizeof stackalloc static string struct switch this throw
true try typeof uint ulong unchecked unsafe ushort using virtual void volatile while
__arglist __makeref __reftype __refvalue
""".split())


def csharp_identifier(name: str, *, internal_base: str | None = None, occupied: Iterable[str] = ()) -> str:
    """Spell one simple identifier, not source text. Exclude this declaration from occupied names."""
    if not re.fullmatch(r"@?[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError("expected one simple C# identifier without a PHP $, not an expression or key")
    bare = name.removeprefix("@")
    used = {other.removeprefix("@") for other in occupied}
    if name.startswith("@") or bare not in _CSHARP_RESERVED or internal_base is None:
        if bare in used:
            raise ValueError("this name is already bound; inspect the declaration and its callers")
        return "@" + bare if bare in _CSHARP_RESERVED else name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", internal_base) or internal_base in _CSHARP_RESERVED:
        raise ValueError("choose an unreserved internal name that describes the declaration")
    candidate = internal_base
    suffix = 2
    while candidate in used:
        candidate = f"{internal_base}{suffix}"
        suffix += 1
    return candidate


def csharp_literal(value: str | int | bool | None) -> str:
    """Encode a decoded primitive, never PHP/Python spelling or a C# expression."""
    if type(value) not in (str, int, bool, type(None)):
        raise ValueError("expected a string, signed 64-bit integer, boolean or null; inspect other types")
    if type(value) is int and not -(2**63) <= value < 2**63:
        raise ValueError("integer is outside the signed 64-bit range")
    # ASCII escapes also keep Unicode line separators inside C# string literals.
    return json.dumps(value, ensure_ascii=True)


def decode_literal(literal: str) -> str:
    """Decode one complete quoted literal; never evaluate PHP or guess a value."""
    if re.fullmatch(STRING_LITERAL, literal) is None:
        raise ValueError("expected one complete quoted PHP literal")
    single = literal[0] == "'"
    body = literal[1:-1]
    output = []
    index = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "v": "\v", "e": "\x1b", "f": "\f",
               "\\": "\\", "$": "$", '"': '"'}
    while index < len(body):
        char = body[index]
        if char != "\\":
            if not single and char == "$":
                raise ValueError("possible PHP interpolation; inspect the original expression")
            output.append(char)
            index += 1
            continue
        char = body[index + 1]  # A final unpaired backslash was rejected by fullmatch.
        index += 2
        if single:
            output.append(char if char in {"\\", "'"} else "\\" + char)
        elif char in escapes:
            output.append(escapes[char])
        elif char == "u" and body[index:index + 1] == "{":
            match = re.match(r"\{([0-9a-fA-F]+)\}", body[index:])
            if match is None:
                raise ValueError("invalid PHP Unicode escape")
            value = int(match[1], 16)
            if value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                raise ValueError("Unicode escape is not a Unicode scalar value")
            output.append(chr(value))
            index += len(match[0])
        else:
            if char == "x":
                match = re.match(r"[0-9a-fA-F]{1,2}", body[index:])
                digits = match[0] if match else ""
                base = 16
            elif char in "01234567":
                match = re.match(r"[0-7]{0,2}", body[index:])
                digits = char + match[0]
                base = 8
            else:
                digits = ""
            if not digits:
                output.append("\\" + char)
                continue
            value = int(digits, base) % 256
            if value > 127:
                raise ValueError("non-ASCII byte escape needs the original encoding; do not guess")
            output.append(chr(value))
            index += len(digits) - (char != "x")
    return "".join(output)


def extract_appends(statements: Iterable[str], variable: str) -> list[str]:
    """Extract literal array append statements in order, keeping duplicates.

Pass unnumbered, executable source statements in the assigned range. Handle all
other statements separately; this helper does not infer their runtime effects.
Unsupported assignments to this array raise instead of silently losing rows.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", variable) is None:
        raise ValueError("expected a simple PHP variable name without $")
    prefix = rf"\s*\${variable}\s*\["
    assignment = re.compile(prefix + rf"\s*\]\s*=\s*({STRING_LITERAL})\s*;\s*")
    values = []
    for statement in statements:
        if re.match(prefix, statement) is None:
            continue
        match = assignment.fullmatch(statement)
        if match is None:
            raise ValueError("unsupported or incomplete array assignment: " + statement[:120])
        values.append(decode_literal(match[1]))
    if not values:
        raise ValueError("no complete literal append statements found")
    return values


def capsule_appends(path: str, variable: str) -> list[str]:
    """Read only the capsule's complete, numbered PHP excerpt, never boundary.md.

Character slices, multiple PHP excerpts and incomplete rows require inspecting
the original statements instead of combining uncertain ranges automatically.
    """
    text = Path(path).read_text(encoding="utf-8")
    headings = list(re.finditer(r"(?m)^## .*", text))
    excerpts = []
    for index, heading in enumerate(headings):
        match = re.fullmatch(r"## php — \x60[^\x60]+\x60 lines (\d+)-(\d+)", heading[0])
        if match:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            excerpts.append((int(match[1]), int(match[2]), text[heading.end():end]))
    if len(excerpts) != 1:
        raise ValueError("expected exactly one complete PHP line excerpt; do not use boundary context")
    first, last, source = excerpts[0]
    statements = []
    for line in source.split("\n"):
        if line in {"", "```text", "```"}:
            continue
        row = re.fullmatch(r"\s*(\d+) \| (.*)", line)
        if row is None or int(row[1]) != first + len(statements) or int(row[1]) > last:
            raise ValueError("incomplete, sliced or out-of-order PHP source row")
        statements.append(row[2])
    if first + len(statements) != last + 1:
        raise ValueError("PHP excerpt is missing source rows")
    return extract_appends(statements, variable)
