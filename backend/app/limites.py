"""
Topes de uso (LLM10: consumo sin límite), contados en la tabla `uso` de PostgreSQL.

- Por IP (hash): N preguntas por minuto y M por día.
- Global: un tope diario para todo el bot (protege el crédito de OpenRouter si alguien abusa).

Se guarda en la BD y no en memoria: sobrevive a reinicios y despliegues (lección de FerreInnova).
Cada pregunta suma con un UPSERT atómico; si se pasa de un tope, se rechaza.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from .config import settings

_SQL = """
INSERT INTO uso (clave, dia, conteo) VALUES (%s, %s, 1)
ON CONFLICT (clave, dia) DO UPDATE SET conteo = uso.conteo + 1
RETURNING conteo
"""


@dataclass
class Veredicto:
    permitido: bool
    motivo: str | None = None          # 'minuto' | 'dia' | 'global'
    reintentar_en_s: int | None = None
    mensaje: str | None = None


async def _sumar(conn: psycopg.AsyncConnection, clave: str, dia) -> int:
    cur = await conn.execute(_SQL, (clave, dia))
    return (await cur.fetchone())[0]


async def registrar_pregunta(conn: psycopg.AsyncConnection, ip_hash: str | None) -> Veredicto:
    cfg = settings()
    ahora = datetime.now(timezone.utc)
    hoy = ahora.date()

    if await _sumar(conn, "global", hoy) > cfg.limite_global_dia:
        return Veredicto(False, "global", None, "El asistente alcanzó su límite de consultas de hoy. Intenta mañana o escribe a la universidad.")
    if ip_hash:
        minuto = ahora.strftime("%H%M")
        if await _sumar(conn, f"ipmin:{ip_hash}:{minuto}", hoy) > cfg.limite_ip_minuto:
            return Veredicto(False, "minuto", 60 - ahora.second, "Hiciste muchas preguntas seguidas. Intenta de nuevo en un minuto.")
        if await _sumar(conn, f"ip:{ip_hash}", hoy) > cfg.limite_ip_dia:
            return Veredicto(False, "dia", None, "Llegaste al límite de preguntas de hoy. Intenta mañana.")
    return Veredicto(True)


async def limpiar(conn: psycopg.AsyncConnection) -> None:
    """Borra contadores de días anteriores (los de minuto crecen rápido)."""
    await conn.execute("DELETE FROM uso WHERE dia < (now() AT TIME ZONE 'UTC')::date - 1")
