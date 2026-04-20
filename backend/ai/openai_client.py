from __future__ import annotations
import logging
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Model-ID prefix → (provider name, base_url)
_PROVIDERS: list[tuple[str, str, str]] = [
    ("moonshot",  "Kimi",     "https://api.moonshot.cn/v1"),
    ("deepseek",  "DeepSeek", "https://api.deepseek.com"),
    ("glm",       "GLM",      "https://open.bigmodel.cn/api/paas/v4"),
    ("chatglm",   "GLM",      "https://open.bigmodel.cn/api/paas/v4"),
    ("minimax-",  "MiniMax",  "https://api.minimax.chat/v1"),
    ("abab",      "MiniMax",  "https://api.minimax.chat/v1"),
]


def detect_provider(model_id: str) -> tuple[str, str] | None:
    """Return (provider_name, base_url) if model_id matches a known OpenAI-compatible provider."""
    lower = model_id.lower()
    for prefix, name, url in _PROVIDERS:
        if lower.startswith(prefix.lower()):
            return name, url
    return None


class OpenAICompatibleClient:
    """
    Async client for Kimi / DeepSeek / MiniMax / GLM — all expose
    an OpenAI-compatible /chat/completions endpoint.
    """

    def __init__(self, model_id: str, api_key: str, base_url: str) -> None:
        self.model_id = model_id
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def converse(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
    ) -> str:
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        try:
            resp = await self._client.chat.completions.create(
                model=self.model_id,
                messages=full_messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            logger.error("OpenAI-compatible converse error (model=%s): %s", self.model_id, e)
            raise
