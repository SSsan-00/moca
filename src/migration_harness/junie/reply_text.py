import re


def unwrap_json_fence(text: str) -> str:
    fence = re.fullmatch(r"```(?:json)?(?:\r\n?|\n)(.*?)(?:\r\n?|\n)```", text.strip(), re.DOTALL)
    return fence[1] if fence else text
