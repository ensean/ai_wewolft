from __future__ import annotations
import asyncio
import logging
from typing import Optional

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)


class BedrockClient:
    """
    Async wrapper around boto3 bedrock-runtime Converse API.
    Supports any model available on Bedrock (Claude, Nova, Llama, etc.)
    via the unified Converse interface.
    boto3 is synchronous, so calls are offloaded to a thread-pool executor.
    """

    def __init__(
        self,
        model_id: str,
        region: str = "us-east-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ) -> None:
        self.model_id = model_id

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

    async def converse(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
    ) -> str:
        """
        Call the Bedrock Converse API asynchronously.
        messages: [{"role": "user"|"assistant", "content": "..."}]
        Returns the assistant's text reply.
        """
        converse_messages = []
        for m in messages:
            converse_messages.append({
                "role": m["role"],
                "content": [{"text": m["content"]}],
            })

        system_block = [{"text": system}] if system else []

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
            logger.error("Bedrock converse error (model=%s): %s", self.model_id, e)
            raise

        return response["output"]["message"]["content"][0]["text"]
