from pydantic import Field, ConfigDict
from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ALLOWED_HOSTS: list[str]
    database_url: str
    jwt_secret: str
    jwt_algorithm: str
    access_token_expire_minutes: int
    refresh_token_expire_days: int
    paystack_secret_key: str
    paystack_webhook_secret: str
    elasticemail_api_key: str
    elasticemail_sender: str

    model_config = ConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)


settings = Settings()
