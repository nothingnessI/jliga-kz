from pydantic_settings import BaseSettings, SettingsConfigDict

from pathlib import Path


ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # APP

    APP_NAME: str
    APP_HOST: str
    APP_PORT: int

    # DB

    DB_USER: str
    DB_PASSWORD: str
    DB_HOST: str
    DB_PORT: int
    DB_NAME: str


    # Tokens

    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int


    @property
    def get_db_url(self):
        return f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"


settings = Settings()