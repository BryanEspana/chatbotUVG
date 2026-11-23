"""
Búsqueda híbrida: vectores (pgvector, coseno) + texto completo en español (tsvector),
combinadas con Reciprocal Rank Fusion (RRF).

- Vectorial: entiende sinónimos ("papeles para aplicar" → "Documentos requeridos").
- Texto: acierta en nombres exactos ("Canvas", "UVG-Estudiantes", "Arduino"). Se usa OR entre
  las palabras de la pregunta (con `AND` casi nunca habría coincidencias en preguntas naturales).

Umbral de "no sé": decide SOLO la similitud vectorial del mejor fragmento. La búsqueda por texto
con OR casi siempre encuentra algo ("cuesta", "horario"), así que no sirve para decidir si hay
respuesta; solo ayuda a ordenar.
"""

from dataclasses import dataclass, field

import psycopg

from . import openrouter
from .config import settings
from .db import vector_sql

K_RRF = 60       # constante estándar de RRF
CANDIDATOS = 20  # cuántos trae cada búsqueda antes de fusionar

SQL = """
WITH q AS (
    SELECT %(vec)s::vector AS v,
           (SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | '))
              FROM unnest(to_tsvector('spanish', %(texto)s))) AS tq
),
vec AS (
    SELECT f.id,
           1 - (f.embedding <=> q.v) AS similitud,
           row_number() OVER (ORDER BY f.embedding <=> q.v) AS pos
      FROM fragmentos f, q
     ORDER BY f.embedding <=> q.v
     LIMIT %(candidatos)s
),
txt AS (
    SELECT f.id,
           ts_rank_cd(f.tsv, q.tq) AS rango,
           row_number() OVER (ORDER BY ts_rank_cd(f.tsv, q.tq) DESC) AS pos
      FROM fragmentos f, q
     WHERE q.tq IS NOT NULL AND f.tsv @@ q.tq
     ORDER BY rango DESC
     LIMIT %(candidatos)s
)
SELECT f.id, d.ruta, d.titulo, d.area, d.url_publica, f.seccion, f.texto,
       COALESCE(1.0 / (%(k)s + vec.pos), 0) + COALESCE(1.0 / (%(k)s + txt.pos), 0) AS puntaje,
       COALESCE(vec.similitud, 1 - (f.embedding <=> (SELECT v FROM q))) AS similitud,
       COALESCE(txt.rango, 0) AS rango_texto,
       (SELECT max(similitud) FROM vec) AS mejor_vectorial
  FROM vec
  FULL OUTER JOIN txt USING (id)
  JOIN fragmentos f ON f.id = COALESCE(vec.id, txt.id)
  JOIN documentos d ON d.id = f.documento_id
 ORDER BY puntaje DESC
 LIMIT %(top_k)s
"""


@dataclass
class Resultado:
    id: int
    ruta: str
    titulo: str
    area: str
    url_publica: str | None
    seccion: str
    texto: str
    puntaje: float
    similitud: float
    rango_texto: float


@dataclass
class Busqueda:
    consulta: str
    resultados: list[Resultado] = field(default_factory=list)
    mejor_similitud: float = 0.0
    suficiente: bool = False  # ¿vale la pena llamar al LLM?


def armar_consulta(pregunta: str, preguntas_previas: list[str] | None = None) -> str:
    """Para seguimientos ("¿y la puedo repetir?") se concatena la pregunta anterior del usuario."""
    previas = [p for p in (preguntas_previas or []) if p.strip()]
    return f"{previas[-1]} {pregunta}" if previas else pregunta


async def buscar(
    conn: psycopg.AsyncConnection,
    pregunta: str,
    preguntas_previas: list[str] | None = None,
    top_k: int | None = None,
    vector: list[float] | None = None,
) -> Busqueda:
    """
    Si ya se tiene el vector de la consulta (p. ej. se calculó antes), se puede pasar en `vector`
    para no pagar otro embedding.
    """
    cfg = settings()
    consulta = armar_consulta(pregunta, preguntas_previas)
    if vector is None:
        vectores, _ = await openrouter.embeddings([openrouter.preparar_consulta(consulta)])
        vector = vectores[0]

    cur = await conn.execute(
        SQL,
        {"vec": vector_sql(vector), "texto": consulta, "candidatos": CANDIDATOS, "k": K_RRF, "top_k": top_k or cfg.top_k},
    )
    filas = await cur.fetchall()
    resultados = [Resultado(*fila[:-1]) for fila in filas]
    # El umbral usa la mejor similitud de TODA la búsqueda vectorial, no solo de los top_k fusionados
    mejor = float(filas[0][-1]) if filas and filas[0][-1] is not None else 0.0
    if mejor != mejor:  # NaN (vector nulo): se trata como "sin coincidencias"
        mejor = 0.0
    return Busqueda(consulta=consulta, resultados=resultados, mejor_similitud=mejor,
                    suficiente=mejor >= cfg.umbral_similitud)
