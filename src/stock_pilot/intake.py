"""Optional natural-language intake.

The model is deliberately kept outside the execution path. It receives free
text and returns *candidate parameters*; this module validates them and hands
back a draft. The draft is not persisted and grants no authority: the caller
must submit it through ``POST /tasks``, where inventory, mapping, approval, and
timeout rules run exactly as before. The LLM client never receives a tool
client, so it cannot read stock, stage a price, or publish anything.

When no provider is configured (the default) the endpoint is disabled and the
base install stays deterministic and credential-free.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stock_pilot.config import Settings
from stock_pilot.enums import ErrorCode
from stock_pilot.errors import ToolFailure
from stock_pilot.models import TaskDraft, from_cents, to_cents

logger = logging.getLogger(__name__)

try:  # optional dependency: stock-pilot[llm]
    import anthropic as _anthropic
except ImportError:  # pragma: no cover - exercised when the extra is absent
    _anthropic = None

_SYSTEM_PROMPT = """\
You extract parameters for an inventory-driven e-commerce repricing task.
Return only the structured fields. Copy the SKU exactly as written; never
invent a SKU, spec, or price. Leave a field null when the text does not
contain it. The reason should be a short restatement of why the price changes.
You do not decide whether the change is allowed; a separate approval flow does.
"""


class ExtractedTask(BaseModel):
    """The model-facing schema. Missing values stay null; they are never filled."""

    model_config = ConfigDict(extra="forbid")

    sku_id: str | None = Field(default=None, max_length=64)
    spec: str | None = Field(default=None, max_length=128)
    target_price: float | None = Field(default=None, ge=0.01, le=999999.99)
    reason: str | None = Field(default=None, max_length=500)


class LLMClient(Protocol):
    """Returns raw extraction data; validation happens in ``extract_task``."""

    def extract(self, text: str) -> dict[str, Any]: ...


class AnthropicLLMClient:
    """Thin wrapper over the official SDK's structured-output helper.

    One request, no retries, bounded by the configured timeout. The API key is
    held by the SDK client and never appears in messages or logs.
    """

    def __init__(
        self, *, api_key: str, model: str, timeout_seconds: float
    ) -> None:
        if _anthropic is None:
            raise RuntimeError(
                "the intake provider requires the optional 'llm' extra:"
                " pip install 'stock-pilot[llm]'"
            )
        if not api_key:
            raise ValueError("api_key must not be empty")
        self.model = model
        self._client = _anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )

    def extract(self, text: str) -> dict[str, Any]:
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
                output_format=ExtractedTask,
            )
        except _anthropic.APITimeoutError as exc:
            raise ToolFailure(
                ErrorCode.LLM_TIMEOUT, "model request timed out"
            ) from exc
        except _anthropic.APIConnectionError as exc:
            raise ToolFailure(
                ErrorCode.LLM_UNAVAILABLE, "model service is unreachable"
            ) from exc
        except _anthropic.APIStatusError as exc:
            raise ToolFailure(
                ErrorCode.LLM_UNAVAILABLE,
                f"model service returned HTTP {exc.status_code}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - schema/JSON failures
            raise ToolFailure(
                ErrorCode.LLM_INVALID_OUTPUT,
                "model output failed schema validation",
            ) from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise ToolFailure(
                ErrorCode.LLM_INVALID_OUTPUT,
                "model declined the extraction request",
            )
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ToolFailure(
                ErrorCode.LLM_INVALID_OUTPUT,
                "model returned no structured output",
            )
        return parsed.model_dump()


def create_llm_client(settings: Settings) -> LLMClient | None:
    if settings.llm_provider == "off":
        return None
    if settings.llm_provider == "anthropic":
        return AnthropicLLMClient(
            api_key=settings.llm_api_key or "",
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    raise ValueError(f"unsupported llm provider: {settings.llm_provider!r}")


def extract_task(client: LLMClient, text: str) -> TaskDraft:
    """Validate model output into a draft. Never writes and never calls a tool."""
    raw = client.extract(text)
    try:
        extracted = ExtractedTask.model_validate(raw)
    except ValidationError as exc:
        logger.warning(
            "intake extraction rejected: %d schema error(s)", exc.error_count()
        )
        raise ToolFailure(
            ErrorCode.LLM_INVALID_OUTPUT,
            "model output did not match the task schema",
        ) from exc

    missing = [
        name
        for name, value in (
            ("sku_id", extracted.sku_id),
            ("spec", extracted.spec),
            ("target_price", extracted.target_price),
        )
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        raise ToolFailure(
            ErrorCode.LLM_INVALID_OUTPUT,
            f"model did not extract required field(s): {', '.join(missing)}",
        )

    warnings: list[str] = []
    reason = (extracted.reason or "").strip()
    if not reason:
        reason = "自然语言入口提交"
        warnings.append("模型未提取到理由，已使用默认理由")

    price = from_cents(to_cents(Decimal(str(extracted.target_price))))
    try:
        return TaskDraft(
            sku_id=extracted.sku_id.strip(),
            spec=extracted.spec.strip(),
            target_price=price,
            reason=reason,
            warnings=warnings,
        )
    except ValidationError as exc:
        logger.warning("intake draft rejected by task constraints")
        raise ToolFailure(
            ErrorCode.LLM_INVALID_OUTPUT,
            "extracted parameters violate task constraints",
        ) from exc
