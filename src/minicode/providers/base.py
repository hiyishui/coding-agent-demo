"""模型适配器接口与错误类型（设计文档第 5 节）。

适配器只做两件事：把中立消息翻译成厂商请求、把厂商响应翻译回中立消息。
重试统一在 agent 层做，适配器自身不重试（SDK 自带的重试设为 0），
这样退避策略、预算和日志只有一处实现。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from ..messages import Message, ModelResponse, ToolSpec

TextCallback = Callable[[str], None]


class ProviderError(Exception):
    """不可重试的模型调用错误（认证失败、请求格式错误等）。"""


class RetryableError(ProviderError):
    """可重试的错误：429、5xx、超时、连接错误。retry_after 来自服务端提示（秒）。"""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Provider(Protocol):
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        on_text: TextCallback | None = None,
    ) -> ModelResponse: ...


def classify_http_error(status: int | None, headers: Mapping[str, str] | None, message: str) -> ProviderError:
    """按 HTTP 状态码把错误分成“可重试”和“不可重试”两类。status 为 None 表示连接层错误。"""
    if status is None or status == 408 or status == 429 or status >= 500:
        return RetryableError(message, retry_after=_parse_retry_after(headers))
    return ProviderError(message)


def _parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:  # HTTP 日期格式的 Retry-After 这里不解析，交给指数退避
        return None
