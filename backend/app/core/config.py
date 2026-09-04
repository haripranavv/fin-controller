from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central app configuration, loaded from environment / .env.

    See backend/.env.example for the full list of variables.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://finance:finance@localhost:5432/finance_controller"

    # Ground truth lives in its own Postgres schema so it is isolated at the
    # database level, not just by Python import discipline. See
    # app/db/groundtruth_session.py.
    ground_truth_schema: str = "ground_truth"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # The root-cause investigator's real LLM provider (app.rootcause.client.
    # GeminiRootCauseClient). anthropic_api_key above is kept as a fallback
    # real provider, never removed — see app.rootcause.client's docstring.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Demo login only - see app/api/routes_auth.py's module docstring for
    # exactly what this is and isn't. Not a real user/credential store.
    demo_email: str = "operator@financecontroller.demo"
    demo_password: str = "finance-demo-2026"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
