import json
from pathlib import Path
from typing import Annotated, Optional

from pydantic import ConfigDict, field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    ALLOWED_HOSTS: Annotated[list[str], NoDecode]
    database_url: str
    jwt_secret: str
    jwt_algorithm: str
    access_token_expire_minutes: int
    refresh_token_expire_days: int
    paystack_secret_key: str
    paystack_webhook_secret: str
    elasticemail_api_key: str
    elasticemail_sender: str
    invitation_base_url: str = "https://y3ndidi.com/invite"

    model_config = ConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @field_validator("ALLOWED_HOSTS", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if value.startswith("["):
            return json.loads(value)
        return [host.strip() for host in value.split(",") if host.strip()]


settings = Settings()
