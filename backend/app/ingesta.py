"""
Ingesta: conocimiento/ → fragmentos → embeddings → PostgreSQL.

    python -m app.ingesta              # procesa solo lo nuevo o cambiado
    python -m app.ingesta --forzar     # vuelve a vectorizar todo
    python -m app.ingesta --simulacro  # muestra los fragmentos sin gastar ni escribir en la BD

Es idempotente: si un archivo no cambió (mismo hash y mismo modelo de embeddings), se salta y
no cuesta nada. Los documentos que ya no están en la carpeta se borran de la base.
Cada documento se reemplaza en una transacción: nunca queda a medias.
"""

import argparse
import asyncio
import hashlib
import sys
import time
from pathlib import Path

import httpx
import yaml

from . import openrouter
from .config import settings
from .db import conectar, vector_sql
from .fragmentar import Documento, fragmentar, leer_documento, listar_documentos, texto_para_embedding


def cargar_areas(raiz: Path) -> dict[str, dict]:
    archivo = raiz / "_areas.yaml"
    if not archivo.exists():
        return {}
    datos = yaml.safe_load(archivo.read_text(encoding="utf-8")) or {}
    return {str(k): (v or {}) for k, v in datos.items()}


def hash_archivo(ruta: Path, modelo: str) -> str:
    h = hashlib.sha256(ruta.read_bytes())
    h.update(modelo.encode())  # cambiar de modelo obliga a re-vectorizar
    return h.hexdigest()


async def ingestar(forzar: bool = False, simulacro: bool = False) -> dict:
    cfg = settings()
    raiz = cfg.carpeta_conocimiento
    if not raiz.exists():
        sys.exit(f"No existe la carpeta de conocimiento: {raiz}")
    areas = cargar_areas(raiz)
    archivos = listar_documentos(raiz)
    print(f"Conocimiento: {raiz}  ·  {len(archivos)} archivo(s)  ·  embeddings: {cfg.modelo_embeddings}")

    resumen = {"nuevos_o_cambiados": 0, "sin_cambios": 0, "borrados": 0, "fragmentos": 0, "tokens_embeddings": 0, "errores": 0}

    if simulacro:
        for ruta in archivos:
            doc = leer_documento(ruta, raiz)
            frags = fragmentar(doc, cfg.fragmento_tokens, cfg.fragmento_traslape)
            print(f"\n{doc.ruta}  ·  «{doc.titulo}»  ·  área {doc.area}  ·  {len(frags)} fragmento(s)")
            for f in frags:
                print(f"   [{f.orden}] {f.seccion}  (~{f.tokens} tokens)")
            resumen["fragmentos"] += len(frags)
        return resumen

    conn = await conectar()
    t0 = time.time()
    try:
        cur = await conn.execute("SELECT ruta, hash_contenido FROM documentos")
        en_bd = dict(await cur.fetchall())
        vistos = set()

        async with httpx.AsyncClient(timeout=60) as cliente:
            for ruta in archivos:
                rel = ruta.relative_to(raiz).as_posix()
                vistos.add(rel)
                h = hash_archivo(ruta, cfg.modelo_embeddings)
                if not forzar and en_bd.get(rel) == h:
                    resumen["sin_cambios"] += 1
                    continue
                try:
                    doc: Documento = leer_documento(ruta, raiz)
                    if doc.area not in areas and areas:
                        print(f"  ! {rel}: el área '{doc.area}' no está en _areas.yaml; se usará el contacto general")
                    frags = fragmentar(doc, cfg.fragmento_tokens, cfg.fragmento_traslape)
                    if not frags:
                        print(f"  ! {rel}: no tiene texto indexable, se omite")
                        continue
                    vectores, tokens = await openrouter.embeddings([texto_para_embedding(doc, f) for f in frags], cliente)
                except Exception as e:  # un archivo malo no detiene la ingesta
                    resumen["errores"] += 1
                    print(f"  ✗ {rel}: {e}")
                    continue

                async with conn.transaction():
                    cur = await conn.execute(
                        """INSERT INTO documentos (ruta, titulo, area, url_publica, vigente_hasta, hash_contenido, modelo_embeddings, actualizado_en)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                           ON CONFLICT (ruta) DO UPDATE SET titulo = EXCLUDED.titulo, area = EXCLUDED.area,
                               url_publica = EXCLUDED.url_publica, vigente_hasta = EXCLUDED.vigente_hasta,
                               hash_contenido = EXCLUDED.hash_contenido, modelo_embeddings = EXCLUDED.modelo_embeddings,
                               actualizado_en = now()
                           RETURNING id""",
                        (rel, doc.titulo, doc.area, doc.url_publica, doc.vigente_hasta, h, cfg.modelo_embeddings),
                    )
                    doc_id = (await cur.fetchone())[0]
                    await conn.execute("DELETE FROM fragmentos WHERE documento_id = %s", (doc_id,))
                    async with conn.cursor() as c:
                        await c.executemany(
                            "INSERT INTO fragmentos (documento_id, orden, seccion, texto, tokens, embedding) VALUES (%s, %s, %s, %s, %s, %s::vector)",
                            [(doc_id, f.orden, f.seccion, f.texto, f.tokens, vector_sql(v)) for f, v in zip(frags, vectores)],
                        )
                resumen["nuevos_o_cambiados"] += 1
                resumen["fragmentos"] += len(frags)
                resumen["tokens_embeddings"] += tokens
                print(f"  ✓ {rel}: {len(frags)} fragmento(s)")

            # documentos borrados de la carpeta
            for rel in set(en_bd) - vistos:
                await conn.execute("DELETE FROM documentos WHERE ruta = %s", (rel,))
                resumen["borrados"] += 1
                print(f"  − {rel}: ya no está en la carpeta, borrado")

        # Todos los vectores deben venir del mismo modelo (si no, las distancias no tienen sentido)
        cur = await conn.execute("SELECT DISTINCT modelo_embeddings FROM documentos")
        modelos = [r[0] for r in await cur.fetchall()]
        if len(modelos) > 1:
            print(f"  ! Hay documentos con modelos distintos {modelos}. Corre: python -m app.ingesta --forzar")
    finally:
        await conn.close()

    print(
        f"\nListo en {time.time() - t0:.1f} s · {resumen['nuevos_o_cambiados']} procesado(s), "
        f"{resumen['sin_cambios']} sin cambios, {resumen['borrados']} borrado(s), {resumen['errores']} error(es) · "
        f"{resumen['fragmentos']} fragmentos nuevos · {resumen['tokens_embeddings']} tokens de embeddings"
    )
    return resumen


def main() -> None:
    ap = argparse.ArgumentParser(description="Carga conocimiento/ en la base de datos")
    ap.add_argument("--forzar", action="store_true", help="re-vectoriza todos los documentos")
    ap.add_argument("--simulacro", action="store_true", help="solo muestra cómo se parten, sin red ni BD")
    args = ap.parse_args()
    r = asyncio.run(ingestar(forzar=args.forzar, simulacro=args.simulacro))
    if r["errores"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
