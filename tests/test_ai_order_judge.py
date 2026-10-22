from types import SimpleNamespace
from unittest.mock import patch

from src.intelligence_layer import ai_order_judge
from src.intelligence_layer.llm_client import AiUnavailableError
from src.intelligence_layer.rule_checks import RuleCheckResult
from tests.sample_orders import get_sample_orders
from src.utils.models import Rule, RuleFinding

ORDER = get_sample_orders()[0]
EMBARGO = RuleFinding(1, "Embargoed destination", "reject", "Embargoed.", False)
SOFT_RULE = Rule(
    "Power cords per appliance", "soft", "reject", "d", guidance="Count cords."
)


def ai_says(text):
    return patch(
        "src.intelligence_layer.llm_client.ask_ai",
        return_value=SimpleNamespace(content=text),
    )


def ai_on(monkeypatch):
    for name, value in [("LITELLM_BASE_URL", "http://x"), ("LITELLM_API_KEY", "k")]:
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LITELLM_MODEL", "test-model")


def test_without_ai_only_fixed_checks_decide():
    checks = RuleCheckResult(facts={}, findings=[EMBARGO], judgement_rules=[SOFT_RULE])
    judgement = ai_order_judge.judge_order(ORDER, checks)
    assert judgement.verdict == "reject"
    assert not judgement.used_ai
    assert any("Power cords per appliance" in reason for reason in judgement.reasons)


def test_ai_verdict_is_used(monkeypatch):
    ai_on(monkeypatch)
    reply = '{"verdict": "hold", "summary": "Check cords.", "reasons": ["r"], "confidence": "low"}'
    with ai_says(reply):
        judgement = ai_order_judge.judge_order(ORDER, RuleCheckResult(facts={}))
    assert (judgement.verdict, judgement.summary, judgement.used_ai) == (
        "hold",
        "Check cords.",
        True,
    )


def test_ai_can_not_approve_what_a_fixed_check_rejects(monkeypatch):
    ai_on(monkeypatch)
    with ai_says('Sure! {"verdict": "approve", "summary": "Fine.", "reasons": []}'):
        judgement = ai_order_judge.judge_order(
            ORDER, RuleCheckResult(facts={}, findings=[EMBARGO])
        )
    assert judgement.verdict == "reject"


def test_odd_or_failed_ai_reply_falls_back(monkeypatch):
    ai_on(monkeypatch)
    with ai_says('{"verdict": "maybe"}'):
        assert not ai_order_judge.judge_order(ORDER, RuleCheckResult(facts={})).used_ai
    with patch(
        "src.intelligence_layer.llm_client.ask_ai", side_effect=AiUnavailableError("x")
    ):
        assert (
            ai_order_judge.judge_order(ORDER, RuleCheckResult(facts={})).verdict
            == "approve"
        )


def test_blocked_proxy_gets_a_plain_explanation():
    import openai

    from src.intelligence_layer import llm_client

    blocked = SimpleNamespace(status_code=403)
    assert "VPN" in llm_client.describe_failure(blocked)
    assert "API key" in llm_client.describe_failure(SimpleNamespace(status_code=401))
    assert "too large" in llm_client.describe_failure(SimpleNamespace(status_code=413))
    assert "network" in llm_client.describe_failure(
        openai.APIConnectionError(request=None)
    )
    assert llm_client.describe_failure(ValueError("bad\nthing")) == "bad thing"


def test_the_reason_reaches_the_reviewer(monkeypatch):
    ai_on(monkeypatch)
    failure = AiUnavailableError("the AI server blocked the request (403 Forbidden).")
    with patch("src.intelligence_layer.llm_client.ask_ai", side_effect=failure):
        judgement = ai_order_judge.judge_order(ORDER, RuleCheckResult(facts={}))
    assert "blocked the request (403" in judgement.summary and not judgement.used_ai
