from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_MODELS_URL = "https://models.dev/api.json"
TERMINAL_API_PATHS = (
    "/chat/completions",
    "/responses",
    "/completions",
)


@dataclass(frozen=True)
class ModelLimits:
    input: int | None
    output: int | None
    context: int | None


@dataclass(frozen=True)
class _ModelCandidate:
    model: str
    api_url: str | None
    limits: ModelLimits


def normalize_model_base_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return None

    path = parsed.path or ""
    normalized_path = path.rstrip("/")
    lowered_path = normalized_path.lower()
    for terminal in TERMINAL_API_PATHS:
        if lowered_path.endswith(terminal):
            normalized_path = normalized_path[: -len(terminal)]
            break
    normalized_path = normalized_path.rstrip("/")

    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=normalized_path,
        params="",
        query="",
        fragment="",
    )
    return urlunparse(normalized).rstrip("/")


class ModelLimitRegistry:
    def __init__(self, cache_path: Path, remote_url: str = DEFAULT_MODELS_URL):
        self.cache_path = Path(cache_path)
        self.remote_url = str(remote_url)
        self._raw: dict[str, Any] = {}
        self._candidates: list[_ModelCandidate] = []

    def load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._raw = {}
            self._candidates = []
            return
        except Exception as exc:
            logger.warning("Failed to load model limit cache '%s': %s", self.cache_path, exc)
            self._raw = {}
            self._candidates = []
            return

        self._set_payload(payload)

    async def refresh_cache_best_effort(self) -> None:
        try:
            payload = await asyncio.to_thread(self._download_payload)
        except Exception as exc:
            logger.warning("Failed to refresh model limits from '%s': %s", self.remote_url, exc)
            return

        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.cache_path)
        except Exception as exc:
            logger.warning("Failed to persist model limits cache '%s': %s", self.cache_path, exc)

        self._set_payload(payload)

    def get_limits(self, model: str, base_url: str | None) -> Optional[ModelLimits]:
        model_id = str(model or "").strip()
        if not model_id:
            return None

        coarse = [item for item in self._candidates if item.model == model_id]
        if not coarse:
            return None

        normalized_url = normalize_model_base_url(base_url)
        if not normalized_url:
            return None

        matched = [
            item
            for item in coarse
            if item.api_url and normalize_model_base_url(item.api_url) == normalized_url
        ]
        if len(matched) != 1:
            return None
        return matched[0].limits

    def _download_payload(self) -> dict[str, Any]:
        request = Request(
            self.remote_url,
            headers={"User-Agent": "StewardFlow/overflow-compaction"},
        )
        with urlopen(request, timeout=10) as response:
            status = getattr(response, "status", None)
            if status and int(status) >= 400:
                raise ValueError(f"models.dev request failed with status {status}")
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("models.dev payload must be an object")
        return payload

    def _set_payload(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("model limits payload must be an object")
        self._raw = payload
        self._candidates = self._flatten_candidates(payload)

    def _flatten_candidates(self, payload: dict[str, Any]) -> list[_ModelCandidate]:
        candidates: list[_ModelCandidate] = []
        for provider in payload.values():
            if not isinstance(provider, dict):
                continue
            provider_api = provider.get("api")
            models = provider.get("models")
            if not isinstance(models, dict):
                continue
            for model_key, model_data in models.items():
                if not isinstance(model_data, dict):
                    continue
                limits = model_data.get("limit")
                if not isinstance(limits, dict):
                    continue
                model_id = str(model_data.get("id") or model_key or "").strip()
                if not model_id:
                    continue
                api_url = None
                provider_override = model_data.get("provider")
                if isinstance(provider_override, dict):
                    api_url = provider_override.get("api")
                if not api_url:
                    api_url = provider_api
                candidates.append(
                    _ModelCandidate(
                        model=model_id,
                        api_url=str(api_url).strip() if api_url else None,
                        limits=ModelLimits(
                            input=_coerce_positive_int(limits.get("input")),
                            output=_coerce_positive_int(limits.get("output")),
                            context=_coerce_positive_int(limits.get("context")),
                        ),
                    )
                )
        return candidates


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
        return value if value > 0 else None
    return None
