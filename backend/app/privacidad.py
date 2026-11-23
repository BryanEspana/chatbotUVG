"""
Enmascarado de datos personales (LLM02: divulgación de información sensible).

Se aplica a la pregunta ANTES de mandarla al modelo, a los embeddings y a la base de datos:
ni OpenRouter ni los logs ven un DPI, un carné, un correo, un teléfono o una tarjeta.
El bot no necesita esos datos para contestar (no tiene acceso a información personal).

También se calcula el hash de la IP: nunca se guarda la IP en claro.
"""

import hashlib
import re

from .config import settings

# El orden importa: primero lo más largo/específico.
_PATRONES: list[tuple[re.Pattern, str]] = [
    # correos
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "[CORREO]"),
    # tarjetas: 15-16 dígitos (con o sin espacios/guiones en grupos de 4)
    (re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{3,4}(?!\d)"), "[TARJETA]"),
    # DPI de Guatemala: 13 dígitos, a veces escrito 4-5-4
    (re.compile(r"(?<!\d)\d{4}[ -]?\d{5}[ -]?\d{4}(?!\d)"), "[DPI]"),
    # carné: solo si viene precedido de la palabra (un número suelto puede ser un año o un monto)
    (re.compile(r"(carn[eé]t?\s*(?:es|:|#|n[uú]mero|no\.?)?\s*)\d{4,9}", re.I), r"\1[CARNÉ]"),
    # teléfonos de Guatemala: 8 dígitos (2xxx, 3xxx, 4xxx, 5xxx, 6xxx, 7xxx) con +502 opcional
    (re.compile(r"(?<![\d(])(?:\+?502[ -]?)?[2-7]\d{3}[ -]?\d{4}(?!\d)"), "[TELÉFONO]"),
]


_RANGO_ANIOS = re.compile(r"(?:\+?502[ -]?)?(19|20)\d{2}[ -](19|20)\d{2}")


def _sustituir(reemplazo: str):
    def f(m: re.Match) -> str:
        if reemplazo == "[TELÉFONO]" and _RANGO_ANIOS.fullmatch(m.group(0)):
            return m.group(0)  # "2026-2027" es un rango de años, no un teléfono
        return m.expand(reemplazo)
    return f


def enmascarar(texto: str) -> str:
    for patron, reemplazo in _PATRONES:
        texto = patron.sub(_sustituir(reemplazo), texto)
    return texto


def hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    return hashlib.sha256(f"{settings().secreto_sesion}|{ip}".encode()).hexdigest()[:32]


_MARCAS = re.compile(r"\s*\[(?:CORREO|TARJETA|DPI|CARNÉ|TELÉFONO)\]")


def sin_marcas(texto: str) -> str:
    """
    Quita las marcas [DPI], [CORREO], etc. de un texto YA enmascarado. Se usa solo para la consulta
    al buscador: la marca no dice nada de la pregunta y baja la similitud con el documento correcto
    ("Mi DPI es [DPI], ¿cuánto cuesta el examen?" quedaba bajo el umbral).
    """
    return re.sub(r"\s{2,}", " ", _MARCAS.sub("", texto)).strip()
