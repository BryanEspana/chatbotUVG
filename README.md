# Chatbot UVG

Chatbot con IA para aspirantes y estudiantes de la Universidad del Valle de Guatemala. Responde con documentos oficiales (RAG), cita la fuente y, cuando no sabe, remite a un contacto humano.

- **Diseño completo:** [`ARQUITECTURA.md`](ARQUITECTURA.md)
- **Elección del modelo:** [`investigacion_modelos_uvg.md`](investigacion_modelos_uvg.md) (`openai/gpt-oss-120b` vía OpenRouter)

## Estado

| Fase | Estado |
|---|---|
| 0. Elegir el modelo | ✅ |
| 1. Base y conocimiento: migraciones, ingesta, búsqueda híbrida | ✅ embeddings `openai/text-embedding-3-small` (Acierto@4 25/26), umbral 0.40 |
| 2. Chat con streaming (prototipo mínimo) | ✅ examen contra el backend: **99.3**, 8/8 trampas, 0 fallas graves, primer token p50 2.2 s y p90 2.7 s |
| 3. Widget | pendiente |
| 4. Panel de preguntas frecuentes | pendiente |
| 5. Producción (hosting a definir por la UVG) | pendiente |

---

## Instalación local (fase 1)

Requisitos: **Python 3.10+** y **PostgreSQL con pgvector** (Homebrew). No hace falta Docker.

### 1. Base de datos

```bash
brew install pgvector                  # extensión de vectores para Postgres
createdb chatbot_uvg
psql chatbot_uvg -c "CREATE EXTENSION IF NOT EXISTS vector;"   # prueba que la extensión carga
```

Si el último comando dice `could not open extension control file ... vector.control`, el pgvector de Homebrew se compiló para otra versión de Postgres. Hay dos soluciones:

- Usar la versión de Postgres que pide `brew info pgvector`.
- Compilarlo para tu versión:

  ```bash
  cd /tmp && git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git && cd pgvector
  make PG_CONFIG=$(which pg_config) && make install PG_CONFIG=$(which pg_config)
  ```

### 2. Backend

```bash
cd ~/Documents/UVG/chatbotUVG
cp .env.example .env                   # pega tu OPENROUTER_API_KEY (la misma del examen)

cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m app.migrar                   # crea las tablas
python -m app.ingesta --simulacro      # GRATIS: muestra cómo se parten los documentos
python -m app.ingesta                  # vectoriza conocimiento/ (centavos)
python -m app.evaluar_busqueda         # mide la búsqueda con las 30 preguntas del examen
```

### 3. Probar la búsqueda en el navegador

```bash
uvicorn app.main:app --reload
```

- http://localhost:8000/api/salud: estado y número de fragmentos.
- http://localhost:8000/api/buscar?q=cuanto%20cuesta%20la%20prueba%20de%20admision: los fragmentos que recibiría el LLM, su similitud y si pasaría el umbral. Solo funciona en `ENTORNO=desarrollo`.
- http://localhost:8000/api/docs: documentación interactiva de la API (solo en desarrollo).

---

## Fase 1 (hecho): elegir el modelo de embeddings

Corre las dos opciones. Cuestan fracciones de centavo: son unos 40 fragmentos y 30 preguntas.

```bash
MODELO_EMBEDDINGS=qwen/qwen3-embedding-0.6b python -m app.ingesta --forzar
MODELO_EMBEDDINGS=qwen/qwen3-embedding-0.6b python -m app.evaluar_busqueda

MODELO_EMBEDDINGS=openai/text-embedding-3-small python -m app.ingesta --forzar
MODELO_EMBEDDINGS=openai/text-embedding-3-small python -m app.evaluar_busqueda
```

Cómo elegir:

1. Gana el que tenga mayor **Acierto@4**: en cuántas de las 26 preguntas con respuesta el fragmento correcto llega al modelo. La meta es al menos 25 de 26. Si empatan, gana el de mayor **MRR**, que mide qué tan arriba aparece.
2. Pon el ganador en `MODELO_EMBEDDINGS` dentro de `.env` y corre `python -m app.ingesta --forzar` una vez más.
3. Anota el **umbral sugerido** que imprime el script en `UMBRAL_SIMILITUD`. Se termina de calibrar en la fase 2 con el examen completo.

Las variables de entorno de la línea de comandos tienen prioridad sobre `.env`, por eso el `MODELO_EMBEDDINGS=...` delante del comando funciona.

