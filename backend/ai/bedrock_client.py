from __future__ import annotations
import asyncio
import logging
from typing import Optional

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)


def _supports_prompt_cache(model_id: str) -> bool:
    """Prompt caching is supported for Anthropic Claude + Amazon Nova on Bedrock."""
    m = model_id.lower()
    return "anthropic" in m or "claude" in m or ".nova-" in m


class BedrockClient:
    """
    Async wrapper around boto3 bedrock-runtime Converse API.
    Uses prompt caching (where supported) to cut cost on repeated system prompts.
    """

    def __init__(
        self,
        model_id: str,
        region: str = "us-east-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ) -> None:
        self.model_id = model_id
        self.cache_enabled = _supports_prompt_cache(model_id)

        session_kwargs: dict = {"region_name": region}
        if aws_access_key_id and aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = aws_access_key_id
            session_kwargs["aws_secret_access_key"] = aws_secret_access_key

        session = boto3.Session(**session_kwargs)
        self._client = session.client(
            "bedrock-runtime",
            config=Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
                read_timeout=120,
                connect_timeout=10,
            ),
        )
        # Aggregate cache stats across all calls by this client
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    async def converse(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
    ) -> str:
        converse_messages = [
            {"role": m["role"], "content": [{"text": m["content"]}]}
            for m in messages
        ]

        # Build system block with cachePoint at end for token caching.
        # Bedrock caches everything BEFORE the cachePoint — the fixed system
        # prompt — so repeated calls reuse cached tokens.
        system_block: list = []
        if system:
            system_block.append({"text": system})
            if self.cache_enabled:
                system_block.append({"cachePoint": {"type": "default"}})

        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self._client.converse(
                    modelId=self.model_id,
                    messages=converse_messages,
                    system=system_block,
                    inferenceConfig={
                        "maxTokens": max_tokens,
                        "temperature": temperature,
                    },
                ),
            )
        except Exception as e:
            # If caching caused the error (some models don't support it), retry without cache
            if self.cache_enabled and "cachePoint" in str(e):
                logger.warning("Disabling prompt cache for %s: %s", self.model_id, e)
                self.cache_enabled = False
                return await self.converse(messages, system, max_tokens, temperature)
            logger.error("Bedrock converse error (model=%s): %s", self.model_id, e)
            raise

        # Track usage
        usage = response.get("usage", {}) or {}
        self.total_input_tokens  += usage.get("inputTokens", 0)
        self.total_output_tokens += usage.get("outputTokens", 0)
        self.cache_read_tokens   += usage.get("cacheReadInputTokens", 0)
        self.cache_write_tokens  += usage.get("cacheWriteInputTokens", 0)

        return response["output"]["message"]["content"][0]["text"]
