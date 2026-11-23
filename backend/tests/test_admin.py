"""
Panel /admin de punta a punta: login, documentos (subir, reemplazar, borrar) y preguntas frecuentes.

Usa una COPIA temporal del corpus ficticio del examen, para no tocar los documentos reales.
Solo corre con TEST_DATABASE_URL (base vacía y desechable).
"""

import asyncio
import os
import shutil

import httpx
import psycopg
import pytest

from app import openrouter
from app.config import RAIZ, settings
from tests.openrouter_falso import OpenRouterFalso

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="definir TEST_DATABASE_URL para correr la integración")
CLAVE = "clave-de-prueba-123"


@pytest.fixture
def panel(monkeypatch, tmp_path):
    carpeta = tmp_path / "conocimiento"
    shutil.copytree(RAIZ / "examen" / "conocimiento_ficticio_fase1", carpeta)  # corpus ficticio del examen
    monkeypatch.setenv("CARPETA_CONOCIMIENTO", str(carpeta))
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("MODELO_EMBEDDINGS", "local")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-falsa")
    monkeypatch.setenv("UMBRAL_SIMILITUD", "0.05")
    monkeypatch.setenv("ADMIN_PASSWORD", CLAVE)
    monkeypatch.setenv("ENTORNO", "desarrollo")
    monkeypatch.setenv("LIMITE_IP_MINUTO", "1000")
    settings.cache_clear()

    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    from app.ingesta import ingestar
    from app.migrar import migrar

    asyncio.run(migrar())
    asyncio.run(ingestar())

    falso = OpenRouterFalso()
    monkeypatch.setattr(openrouter, "_transporte", httpx.MockTransport(falso))

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as cliente:
        yield cliente, carpeta
    settings.cache_clear()


def entrar(cliente):
    r = cliente.post("/api/admin/login", json={"password": CLAVE})
    assert r.status_code == 200
    return cliente


def test_panel_pide_sesion(panel):
    cliente, _ = panel
    assert cliente.get("/admin").status_code == 200
    assert cliente.get("/api/admin/documentos").status_code == 401
    assert cliente.get("/api/admin/resumen").status_code == 401


def test_contraseña_incorrecta_y_cookie_correcta(panel):
    cliente, _ = panel
    r = cliente.post("/api/admin/login", json={"password": "mala"})
    assert r.status_code == 401
    r = cliente.post("/api/admin/login", json={"password": CLAVE})
    assert r.status_code == 200
    assert "admin_sesion" in r.headers.get("set-cookie", "")
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert cliente.get("/api/admin/sesion").status_code == 200


def test_sesion_falsa_no_entra(panel):
    cliente, _ = panel
    cliente.cookies.set("admin_sesion", "9999999999.abc.firmamala")
    assert cliente.get("/api/admin/documentos").status_code == 401


def test_sin_contraseña_configurada_el_panel_se_desactiva(panel, monkeypatch):
    cliente, _ = panel
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    settings.cache_clear()
    assert cliente.post("/api/admin/login", json={"password": "x"}).status_code == 503


def test_listar_subir_reemplazar_y_borrar(panel):
    cliente, carpeta = panel
    entrar(cliente)

    docs = cliente.get("/api/admin/documentos").json()["documentos"]
    antes = {d["ruta"] for d in docs}
    assert "admisiones.md" in antes

    nuevo = "horario_nuevo.md"
    contenido = "---\ntitulo: Horario especial\narea: general\n---\n\n# Horario especial\n\n## Vacaciones\nLa oficina cierra del 20 al 31 de diciembre.\n"
    r = cliente.post("/api/admin/documentos", files={"archivo": (nuevo, contenido.encode(), "text/markdown")})
    assert r.status_code == 200, r.text
    assert r.json()["fragmentos"] >= 1 and r.json()["reemplazado"] is False
    assert (carpeta / nuevo).exists()

    # reemplazar: mismo nombre, otra versión
    r = cliente.post("/api/admin/documentos", files={"archivo": (nuevo, b"## Vacaciones\nCierre distinto.\n", "text/markdown")})
    assert r.status_code == 200 and r.json()["reemplazado"] is True

    docs = cliente.get("/api/admin/documentos").json()["documentos"]
    assert any(d["ruta"] == nuevo and d["fragmentos"] >= 1 for d in docs)

    assert cliente.delete(f"/api/admin/documentos/{nuevo}").status_code == 200
    assert not (carpeta / nuevo).exists()
    docs = cliente.get("/api/admin/documentos").json()["documentos"]
    assert not any(d["ruta"] == nuevo for d in docs)


def test_nombres_peligrosos_se_rechazan(panel):
    cliente, carpeta = panel
    entrar(cliente)
    for nombre in ["../secreto.md", "Mi Archivo.pdf", "a/b.md", "script.exe", ".oculto.md", "_areas.yaml"]:
        r = cliente.post("/api/admin/documentos", files={"archivo": (nombre, b"hola", "text/plain")})
        assert r.status_code == 400, nombre
    assert not (carpeta.parent / "secreto.md").exists()
    # una ruta con ".." no coincide con ningún documento: no se borra nada fuera de conocimiento/
    assert cliente.delete("/api/admin/documentos/..%2Fconfig.py").status_code in (400, 404)
    assert (RAIZ / "backend" / "app" / "config.py").exists()


def test_archivo_sin_texto_no_se_guarda(panel):
    cliente, carpeta = panel
    entrar(cliente)
    r = cliente.post("/api/admin/documentos", files={"archivo": ("vacio_util.md", b"# Solo titulo\n", "text/markdown")})
    assert r.status_code == 422
    assert not (carpeta / "vacio_util.md").exists()


def test_archivo_muy_grande(panel, monkeypatch):
    cliente, _ = panel
    entrar(cliente)
    monkeypatch.setenv("MAX_SUBIDA_MB", "1")
    settings.cache_clear()
    r = cliente.post("/api/admin/documentos", files={"archivo": ("grande.md", b"x" * (1024 * 1024 + 10), "text/markdown")})
    assert r.status_code == 413


def test_preguntas_frecuentes_y_sin_respuesta(panel):
    cliente, _ = panel
    # tres preguntas casi iguales y una distinta; una sin respuesta
    for texto in ["¿Cuánto cuesta la prueba de admisión?", "cuanto cuesta la prueba de admision", "¿Cuánto cuesta la prueba de admisión, por favor?"]:
        cliente.post("/api/chat", json={"mensaje": texto})
    cliente.post("/api/chat", json={"mensaje": "¿Cuál es el horario de la biblioteca?"})
    cliente.post("/api/chat", json={"mensaje": "¿Cuándo es la graduación de 2028?"})

    entrar(cliente)
    faq = cliente.get("/api/admin/preguntas?dias=30").json()
    assert faq["total"] >= 4
    assert faq["grupos"][0]["veces"] >= 2  # las preguntas parecidas se agrupan

    res = cliente.get("/api/admin/resumen?dias=7").json()
    assert res["preguntas"] >= 4 and res["documentos"] >= 1

    sin = cliente.get("/api/admin/sin-respuesta?dias=30").json()
    assert isinstance(sin["grupos"], list)


def test_borrar_documento_inexistente(panel):
    cliente, _ = panel
    entrar(cliente)
    assert cliente.delete("/api/admin/documentos/no_existe.md").status_code == 404
