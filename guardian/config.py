from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ring_api_base_url: str = "http://127.0.0.1:8787"
    ring_access_token: str = "dev-token"
    ring_webhook_secret: str = "dev-hmac-key"
    ring_poll_seconds: float = 15
    sensor_stale_minutes: int = 60
    valve_close_seconds: float = 4
    verify_quiet_seconds: float = 60
    approval_timeout_seconds: float = 120
    corroboration_seconds: float = 30
    home_timezone: str = "Asia/Kolkata"

    # Bedrock grounded reports; empty model id or no AWS credentials → template report
    bedrock_model_id: str = ""
    aws_region: str = "us-east-1"
    bedrock_timeout_seconds: float = 8

    # Module 6: Amazon SNS (optional). Per-role topics fall back to SNS_TOPIC_ARN.
    sns_topic_arn: str = ""
    sns_topic_arn_owner: str = ""
    sns_topic_arn_family: str = ""
    sns_topic_arn_neighbor: str = ""

    # Module 4
    emergency_number_fire: str = "101"
    chime_warning_slot: str = "ring-appstore-event-1"
    temp_low_c: float = 5
    temp_high_c: float = 35
    temp_verify_minutes: float = 20

    # Module 6: escalation ladder owner -> family -> neighbor until acknowledged
    escalation_roles: str = "owner,family,neighbor"
    escalation_interval_seconds: float = 120
    escalation_max_rounds: int = 3

    # Module 3: routine + check-ins
    checkin_timeout_seconds: float = 180
    routine_repeat_seconds: float = 300  # missed-routine family re-alert interval
    routine_night_start: str = "22:00"
    routine_night_end: str = "05:00"

    # Module 2: Alexa+ MCP server, OAuth, owner login
    env: str = "development"  # development | production
    public_base_url: str = "http://127.0.0.1:8000"  # public HTTPS URL (cloudflared) in real use
    owner_email: str = ""
    owner_password_hash: str = ""  # bcrypt; empty in development = dashboard login disabled
    session_secret: str = ""  # cookie signing key; random per process if empty (development)
    alexa_oauth_client_id: str = "guardian-alexa"
    alexa_oauth_client_secret: str = ""
    alexa_oauth_redirect_uris: str = ""  # comma-separated allowlist
    jwt_private_key_path: str = "keys/jwt_private.pem"
    dev_mcp_token: str = ""  # bearer token for MCP Inspector; ignored when ENV=production

    database_url: str = "sqlite:///guardian.db"

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def mcp_resource(self) -> str:
        """Canonical URI of the MCP server: the OAuth `resource` and the token `aud`."""
        return self.public_base_url.rstrip("/") + "/mcp"

    @property
    def redirect_uris(self) -> list[str]:
        return [u.strip() for u in self.alexa_oauth_redirect_uris.split(",") if u.strip()]
    public_url: str = "http://127.0.0.1:8000"  # where the emulator delivers webhooks


settings = Settings()
