-- 001_inicial.sql — esquema inicial del chatbot UVG (ver ARQUITECTURA.md §5)
-- REGLA: una migración que ya corrió NO se edita. Cualquier cambio va en 002_..., 003_..., etc.

CREATE EXTENSION IF NOT EXISTS vector;

-- Un registro por archivo de conocimiento/
CREATE TABLE documentos (
    id                 serial PRIMARY KEY,
    ruta               text NOT NULL UNIQUE,          -- relativa a conocimiento/, p. ej. "admisiones.md"
    titulo             text NOT NULL,
    area               text NOT NULL DEFAULT 'general', -- clave de conocimiento/_areas.yaml
    url_publica        text,                           -- página oficial para el enlace de la cita
    vigente_hasta      date,
    hash_contenido     text NOT NULL,                  -- sha256(archivo + modelo de embeddings)
    modelo_embeddings  text NOT NULL,
    actualizado_en     timestamptz NOT NULL DEFAULT now()
);

-- Secciones de ~400 tokens. El embedding no tiene dimensión fija para poder elegir el modelo
-- en la fase 1; con menos de ~10k fragmentos la búsqueda exacta no necesita índice.
CREATE TABLE fragmentos (
    id            bigserial PRIMARY KEY,
    documento_id  integer NOT NULL REFERENCES documentos(id) ON DELETE CASCADE,
    orden         integer NOT NULL,
    seccion       text NOT NULL,
    texto         text NOT NULL,
    tokens        integer NOT NULL,
    embedding     vector NOT NULL,
    tsv           tsvector GENERATED ALWAYS AS (
                      to_tsvector('spanish', seccion || ' ' || texto)
                  ) STORED
);
CREATE INDEX fragmentos_documento_idx ON fragmentos (documento_id);
CREATE INDEX fragmentos_tsv_idx ON fragmentos USING gin (tsv);

-- Una conversación por sesión del widget. Nunca se guarda la IP en claro.
CREATE TABLE conversaciones (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    origen     text,                -- dominio de la página que hospeda el widget
    ip_hash    text,
    creada_en  timestamptz NOT NULL DEFAULT now()
);

-- Preguntas y respuestas. El texto se guarda con datos personales enmascarados.
CREATE TABLE mensajes (
    id                  bigserial PRIMARY KEY,
    conversacion_id     uuid NOT NULL REFERENCES conversaciones(id) ON DELETE CASCADE,
    rol                 text NOT NULL CHECK (rol IN ('usuario', 'asistente')),
    texto               text NOT NULL,
    embedding_pregunta  vector,                 -- solo en mensajes del usuario; alimenta el panel de FAQ
    fragmentos_usados   bigint[],
    citas               integer[],
    estado              text CHECK (estado IN ('ok', 'sin_info', 'no_sabe', 'bloqueado', 'error')),
    feedback            smallint NOT NULL DEFAULT 0 CHECK (feedback BETWEEN -1 AND 1),
    modelo              text,
    tokens_entrada      integer,
    tokens_salida       integer,
    costo_usd           numeric(12, 8),
    ttft_ms             integer,
    latencia_ms         integer,
    creada_en           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX mensajes_conversacion_idx ON mensajes (conversacion_id);
CREATE INDEX mensajes_creada_idx ON mensajes (creada_en);

-- Contadores para los topes de uso (rate limit por IP y global). En la BD, no en memoria,
-- para que sobrevivan a reinicios y despliegues.
CREATE TABLE uso (
    clave   text NOT NULL,          -- 'ip:<hash>' o 'global'
    dia     date NOT NULL,
    conteo  integer NOT NULL DEFAULT 0,
    PRIMARY KEY (clave, dia)
);
