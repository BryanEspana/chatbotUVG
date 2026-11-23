"""
Acceso a PostgreSQL con psycopg 3, sin ORM.

- La app web usa un pool asíncrono (se abre y cierra en el lifespan de FastAPI).
- Los scripts de consola (migrar, ingesta, evaluar_busqueda) abren una conexión simple.

Los vectores se envían como texto '[0.1,0.2,...]' y se convierten con ::vector en el SQL,
así no hace falta el paquete pgvector de Python.
"""

from typing import Sequence

import psycopg
from psycopg_pool import AsyncConnectionPool

from .config import settings

_pool: AsyncConnectionPool | None = None


async def abrir_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(settings().database_url, min_size=1, max_size=10, open=False)
        await _pool.open(wait=True, timeout=15)
    return _pool


async def cerrar_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("El pool no está abierto (¿se llamó abrir_pool en el lifespan?)")
    return _pool


async def conectar() -> psycopg.AsyncConnection:
    """
    Conexión suelta para scripts de consola. En autocommit: así `async with conn.transaction()`
    abre y confirma una transacción real (sin autocommit sería un savepoint que nunca se confirma).
    """
    return await psycopg.AsyncConnection.connect(settings().database_url, autocommit=True)


def vector_sql(v: Sequence[float]) -> str:
    """Convierte una lista de floats al literal de texto que entiende pgvector."""
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"
