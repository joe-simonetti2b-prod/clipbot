"""
Accès unifié aux modèles de langage pour la rédaction, la note et la traduction.

Ordre de préférence :
  1. Claude (ANTHROPIC_API_KEY)  — meilleure qualité, payant à l'usage
  2. Groq   (GROQ_API_KEY)       — gratuit, déjà utilisé pour la transcription
  3. aucun  -> gabarits sans IA

Chaque appel demande un objet JSON et renvoie None en cas d'échec : l'appelant
retombe toujours sur un comportement sans IA, un clip n'est jamais bloqué.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = "https://api.groq.com/openai/v1/models"
# Ordre de préférence : Groq retire et renomme régulièrement ses modèles, et tous les
# comptes n'ont pas accès aux mêmes. On choisit le premier réellement disponible.
GROQ_PREFERRED = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b",
                  "llama-3.1-8b-instant"]
_groq_model_cache: dict[str, str] = {}
_groq_bad_models: set[str] = set()   # modèles refusés (404) pendant cette exécution


async def _groq_model(session: aiohttp.ClientSession, llm: "LLM") -> str:
    """Modèle Groq utilisable avec cette clé (interrogé une fois, puis mémorisé)."""
    if llm.key in _groq_model_cache:
        return _groq_model_cache[llm.key]
    chosen = llm.model
    try:
        async with session.get(GROQ_MODELS, headers={"Authorization": f"Bearer {llm.key}"},
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            ids = [m["id"] for m in (await r.json()).get("data", [])] if r.status == 200 else []
        ids = [i for i in ids if i not in _groq_bad_models]
        if llm.model in _groq_bad_models or (ids and llm.model not in ids):
            text_models = [i for i in ids if not any(x in i for x in ("whisper", "guard", "tts", "orpheus"))]
            chosen = next((m for m in GROQ_PREFERRED if m in ids), text_models[0] if text_models else llm.model)
            log.info("Modèle Groq %s indisponible pour cette clé -> %s", llm.model, chosen)
    except Exception as e:
        log.warning("Liste des modèles Groq inaccessible (%s)", e)
    _groq_model_cache[llm.key] = chosen
    return chosen


ANTHROPIC = "https://api.anthropic.com/v1/messages"


@dataclass
class LLM:
    provider: str      # "anthropic" | "groq" | ""
    key: str
    model: str

    @classmethod
    def from_settings(cls, p) -> "LLM":
        if p.anthropic_api_key:
            return cls("anthropic", p.anthropic_api_key, p.anthropic_model)
        if p.groq_api_key:
            return cls("groq", p.groq_api_key, p.groq_llm_model)
        return cls("", "", "")

    @property
    def enabled(self) -> bool:
        return bool(self.provider)


def _extract_json(text: str) -> dict | list | None:
    m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    return json.loads(m.group(1)) if m else None


async def ask_json(session: aiohttp.ClientSession, llm: LLM, prompt: str,
                   max_tokens: int = 800) -> dict | None:
    if not llm.enabled:
        return None
    try:
        if llm.provider == "anthropic":
            async with session.post(
                ANTHROPIC,
                headers={"x-api-key": llm.key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": llm.model, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                r.raise_for_status()
                data = await r.json()
            text = "".join(b.get("text", "") for b in data.get("content", []))
        else:
            text = ""
            no_reasoning_param = False
            for attempt in range(3):  # après un refus, on réessaie aussitôt autrement
                model = await _groq_model(session, llm)
                body = {"model": model, "max_tokens": max_tokens, "temperature": 0.7,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": "Tu réponds uniquement par un objet JSON valide."},
                            {"role": "user", "content": prompt}]}
                if "gpt-oss" in model and not no_reasoning_param:
                    # Modèles « à raisonnement » : raisonnement court, sinon il consomme
                    # tout le budget de jetons avant d'écrire la réponse.
                    body.update(reasoning_effort="low", max_tokens=max_tokens + 1500)
                async with session.post(GROQ_CHAT, headers={"Authorization": f"Bearer {llm.key}"},
                                        json=body, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 404 and attempt < 2:
                        log.warning("Modèle Groq %s refusé, essai d'un autre", model)
                        _groq_bad_models.add(model)
                        _groq_model_cache.pop(llm.key, None)
                        continue
                    if r.status == 400 and "reasoning_effort" in body and attempt < 2:
                        no_reasoning_param = True  # paramètre refusé : on s'en passe
                        continue
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status} : {(await r.text())[:200]}")
                    data = await r.json()
                text = data["choices"][0]["message"].get("content") or ""
                break
        obj = _extract_json(text)
        return obj if isinstance(obj, dict) else None
    except Exception as e:
        log.warning("IA (%s) indisponible : %s", llm.provider, e)
        return None
