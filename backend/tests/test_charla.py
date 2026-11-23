"""Detector de charla: los mensajes de cortesía no pasan por la búsqueda; las preguntas reales sí."""

import pytest

from app.charla import es_charla


@pytest.mark.parametrize("m", [
    "hola", "Hola!", "buenas tardes", "¿Qué tal?", "hola, ¿cómo estás?", "Buenos días Jacko", "hey",
    "gracias", "Muchas gracias!", "ok gracias", "mil gracias, adiós",
    "adiós", "chao", "hasta luego", "nos vemos", "ok", "va", "listo", "perfecto",
])
def test_cortesia_es_charla(m):
    assert es_charla(m)


@pytest.mark.parametrize("m", [
    "hola, ¿cuánto cuesta el examen?", "¿Qué carreras hay?", "¿Cómo aplico?", "¿qué tal la biblioteca?",
    "hola quiero estudiar computación", "gracias, ¿y las becas?", "", "?",
    "hola hola hola hola hola hola hola hola hola",
])
def test_preguntas_reales_no_son_charla(m):
    assert not es_charla(m)
