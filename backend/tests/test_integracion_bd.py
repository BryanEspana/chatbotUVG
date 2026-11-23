"""
Integración con PostgreSQL + pgvector: migrar → ingestar → buscar.

Solo corre si existe la variable TEST_DATABASE_URL (una base VACÍA y desechable), p. ej.:

    createdb chatbot_uvg_test
    TEST_DATABASE_URL=postgresql://localhost:5432/chatbot_uvg_test pytest

Usa embeddings "local" (sin red, sin costo) y el corpus ficticio del examen
(examen/conocimiento_ficticio_fase1/), para que las aserciones no dependan de los documentos reales.
"""

import asyncio
import os

import pytest

from app.config import RAIZ, settings

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="definir TEST_DATABASE_URL para correr la integración")


@pytest.fixture
def bd(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("MODELO_EMBEDDINGS", "local")
    monkeypatch.setenv("CARPETA_CONOCIMIENTO", str(RAIZ / "examen" / "conocimiento_ficticio_fase1"))
    settings.cache_clear()

    import psycopg

    with psycopg.connect(URL, autocommit=True) as c:  # base limpia en cada prueba
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    yield
    settings.cache_clear()


def test_migrar_ingestar_buscar(bd):
    from app.busqueda import buscar
    from app.db import conectar
    from app.ingesta import ingestar
    from app.migrar import migrar

    async def correr():
        assert await migrar() == ["001_inicial.sql"]
        assert await migrar() == []  # idempotente

        r1 = await ingestar()
        assert r1["nuevos_o_cambiados"] >= 1 and r1["errores"] == 0
        r2 = await ingestar()
        assert r2["nuevos_o_cambiados"] == 0 and r2["sin_cambios"] == r1["nuevos_o_cambiados"]

        conn = await conectar()
        try:
            b = await buscar(conn, "red wifi UVG-Estudiantes")
            assert b.resultados, "la búsqueda no devolvió nada"
            assert b.resultados[0].ruta == "plataformas_digitales.md"
            assert b.resultados[0].seccion.startswith("Wifi")
            assert len(b.resultados) <= settings().top_k

            seg = await buscar(conn, "¿y la puedo repetir?", ["¿Cuánto cuesta la prueba de admisión?"])
            assert seg.consulta.startswith("¿Cuánto cuesta la prueba")
            assert any(r.seccion.startswith("Prueba de admisión") for r in seg.resultados)

            # entradas raras no rompen el SQL
            for q in ["'; DROP TABLE fragmentos; --", "a & b | !c", "¿y?"]:
                await buscar(conn, q)
            cur = await conn.execute("SELECT count(*) FROM fragmentos")
            assert (await cur.fetchone())[0] > 0
        finally:
            await conn.close()

    asyncio.run(correr())
