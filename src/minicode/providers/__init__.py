"""模型适配器：中立消息模型 ↔ 各厂商协议。"""

from __future__ import annotations

from ..config import Settings
from .base import Provider, ProviderError, RetryableError

__all__ = ["Provider", "ProviderError", "RetryableError", "make_provider"]


def make_provider(settings: Settings) -> Provider:
    if settings.provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(model=settings.model, api_key=settings.anthropic_api_key)
    if settings.provider == "openai":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            model=settings.model, api_key=settings.openai_api_key, base_url=settings.openai_base_url
        )
    from .mock import ScriptedProvider

    if settings.mock_script:
        return ScriptedProvider.from_file(settings.mock_script, model=settings.model)
    return ScriptedProvider([], model=settings.model)
