from __future__ import annotations

from dataclasses import dataclass


# Junie ModelAlias / ModelOption setting IDs. Only use this table for the verified releases.
_JUNIE_ALIASES = {
    "gpt": "gpt-6.1-sol", "gpt-sol": "gpt-6.1-sol", "gpt-codex": "gpt-5.3-codex",
    "gpt-astra": "gpt-6-astra", "gpt-luna": "gpt-6-luna", "gpt-terra": "gpt-5.6-terra",
    "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "fable": "claude-fable-5-1",
    "gemini-pro": "gemini-3.1-pro-preview", "gemini-flash": "gemini-3.8-flash", "grok": "grok-4.7",
}


class ModelSelectionError(ValueError):
    pass


class ModelMismatch(ModelSelectionError):
    pass


def config_option(session: dict, category: str, ids: tuple[str, ...]) -> dict | None:
    options = session.get("configOptions")
    if not isinstance(options, list):
        return None
    matches = [option for option in options if isinstance(option, dict)
               and (option.get("category") == category or option.get("id") in ids)]
    if len(matches) > 1:
        raise ModelSelectionError(f"Junie ACP が複数の {category} 設定を返したため、選択を確認できません。")
    return matches[0] if matches else None


def acp_effort_option(session: dict) -> dict | None:
    return config_option(session, "thought_level", ("thought_level", "effort"))


def _setting_id(value: str) -> str:
    if value.startswith("v1:"):
        # GroupedModelConfigValue prefixes the provider's UTF-16 length. Both IDs may contain colons.
        length, sep, body = value[3:].partition(":")
        if sep and length.isascii() and length.isdecimal() and 0 < len(length) <= 10:
            end = int(length) * 2
            encoded = body.encode("utf-16-le", errors="surrogatepass")
            if 0 < end < len(encoded) - 2 and encoded[end:end + 2] == b":\0":
                return encoded[end + 2:].decode("utf-16-le", errors="surrogatepass")
    return value


def _choices(option: dict):
    for choice in option.get("options") or []:
        if isinstance(choice, dict):
            if "group" in choice:
                yield from (item.get("value") for item in choice.get("options", []) if isinstance(item, dict))
            else:
                yield choice.get("value")


@dataclass(frozen=True)
class RequestedModel:
    model: str | None
    agent_version: str | None

    def matches(self, value) -> bool:
        if not self.model or not isinstance(value, str):
            return False
        setting = _setting_id(value)
        if self.model == value:
            return True
        if self.model.startswith("v1:"):
            return False
        if self.model == setting:
            return True
        return (self.agent_version in ("26.11.1 (4020.10)", "26.11.2 (4100.11)")
                and setting == _JUNIE_ALIASES.get(self.model))

    def check(self, info: dict, *, require_current: bool = False) -> None:
        if not self.model:
            return
        option = config_option(info, "model", ("model",))
        current = option.get("currentValue") if option else None
        legacy = info.get("models")
        legacy_id = legacy.get("currentModelId") if isinstance(legacy, dict) else None
        if require_current and current is None and not legacy_id:
            raise self.mismatch("未確認")
        if current is not None and not self.matches(current):
            raise self.mismatch(current)
        if legacy_id and not self.matches(legacy_id):
            if not isinstance(current, str) or _setting_id(current) != legacy_id:
                raise self.mismatch(legacy_id)

    def mismatch(self, value) -> ModelMismatch:
        return ModelMismatch(f"Junie ACP のモデルが指定と異なります。指定 {self.model}、応答 {value}。"
                             "モデル一覧のIDと利用資格を確認してください。")

    def change_for(self, session: dict, effort: str | None) -> ModelChange | None:
        if not self.model:
            return None
        try:
            self.check(session, require_current=True)
            return None
        except ModelMismatch:
            option = config_option(session, "model", ("model",))
            if option is not None:
                matches = {value for value in _choices(option) if self.matches(value)}
                if len(matches) != 1 or not isinstance(option.get("id"), str) or not option["id"]:
                    raise
                params = {"configId": option["id"], "value": matches.pop()}
                return ModelChange(self, "session/set_config_option", params, session)

            models = session.get("models")
            if models is not None and not isinstance(models, dict):
                raise
            catalog = (models or {}).get("availableModels")
            if isinstance(catalog, list) and catalog:
                matches = {item["modelId"] for item in catalog
                           if isinstance(item, dict) and self.matches(item.get("modelId"))}
                if len(matches) != 1:
                    raise
                model_id = matches.pop()
                # A legacy setter may acknowledge with {}. The previous model and its effort predate the switch.
                pending = {key: value for key, value in session.items() if key != "models"}
                if effort and (selector := acp_effort_option(session)):
                    pending["configOptions"] = [dict(opt, currentValue=None) if opt is selector else opt
                                                for opt in session["configOptions"]]
            elif (self.model == "junie-lite" and effort in (None, "none")
                  and catalog in (None, []) and not (models or {}).get("currentModelId")
                  and (session.get("configOptions") is None or isinstance(session["configOptions"], list))):
                # Junie 4100.11 can advertise no models until Lite is set on the session.
                model_id = self.model
                pending = session
            else:
                raise
            return ModelChange(self, "session/set_model", {"modelId": model_id}, pending)


@dataclass
class ModelChange:
    requested: RequestedModel
    method: str
    params: dict
    session: dict

    def confirmed(self, message: dict) -> dict:
        result = message.get("result")
        if self.method == "session/set_model":
            if "error" in message or "result" not in message or (result is not None and not isinstance(result, dict)):
                raise ModelSelectionError(f"Junie ACP でモデル {self.requested.model} を選択できませんでした。")
            info = {**self.session, **(result or {})}
        else:
            info = result if isinstance(result, dict) else {}
            option = config_option(info, "model", ("model",))
            if ("error" in message or not option or option.get("id") != self.params["configId"]
                    or option.get("currentValue") != self.params["value"]):
                raise ModelSelectionError(f"Junie ACP でモデル {self.requested.model} の設定を確認できませんでした。")
        self.requested.check(info)
        return info
