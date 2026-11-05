from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NIFTYPILOT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./niftypilot.db"
    # Paper is the only mode that exists. Any other value fails validation at startup;
    # live execution needs a separate, explicit activation process (spec §6, §12).
    trading_mode: Literal["paper"] = "paper"
    cors_origins: list[str] = ["http://localhost:5173"]
