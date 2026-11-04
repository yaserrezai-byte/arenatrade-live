import json
from typing import Any, Dict, Optional, Tuple
import httpx

from core.config import settings


class OptimalLLMRouter:
    """
    Optimal Hybrid AI Router ('بهترین و بهینه‌ترین'):
    - Uses fast, cost-efficient reasoning LLMs (Gemini 2.5 Flash / DeepSeek V3 / OpenRouter / GPT-4o-mini)
      whenever an API key is configured.
    - Gracefully falls back to the Quantitative Expert Engine with zero latency & zero failure
      when no LLM API key is set yet.
    """

    def active_provider_info(self) -> Dict[str, str]:
        preferred = str(settings.get("AI_PROVIDER", "auto")).lower()

        providers = [
            ("gemini", "GEMINI_API_KEY", settings.get("GEMINI_MODEL", "gemini-2.5-flash")),
            ("openrouter", "OPENROUTER_API_KEY", settings.get("OPENROUTER_MODEL", "deepseek/deepseek-chat")),
            ("deepseek", "DEEPSEEK_API_KEY", settings.get("DEEPSEEK_MODEL", "deepseek-chat")),
            ("openai", "OPENAI_API_KEY", settings.get("OPENAI_MODEL", "gpt-4o-mini")),
        ]

        if preferred != "auto":
            for p_name, key_env, model in providers:
                if p_name == preferred and settings.get(key_env):
                    return {"provider": p_name, "model": str(model), "mode": "HYBRID_LLM"}

        for p_name, key_env, model in providers:
            if settings.get(key_env):
                return {"provider": p_name, "model": str(model), "mode": "HYBRID_LLM"}

        return {
            "provider": "quant_neural_heuristic",
            "model": "Quant-MultiAgent-v2 (بدون نیاز به کلید پولی)",
            "mode": "QUANT_ALGORITHMIC",
        }

    async def generate_json(
        self, system_prompt: str, user_prompt: str, timeout: float = 14.0
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        info = self.active_provider_info()
        provider = info["provider"]
        model = info["model"]

        if provider == "quant_neural_heuristic":
            return None, info["model"]

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if provider == "gemini":
                    api_key = settings.get("GEMINI_API_KEY")
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                    payload = {
                        "systemInstruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"parts": [{"text": user_prompt}]}],
                        "generationConfig": {
                            "temperature": 0.2,
                            "responseMimeType": "application/json",
                        },
                    }
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    txt = data["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(txt), f"Gemini ({model})"

                # OpenAI-compatible endpoints (OpenRouter / DeepSeek / OpenAI)
                if provider == "openrouter":
                    url = "https://openrouter.ai/api/v1/chat/completions"
                    api_key = settings.get("OPENROUTER_API_KEY")
                elif provider == "deepseek":
                    url = "https://api.deepseek.com/chat/completions"
                    api_key = settings.get("DEEPSEEK_API_KEY")
                else:
                    url = "https://api.openai.com/v1/chat/completions"
                    api_key = settings.get("OPENAI_API_KEY")

                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": model,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                }
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                txt = resp.json()["choices"][0]["message"]["content"]
                return json.loads(txt), f"{provider.upper()} ({model})"
        except Exception:
            return None, f"{info['model']} -> Fallback Quant"


llm_router = OptimalLLMRouter()
