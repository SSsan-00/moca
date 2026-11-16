"""Read-only helpers for Junie's extraction of plain PHP literal statements.

This is not a PHP parser: identify executable statements and control flow first.
Dynamic strings and encoding-dependent byte escapes require source inspection.
PHP semantics: https://www.php.net/manual/en/language.types.string.php
The file is also copied into the agent workspace, so use only the standard library.
"""

from collections.abc import Iterable
import re


# An escaped quote belongs to the token; a lazy quote-to-quote regex loses data.
STRING_LITERAL = r"""(?:'(?:\\[\s\S]|[^'\\])*'|"(?:\\[\s\S]|[^"\\])*")"""


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
