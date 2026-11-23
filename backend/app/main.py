"""
Aplicación FastAPI (monolito): API + (desde la fase 3) los builds de React.

    uvicorn app.main:app --reload

Rutas:
- /api/chat, /api/feedback, /api/sugerencias, /api/salud   → rutas/publico.py
- GET /api/buscar?q=...  búsqueda híbrida, SOLO en desarrollo (para revisar la recuperación)
- /api/docs              documentación interactiva, SOLO en desarrollo
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from . import db
from .busqueda import buscar
from .config import settings
from .limites import limpiar as limpiar_topes
from . import admin
from .rutas import publico

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("chatbot")


async def retencion() -> None:
    """Borra conversaciones viejas (y sus mensajes, en cascada) y contadores de días pasados."""
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "DELETE FROM conversaciones WHERE creada_en < now() - make_interval(days => %s)",
            (settings().retencion_dias,),
        )
        await limpiar_topes(conn)
    if cur.rowcount:
        log.info("retención: %s conversación(es) borrada(s)", cur.rowcount)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.abrir_pool()
    try:
        await retencion()
    except Exception:
        log.exception("no se pudo aplicar la retención al arrancar")
    yield
    await db.cerrar_pool()


app = FastAPI(
    title="Chatbot UVG",
    lifespan=lifespan,
    docs_url="/api/docs" if settings().es_desarrollo else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings().es_desarrollo else None,
)
app.include_router(publico.router)
app.include_router(admin.router)

WEB = Path(__file__).resolve().parents[1] / "web"

# Cabeceras de seguridad (ARQUITECTURA.md §10). La página es de origen propio, sin recursos externos.
@app.middleware("http")
async def cabeceras_seguridad(request: Request, call_next):
    respuesta = await call_next(request)
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    respuesta.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    )
    return respuesta


@app.get("/", include_in_schema=False)
async def pagina_principal():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/admin", include_in_schema=False)
async def pagina_admin():
    return FileResponse(WEB / "admin.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/buscar")
async def api_buscar(q: str = Query(..., min_length=2, max_length=500)):
    """Herramienta de desarrollo: muestra qué fragmentos recibiría el LLM. Se desactiva en producción."""
    if not settings().es_desarrollo:
        raise HTTPException(status_code=404)
    async with db.pool().connection() as conn:
        b = await buscar(conn, q)
    return {
        "consulta": b.consulta,
        "mejor_similitud": round(b.mejor_similitud, 4),
        "umbral": settings().umbral_similitud,
        "llamaria_al_llm": b.suficiente,
        "resultados": [
            {
                "n": i,
                "documento": r.ruta,
                "titulo": r.titulo,
                "seccion": r.seccion,
                "similitud": round(r.similitud, 4),
                "rango_texto": round(r.rango_texto, 4),
                "puntaje": round(r.puntaje, 5),
                "texto": r.texto[:300],
            }
            for i, r in enumerate(b.resultados, 1)
        ],
    }
