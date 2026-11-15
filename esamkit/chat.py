"""
Chat model client: any OpenAI-compatible endpoint, over the Chat Completions
API or the Responses API, with fallback endpoints.

On the pilot this is the IL5 API Gateway (GPT-5.1). Configure:
    ESAM_REASONING_BASE_URL=https://<gateway>/v1
    ESAM_REASONING_API_KEY=...
    ESAM_REASONING_MODEL=<the model name the gateway exposes>

Optional:
    ESAM_REASONING_API=chat | responses | auto      # auto = chat, then responses if chat fails
    ESAM_REASONING_FALLBACK_1_BASE_URL=https://<second endpoint>/v1
    ESAM_REASONING_FALLBACK_1_API_KEY=...           # each field defaults to the primary's
    ESAM_REASONING_FALLBACK_1_MODEL=...
    ESAM_REASONING_FALLBACK_1_API=responses

The routing and failover rules are in esamkit/llm.py.

If the URL and model aren't set, `chat_configured()` is False and lesson 05
runs in "dry run" mode: it prints the exact prompt it would have sent. That's
useful on its own. Most of what makes RAG good or bad is visible in the prompt.
"""

from . import llm
from .config import Settings, settings as default_settings


def chat_configured(s: Settings = default_settings) -> bool:
    return bool(s.chat_base_url and s.chat_model)


def complete(system: str, user: str, s: Settings = default_settings) -> str:
    return llm.complete(system, user, s=s)
