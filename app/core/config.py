"""api_consum settings — .env-overridable, nothing hardcoded."""
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "CONSUM-IA"
    DESCRIPTION: str = ("CONSUM-IA server side — public landing, pilot fleet "
                        "services and the family-notify subscriber.")
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"

    HOST: str = "0.0.0.0"
    PORT: int = 8183

    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DB_PATH: Path = Path(__file__).resolve().parent.parent.parent / "consum.db"

    # MQTT uplink (same broker the edge gateways publish to). Empty broker
    # disables the family-notify subscriber — landing still works.
    MQTT_BROKER: str = ""
    MQTT_PORT: int = 1883
    MQTT_USER: str = ""
    MQTT_PASS: str = ""
    # Edge gateways publish actionable events (threshold alerts, left-on)
    # here — see cm4-consumia poller/notify.py.
    MQTT_EVENT_TOPIC: str = "home/+/+/event"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


settings = Settings()
