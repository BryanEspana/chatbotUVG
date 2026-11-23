"""
Evalúa la recuperación con las 30 preguntas del examen (examen/preguntas.json).

    python -m app.evaluar_busqueda

Requiere los documentos del examen, archivados en examen/conocimiento_ficticio_fase1/ desde que
conocimiento/ tiene la información real de la UVG. Usar una base aparte y apuntar ahí:
    CARPETA_CONOCIMIENTO=../examen/conocimiento_ficticio_fase1 DATABASE_URL=... python -m app.ingesta Mide:

- Acierto@k: en cuántas preguntas el fragmento esperado está entre los top_k que recibe el LLM.
- MRR: qué tan arriba aparece (1 = siempre primero).
- Similitudes de las preguntas que SÍ tienen respuesta vs. las que NO (trampas sin documento),
  para elegir UMBRAL_SIMILITUD: debe quedar por debajo de casi todas las primeras y por encima
  de las segundas. Si se traslapan, el umbral se pone bajo (lo importante es no cortar preguntas
  válidas; el modelo igual dice "no sé" en las otras, como mostró el examen).

Costo: 30 embeddings de consulta (fracciones de centavo).
"""

import argparse
import asyncio
import json
import statistics
import sys

from . import openrouter
from .busqueda import buscar
from .config import RAIZ, settings
from .db import conectar

PREGUNTAS = RAIZ / "examen" / "preguntas.json"
# Preguntas cuya respuesta NO está en ningún documento: idealmente quedan bajo el umbral.
SIN_DOCUMENTO = {"TR1", "TR2", "TR5", "TR7"}


def _coincide(resultado, esperado: str) -> bool:
    doc, _, seccion = esperado.partition("#")
    return resultado.ruta == doc and resultado.seccion.startswith(seccion)


async def evaluar(detalle: bool) -> int:
    cfg = settings()
    if not PREGUNTAS.exists():
        sys.exit(f"No encuentro {PREGUNTAS}")
    preguntas = json.loads(PREGUNTAS.read_text(encoding="utf-8"))["preguntas"]

    # Un solo lote de embeddings para todas las consultas (más barato y rápido)
    from .busqueda import armar_consulta

    consultas = [
        armar_consulta(p["pregunta"], [m["content"] for m in p.get("historial", []) if m["role"] == "user"])
        for p in preguntas
    ]
    vectores, tokens = await openrouter.embeddings([openrouter.preparar_consulta(c) for c in consultas])

    conn = await conectar()
    aciertos, total, rr = 0, 0, []
    sims_con, sims_sin = [], []
    try:
        cur = await conn.execute("SELECT count(*) FROM fragmentos")
        if (await cur.fetchone())[0] == 0:
            sys.exit("No hay fragmentos. Corre primero: python -m app.ingesta")
        print(f"Embeddings: {cfg.modelo_embeddings} · top_k={cfg.top_k} · umbral={cfg.umbral_similitud}\n")
        for p, v in zip(preguntas, vectores):
            b = await buscar(conn, p["pregunta"], [m["content"] for m in p.get("historial", []) if m["role"] == "user"], vector=v)
            esperados = p.get("fragmentos", [])
            marca = "    "
            if esperados:
                total += 1
                pos = next((i for i, r in enumerate(b.resultados, 1) if any(_coincide(r, e) for e in esperados)), None)
                if pos:
                    aciertos += 1
                    rr.append(1 / pos)
                    marca = f"ok{pos} "
                else:
                    rr.append(0)
                    marca = "MAL "
            if p["id"] in SIN_DOCUMENTO:
                sims_sin.append(b.mejor_similitud)
            elif esperados:
                sims_con.append(b.mejor_similitud)
            corte = "" if b.suficiente else "  ← bajo el umbral"
            print(f"{marca} {p['id']:4} sim {b.mejor_similitud:.3f}  {p['pregunta'][:58]:58}{corte}")
            if detalle or marca == "MAL ":
                for i, r in enumerate(b.resultados, 1):
                    print(f"        {i}. {r.ruta}#{r.seccion}  (sim {r.similitud:.3f}, texto {r.rango_texto:.3f})")
    finally:
        await conn.close()

    print(f"\nAcierto@{cfg.top_k}: {aciertos}/{total}   ·   MRR: {statistics.mean(rr):.3f}   ·   tokens de embeddings: {tokens}")
    if sims_con and sims_sin:
        print(f"Similitud con respuesta:  mín {min(sims_con):.3f} · p10 {sorted(sims_con)[len(sims_con) // 10]:.3f} · mediana {statistics.median(sims_con):.3f}")
        print(f"Similitud sin documento:  máx {max(sims_sin):.3f} · valores {[round(s, 3) for s in sims_sin]}")
        if max(sims_sin) < min(sims_con):
            sugerido = (max(sims_sin) + min(sims_con)) / 2
            print(f"→ Se separan bien. Umbral sugerido: {sugerido:.2f}")
        else:
            print(f"→ Se traslapan. Umbral sugerido (conservador, no corta preguntas válidas): {min(sims_con) - 0.02:.2f}")
    return aciertos


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detalle", action="store_true", help="muestra los fragmentos de todas las preguntas")
    args = ap.parse_args()
    asyncio.run(evaluar(args.detalle))


if __name__ == "__main__":
    main()
