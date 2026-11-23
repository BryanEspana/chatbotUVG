"""
Lectura de documentos y partición en fragmentos citables.

Formatos: .md (recomendado), .txt, .pdf, .docx.

Reglas:
- Markdown: el título es el front matter `titulo`, o el primer `# `, o el nombre del archivo.
  Cada `## ` es una sección citable. El texto antes del primer `## ` es la sección "Introducción".
  Los `### ` se quedan dentro de su `## ` (se conserva el encabezado en el texto).
  Las líneas que empiezan con `>` se tratan como notas internas y NO se indexan.
- PDF: una sección por página ("Página N").
- DOCX: los estilos de encabezado de nivel 2 ("Heading 2" / "Título 2") abren sección.
- Una sección muy larga se parte en trozos de ~`fragmento_tokens` con `fragmento_traslape`
  de traslape, cortando por párrafos (o por oraciones si un párrafo es enorme).

Esta parte no toca la red ni la base de datos: se prueba sola (tests/test_fragmentar.py).
"""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

EXTENSIONES = {".md", ".txt", ".pdf", ".docx"}


@dataclass
class Fragmento:
    seccion: str
    texto: str
    orden: int = 0

    @property
    def tokens(self) -> int:
        return estimar_tokens(self.texto)


@dataclass
class Documento:
    ruta: str  # relativa a conocimiento/
    titulo: str
    area: str = "general"
    url_publica: str | None = None
    vigente_hasta: date | None = None
    secciones: list[tuple[str, str]] = field(default_factory=list)  # (sección, texto)


def estimar_tokens(texto: str) -> int:
    """Aproximación barata: ~4 caracteres por token en español."""
    return max(1, len(texto) // 4)


# ---------------------------------------------------------------- lectura por formato

def _front_matter(texto: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", texto, flags=re.S)
    if not m:
        return {}, texto
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"front matter inválido: {e}") from e
    return (meta if isinstance(meta, dict) else {}), texto[m.end():]


def _leer_markdown(ruta: Path, rel: str) -> Documento:
    meta, cuerpo = _front_matter(ruta.read_text(encoding="utf-8"))
    lineas = [l for l in cuerpo.splitlines() if not l.lstrip().startswith(">")]
    titulo = meta.get("titulo")
    secciones: list[tuple[str, list[str]]] = [("Introducción", [])]
    for l in lineas:
        if l.startswith("# ") and not titulo:
            titulo = l[2:].strip()
        elif l.startswith("# "):
            continue
        elif l.startswith("## "):
            secciones.append((l[3:].strip(), []))
        else:
            secciones[-1][1].append(l)
    titulo = titulo or ruta.stem.replace("_", " ").capitalize()
    return Documento(
        ruta=rel,
        titulo=str(titulo),
        area=str(meta.get("area") or "general"),
        url_publica=meta.get("url"),
        vigente_hasta=meta.get("vigente_hasta") if isinstance(meta.get("vigente_hasta"), date) else None,
        secciones=[(s, "\n".join(t).strip()) for s, t in secciones if "\n".join(t).strip()],
    )


def _leer_pdf(ruta: Path, rel: str) -> Documento:
    from pypdf import PdfReader

    lector = PdfReader(str(ruta))
    meta = lector.metadata or {}
    secciones = []
    for i, pagina in enumerate(lector.pages, 1):
        texto = (pagina.extract_text() or "").strip()
        if texto:
            secciones.append((f"Página {i}", texto))
    titulo = (getattr(meta, "title", None) or ruta.stem.replace("_", " ").capitalize())
    return Documento(ruta=rel, titulo=str(titulo), secciones=secciones)


def _leer_docx(ruta: Path, rel: str) -> Documento:
    import docx

    d = docx.Document(str(ruta))
    titulo = (d.core_properties.title or "").strip()
    secciones: list[tuple[str, list[str]]] = [("Introducción", [])]
    for p in d.paragraphs:
        estilo = (p.style.name if p.style is not None else "").lower()
        texto = p.text.strip()
        if not texto:
            continue
        if estilo in ("title", "título", "heading 1", "título 1") and not titulo:
            titulo = texto
        elif estilo in ("heading 2", "título 2"):
            secciones.append((texto, []))
        else:
            secciones[-1][1].append(texto)
    titulo = titulo or ruta.stem.replace("_", " ").capitalize()
    return Documento(ruta=rel, titulo=titulo, secciones=[(s, "\n".join(t)) for s, t in secciones if t])


def leer_documento(ruta: Path, raiz: Path) -> Documento:
    rel = ruta.relative_to(raiz).as_posix()
    ext = ruta.suffix.lower()
    if ext in (".md", ".txt"):
        return _leer_markdown(ruta, rel)
    if ext == ".pdf":
        return _leer_pdf(ruta, rel)
    if ext == ".docx":
        return _leer_docx(ruta, rel)
    raise ValueError(f"Formato no soportado: {ruta.name}")


def listar_documentos(raiz: Path) -> list[Path]:
    """Archivos indexables. Se ignoran los que empiezan con '_' o '.' (p. ej. _areas.yaml)."""
    return sorted(
        p for p in raiz.rglob("*")
        if p.is_file() and p.suffix.lower() in EXTENSIONES
        and not any(parte.startswith(("_", ".")) for parte in p.relative_to(raiz).parts)
    )


# ---------------------------------------------------------------- partición

def _partir_largo(texto: str, max_tokens: int, traslape: int) -> list[str]:
    """Parte por párrafos (y por oraciones si hace falta) en trozos de ~max_tokens con traslape."""
    if estimar_tokens(texto) <= max_tokens:
        return [texto]
    piezas: list[str] = []
    for parrafo in re.split(r"\n\s*\n", texto):
        if estimar_tokens(parrafo) <= max_tokens:
            piezas.append(parrafo)
        else:
            piezas += re.split(r"(?<=[\.\?\!:;])\s+", parrafo)

    trozos, actual = [], []
    for pieza in piezas:
        if actual and estimar_tokens("\n".join(actual + [pieza])) > max_tokens:
            cerrado = "\n".join(actual)
            trozos.append(cerrado)
            # traslape: los últimos ~traslape tokens del trozo cerrado, cortados en un límite de palabra
            actual = [_cola(cerrado, traslape)] if traslape > 0 else []
        actual.append(pieza)
    if actual:
        trozos.append("\n".join(actual))
    return [t.strip() for t in trozos if t.strip()]


def _cola(texto: str, tokens: int) -> str:
    """Últimos ~`tokens` tokens de `texto`, empezando en un inicio de palabra."""
    n = tokens * 4
    if len(texto) <= n:
        return texto
    cola = texto[-n:]
    espacio = cola.find(" ")
    return cola[espacio + 1:] if 0 <= espacio < len(cola) - 1 else cola


def fragmentar(doc: Documento, max_tokens: int = 400, traslape: int = 60) -> list[Fragmento]:
    fragmentos: list[Fragmento] = []
    for seccion, texto in doc.secciones:
        trozos = _partir_largo(texto, max_tokens, traslape)
        for i, trozo in enumerate(trozos, 1):
            nombre = seccion if len(trozos) == 1 else f"{seccion} ({i}/{len(trozos)})"
            fragmentos.append(Fragmento(seccion=nombre, texto=trozo))
    for i, f in enumerate(fragmentos):
        f.orden = i
    return fragmentos


def texto_para_embedding(doc: Documento, f: Fragmento) -> str:
    """Lo que se vectoriza: título y sección dan contexto al fragmento."""
    return f"{doc.titulo} — {f.seccion}\n{f.texto}"
