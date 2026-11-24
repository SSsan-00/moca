import json
import re


class DuplicateJsonKey(ValueError):
    pass


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey("duplicate JSON key")
        result[key] = value
    return result


def load_unique_json(text: str):
    return json.loads(text, object_pairs_hook=_unique_keys)


def unwrap_json_fence(text: str) -> str:
    fence = re.fullmatch(r"```(?:json)?(?:\r\n?|\n)(.*?)(?:\r\n?|\n)```", text.strip(), re.DOTALL)
    return fence[1] if fence else text
