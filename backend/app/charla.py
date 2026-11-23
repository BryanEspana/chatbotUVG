"""
Detector de charla corta: saludos, agradecimientos, despedidas y confirmaciones ("ok", "listo").

La respuesta la redacta SIEMPRE el modelo, con sus propias palabras. Este detector solo decide
cómo se trata el mensaje:
- No se gasta un embedding ni una búsqueda en un "hola": el modelo lo recibe sin fragmentos.
- No se marca como "sin respuesta" en el panel ni se muestra la tarjeta de contacto.

Solo cuenta como charla un mensaje sin ninguna pregunta real. "hola, ¿cuánto cuesta el examen?"
sigue el flujo normal con búsqueda, porque le sobran palabras que no son de cortesía.
"""

import re
import unicodedata

# Palabras que, solas o combinadas, forman un saludo o una frase de cortesía.
_CORTESIA = {
    # saludos
    "hola", "holi", "holis", "ola", "hey", "hi", "hello", "buenas", "buenos", "buen", "dia", "dias",
    "tarde", "tardes", "noche", "noches", "saludos", "que", "tal", "onda", "hubo", "como", "estas",
    "esta", "estan", "va", "todo", "bien", "jacko", "uvg", "bot", "asistente", "amigo",
    # agradecimientos
    "gracias", "muchas", "mil", "muchisimas", "te", "lo", "agradezco", "ty", "thanks", "genial",
    "excelente", "super",
    # despedidas
    "adios", "chao", "bye", "nos", "vemos", "hasta", "luego", "pronto", "manana", "cuidate",
    # confirmaciones
    "ok", "okay", "oki", "vale", "listo", "entendido", "perfecto", "dale", "si", "sale", "bueno", "ah", "ya",
}


def _palabras(texto: str) -> list[str]:
    sin_tildes = unicodedata.normalize("NFD", texto.lower())
    sin_tildes = "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-zñ]+", sin_tildes)


def es_charla(mensaje: str) -> bool:
    """True si el mensaje es solo cortesía: hasta 8 palabras, todas de cortesía."""
    palabras = _palabras(mensaje)
    return 0 < len(palabras) <= 8 and set(palabras) <= _CORTESIA