**Resultado (3 de noviembre de 2026):** `qwen/qwen3-embedding-0.6b` no tenía proveedores en OpenRouter (404). `openai/text-embedding-3-small` logró **Acierto@4 25/26 y MRR 0.885**; el único fallo (C3) es de redacción del documento. Umbral: **0.40**. El script sugería 0.45, pero se eligió uno conservador porque cortar una pregunta válida es peor que dejar pasar una al modelo, que igual dice "no sé".

---

## Fase 2: chat con streaming

```bash
cd backend && source .venv/bin/activate
uvicorn app.main:app --reload
```

Pregunta desde otra terminal. El `-N` muestra el streaming en vivo:

```bash
curl -N -X POST localhost:8000/api/chat -H 'Content-Type: application/json' \
     -d '{"mensaje":"¿Cuánto cuesta la prueba de admisión?"}'
```

Llegan estos eventos SSE:

| Evento | Qué trae |
|---|---|
| `conversacion` | El id de la conversación, que el widget manda de vuelta en la siguiente pregunta |
| `fuentes` | Los fragmentos que recibió el modelo, numerados para las citas |
| `delta` | Trozos de la respuesta en tiempo real |
| `fin` | Estado (`ok`, `no_sabe` o `sin_info`), citas válidas, citas inventadas y, si hace falta, la tarjeta de contacto del área |
| `error` | Límite alcanzado, modelo caído o pregunta inválida, con un mensaje amable |

**Chat en el navegador:** con el servidor corriendo, abre **http://localhost:8000**. Es la página del diseño aprobado, conectada al backend real: las respuestas llegan en streaming, las citas abren el panel de fuentes y los 👍/👎 se guardan.

También se puede probar desde http://localhost:8000/api/docs, en `POST /api/chat`.

### Examen contra el backend real (prueba de regresión del sistema completo)

Mide todo junto: buscador, prompt, `gpt-oss-120b` y posproceso. Cuesta unos US$0.002.

```bash
# terminal 1: el backend con el tope por minuto alto para que el examen no choque con él
cd backend && source .venv/bin/activate
LIMITE_IP_MINUTO=1000 uvicorn app.main:app

# terminal 2
cd examen && python3 examen.py --backend http://localhost:8000
```

Deja los reportes en `examen/resultados/reporte_backend.md` y `respuestas_backend.md`, sin pisar los del examen de modelos.

**Resultados (3 de noviembre de 2026):**

| Corrida | Nota | Trampas | Fallas graves | Primer token p50 / p90 | Cambios |
|---|---|---|---|---|---|
| v1 | 94.5 | 8/8 | 0 | 3.3 s / 14.2 s | — |
| v2 | **99.3** | 8/8 | 0 | **2.2 s / 2.7 s** | Prompt: los fragmentos van ordenados por relevancia y las preguntas ambiguas se responden pensando en un aspirante. Sección de orientación vocacional reescrita. `provider.sort = "latency"` |

### Qué hace el backend en cada pregunta

1. **Topes:** 10 preguntas por minuto y 100 por día por IP, más un tope global diario. Se cuentan en la BD.
2. **Enmascarado:** DPI, carné, correo, teléfono y tarjeta se tapan **antes** de mandar la pregunta al modelo y de guardarla.
3. **Charla y búsqueda.** Los saludos, agradecimientos y despedidas no pasan por la búsqueda: el modelo los contesta con sus propias palabras. Las demás preguntas pasan por la búsqueda híbrida; si la mejor similitud queda bajo `UMBRAL_SIMILITUD`, el modelo recibe **cero fragmentos**, así que conversa y dice que no tiene el dato, pero no puede dar datos de la UVG.
4. **Streaming** de `gpt-oss-120b`:
   - `【n†…】` se convierte en `[n]`.
   - Se quitan los emojis.
   - Si la respuesta llega vacía, reintenta una vez.
5. **Al final:**
   - Valida las citas y descarta las inventadas.
   - Si el modelo dice que no sabe y no dio contacto, agrega el del área.
   - Si la respuesta repite el prompt, la reemplaza.
   - Guarda las métricas: tokens, costo, TTFT y latencia.

---

## Panel de administración (`/admin`)

Para que la universidad pueda actualizar el asistente **sin programar**:

1. Pon una contraseña en el `.env`: `ADMIN_PASSWORD=...` (una larga, que no sea de ejemplo).
2. Reinicia el servidor y abre **http://localhost:8000/admin**.

Desde el panel:

