"""
Panel de administración (fase 4). Se abre en /admin.

- Acceso con contraseña (ADMIN_PASSWORD). Sesión firmada en cookie HttpOnly, de 8 horas.
  Límite de 10 intentos de inicio de sesión por IP al día.
- Documentos: subir, reemplazar y borrar archivos de conocimiento/ desde el navegador.
  Cada cambio se indexa al instante con la misma ingesta de siempre (sin correr comandos).
- Preguntas frecuentes y preguntas sin respuesta, agrupadas por similitud de significado.
- Resumen de uso: preguntas, sin respuesta, 👍/👎, costo y tiempo al primer token.
"""

import hashlib
import hmac
import math
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from . import db
from .config import settings
from .ingesta import ingestar
from .privacidad import hash_ip
from .rutas.publico import ip_cliente

router = APIRouter(prefix="/api/admin")

COOKIE = "admin_sesion"
DURACION_S = 8 * 3600
INTENTOS_LOGIN_DIA = 10
UMBRAL_MISMA_PREGUNTA = 0.85
# Nombres seguros: sin rutas, sin mayúsculas ni espacios. Evita escribir fuera de conocimiento/.
NOMBRE_VALIDO = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}\.(md|txt|pdf|docx)$")


# ---------------------------------------------------------------- sesión

def _firma(payload: str) -> str:
    return hmac.new(settings().secreto_sesion.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _nueva_sesion() -> str:
    payload = f"{int(time.time()) + DURACION_S}.{secrets.token_hex(8)}"
    return f"{payload}.{_firma(payload)}"


def _sesion_valida(token: str) -> bool:
    try:
        exp, nonce, firma = token.split(".")
        return hmac.compare_digest(firma, _firma(f"{exp}.{nonce}")) and int(exp) > time.time()
    except ValueError:
        return False


async def requiere_admin(request: Request) -> None:
    if not settings().admin_password:
        raise HTTPException(status_code=503, detail="El panel está desactivado: falta ADMIN_PASSWORD en el .env.")
    if not _sesion_valida(request.cookies.get(COOKIE, "")):
        raise HTTPException(status_code=401, detail="Inicia sesión para continuar.")


class Acceso(BaseModel):
    password: str = Field(max_length=200)


@router.post("/login")
async def iniciar_sesion(p: Acceso, request: Request, response: Response):
    cfg = settings()
    if not cfg.admin_password:
        raise HTTPException(status_code=503, detail="El panel está desactivado: falta ADMIN_PASSWORD en el .env.")
    clave_ip = f"login:{hash_ip(ip_cliente(request)) or 'sin-ip'}"
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "INSERT INTO uso (clave, dia, conteo) VALUES (%s, current_date, 1) "
            "ON CONFLICT (clave, dia) DO UPDATE SET conteo = uso.conteo + 1 RETURNING conteo",
            (clave_ip,),
        )
        (intentos,) = await cur.fetchone()
    if intentos > INTENTOS_LOGIN_DIA:
        raise HTTPException(status_code=429, detail="Demasiados intentos hoy. Intenta mañana.")
    if not hmac.compare_digest(p.password.encode(), cfg.admin_password.encode()):
        raise HTTPException(status_code=401, detail="Contraseña incorrecta.")
    response.set_cookie(
        COOKIE, _nueva_sesion(), httponly=True, samesite="strict",
        secure=not cfg.es_desarrollo, max_age=DURACION_S, path="/",
    )
    return {"ok": True}


@router.post("/logout")
async def cerrar_sesion(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/sesion", dependencies=[Depends(requiere_admin)])
async def sesion_activa():
    return {"ok": True}


# ---------------------------------------------------------------- documentos

@router.get("/documentos", dependencies=[Depends(requiere_admin)])
async def listar_documentos():
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            """SELECT d.ruta, d.titulo, d.area, d.actualizado_en, count(f.id)
                 FROM documentos d LEFT JOIN fragmentos f ON f.documento_id = d.id
                GROUP BY d.id ORDER BY d.titulo"""
        )
        filas = await cur.fetchall()
    return {"documentos": [
        {"ruta": r, "titulo": t, "area": a, "actualizado": u.isoformat(), "fragmentos": n}
        for r, t, a, u, n in filas
    ]}


async def _fragmentos_de(ruta: str) -> int:
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "SELECT count(f.id) FROM documentos d LEFT JOIN fragmentos f ON f.documento_id = d.id WHERE d.ruta = %s",
            (ruta,),
        )
        return (await cur.fetchone())[0]


@router.post("/documentos", dependencies=[Depends(requiere_admin)])
async def subir_documento(archivo: UploadFile = File(...)):
    nombre = (archivo.filename or "").strip().lower()
    if not NOMBRE_VALIDO.match(nombre):
        raise HTTPException(
            status_code=400,
            detail="Nombre no válido. Usa letras minúsculas, números, guiones y guion bajo, y una extensión .md, .txt, .pdf o .docx. Ejemplo: biblioteca_horarios.pdf",
        )
    limite = settings().max_subida_mb * 1024 * 1024
    datos = await archivo.read(limite + 1)
    if len(datos) > limite:
        raise HTTPException(status_code=413, detail=f"El archivo supera {settings().max_subida_mb} MB.")
    if not datos:
        raise HTTPException(status_code=400, detail="El archivo está vacío.")

    carpeta = settings().carpeta_conocimiento
    destino = carpeta / nombre
    existia = destino.exists()
    anterior = destino.read_bytes() if existia else None
    destino.write_bytes(datos)

    try:
        await ingestar()
    except Exception as e:  # error de red o de OpenRouter: se deshace el cambio
        _restaurar(destino, anterior)
        raise HTTPException(status_code=502, detail=f"No pude indexar el documento: {e}. Intenta de nuevo.")

    fragmentos = await _fragmentos_de(nombre)
    if fragmentos == 0:
        _restaurar(destino, anterior)
        await ingestar()
        raise HTTPException(
            status_code=422,
            detail="No encontré texto en el documento. Si es un PDF escaneado (una foto), no se puede leer: súbelo como Word o como texto.",
        )
    return {"ruta": nombre, "reemplazado": existia, "fragmentos": fragmentos}


