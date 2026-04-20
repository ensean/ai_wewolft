"""
Fetch available Bedrock models by combining:
  1. list_foundation_models   (Amazon, Meta, Mistral, MiniMax, DeepSeek, etc.)
  2. list_inference_profiles  (Claude via cross-region profiles us.anthropic.*)

Deduplication: for inference profiles, keep only the LATEST version per model
family so the user sees e.g. "Claude Sonnet 4.6" and not also 4.5 / 3.7.
"""

from __future__ import annotations
import asyncio
import logging
import re
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

_SKIP_KEYWORDS = (
    "embed", "rerank", "pegasus", "encode", "diffus",
    "safeguard",       # content-moderation models
    "upscale", "inpaint", "erase", "outpaint", "background",  # image ops
    "style-guide", "style-transfer", "style guide", "style transfer",
)

# Normalize provider names extracted from profile IDs
_PROVIDER_MAP: dict[str, str] = {
    "anthropic":  "Anthropic",
    "amazon":     "Amazon",
    "meta":       "Meta",
    "deepseek":   "DeepSeek",
    "mistral":    "Mistral AI",
    "moonshot":   "Moonshot AI",
    "moonshotai": "Moonshot AI",
    "writer":     "Writer",
    "stability":  "Stability AI",
    "nvidia":     "NVIDIA",
    "openai":     "OpenAI",
    "qwen":       "Qwen",
    "zai":        "Z.AI",
    "cohere":     "Cohere",
    "minimax":    "MiniMax",
    "google":     "Google",
    "ai21":       "AI21 Labs",
}


def _provider_from_profile_id(pid: str) -> str:
    """'us.anthropic.claude-...' → 'Anthropic'  (normalized)"""
    parts = pid.split(".")
    segment = parts[1].lower() if len(parts) >= 2 else ""
    return _PROVIDER_MAP.get(segment, segment.capitalize())


def _family_key(display_name: str) -> str:
    """Strip version numbers (X.Y) to get a comparable family key."""
    key = re.sub(r"\b\d+\.\d+\b", "", display_name)
    return re.sub(r"\s+", " ", key).strip().lower()


def _version_tuple(name: str) -> tuple[int, ...]:
    """Extract highest X.Y version as a comparable tuple."""
    matches = re.findall(r"\d+\.\d+", name)
    if matches:
        best = max(matches, key=lambda v: tuple(int(x) for x in v.split(".")))
        return tuple(int(x) for x in best.split("."))
    return (0,)


async def fetch_bedrock_models(
    region: str = "us-east-1",
    aws_access_key_id: Optional[str] = None,
    aws_secret_access_key: Optional[str] = None,
) -> list[dict]:
    session_kwargs: dict = {"region_name": region}
    if aws_access_key_id and aws_secret_access_key:
        session_kwargs["aws_access_key_id"] = aws_access_key_id
        session_kwargs["aws_secret_access_key"] = aws_secret_access_key

    session = boto3.Session(**session_kwargs)
    client = session.client("bedrock")
    loop = asyncio.get_event_loop()

    # ---- 1. Foundation models ------------------------------------------------
    fm_resp = await loop.run_in_executor(
        None,
        lambda: client.list_foundation_models(
            byInferenceType="ON_DEMAND",
            byOutputModality="TEXT",
        ),
    )
    foundation_models = [
        {
            "id": m["modelId"],
            "label": m["modelName"],
            "group": m["providerName"],
        }
        for m in fm_resp.get("modelSummaries", [])
        if m.get("modelLifecycle", {}).get("status") == "ACTIVE"
        and not any(kw in m["modelName"].lower() for kw in _SKIP_KEYWORDS)
    ]

    # ---- 2. Inference profiles (us.* cross-region) ---------------------------
    ip_resp = await loop.run_in_executor(
        None,
        lambda: client.list_inference_profiles(typeEquals="SYSTEM_DEFINED"),
    )
    raw_profiles = [
        p for p in ip_resp.get("inferenceProfileSummaries", [])
        if p.get("status") == "ACTIVE"
        and p.get("inferenceProfileId", "").startswith("us.")
        and "stability" not in p.get("inferenceProfileId", "")
        and not any(kw in p.get("inferenceProfileName", "").lower() for kw in _SKIP_KEYWORDS)
    ]

    # ---- 3. Dedup profiles: keep latest version per family -------------------
    family_best: dict[str, dict] = {}
    for p in raw_profiles:
        raw_name = p["inferenceProfileName"]
        # Strip regional prefix for display
        display = re.sub(r"^US\s+", "", raw_name).strip()
        provider = _provider_from_profile_id(p["inferenceProfileId"])
        fk = _family_key(display)
        ver = _version_tuple(raw_name)
        entry = {
            "id": p["inferenceProfileId"],
            "label": display,
            "group": provider,
            "_ver": ver,
        }
        if fk not in family_best or ver > family_best[fk]["_ver"]:
            family_best[fk] = entry

    profiles = [
        {"id": e["id"], "label": e["label"], "group": e["group"]}
        for e in sorted(family_best.values(), key=lambda e: (e["group"], e["label"]))
    ]

    # ---- 4. Merge + cross-dedup (prefer us.* profile over direct model) -----
    # Index profiles by (group, normalised_label)
    profile_labels: dict[tuple, dict] = {
        (e["group"].lower(), _family_key(e["label"])): e
        for e in profiles
    }

    final: list[dict] = list(profiles)
    for fm in foundation_models:
        key = (fm["group"].lower(), _family_key(fm["label"]))
        if key not in profile_labels:
            # No profile covers this model → keep foundation model entry
            final.append(fm)
        # else: profile already in list, skip the duplicate

    final.sort(key=lambda m: (m["group"].lower(), m["label"].lower()))

    logger.info(
        "fetch_bedrock_models: %d profiles + %d foundation → %d after dedup",
        len(profiles), len(foundation_models), len(final),
    )
    return final
