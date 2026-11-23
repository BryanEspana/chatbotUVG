"""
Cliente de OpenRouter.

- embeddings(): POST {openrouter_url}/embeddings con el formato de la API de OpenAI.
- chat_stream(): POST {openrouter_url}/chat/completions con streaming (SSE). Sin parámetro de
  razonamiento: gpt-oss-120b usa el suyo normal (con "low" inventó datos en el examen).
- `data_collection: "deny"`: solo proveedores que no entrenan con los datos.
- Modelo "local": embeddings falsos y deterministas SIN red, para pruebas y desarrollo
  sin gastar. No sirven para calidad real.
"""

import asyncio
import hashlib
import math
import re
import unicodedata

import json
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from .config import settings

LOTE = 64  # textos por petición de embeddings

# Solo para pruebas: un httpx.MockTransport que imita a OpenRouter (tests/openrouter_falso.py).
_transporte: httpx.AsyncBaseTransport | None = None


def _cliente(timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transporte) if _transporte else httpx.AsyncClient(timeout=timeout)


class ErrorOpenRouter(RuntimeError):
    pass


def _cabeceras() -> dict[str, str]:
    key = settings().openrouter_api_key
    if not key:
        raise ErrorOpenRouter("Falta OPENROUTER_API_KEY en .env (o usa MODELO_EMBEDDINGS=local para probar sin red).")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": "Chatbot UVG",
    }


def preparar_consulta(texto: str) -> str:
    """
    Algunos modelos de embeddings rinden mejor si la CONSULTA (no los documentos) lleva una
    instrucción. Qwen3-Embedding lo recomienda; el resto se usa tal cual.
    """
    if settings().modelo_embeddings.startswith("qwen/qwen3-embedding"):
        return f"Instruct: Dada una pregunta de un aspirante o estudiante universitario, recupera el fragmento que la responde\nQuery: {texto}"
    return texto


async def embeddings(textos: list[str], cliente: httpx.AsyncClient | None = None) -> tuple[list[list[float]], int]:
    """Devuelve (vectores, tokens usados). Respeta el orden de entrada."""
    modelo = settings().modelo_embeddings
    if modelo == "local":
        return [embedding_local(t) for t in textos], 0

    propio = cliente is None
    cliente = cliente or _cliente(60)
    vectores: list[list[float]] = []
    tokens = 0
    try:
        for i in range(0, len(textos), LOTE):
            lote = textos[i : i + LOTE]
            cuerpo = {"model": modelo, "input": lote, "provider": {"data_collection": "deny"}}
            for intento in range(3):
                r = await cliente.post(f"{settings().openrouter_url}/embeddings", json=cuerpo, headers=_cabeceras())
                if r.status_code in (429, 500, 502, 503, 504) and intento < 2:
                    await asyncio.sleep(2 * (intento + 1))
                    continue
                if r.status_code == 400 and "provider" in cuerpo:
                    # por si el endpoint de embeddings no acepta preferencias de proveedor
                    cuerpo.pop("provider")
                    continue
                break
            if r.status_code != 200:
                raise ErrorOpenRouter(f"Embeddings HTTP {r.status_code}: {r.text[:300]}")
            datos = r.json()
            if "data" not in datos:
                raise ErrorOpenRouter(f"Respuesta inesperada de embeddings: {str(datos)[:300]}")
            filas = sorted(datos["data"], key=lambda d: d["index"])
            vectores += [f["embedding"] for f in filas]
            tokens += (datos.get("usage") or {}).get("prompt_tokens", 0) or 0
    finally:
        if propio:
            await cliente.aclose()
    if len(vectores) != len(textos):
        raise ErrorOpenRouter(f"Se pidieron {len(textos)} embeddings y llegaron {len(vectores)}")
    return vectores, tokens


# ---------------------------------------------------------------- chat con streaming

@dataclass
class UsoChat:
    """Métricas de una respuesta; se llena al terminar el stream."""
    modelo: str = ""
    proveedor: str | None = None
    tokens_entrada: int | None = None
    tokens_salida: int | None = None
    costo_usd: float | None = None
    ttft_ms: int | None = None
    latencia_ms: int | None = None
    intentos: int = 0
    extra: dict = field(default_factory=dict)


