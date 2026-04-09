import os
from enum import Enum
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

# Load environment variables (e.g., OPENAI_API_KEY, OPENAI_BASE_URL)
load_dotenv()


class LLMTier(Enum):
    FAST = "FAST"
    BALANCED = "BALANCED"
    REASONING = "REASONING"


class LLMRouter:
    def __init__(self):
        # Allow overrides for different models per tier, falling back to reasonable defaults
        # The base URL and API key will be automatically picked up by ChatOpenAI
        # from the OPENAI_BASE_URL and OPENAI_API_KEY env variables.
        self.fast_model_name = os.getenv("FAST_MODEL_NAME", "gpt-4o-mini")
        self.balanced_model_name = os.getenv("BALANCED_MODEL_NAME", "gpt-4o")
        self.reasoning_model_name = os.getenv("REASONING_MODEL_NAME", "o1-preview")

        self._models = {
            LLMTier.FAST: ChatOpenAI(model=self.fast_model_name, temperature=0.1),
            LLMTier.BALANCED: ChatOpenAI(
                model=self.balanced_model_name, temperature=0.2
            ),
            LLMTier.REASONING: ChatOpenAI(
                model=self.reasoning_model_name, temperature=0.5
            ),
        }

    def get_model(self, tier: LLMTier) -> ChatOpenAI:
        """
        Retrieve the appropriate LLM instance for a given tier.
        FAST: Cheap routing, classification, memory (e.g. gpt-4o-mini)
        BALANCED: Context-heavy synthesis, namespace adaptation (e.g. gpt-4o)
        REASONING: Deep structural refactoring, extended thinking (e.g. o1-preview)
        """
        if tier not in self._models:
            raise ValueError(f"Unknown tier: {tier}")
        return self._models[tier]


# Expose a default instance for convenience (do not initialize immediately to allow env mocking in tests)
router = None


def get_default_router() -> LLMRouter:
    global router
    if router is None:
        router = LLMRouter()
    return router
