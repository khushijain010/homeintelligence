"""Thin wrapper over the Bedrock Converse API. Returns None when Bedrock isn't configured,
so callers fall back to the template report without errors."""

import logging
from typing import Protocol

from guardian.config import settings

log = logging.getLogger("guardian.bedrock")


class ReportModel(Protocol):
    model_id: str

    def converse(self, system: str, messages: list[dict]) -> str: ...


class BedrockClient:
    def __init__(self, model_id: str, region: str, timeout: float):
        import boto3
        from botocore.config import Config

        self.model_id = model_id
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 1}),
        )

    def converse(self, system: str, messages: list[dict]) -> str:
        resp = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=messages,
            inferenceConfig={"temperature": 0, "maxTokens": 600},
        )
        return "".join(block.get("text", "") for block in resp["output"]["message"]["content"])


_client: ReportModel | None = None
_checked = False


def get_client() -> ReportModel | None:
    global _client, _checked
    if _checked:
        return _client
    _checked = True
    if not settings.bedrock_model_id:
        log.info("BEDROCK_MODEL_ID not set; using template reports")
        return None
    try:
        import boto3

        if boto3.Session().get_credentials() is None:
            log.info("no AWS credentials; using template reports")
            return None
        _client = BedrockClient(settings.bedrock_model_id, settings.aws_region, settings.bedrock_timeout_seconds)
    except Exception as exc:  # missing/invalid config must never break incident handling
        log.warning("Bedrock unavailable (%s); using template reports", exc)
        _client = None
    return _client


def set_client(client: ReportModel | None) -> None:
    """Tests inject a fake; None means 'not configured'."""
    global _client, _checked
    _client, _checked = client, True
