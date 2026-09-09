"""Provider boundary used by the UDP companion daemon."""

from .openai import LLMError, OpenAICompatibleClient

# Naming aliases keep the boundary easy to adopt from the old bridge's
# ``Llm`` spelling without coupling the daemon to that legacy module.
OpenAIClient = OpenAICompatibleClient
Llm = OpenAICompatibleClient
LlmError = LLMError

__all__ = [
    "LLMError",
    "OpenAIClient",
    "OpenAICompatibleClient",
    "Llm",
    "LlmError",
]
