"""
Orquesta una respuesta (ARQUITECTURA.md §7):

    topes → enmascarar → ¿charla? → embedding → búsqueda → ¿umbral? → LLM en streaming → posproceso → guardar → fin

El modelo redacta SIEMPRE la respuesta, para que la conversación suene humana:
- Charla (saludo, gracias, despedida): no se busca nada; el modelo conversa sin fragmentos.
- Pregunta bajo el umbral: el modelo recibe cero fragmentos, así que no puede dar datos de la UVG;
  dice con naturalidad que no tiene esa información o reconduce si el tema es ajeno a la UVG.
- Pregunta con contexto: el modelo responde solo con los fragmentos y los cita.

Es un generador de eventos (nombre, datos) que la ruta convierte a SSE:
    conversacion · fuentes · delta · fin · error

Las conexiones a la BD se piden por pasos cortos (no se retiene una conexión mientras el modelo
escribe), para que el pool aguante varias conversaciones a la vez.
"""

import logging
import uuid
from typing import AsyncIterator

from . import db
from .busqueda import armar_consulta, buscar
from .charla import es_charla
from .config import settings
from .db import vector_sql
from .ingesta import cargar_areas
from .limites import registrar_pregunta
from .openrouter import ErrorOpenRouter, UsoChat, chat_stream, embeddings, preparar_consulta
from .posproceso import NormalizadorStream, citas, dice_no_saber, filtra_prompt, tiene_contacto
from .privacidad import enmascarar, hash_ip
from .prompt import armar_mensajes

log = logging.getLogger("chatbot")

TEXTO_ERROR = "No pude responder ahora. Intenta de nuevo en un momento."
TEXTO_FILTRADO = "Solo puedo ayudarte con información de la UVG. ¿Qué te gustaría saber?"

Evento = tuple[str, dict | list]


def contacto_de(area: str | None) -> dict:
    areas = cargar_areas(settings().carpeta_conocimiento)
    datos = areas.get(area or "general") or areas.get("general") or {}
    return {k: datos.get(k) for k in ("nombre", "correo", "telefono", "horario") if datos.get(k)}


def _historial_valido(historial: list[dict]) -> list[dict]:
    """Últimos N turnos, solo roles conocidos, texto enmascarado y recortado."""
    limpio = [
        {"rol": t["rol"], "texto": enmascarar(str(t["texto"]))[:1500]}
        for t in historial
        if t.get("rol") in ("usuario", "asistente") and str(t.get("texto", "")).strip()
    ]
    return limpio[-settings().historial_max:]


async def _conversacion(conn, conversacion_id: str | None, origen: str | None, ip_h: str | None) -> str:
    if conversacion_id:
        try:
            uuid.UUID(conversacion_id)
            cur = await conn.execute("SELECT id FROM conversaciones WHERE id = %s", (conversacion_id,))
            if await cur.fetchone():
                return conversacion_id
        except ValueError:
            pass
    cur = await conn.execute(
        "INSERT INTO conversaciones (origen, ip_hash) VALUES (%s, %s) RETURNING id", (origen, ip_h)
    )
    return str((await cur.fetchone())[0])


async def _guardar(conn, conv_id: str, rol: str, texto: str, **campos) -> int:
    columnas = ["conversacion_id", "rol", "texto"] + list(campos)
    valores = [conv_id, rol, texto] + list(campos.values())
    marcas = ["%s"] * len(columnas)
    if "embedding_pregunta" in campos:
        marcas[columnas.index("embedding_pregunta")] = "%s::vector"
    cur = await conn.execute(
        f"INSERT INTO mensajes ({', '.join(columnas)}) VALUES ({', '.join(marcas)}) RETURNING id", valores
    )
    return (await cur.fetchone())[0]