- **Documentos:** sube un PDF, Word, Markdown o texto. Se indexa al instante y el asistente lo usa en la siguiente pregunta. Si el nombre ya existe, reemplaza la versión anterior. Si el PDF es una foto escaneada, no tiene texto y se rechaza.
- **Preguntas frecuentes:** lo que más preguntan, agrupado aunque esté escrito distinto.
- **Sin respuesta:** preguntas que el asistente no pudo responder o que alguien marcó como no útiles. Es la lista de documentos que faltan o hay que mejorar.
- **Resumen:** preguntas de la semana, costo y tiempo de respuesta.

> **Sobre "entrenar" la IA:** el asistente no se reentrena. Lee los documentos en cada pregunta. Por eso subir un documento es la forma de enseñarle algo nuevo, y la calidad de las respuestas depende de qué tan claros estén los documentos.

Los nombres de archivo deben tener letras minúsculas, números, guiones o guion bajo, y una extensión `.md`, `.txt`, `.pdf` o `.docx`. Ejemplo: `biblioteca_horarios.pdf`.

## Actualizar el conocimiento

1. Agrega, cambia o borra archivos en `conocimiento/`. Funcionan `.md` (recomendado), `.pdf`, `.docx` y `.txt`.
2. Corre `python -m app.ingesta`. Solo procesa lo que cambió, y lo que se borró de la carpeta se borra de la base.

Formato recomendado en Markdown:

```markdown
---
titulo: Biblioteca
area: biblioteca                       # clave de conocimiento/_areas.yaml (contacto del área)
url: https://www.uvg.edu.gt/biblioteca/  # opcional: enlace de la cita
vigente_hasta: 2027-07-01              # opcional
---

# Biblioteca

## Horario
...

## Préstamo de libros
...
```

- Cada `##` es una sección que se cita.
- Las líneas que empiezan con `>` son notas internas y no se indexan.
- Los archivos que empiezan con `_` no se indexan; por ejemplo, `_areas.yaml` guarda los contactos por área.

> **`conocimiento/` tiene los documentos reales**, tomados de uvg.edu.gt el 17 de noviembre de 2026. Los documentos FICTICIOS del examen, con cifras inventadas y una inyección de prompt a propósito en `servicios_campus.md`, están en `examen/conocimiento_ficticio_fase1/` y los usan las pruebas de integración.

---

## Pruebas

```bash
cd backend && source .venv/bin/activate
pytest                                 # partición, posproceso, privacidad, cliente de OpenRouter (simulado)

# integración con Postgres: ingesta, búsqueda y /api/chat de punta a punta con un OpenRouter FALSO
# (usa una base VACÍA y desechable; sin red y sin costo)
createdb chatbot_uvg_test
TEST_DATABASE_URL=postgresql://localhost:5432/chatbot_uvg_test pytest
```

Para probar sin red ni costo, pon `MODELO_EMBEDDINGS=local`. Son embeddings falsos, que sirven solo para revisar que el flujo funciona, no para medir calidad.

---

## Estructura

```
chatbotUVG/
├── backend/
│   ├── app/
│   │   ├── config.py            configuración (.env)
│   │   ├── db.py                PostgreSQL (psycopg 3, sin ORM)
│   │   ├── migrar.py            aplica migraciones/*.sql una vez cada una
│   │   ├── openrouter.py        embeddings (OpenRouter o "local")
│   │   ├── fragmentar.py        lee .md/.pdf/.docx/.txt y los parte en secciones
│   │   ├── ingesta.py           conocimiento/ → fragmentos → embeddings → BD (idempotente)
│   │   ├── busqueda.py          búsqueda híbrida vector + texto (RRF) y umbral
│   │   ├── evaluar_busqueda.py  mide la búsqueda con las preguntas del examen
│   │   ├── prompt.py            prompt de sistema + armado de mensajes
│   │   ├── chat.py              orquesta una respuesta (topes → búsqueda → LLM → posproceso → BD)
│   │   ├── posproceso.py        【n】→[n], emojis, citas válidas, "no sé", filtro de salida
│   │   ├── privacidad.py        enmascara DPI, carné, correo, teléfono y tarjeta; hash de IP
│   │   ├── limites.py           topes por IP y global (en la BD)
│   │   ├── rutas/publico.py     /api/chat (SSE), /api/feedback, /api/sugerencias, /api/salud
│   │   └── main.py              app FastAPI, retención, /api/buscar (desarrollo)
│   ├── migraciones/001_inicial.sql
│   ├── tests/
│   └── requirements.txt
├── conocimiento/                documentos fuente + _areas.yaml
├── examen/                      examen de modelos (prueba de regresión)
├── ARQUITECTURA.md
└── investigacion_modelos_uvg.md
```
