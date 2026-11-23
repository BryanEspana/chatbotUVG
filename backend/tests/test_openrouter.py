"""Cliente de embeddings contra un OpenRouter simulado (sin red, sin costo)."""

import asyncio
import json
import math

import httpx
import pytest

from app import openrouter
from app.config import settings


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MODELO_EMBEDDINGS", "openai/text-embedding-3-small")
    settings.cache_clear()
    yield
    settings.cache_clear()


def _servidor(registro, rechazar_provider=False):
    def manejar(request: httpx.Request) -> httpx.Response:
        cuerpo = json.loads(request.content)
        registro.append(cuerpo)
        if rechazar_provider and "provider" in cuerpo:
            return httpx.Response(400, json={"error": {"message": "provider not supported"}})
        # devuelve los vectores DESORDENADOS a propósito, con su índice
        data = [{"index": i, "embedding": [float(i), float(len(t))]} for i, t in enumerate(cuerpo["input"])]
        return httpx.Response(200, json={"data": list(reversed(data)), "usage": {"prompt_tokens": 7}})

    return httpx.AsyncClient(transport=httpx.MockTransport(manejar))


def test_lotes_y_orden(config):
    registro = []
    textos = [f"texto {i}" for i in range(150)]  # 3 lotes de 64, 64 y 22

    async def correr():
        async with _servidor(registro) as cliente:
            return await openrouter.embeddings(textos, cliente)

    vectores, tokens = asyncio.run(correr())
    assert [len(c["input"]) for c in registro] == [64, 64, 22]
    assert vectores[0] == [0.0, 7.0] and vectores[63] == [63.0, 8.0] and vectores[64] == [0.0, 8.0]
    assert tokens == 21
    assert all(c["provider"] == {"data_collection": "deny"} for c in registro)
    assert registro[0]["model"] == "openai/text-embedding-3-small"


def test_reintenta_sin_provider_si_lo_rechaza(config):
    registro = []

    async def correr():
        async with _servidor(registro, rechazar_provider=True) as cliente:
            return await openrouter.embeddings(["hola"], cliente)

    vectores, _ = asyncio.run(correr())
    assert len(vectores) == 1
    assert "provider" in registro[0] and "provider" not in registro[1]


def test_sin_key_da_error_claro(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("MODELO_EMBEDDINGS", "openai/text-embedding-3-small")
    settings.cache_clear()
    with pytest.raises(openrouter.ErrorOpenRouter, match="OPENROUTER_API_KEY"):
        asyncio.run(openrouter.embeddings(["hola"]))
    settings.cache_clear()


def test_embedding_local_determinista_y_normalizado():
    a = openrouter.embedding_local("Horario de la biblioteca")
    assert a == openrouter.embedding_local("horario de la BIBLIOTECA")  # sin mayúsculas
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-9)
    vacio = openrouter.embedding_local("日本語")  # sin rasgos latinos: no debe ser el vector cero
    assert any(vacio)


def test_prefijo_de_consulta_solo_para_qwen(monkeypatch):
    monkeypatch.setenv("MODELO_EMBEDDINGS", "qwen/qwen3-embedding-0.6b")
    settings.cache_clear()
    assert openrouter.preparar_consulta("hola").startswith("Instruct:")
    monkeypatch.setenv("MODELO_EMBEDDINGS", "openai/text-embedding-3-small")
    settings.cache_clear()
    assert openrouter.preparar_consulta("hola") == "hola"
    settings.cache_clear()
