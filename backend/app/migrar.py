"""
Aplica las migraciones SQL de backend/migraciones/ en orden, una sola vez cada una.

    python -m app.migrar

Lleva el registro en la tabla schema_migrations. Cada archivo corre en su propia transacción:
si falla, no queda a medias. Nunca edites una migración que ya corrió; crea una nueva.
"""

import asyncio
from pathlib import Path

from .db import conectar

CARPETA = Path(__file__).resolve().parents[1] / "migraciones"


async def migrar() -> list[str]:
    aplicadas_ahora = []
    conn = await conectar()
    try:
        async with conn.transaction():
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, aplicada_en timestamptz NOT NULL DEFAULT now())"
            )
        cur = await conn.execute("SELECT version FROM schema_migrations")
        ya = {r[0] for r in await cur.fetchall()}

        for archivo in sorted(CARPETA.glob("*.sql")):
            if archivo.name in ya:
                continue
            async with conn.transaction():
                await conn.execute(archivo.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (archivo.name,))
            aplicadas_ahora.append(archivo.name)
            print(f"  aplicada {archivo.name}")
    finally:
        await conn.close()
    return aplicadas_ahora


def main() -> None:
    aplicadas = asyncio.run(migrar())
    print("Migraciones al día." if not aplicadas else f"{len(aplicadas)} migración(es) aplicada(s).")


if __name__ == "__main__":
    main()