async def chat_stream(mensajes: list[dict], uso: UsoChat, cliente: httpx.AsyncClient | None = None) -> AsyncIterator[str]:
    """
    Genera los trozos de texto de la respuesta a medida que llegan.
    Al terminar, `uso` tiene tokens, costo, proveedor, TTFT y latencia.

    Reintenta UNA vez si falla antes del primer token (429/5xx, corte de red) o si la respuesta
    llega vacía (en el examen, un proveedor devolvía respuestas vacías). Si ya se envió texto al
    usuario, no se reintenta: se lanza ErrorOpenRouter.
    """
    cfg = settings()
    cuerpo = {
        "model": cfg.modelo_chat,
        "messages": mensajes,
        "temperature": 0,
        "max_tokens": cfg.max_tokens_respuesta,
        "stream": True,
        # sort=latency: en el examen contra el backend, algunos proveedores tardaban 12-22 s en el primer token
        "provider": {"ignore": ["Alibaba"], "data_collection": "deny", "sort": "latency"},
        "usage": {"include": True},
    }
    uso.modelo = cfg.modelo_chat
    propio = cliente is None
    cliente = cliente or _cliente(httpx.Timeout(30, connect=10))
    t0 = time.perf_counter()
    try:
        for intento in (1, 2):
            uso.intentos = intento
            emitido = False
            try:
                async with cliente.stream("POST", f"{cfg.openrouter_url}/chat/completions", json=cuerpo, headers=_cabeceras()) as r:
                    if r.status_code != 200:
                        detalle = (await r.aread()).decode("utf-8", "replace")[:300]
                        if r.status_code in (429, 500, 502, 503, 504) and intento == 1:
                            await asyncio.sleep(1.5)
                            continue
                        raise ErrorOpenRouter(f"Chat HTTP {r.status_code}: {detalle}")
                    async for linea in r.aiter_lines():
                        if not linea.startswith("data:"):
                            continue  # comentarios ": OPENROUTER PROCESSING" y líneas vacías
                        dato = linea[5:].strip()
                        if dato == "[DONE]":
                            break
                        try:
                            ev = json.loads(dato)
                        except json.JSONDecodeError:
                            continue
                        if "error" in ev:
                            raise ErrorOpenRouter(f"Error del proveedor: {str(ev['error'])[:300]}")
                        uso.proveedor = ev.get("provider") or uso.proveedor
                        if ev.get("usage"):
                            u = ev["usage"]
                            uso.tokens_entrada = u.get("prompt_tokens")
                            uso.tokens_salida = u.get("completion_tokens")
                            uso.costo_usd = u.get("cost")
                        for ch in ev.get("choices") or []:
                            texto = (ch.get("delta") or {}).get("content")
                            if texto:
                                if uso.ttft_ms is None:
                                    uso.ttft_ms = int((time.perf_counter() - t0) * 1000)
                                emitido = True
                                yield texto
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if not emitido and intento == 1:
                    await asyncio.sleep(1.0)
                    continue
                raise ErrorOpenRouter(f"Red: {type(e).__name__}") from e
            if emitido:
                return
            if intento == 1:
                continue  # respuesta vacía: un reintento
            raise ErrorOpenRouter("El modelo devolvió una respuesta vacía")
    finally:
        uso.latencia_ms = int((time.perf_counter() - t0) * 1000)
        if propio:
            await cliente.aclose()


# ---------------------------------------------------------------- embeddings locales (solo pruebas)

DIM_LOCAL = 512


def _normalizar(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def embedding_local(texto: str) -> list[float]:
    """Hashing de palabras y trigramas de caracteres. Determinista, sin red, sin costo."""
    v = [0.0] * DIM_LOCAL
    palabras = re.findall(r"[a-z0-9]+", _normalizar(texto))
    rasgos = palabras + [p[i : i + 3] for p in palabras for i in range(max(1, len(p) - 2))]
    rasgos = rasgos or ["<vacio>"]  # evita el vector cero (similitud NaN)
    for r in rasgos:
        h = int(hashlib.md5(r.encode()).hexdigest(), 16)
        v[h % DIM_LOCAL] += 1.0 if (h >> 9) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]
