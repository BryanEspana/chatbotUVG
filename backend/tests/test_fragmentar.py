"""Partición de documentos: no usa red ni base de datos."""

from datetime import date

from app.fragmentar import (
    Documento,
    estimar_tokens,
    fragmentar,
    leer_documento,
    listar_documentos,
    texto_para_embedding,
)

MD = """---
titulo: Biblioteca central
area: biblioteca
url: https://www.uvg.edu.gt/biblioteca/
vigente_hasta: 2027-07-01
---

# Título que se ignora porque hay front matter

> Nota interna: esto NO se indexa.

Texto de introducción.

## Horario

Lunes a viernes de 7:00 a 21:00.

### Asuetos

Cerrado en asuetos.

## Préstamo

Hasta 5 libros por 7 días.
"""


def test_markdown_front_matter_y_secciones(tmp_path):
    (tmp_path / "biblioteca.md").write_text(MD, encoding="utf-8")
    doc = leer_documento(tmp_path / "biblioteca.md", tmp_path)
    assert doc.titulo == "Biblioteca central"
    assert doc.area == "biblioteca"
    assert doc.url_publica == "https://www.uvg.edu.gt/biblioteca/"
    assert doc.vigente_hasta == date(2027, 7, 1)
    nombres = [s for s, _ in doc.secciones]
    assert nombres == ["Introducción", "Horario", "Préstamo"]
    horario = dict(doc.secciones)["Horario"]
    assert "### Asuetos" in horario and "Cerrado en asuetos" in horario  # el ### se queda dentro
    assert all("Nota interna" not in t for _, t in doc.secciones)  # las líneas '>' no se indexan


def test_markdown_sin_front_matter_usa_primer_titulo(tmp_path):
    (tmp_path / "x.md").write_text("# Mi título\n\n## Uno\nhola\n", encoding="utf-8")
    doc = leer_documento(tmp_path / "x.md", tmp_path)
    assert doc.titulo == "Mi título" and doc.area == "general"
    assert doc.secciones == [("Uno", "hola")]


def test_markdown_sin_titulo_usa_nombre_de_archivo(tmp_path):
    (tmp_path / "prestamo_equipo.md").write_text("## Tablets\nPor 4 horas.\n", encoding="utf-8")
    assert leer_documento(tmp_path / "prestamo_equipo.md", tmp_path).titulo == "Prestamo equipo"


def test_seccion_larga_se_parte_con_traslape():
    parrafos = [f"Párrafo {i}. " + ("palabra " * 60).strip() for i in range(12)]
    doc = Documento(ruta="largo.md", titulo="Largo", secciones=[("Reglamento", "\n\n".join(parrafos))])
    frags = fragmentar(doc, max_tokens=400, traslape=60)
    assert len(frags) > 1
    assert all(f.tokens <= 400 + 120 for f in frags)  # margen: un párrafo no se corta a la mitad
    assert frags[0].seccion == f"Reglamento (1/{len(frags)})"
    # traslape: el final de un trozo reaparece al inicio del siguiente (~60 tokens ≈ 240 caracteres)
    for a, b in zip(frags, frags[1:]):
        inicio_b = b.texto.split("\n")[0]
        assert a.texto.endswith(inicio_b)
        assert 150 <= len(inicio_b) <= 240
        assert not inicio_b.startswith(" ")
    assert [f.orden for f in frags] == list(range(len(frags)))


def test_seccion_corta_no_se_parte():
    doc = Documento(ruta="c.md", titulo="C", secciones=[("Horario", "Abre a las 7.")])
    frags = fragmentar(doc)
    assert len(frags) == 1 and frags[0].seccion == "Horario"


def test_listar_ignora_archivos_internos(tmp_path):
    for nombre in ["a.md", "b.pdf", "_areas.yaml", "_borrador.md", ".oculto.md", "foto.png"]:
        (tmp_path / nombre).write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.docx").write_text("x", encoding="utf-8")
    assert [p.relative_to(tmp_path).as_posix() for p in listar_documentos(tmp_path)] == ["a.md", "b.pdf", "sub/c.docx"]


def test_docx_por_encabezados(tmp_path):
    import docx

    d = docx.Document()
    d.add_heading("Préstamo de equipo", level=1)
    d.add_paragraph("Introducción breve.")
    d.add_heading("Tablets", level=2)
    d.add_paragraph("Se prestan por 4 horas.")
    d.add_heading("Cámaras", level=2)
    d.add_paragraph("Reservar con 48 horas.")
    d.save(tmp_path / "equipo.docx")
    doc = leer_documento(tmp_path / "equipo.docx", tmp_path)
    assert doc.titulo == "Préstamo de equipo"
    assert [s for s, _ in doc.secciones] == ["Introducción", "Tablets", "Cámaras"]


def test_texto_para_embedding_incluye_contexto():
    doc = Documento(ruta="b.md", titulo="Biblioteca", secciones=[("Horario", "7 a 21")])
    f = fragmentar(doc)[0]
    assert texto_para_embedding(doc, f).startswith("Biblioteca — Horario\n")


def test_estimar_tokens():
    assert estimar_tokens("") == 1
    assert estimar_tokens("a" * 400) == 100
