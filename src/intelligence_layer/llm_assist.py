"""The only place the LLM is used: explaining findings the rules engine already made.

The LLM never decides pass/fail or routing. Calls go through Infoblox's LiteLLM proxy
(OpenAI-compatible) when LITELLM_BASE_URL, LITELLM_API_KEY and LITELLM_MODEL are set;
otherwise, or if the call fails, a plain template is used.
"""

import json
from dataclasses import asdict
from typing import List, Optional

from openai import OpenAI

from src.utils.config import Config, load_config
from src.utils.logging_config import get_logger
from src.utils.models import Order, ValidationResult

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "You help Infoblox Manufacturing Operations (MfgOps) review sales orders before they "
    "are released to Arrow. A deterministic rules engine has already found the issues "
    "listed. Do not add, remove or overrule findings. For each issue, explain it in one or "
    "two plain sentences and say who must act and how: the OM team (by correcting the "
    "order in Fusion) or the MfgOps reviewer (e.g. confirming a document by email). "
    "Be concise and use a short bullet list."
)


def build_prompt(order: Order, findings: List[ValidationResult]) -> str:
    """Write the user message: the full order plus the findings to explain.

    Args:
        order: The order, including its lines.
        findings: Rules that fired on the order.

    Returns:
        The prompt text.
    """
    order_data = asdict(order)
    order_data.pop("raw_json", None)
    finding_lines = "\n".join(
        f"- [{item.action}] {item.rule_name}: {item.message}" for item in findings
    )
    return f"Order:\n{json.dumps(order_data, indent=2, default=str)}\n\nFindings:\n{finding_lines}"


def call_llm_proxy(prompt: str, config: Config) -> Optional[str]:
    """Send one prompt to the LiteLLM proxy.

    Args:
        prompt: The user message.
        config: Settings holding the LiteLLM base URL, key and model.

    Returns:
        The model's reply, or None if the call failed (the failure is logged).
    """
    try:
        client = OpenAI(
            api_key=config.litellm_api_key,
            base_url=config.litellm_base_url,
            timeout=30,
            max_retries=1,
        )
        response = client.chat.completions.create(
            model=config.litellm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return (response.choices[0].message.content or "").strip() or None
    # Any LLM failure must fall back to the template, never break validation.
    except Exception as error:
        logger.warning("LLM explanation failed, using template instead: %s", error)
        return None


def fallback_explanation(findings: List[ValidationResult]) -> str:
    """Explain findings without an LLM, straight from the rule messages.

    Args:
        findings: Rules that fired on the order.

    Returns:
        A bullet list, one line per finding.
    """
    labels = {"block": "Blocking", "require_doc": "Missing document", "warn": "Check"}
    return "\n".join(
        f"- {labels.get(item.action, item.action)}: {item.message}" for item in findings
    )


def explain_discrepancies(order: Order, findings: List[ValidationResult]) -> str:
    """Explain an order's findings in plain language for the MfgOps user and OM.

    Args:
        order: The order.
        findings: Rules that fired on the order.

    Returns:
        The explanation; "" if there are no findings.
    """
    if not findings:
        return ""
    config = load_config()
    if config.llm_enabled:
        explanation = call_llm_proxy(build_prompt(order, findings), config)
        if explanation:
            return explanation
    return fallback_explanation(findings)
