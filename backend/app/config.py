"""
Configuración tipada, leída de variables de entorno o del archivo .env en la raíz del repo.

Todo lo que puede cambiar entre desarrollo y producción (modelo, umbral, topes, secretos)
vive aquí. Cambiar de modelo es cambiar una variable, no el código.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> raíz del repo
RAIZ = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=RAIZ / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- entorno ---
    entorno: str = "desarrollo"  # "desarrollo" | "produccion"

    # --- base de datos ---
    database_url: str = "postgresql://localhost:5432/chatbot_uvg"

    # --- OpenRouter ---
    openrouter_api_key: str = ""
    openrouter_url: str = "https://openrouter.ai/api/v1"
    modelo_chat: str = "openai/gpt-oss-120b"
    # "local" = embeddings falsos sin red, solo para pruebas (ver openrouter.py).
    modelo_embeddings: str = "openai/text-embedding-3-small"

    # --- conocimiento y búsqueda ---
    carpeta_conocimiento: Path = RAIZ / "conocimiento"
    top_k: int = 4
    # Similitud coseno mínima del mejor fragmento para llamar al LLM. Se calibra con
    # `python -m app.evaluar_busqueda` (fase 1) y con el examen (fase 2).
    umbral_similitud: float = 0.40
    # Tamaño de los fragmentos, en tokens aproximados (1 token ≈ 4 caracteres en español).
    fragmento_tokens: int = 400
    fragmento_traslape: int = 60

    # --- chat (fase 2) ---
    max_tokens_respuesta: int = 700
    largo_max_mensaje: int = 500
    historial_max: int = 6           # turnos (usuario + asistente) que manda el cliente
    zona_horaria: str = "America/Guatemala"

    # --- topes de uso (LLM10: consumo sin límite) ---
    limite_ip_minuto: int = 10
    limite_ip_dia: int = 100
    limite_global_dia: int = 3000

    # --- seguridad y privacidad ---
    # Firma de cookies del panel y sal del hash de IP. En producción: valor largo y aleatorio.
    secreto_sesion: str = "cambiar-en-produccion"
    # Orígenes (además del propio) que pueden llamar a /api/chat desde un navegador.
    dominios_permitidos: str = "localhost,127.0.0.1"
    retencion_dias: int = 90

    # --- panel de administración (fase 4) ---
    # Contraseña del panel /admin. Si está vacía, el panel queda desactivado.
    admin_password: str = ""
    max_subida_mb: int = 5

    @property
    def lista_dominios(self) -> list[str]:
        return [d.strip().lower() for d in self.dominios_permitidos.split(",") if d.strip()]

    @property
    def es_desarrollo(self) -> bool:
        return self.entorno == "desarrollo"


@lru_cache
def settings() -> Settings:
    return Settings()
