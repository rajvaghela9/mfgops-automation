"""The one place that talks to the AI model, through Infoblox's LiteLLM proxy.

LiteLLM is OpenAI-compatible, so the model can be swapped with LITELLM_MODEL in .env.
When LITELLM_BASE_URL, LITELLM_API_KEY or LITELLM_MODEL is missing, the AI is off and
callers fall back to the fixed rules only.
"""

import json
import re
from typing import Any, Dict, List, Optional

import openai
from openai import OpenAI

from src.utils.config import load_config
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class AiUnavailableError(Exception):
    """Raised when the AI is switched off or the call to it failed."""


def describe_failure(error: Exception) -> str:
    """Turn an AI call error into one short sentence a MfgOps user can act on.

    Args:
        error: The exception raised by the call.

    Returns:
        A readable reason, e.g. a hint to connect to the VPN for a blocked request.
    """
    status = getattr(error, "status_code", None)
    if status == 403:
        return (
            "the AI server blocked the request (403 Forbidden). "
            "Are you connected to the Infoblox VPN?"
        )
    if status == 401:
        return "the AI server rejected the API key (LITELLM_API_KEY in .env)"
    if isinstance(error, (openai.APIConnectionError, openai.APITimeoutError)):
        return "could not reach the AI server. Check your network or VPN connection"
    return " ".join(str(error).split())[:200]


def is_ai_configured() -> bool:
    """Tell whether the LiteLLM settings needed to call the AI are all present.

    Returns:
        True when the AI can be used.
    """
    return load_config().llm_enabled


def ask_ai(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 2000,
):
    """Send a conversation to the AI and return its reply.

    Args:
        messages: OpenAI-style messages, starting with the system message.
        tools: Optional OpenAI-style tool definitions the AI may call.
        max_tokens: Upper limit on the reply length.

    Returns:
        The reply message (with .content and, if it used tools, .tool_calls).
    """
    config = load_config()
    if not config.llm_enabled:
        raise AiUnavailableError(
            "The AI is not set up (LiteLLM settings missing in .env)."
        )
    request = {
        "model": config.litellm_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        request["tools"] = tools
    try:
        client = OpenAI(
            api_key=config.litellm_api_key,
            base_url=config.litellm_base_url,
            timeout=90,
            max_retries=2,
        )
        return client.chat.completions.create(**request).choices[0].message
    # Any AI failure must be reported to the caller, never crash the workflow.
    except Exception as error:
        reason = describe_failure(error)
        logger.warning("AI call failed: %s", reason)
        raise AiUnavailableError(reason) from error


def read_json_reply(text: Optional[str]) -> Dict[str, Any]:
    """Pull the JSON object out of an AI reply, even if it added words around it.

    Args:
        text: The AI's reply.

    Returns:
        The parsed JSON object.
    """
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("The AI reply did not contain a JSON object.")
    return json.loads(match.group(0))
