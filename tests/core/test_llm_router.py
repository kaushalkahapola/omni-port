import pytest
import os
from unittest.mock import patch
from src.core.llm_router import LLMRouter, LLMTier


@patch.dict(
    os.environ, {"OPENAI_API_KEY": "dummy_key", "FAST_MODEL_NAME": "custom-fast"}
)
def test_llm_router_initialization():
    router = LLMRouter()
    model = router.get_model(LLMTier.FAST)
    assert model.model_name == "custom-fast"
    assert model.temperature == 0.1


@patch.dict(os.environ, {"OPENAI_API_KEY": "dummy_key"})
def test_llm_router_defaults():
    router = LLMRouter()
    fast_model = router.get_model(LLMTier.FAST)
    balanced_model = router.get_model(LLMTier.BALANCED)
    reasoning_model = router.get_model(LLMTier.REASONING)

    assert fast_model.model_name == "gpt-4o-mini"
    assert balanced_model.model_name == "gpt-4o"
    assert reasoning_model.model_name == "o1-preview"

    with pytest.raises(ValueError):
        router.get_model("UNKNOWN_TIER")
