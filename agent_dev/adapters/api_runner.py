"""Bounded, text-only provider HTTP contracts; no model-generated tools are executed."""

import http.client
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

from agent_dev.adapters.ai_providers import API_PROVIDERS, endpoint, normalize
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.model_routing import select_model

LIMIT = 2_000_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiRunnerAdapter:
    def __init__(self, provider, config):
        if provider not in API_PROVIDERS:
            raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, "Unsupported API provider.")
        self.provider = provider
        self.environment = dict(config.get("process_env", os.environ))
        public = {k: v for k, v in config.items() if k != "process_env"}
        self.config = normalize(provider, dict(public, enabled=True))

    def generate(self, prompt, task, phase, timeout=900):
        selection = dict(select_model(self.config, task, phase), provider=self.provider)
        model, effort = selection["model"], selection["effort"]
        key_name = self.config["credential_ref"]
        key = self.environment.get(key_name, "") if key_name else ""
        if key_name and not key:
            raise DomainError(ErrorCode.AUTH_REQUIRED, "설정한 API 인증 환경변수 값이 필요합니다.")
        if any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise DomainError(ErrorCode.INVALID_INPUT, "API 인증 값 형식을 확인해주세요.")
        base = endpoint(self.config["base_url"])
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        tokens = self.config["max_output_tokens"]
        if self.provider == "openai_api":
            url, body = (
                base + "/responses",
                {"model": model, "input": prompt, "max_output_tokens": tokens, "store": False},
            )
            if effort:
                body["reasoning"] = {"effort": effort}
        elif self.provider == "anthropic_api":
            url = base + "/messages"
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            body = {
                "model": model,
                "max_tokens": tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if effort:
                body["output_config"] = {"effort": effort}
        elif self.provider == "gemini_api":
            url = (
                base
                + "/models/"
                + quote(model.removeprefix("models/"), safe="")
                + ":generateContent"
            )
            headers["x-goog-api-key"] = key
            body = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": tokens},
            }
            if effort:
                body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": effort.upper()}
        else:
            url = base + "/chat/completions"
            body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": tokens,
                "stream": False,
            }
            if effort:
                body["reasoning_effort"] = effort
        # xAI (Grok) serves the OpenAI chat-completions contract.
        if self.provider in {"openai_api", "xai_api", "compatible_api"} and key:
            headers["Authorization"] = "Bearer " + key
        raw = json.dumps(body, ensure_ascii=False).encode()
        if len(raw) > LIMIT:
            raise DomainError(ErrorCode.INVALID_INPUT, "AI 요청 크기를 줄여주세요.")
        request = urllib.request.Request(url, data=raw, headers=headers, method="POST")
        try:
            # No ambient proxy or redirects may receive project credentials.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=max(1, min(timeout, 900))) as response:
                result = response.read(LIMIT + 1)
            if len(result) > LIMIT:
                raise DomainError(ErrorCode.INVALID_INPUT, "AI 응답 크기가 제한을 초과했습니다.")
            payload = json.loads(result)
        except urllib.error.HTTPError as exc:
            code = {
                401: ErrorCode.AUTH_REQUIRED,
                403: ErrorCode.AUTH_REQUIRED,
                429: ErrorCode.CAPACITY_EXHAUSTED,
            }.get(exc.code)
            code = code or (
                ErrorCode.EXTERNAL_TRANSIENT
                if exc.code >= 500
                else ErrorCode.CAPABILITY_UNAVAILABLE
            )
            exc.close()
            raise DomainError(
                code, "AI API 요청에 실패했습니다. 인증·모델·지원 옵션·사용 한도를 확인해주세요."
            ) from None
        except (TimeoutError, OSError, urllib.error.URLError, http.client.HTTPException):
            raise DomainError(
                ErrorCode.EXTERNAL_TRANSIENT, "AI API 연결에 실패하거나 제한시간을 초과했습니다."
            ) from None
        except (ValueError, UnicodeError):
            raise DomainError(
                ErrorCode.INVALID_INPUT, "AI API가 올바른 JSON을 반환하지 않았습니다."
            ) from None
        text, usage = self.decode(payload)
        return PhaseResult(text, self.provider, usage, selection)

    def decode(self, data):
        try:
            if self.provider == "openai_api":
                if data.get("status") != "completed" or data.get("error"):
                    raise ValueError("Incomplete response")
                parts = [
                    part["text"]
                    for item in data["output"]
                    if item.get("type") == "message"
                    for part in item["content"]
                    if part.get("type") == "output_text"
                ]
                usage = data.get("usage", {})
                counters = (usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            elif self.provider == "anthropic_api":
                if data.get("stop_reason") not in {"end_turn", "stop_sequence"}:
                    raise ValueError("Incomplete response")
                parts = [part["text"] for part in data["content"] if part["type"] == "text"]
                usage = data.get("usage", {})
                counters = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0),
                    usage.get("output_tokens", 0),
                )
            elif self.provider == "gemini_api":
                candidate = data["candidates"][0]
                if candidate.get("finishReason") != "STOP":
                    raise ValueError("Incomplete response")
                parts = [
                    part["text"]
                    for part in candidate["content"]["parts"]
                    if "text" in part and not part.get("thought")
                ]
                usage = data.get("usageMetadata", {})
                counters = (
                    usage.get("promptTokenCount", 0),
                    usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
                )
            else:
                choice = data["choices"][0]
                if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
                    raise ValueError("Incomplete response")
                parts = [choice["message"]["content"]]
                usage = data.get("usage", {})
                counters = (usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            text = "\n".join(parts).strip()
            if not text or len(text) > LIMIT or any(type(n) is not int or n < 0 for n in counters):
                raise ValueError("Invalid content")
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                "AI 응답이 비어 있거나 잘림·차단·미지원 형식입니다. 모델과 출력 토큰 상한을 확인해주세요.",
            ) from None
        return text, {
            "input_tokens": counters[0],
            "output_tokens": counters[1],
            "total_tokens": sum(counters),
        }

    def execute(self, request):
        if not request.capabilities <= {"text.generate"}:
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "API 모델은 text.generate 단계만 실행합니다. 파일 읽기·수정에는 개발 CLI가 필요합니다.",
            )
        return self.generate(
            request.context, request.task.definition, request.phase_id, request.timeout_seconds
        )


