"""Optional Shadow Platform LLM SDK bridge.

The SDK runs in this process and calls providers directly. It receives Foliant-owned request bodies,
while its usage sink emits metadata only. Existing llm_router providers remain the fallback.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any


class PlatformLLMUnavailable(RuntimeError):
    pass


_clients: dict[str, tuple[Any, Any]] = {}
_lock = threading.Lock()


def configured() -> bool:
    enabled = os.getenv("SHADOW_LLM_ENABLED", "").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return False
    return bool(
        os.getenv("SHADOW_LLM_REGISTRY_FILE", "").strip()
        and os.getenv("SHADOW_PLATFORM_SECRETS_DIR", "").strip()
    )


def call(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    thinking: bool,
    call_type: str,
    single_attempt: bool = False,
    timeout: float | None = None,
) -> tuple[str, str]:
    if not configured():
        raise PlatformLLMUnavailable("Shadow LLM is not configured")
    alias = os.getenv(
        "SHADOW_LLM_REASONING_ALIAS" if thinking else "SHADOW_LLM_CHAT_ALIAS",
        "reasoning-default" if thinking else "chat-default",
    ).strip()
    client, config = _client(alias)
    if single_attempt:
        # Registry fallbacks and the SDK's retry policy must not multiply a
        # workflow's durable one-call reservation. Do not mutate shared clients.
        client = _single_attempt_client(config, timeout=timeout)
    payload = _provider_payload(
        config.api,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        thinking=thinking,
    )
    try:
        response = client.create(agent_id=_agent_id(call_type), **payload)
        text = _extract_text(config.api, response, thinking=thinking)
        if not text:
            raise PlatformLLMUnavailable("Shadow LLM returned an empty response")
        model = str(response.get("model") or config.model)
        return text, f"shadow:{alias}:{model}"
    finally:
        if single_attempt:
            client.close()


def _single_attempt_client(config, *, timeout):
    from dataclasses import replace
    from shadow_sdk import JsonlUsageSink, LLMClient, NullUsageSink, RetryPolicy
    seconds = min(config.timeout_seconds, max(1, int(timeout or 30)))
    bounded = replace(config, timeout_seconds=seconds, fallbacks=())
    outbox = os.getenv("SHADOW_LLM_USAGE_OUTBOX", "").strip()
    sink = JsonlUsageSink(outbox) if outbox else NullUsageSink()
    return LLMClient([bounded], usage_sink=sink, retry_policy=RetryPolicy(max_retries=0))


def close_clients() -> None:
    with _lock:
        values = list(_clients.values())
        _clients.clear()
    for client, _ in values:
        try:
            client.close()
        except Exception:
            pass


def _client(alias: str) -> tuple[Any, Any]:
    cached = _clients.get(alias)
    if cached is not None:
        return cached
    with _lock:
        cached = _clients.get(alias)
        if cached is not None:
            return cached
        try:
            from shadow_sdk import (
                JsonlUsageSink,
                LLMClient,
                NullUsageSink,
                resolve_llm_config,
            )
        except ImportError as exc:
            raise PlatformLLMUnavailable("Shadow LLM SDK is unavailable") from exc
        registry = os.getenv("SHADOW_LLM_REGISTRY_FILE", "").strip()
        secrets_dir = os.getenv("SHADOW_PLATFORM_SECRETS_DIR", "").strip()
        outbox = os.getenv("SHADOW_LLM_USAGE_OUTBOX", "").strip()
        sink = JsonlUsageSink(outbox) if outbox else NullUsageSink()
        try:
            config = resolve_llm_config(
                registry,
                secrets_dir=secrets_dir,
                app_id="foliant",
                alias=alias,
            )
            client = LLMClient.from_registry(
                registry,
                secrets_dir=secrets_dir,
                app_id="foliant",
                alias=alias,
                usage_sink=sink,
            )
        except Exception as exc:
            raise PlatformLLMUnavailable("Shadow LLM initialization failed") from exc
        _clients[alias] = (client, config)
        return client, config


def _provider_payload(
    api: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    thinking: bool,
) -> dict[str, Any]:
    if api == "chat-completions":
        payload: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
        if not thinking:
            payload["temperature"] = temperature
        return payload
    systems = [item.get("content", "") for item in messages if item.get("role") == "system"]
    non_system = [item for item in messages if item.get("role") != "system"]
    if api == "responses":
        payload = {"input": non_system, "max_output_tokens": max_tokens}
        if systems:
            payload["instructions"] = "\n\n".join(systems)
        if not thinking:
            payload["temperature"] = temperature
        return payload
    payload = {"messages": non_system, "max_tokens": max_tokens}
    if systems:
        payload["system"] = "\n\n".join(systems)
    if not thinking:
        payload["temperature"] = temperature
    return payload


def _extract_text(api: str, response: dict[str, Any], *, thinking: bool) -> str:
    if api == "chat-completions":
        choices = response.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or "")
        if thinking and reasoning:
            return f"【推理过程】\n{reasoning}\n\n{content}".strip()
        return content.strip()
    if api == "responses":
        direct = response.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        chunks = []
        for output in response.get("output") or []:
            for item in output.get("content") or []:
                if item.get("type") in {"output_text", "text"} and item.get("text"):
                    chunks.append(str(item["text"]))
        return "\n".join(chunks).strip()
    chunks = []
    for item in response.get("content") or []:
        if item.get("type") == "text" and item.get("text"):
            chunks.append(str(item["text"]))
    return "\n".join(chunks).strip()


def _agent_id(call_type: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", str(call_type).lower()).strip("-")
    if not value or not value[0].isalpha():
        value = "misc"
    return ("foliant-" + value)[:64].rstrip("-")