def _restaurar(destino, anterior: bytes | None) -> None:
    if anterior is None:
        destino.unlink(missing_ok=True)
    else:
        destino.write_bytes(anterior)


@router.delete("/documentos/{ruta}", dependencies=[Depends(requiere_admin)])
async def borrar_documento(ruta: str):
    if not NOMBRE_VALIDO.match(ruta):
        raise HTTPException(status_code=400, detail="Nombre no válido.")
    destino = settings().carpeta_conocimiento / ruta
    if not destino.exists():
        raise HTTPException(status_code=404, detail="No existe ese documento.")
    destino.unlink()
    await ingestar()  # quita sus fragmentos de la base
    return {"ok": True}


# ---------------------------------------------------------------- preguntas

@dataclass
class _Pregunta:
    id: int
    texto: str
    vec: list[float]
    estado: str | None
    feedback: int


def _vector(texto: str) -> list[float]:
    return [float(x) for x in texto.strip("[]").split(",")]


def _coseno(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


async def _preguntas(dias: int) -> list[_Pregunta]:
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            """SELECT m.id, m.texto, m.embedding_pregunta::text,
                      (SELECT a.estado FROM mensajes a WHERE a.conversacion_id = m.conversacion_id
                         AND a.rol = 'asistente' AND a.id > m.id ORDER BY a.id LIMIT 1),
                      (SELECT a.feedback FROM mensajes a WHERE a.conversacion_id = m.conversacion_id
                         AND a.rol = 'asistente' AND a.id > m.id ORDER BY a.id LIMIT 1)
                 FROM mensajes m
                WHERE m.rol = 'usuario' AND m.embedding_pregunta IS NOT NULL
                  AND m.creada_en >= now() - make_interval(days => %s)
                ORDER BY m.id""",
            (dias,),
        )
        filas = await cur.fetchall()
    return [_Pregunta(i, t, _vector(v), e, fb or 0) for i, t, v, e, fb in filas]


def _agrupar(preguntas: list[_Pregunta]) -> list[dict]:
    """Agrupa preguntas que significan lo mismo (similitud ≥ 0.85) con un recorrido simple."""
    grupos: list[dict] = []
    for p in preguntas:
        for g in grupos:
            if _coseno(p.vec, g["vec"]) >= UMBRAL_MISMA_PREGUNTA:
                g["items"].append(p)
                break
        else:
            grupos.append({"vec": p.vec, "texto": p.texto, "items": [p]})
    salida = []
    for g in grupos:
        items = g["items"]
        salida.append({
            "pregunta": g["texto"],
            "veces": len(items),
            "sin_respuesta": sum(1 for p in items if p.estado in ("sin_info", "no_sabe")),
            "util": sum(1 for p in items if p.feedback == 1),
            "no_util": sum(1 for p in items if p.feedback == -1),
            "ultima": items[-1].id,
        })
    salida.sort(key=lambda g: (-g["veces"], -g["ultima"]))
    return salida


@router.get("/preguntas", dependencies=[Depends(requiere_admin)])
async def preguntas_frecuentes(dias: int = Query(30, ge=1, le=365)):
    grupos = _agrupar(await _preguntas(dias))
    return {"grupos": grupos[:50], "total": sum(g["veces"] for g in grupos)}


@router.get("/sin-respuesta", dependencies=[Depends(requiere_admin)])
async def preguntas_sin_respuesta(dias: int = Query(30, ge=1, le=365)):
    pendientes = [p for p in await _preguntas(dias) if p.estado in ("sin_info", "no_sabe") or p.feedback == -1]
    return {"grupos": _agrupar(pendientes)[:50], "total": len(pendientes)}


# ---------------------------------------------------------------- resumen

@router.get("/resumen", dependencies=[Depends(requiere_admin)])
async def resumen(dias: int = Query(7, ge=1, le=365)):
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FILTER (WHERE m.rol = 'usuario'),
                      count(*) FILTER (WHERE m.rol = 'asistente' AND m.estado IN ('sin_info', 'no_sabe')),
                      count(*) FILTER (WHERE m.rol = 'asistente' AND m.feedback = 1),
                      count(*) FILTER (WHERE m.rol = 'asistente' AND m.feedback = -1),
                      COALESCE(sum(m.costo_usd) FILTER (WHERE m.rol = 'asistente'), 0),
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY m.ttft_ms)
                        FILTER (WHERE m.rol = 'asistente' AND m.ttft_ms IS NOT NULL)
                 FROM mensajes m
                WHERE m.creada_en >= now() - make_interval(days => %s)""",
            (dias,),
        )
        preguntas, sin, util, no_util, costo, ttft = await cur.fetchone()
        cur = await conn.execute("SELECT count(*) FROM documentos")
        (documentos,) = await cur.fetchone()
    costo = float(costo or Decimal(0))
    return {
        "dias": dias,
        "preguntas": preguntas,
        "sin_respuesta": sin,
        "util": util,
        "no_util": no_util,
        "costo_usd": round(costo, 4),
        "ttft_p50_ms": int(ttft) if ttft is not None else None,
        "documentos": documentos,
    }
