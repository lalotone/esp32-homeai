"""Chat completion against any OpenAI-compatible endpoint.

Works with Ollama (/v1), llama.cpp's llama-server, LM Studio, vLLM, etc.
Configure LLM_BASE_URL / LLM_MODEL / LLM_API_KEY in the environment.
"""
import httpx

from .config import settings


async def chat(user_text: str) -> str:
    """Send one user turn and return the assistant's reply text."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": settings.system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.7,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return data["choices"][0]["message"]["content"].strip()