async def responder(
    pregunta: str,
    historial: list[dict],
    conversacion_id: str | None = None,
    ip: str | None = None,
    origen: str | None = None,
) -> AsyncIterator[Evento]:
    cfg = settings()
    pregunta = (pregunta or "").strip()
    if not pregunta or len(pregunta) > cfg.largo_max_mensaje:
        yield "error", {"codigo": "invalido", "mensaje": f"Escribe una pregunta de hasta {cfg.largo_max_mensaje} caracteres."}
        return

    ip_h = hash_ip(ip)
    pregunta_m = enmascarar(pregunta)

    # 1. Topes y conversación
    async with db.pool().connection() as conn:
        veredicto = await registrar_pregunta(conn, ip_h)
        conv_id = await _conversacion(conn, conversacion_id, origen, ip_h)
        if not veredicto.permitido:
            await _guardar(conn, conv_id, "usuario", pregunta_m, estado="bloqueado")
    yield "conversacion", {"id": conv_id}
    if not veredicto.permitido:
        yield "error", {"codigo": "limite", "motivo": veredicto.motivo, "mensaje": veredicto.mensaje,
                        "reintentar_en_s": veredicto.reintentar_en_s}
        return

    hist = _historial_valido(historial)
    charla = es_charla(pregunta)

    # 1b. Charla: sin embedding ni búsqueda; el modelo conversa sin fragmentos
    if charla:
        async with db.pool().connection() as conn:
            await _guardar(conn, conv_id, "usuario", pregunta_m)
        async for evento in _redactar(pregunta_m, hist, [], conv_id, charla=True):
            yield evento
        return

    # 2. Embedding de la consulta (pregunta + la anterior del usuario, para seguimientos)
    previas = [t["texto"] for t in hist if t["rol"] == "usuario"]
    try:
        # Dos vectores en una sola llamada: el de búsqueda (con el contexto del seguimiento) y el de
        # la pregunta sola, que es el que se guarda para agrupar preguntas frecuentes en el panel.
        vectores, _ = await embeddings([
            preparar_consulta(armar_consulta(pregunta_m, previas)),
            preparar_consulta(pregunta_m),
        ])
    except ErrorOpenRouter as e:
        log.error("embeddings: %s", e)
        yield "error", {"codigo": "modelo", "mensaje": TEXTO_ERROR}
        return
    vector, vector_pregunta = vectores

    # 3. Búsqueda y registro de la pregunta
    async with db.pool().connection() as conn:
        busqueda = await buscar(conn, pregunta_m, previas, vector=vector)
        await _guardar(conn, conv_id, "usuario", pregunta_m, embedding_pregunta=vector_sql(vector_pregunta))

    # 4. Si nada se parece lo suficiente, el modelo recibe cero fragmentos: puede conversar y
    #    decir que no tiene el dato, pero no tiene de dónde sacar datos de la UVG.
    res = busqueda.resultados if busqueda.suficiente else []
    async for evento in _redactar(pregunta_m, hist, res, conv_id):
        yield evento


async def _redactar(pregunta_m: str, hist: list[dict], res: list, conv_id: str, charla: bool = False) -> AsyncIterator[Evento]:
    """Fuentes (si hay) → LLM en streaming → posproceso → guardar → fin."""
    if res:
        yield "fuentes", [
            {"n": i, "documento": r.ruta, "titulo": r.titulo, "seccion": r.seccion, "url": r.url_publica, "texto": r.texto}
            for i, r in enumerate(res, 1)
        ]

    uso = UsoChat()
    normalizador = NormalizadorStream()
    partes: list[str] = []
    try:
        async for trozo in chat_stream(armar_mensajes(pregunta_m, hist, res), uso):
            t = normalizador.alimentar(trozo)
            if t:
                partes.append(t)
                yield "delta", {"t": t}
        t = normalizador.terminar()
        if t:
            partes.append(t)
            yield "delta", {"t": t}
    except ErrorOpenRouter as e:
        log.error("chat: %s", e)
        async with db.pool().connection() as conn:
            await _guardar(conn, conv_id, "asistente", "".join(partes) or "(error)", estado="error",
                           modelo=uso.modelo, latencia_ms=uso.latencia_ms)
        yield "error", {"codigo": "modelo", "mensaje": TEXTO_ERROR}
        return

    # 5. Posproceso
    texto = "".join(partes)
    validas, inventadas = citas(texto, len(res))
    if inventadas:
        log.warning("citas inventadas %s en conversación %s", inventadas, conv_id)
    no_sabe = dice_no_saber(texto)
    if charla:
        estado = "ok"
    elif not res:
        estado = "sin_info"  # el panel lo lista en "Sin respuesta"
    else:
        estado = "no_sabe" if no_sabe else "ok"
    contacto = None
    if not charla and no_sabe and not tiene_contacto(texto):
        contacto = contacto_de(res[0].area if res else "general")
    reemplazo = None
    if filtra_prompt(texto):  # LLM07: la respuesta repite el prompt
        log.warning("respuesta con texto del prompt en conversación %s", conv_id)
        texto = reemplazo = TEXTO_FILTRADO
        validas, estado = [], "ok"

    log.info("respuesta conv=%s estado=%s proveedor=%s ttft_ms=%s latencia_ms=%s tokens=%s/%s costo=%s",
             conv_id[:8], estado, uso.proveedor, uso.ttft_ms, uso.latencia_ms, uso.tokens_entrada,
             uso.tokens_salida, uso.costo_usd)
    async with db.pool().connection() as conn:
        mid = await _guardar(
            conn, conv_id, "asistente", texto,
            fragmentos_usados=[r.id for r in res], citas=validas, estado=estado, modelo=uso.modelo,
            tokens_entrada=uso.tokens_entrada, tokens_salida=uso.tokens_salida, costo_usd=uso.costo_usd,
            ttft_ms=uso.ttft_ms, latencia_ms=uso.latencia_ms,
        )
    yield "fin", {"mensaje_id": mid, "estado": estado, "citas": validas, "citas_invalidas": inventadas,
                  "contacto": contacto, "reemplazo": reemplazo}
