import os
from enum import Enum
from langchain_openai import ChatOpenAI, AzureChatOpenAI
from dotenv import load_dotenv

# Load environment variables (e.g., OPENAI_API_KEY, OPENAI_BASE_URL, AZURE_OPENAI_ENDPOINT)
load_dotenv()

# Token budget thresholds (fraction of max_tokens_budget)
_DOWNGRADE_REASONING_THRESHOLD = 0.70   # REASONING → BALANCED
_DOWNGRADE_ALL_THRESHOLD = 0.85         # all tiers → FAST
_ABORT_THRESHOLD = 0.95                 # raise TokenBudgetExceeded


class TokenBudgetExceeded(Exception):
    """Raised when cumulative token usage exceeds 95% of the configured budget."""


class LLMTier(Enum):
    FAST = "FAST"
    BALANCED = "BALANCED"
    REASONING = "REASONING"


class LLMRouter:
    def __init__(self):
        self.fast_model_name = os.getenv("FAST_MODEL_NAME", "gpt-4o-mini")
        self.balanced_model_name = os.getenv("BALANCED_MODEL_NAME", "gpt-4o")
        self.reasoning_model_name = os.getenv("REASONING_MODEL_NAME", "o1-preview")
        self.max_tokens_budget = int(os.getenv("MAX_TOKENS_BUDGET", "200000"))

        self.azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE_OPENAI_API_BASE")
        self.azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
        self.azure_api_version = os.getenv("OPENAI_API_VERSION", "2024-02-15-preview")

    def _create_model(self, model_name: str, temperature: float):
        if self.azure_endpoint and self.azure_api_key:
            return AzureChatOpenAI(
                azure_endpoint=self.azure_endpoint,
                api_key=self.azure_api_key,
                api_version=self.azure_api_version,
                azure_deployment=model_name,
                temperature=temperature
            )
        else:
            return ChatOpenAI(model=model_name, temperature=temperature)

    def _effective_tier(self, requested: LLMTier, tokens_used: int) -> LLMTier:
        """Apply circuit-breaker downgrade rules based on token budget consumption."""
        ratio = tokens_used / self.max_tokens_budget if self.max_tokens_budget > 0 else 0.0

        if ratio >= _ABORT_THRESHOLD:
            raise TokenBudgetExceeded(
                f"Token budget exhausted: {tokens_used}/{self.max_tokens_budget} "
                f"({ratio:.0%}). Aborting to prevent runaway cost."
            )
        if ratio >= _DOWNGRADE_ALL_THRESHOLD:
            return LLMTier.FAST
        if ratio >= _DOWNGRADE_REASONING_THRESHOLD and requested == LLMTier.REASONING:
            return LLMTier.BALANCED
        return requested

    def get_model(self, tier: LLMTier, tokens_used: int = 0):
        """
        Retrieve the appropriate LLM instance for a given tier.
        Applies token circuit-breaker downgrades:
          70% budget → REASONING downgrades to BALANCED
          85% budget → all tiers downgrade to FAST
          95% budget → raises TokenBudgetExceeded
        """
        effective = self._effective_tier(tier, tokens_used)

        if effective == LLMTier.FAST:
            return self._create_model(self.fast_model_name, 0.1)
        elif effective == LLMTier.BALANCED:
            return self._create_model(self.balanced_model_name, 0.2)
        elif effective == LLMTier.REASONING:
            # o1/o3 reasoning models only support temperature=1 (the default).
            return self._create_model(self.reasoning_model_name, 1)
        else:
            raise ValueError(f"Unknown tier: {effective}")


# Expose a default instance for convenience
router = None


def get_default_router() -> LLMRouter:
    global router
    if router is None:
        router = LLMRouter()
    return router
