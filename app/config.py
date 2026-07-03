from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    data_dir: str = "./data"
    tz: str = "Europe/Berlin"

    # Notification credentials live in the environment (Portainer), not in the
    # DB. Only the operational addresses (resend_from / resend_to) are stored
    # in SQLite via the Settings UI.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    resend_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


config = AppConfig()
