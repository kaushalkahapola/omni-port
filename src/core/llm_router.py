import os
from enum import Enum
from langchain_openai import ChatOpenAI, AzureChatOpenAI
from dotenv import load_dotenv

# Load environment variables (e.g., OPENAI_API_KEY, OPENAI_BASE_URL, AZURE_OPENAI_ENDPOINT)
load_dotenv()


class LLMTier(Enum):
    FAST = "FAST"
    BALANCED = "BALANCED"
    REASONING = "REASONING"


class LLMRouter:
    def __init__(self):
        self.fast_model_name = os.getenv("FAST_MODEL_NAME", "gpt-4o-mini")
        self.balanced_model_name = os.getenv("BALANCED_MODEL_NAME", "gpt-4o")
        self.reasoning_model_name = os.getenv("REASONING_MODEL_NAME", "o1-preview")

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

    def get_model(self, tier: LLMTier):
        """
        Retrieve the appropriate LLM instance for a given tier.
        """
        if tier == LLMTier.FAST:
            return self._create_model(self.fast_model_name, 0.1)
        elif tier == LLMTier.BALANCED:
            return self._create_model(self.balanced_model_name, 0.2)
        elif tier == LLMTier.REASONING:
            # o1/o3 reasoning models only support temperature=1 (the default).
            return self._create_model(self.reasoning_model_name, 1)
        else:
            raise ValueError(f"Unknown tier: {tier}")


# Expose a default instance for convenience
router = None


def get_default_router() -> LLMRouter:
    global router
    if router is None:
        router = LLMRouter()
    return router
