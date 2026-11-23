"""
Rutas públicas del widget:

- POST /api/chat        pregunta → respuesta en SSE (eventos: conversacion, fuentes, delta, fin, error)
- POST /api/feedback    👍/👎 de una respuesta
- GET  /api/sugerencias preguntas sugeridas para el estado vacío
- GET  /api/salud       health check
"""

import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db
from ..chat import responder
from ..config import settings

log = logging.getLogger("chatbot")
router = APIRouter(prefix="/api")

# Solo preguntas con respuesta en los documentos oficiales de conocimiento/.
SUGERENCIAS_FIJAS = [
    "¿Cómo aplico a la UVG?",
    "¿Qué becas ofrece la UVG?",
    "No sé qué carrera estudiar, ¿me ayudas?",
    "¿Qué servicios tiene la biblioteca?",
]


class Turno(BaseModel):
    rol: str = Field(pattern="^(usuario|asistente)$")
    texto: str = Field(max_length=4000)


class PeticionChat(BaseModel):
    mensaje: str = Field(max_length=2000)  # el largo real lo valida chat.py con LARGO_MAX_MENSAJE
    conversacion_id: str | None = Field(default=None, max_length=64)
    historial: list[Turno] = Field(default_factory=list, max_length=20)


class PeticionFeedback(BaseModel):
    conversacion_id: str = Field(max_length=64)
    mensaje_id: int
    valor: int = Field(ge=-1, le=1)


def ip_cliente(request: Request) -> str | None:
    # Casi cualquier proxy o plataforma de hosting agrega X-Forwarded-For; el primer valor es el cliente.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def origen_permitido(request: Request) -> tuple[bool, str | None]:
    """
    Un navegador manda `Origin`. Se acepta el propio dominio (el iframe del widget) y los de
    DOMINIOS_PERMITIDOS (y sus subdominios). Sin `Origin` (curl, el examen) se acepta: los topes
    por IP siguen aplicando.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True, None
    host = (urlparse(origin).hostname or "").lower()
    propio = (request.url.hostname or "").lower()
    if host == propio:
        return True, host
    for d in settings().lista_dominios:
        if host == d or host.endswith("." + d):
            return True, host
    return False, host


def _sse(evento: str, datos) -> bytes:
    return f"event: {evento}\ndata: {json.dumps(datos, ensure_ascii=False)}\n\n".encode()


@router.post("/chat")
async def chat(p: PeticionChat, request: Request):
    ok, origen = origen_permitido(request)
    if not ok:
        raise HTTPException(status_code=403, detail="origen no permitido")

    async def eventos():
        try:
            async for nombre, datos in responder(
                p.mensaje, [t.model_dump() for t in p.historial], p.conversacion_id, ip_cliente(request), origen
            ):
                yield _sse(nombre, datos)
        except Exception:
            log.exception("error inesperado en /api/chat")
            yield _sse("error", {"codigo": "interno", "mensaje": "Ocurrió un error. Intenta de nuevo."})

    return StreamingResponse(
        eventos(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/feedback")
async def feedback(p: PeticionFeedback):
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "UPDATE mensajes SET feedback = %s WHERE id = %s AND conversacion_id::text = %s AND rol = 'asistente'",
            (p.valor, p.mensaje_id, p.conversacion_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="mensaje no encontrado")
    return {"ok": True}


@router.get("/sugerencias")
async def sugerencias():
    # Fase 4: saldrán de las preguntas frecuentes reales; mientras tanto, una lista fija.
    return {"sugerencias": SUGERENCIAS_FIJAS}


@router.get("/salud")
async def salud():
    try:
        async with db.pool().connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM fragmentos")
            (n,) = await cur.fetchone()
    except Exception:
        raise HTTPException(status_code=503, detail="base de datos no disponible")
    return {"estado": "ok", "fragmentos": n, "modelo_chat": settings().modelo_chat}
