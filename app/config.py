from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


DEFAULT_TDX_BASE_URL = "https://tdx.transportdata.tw/api/basic"
DEFAULT_TDX_TOKEN_URL = (
    "https://tdx.transportdata.tw/auth/realms/TDXConnect/"
    "protocol/openid-connect/token"
)
DEFAULT_TDX_CITIES = (
    "Taipei",
    "NewTaipei",
    "Taoyuan",
    "Taichung",
    "Tainan",
    "Kaohsiung",
    "Keelung",
    "Hsinchu",
    "HsinchuCounty",
    "MiaoliCounty",
    "ChanghuaCounty",
    "NantouCounty",
    "YunlinCounty",
    "Chiayi",
    "ChiayiCounty",
    "PingtungCounty",
    "YilanCounty",
    "HualienCounty",
    "TaitungCounty",
    "PenghuCounty",
    "KinmenCounty",
    "LienchiangCounty",
)

INTERCITY_CITY_NAME = "InterCity"
INTERCITY_PREFIX = "INT"

CITY_PREFIX_TO_NAME = {
    "CHA": "ChanghuaCounty",
    "CYI": "Chiayi",
    "CYQ": "ChiayiCounty",
    "HSQ": "HsinchuCounty",
    "HSZ": "Hsinchu",
    "HUA": "HualienCounty",
    "ILA": "YilanCounty",
    "INT": INTERCITY_CITY_NAME,
    "KEE": "Keelung",
    "KHH": "Kaohsiung",
    "KIN": "KinmenCounty",
    "LIE": "LienchiangCounty",
    "MIA": "MiaoliCounty",
    "NAN": "NantouCounty",
    "NWT": "NewTaipei",
    "PEN": "PenghuCounty",
    "PIF": "PingtungCounty",
    "TAO": "Taoyuan",
    "TNN": "Tainan",
    "TPE": "Taipei",
    "TTT": "TaitungCounty",
    "TXG": "Taichung",
    "YUN": "YunlinCounty",
}
CITY_NAME_TO_PREFIX = {city: prefix for prefix, city in CITY_PREFIX_TO_NAME.items()}


def load_dotenv(dotenv_path: str | Path) -> None:
    path = Path(dotenv_path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default

    cleaned = value.strip().lower()
    if not cleaned:
        return default
    if cleaned in {"1", "true", "yes", "on"}:
        return True
    if cleaned in {"0", "false", "no", "off"}:
        return False
    return default


def _split_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default

    items = []
    seen = set()
    for item in value.split(","):
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        items.append(cleaned)
    return tuple(items) or default


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    db_path: Path
    download_db_path: Path
    tdx_client_id: str | None
    tdx_client_secret: str | None
    tdx_base_url: str
    tdx_token_url: str
    tdx_cities: tuple[str, ...]
    tdx_request_timeout: int
    tdx_token_refresh_skew: int
    tdx_retry_attempts: int
    tdx_retry_backoff: float
    tdx_min_request_interval: float
    realtime_cache_ttl: int
    realtime_track_ttl: int
    cors_origins: tuple[str, ...]
    auth_public_base_url: str
    auth_state_ttl_seconds: int
    auth_snowflake_node_id: int
    discord_oauth_client_id: str | None
    discord_oauth_client_secret: str | None
    google_oauth_client_id: str | None
    google_oauth_client_secret: str | None
    google_native_oauth_client_ids: tuple[str, ...]
    app_public_base_url: str = "https://busapp.avianjay.sbs"
    fcm_project_id: str = "yabus-111c1"
    fcm_service_account_json: str | None = None
    fcm_service_account_json_path: str | None = None
    fcm_web_api_key: str = "AIzaSyAMzgL6WxQarMcuXYrrqOHZsxUFVytkcuM"
    fcm_web_auth_domain: str = "yabus-111c1.firebaseapp.com"
    fcm_web_storage_bucket: str = "yabus-111c1.firebasestorage.app"
    fcm_web_messaging_sender_id: str = "1011547280811"
    fcm_web_app_id: str = "1:1011547280811:web:7e1e0c0a1baa160df7aeee"
    fcm_web_measurement_id: str = "G-RB7WBXXRQN"
    fcm_web_vapid_key: str = ""
    account_sync_max_payload_bytes: int = 512 * 1024
    account_sync_max_favorites: int = 25
    account_sync_max_group_name_length: int = 120
    account_sync_max_json_depth: int = 16
    # Optional Discord webhook for new-feedback notifications. The notification
    # intentionally contains only metadata and an admin link, never the raw
    # user-submitted title/content, so malicious input cannot reach Discord.
    feedback_discord_webhook_url: str | None = None
    # Application database (auth, analytics, announcements, feedback, account
    # sync). Independent of the static route database so the weekly static sync
    # never touches it. When not provided, it defaults to "app.db" next to db_path.
    app_db_path: Path | None = None
    # Some authorities (公路客運/THB and most counties) give each direction of a
    # route its own SubRouteUID, which used to surface as two separate routes.
    # When enabled, sync_static merges direction siblings into one route with
    # two paths. Turning this off reproduces the pre-merge rows byte for byte,
    # so a rollback is one env change plus a re-sync.
    static_merge_directions: bool = True
    # Cities whose realtime feeds stopped populating SubRouteUID (items carry
    # only RouteUID + Direction). Their batch requests filter on RouteUID
    # instead — a SubRouteUID filter matches nothing there. Set the env var to
    # an empty value to fall back to the legacy SubRouteUID filter everywhere.
    tdx_routeuid_filter_cities: tuple[str, ...] = ("Taipei", "NewTaipei")
    # Whole-city live bus feed (全公車地圖). One unfiltered TDX pull per city is
    # cached for this long and shared by every client, so upstream load is
    # bounded by the TTL rather than by how many people have the map open.
    city_buses_cache_ttl: int = 15
    # How long a snapshot may still be served after upstream starts failing.
    # Past this the endpoint 502s so clients back off instead of showing buses
    # that stopped moving minutes ago.
    city_buses_stale_max_seconds: int = 120
    # $top for the unfiltered pull. Measured 2026-09-06: Taipei 777 / NewTaipei
    # 633 / Taichung 328 items in one page, InterCity 1295 over two pages, so
    # 1000 pages everything without a silent cap.
    city_buses_page_size: int = 1000
    city_buses_rate_limit_requests: int = 30

    def __post_init__(self) -> None:
        if self.app_db_path is None:
            object.__setattr__(self, "app_db_path", self.db_path.parent / "app.db")

    def city_db_path(self, city: str) -> Path:
        return self.download_db_path.parent / f"{city}.db"

    @classmethod
    def from_env(cls) -> "Settings":
        project_dir = Path(__file__).resolve().parent.parent
        load_dotenv(project_dir / ".env")
        db_path = Path(os.getenv("BUS_DB_PATH", project_dir / "bus.db")).resolve()
        download_db_path = Path(
            os.getenv("BUS_DOWNLOAD_DB_PATH", project_dir / "downloads" / "bus.db")
        ).resolve()
        app_db_path = Path(
            os.getenv("BUS_APP_DB_PATH", db_path.parent / "app.db")
        ).resolve()
        return cls(
            project_dir=project_dir,
            db_path=db_path,
            download_db_path=download_db_path,
            app_db_path=app_db_path,
            tdx_client_id=os.getenv("TDX_CLIENT_ID"),
            tdx_client_secret=os.getenv("TDX_CLIENT_SECRET"),
            tdx_base_url=os.getenv("TDX_BASE_URL", DEFAULT_TDX_BASE_URL).rstrip("/"),
            tdx_token_url=os.getenv("TDX_TOKEN_URL", DEFAULT_TDX_TOKEN_URL),
            tdx_cities=_split_csv(os.getenv("TDX_CITIES"), DEFAULT_TDX_CITIES),
            tdx_request_timeout=int(os.getenv("TDX_REQUEST_TIMEOUT", "30")),
            tdx_token_refresh_skew=int(os.getenv("TDX_TOKEN_REFRESH_SKEW", "300")),
            tdx_retry_attempts=int(os.getenv("TDX_RETRY_ATTEMPTS", "6")),
            tdx_retry_backoff=float(os.getenv("TDX_RETRY_BACKOFF", "2.0")),
            tdx_min_request_interval=float(os.getenv("TDX_MIN_REQUEST_INTERVAL", "0.5")),
            realtime_cache_ttl=int(os.getenv("REALTIME_CACHE_TTL", "15")),
            realtime_track_ttl=int(os.getenv("REALTIME_TRACK_TTL", "30")),
            cors_origins=_split_csv(os.getenv("CORS_ORIGINS"), ()),
            auth_public_base_url=os.getenv(
                "AUTH_PUBLIC_BASE_URL",
                "https://bus.avianjay.sbs",
            ).rstrip("/"),
            auth_state_ttl_seconds=int(os.getenv("AUTH_STATE_TTL_SECONDS", "600")),
            auth_snowflake_node_id=int(os.getenv("AUTH_SNOWFLAKE_NODE_ID", "0")),
            discord_oauth_client_id=os.getenv("DISCORD_OAUTH_CLIENT_ID"),
            discord_oauth_client_secret=os.getenv("DISCORD_OAUTH_CLIENT_SECRET"),
            google_oauth_client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
            google_oauth_client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
            google_native_oauth_client_ids=_split_csv(
                os.getenv("GOOGLE_NATIVE_OAUTH_CLIENT_IDS"),
                (),
            ),
            app_public_base_url=os.getenv(
                "APP_PUBLIC_BASE_URL",
                "https://busapp.avianjay.sbs",
            ).rstrip("/"),
            fcm_project_id=os.getenv("FCM_PROJECT_ID", "yabus-111c1").strip()
            or "yabus-111c1",
            fcm_service_account_json=os.getenv("FCM_SERVICE_ACCOUNT_JSON"),
            fcm_service_account_json_path=os.getenv("FCM_SERVICE_ACCOUNT_JSON_PATH"),
            fcm_web_api_key=os.getenv(
                "FCM_WEB_API_KEY",
                "AIzaSyAMzgL6WxQarMcuXYrrqOHZsxUFVytkcuM",
            ).strip(),
            fcm_web_auth_domain=os.getenv(
                "FCM_WEB_AUTH_DOMAIN",
                "yabus-111c1.firebaseapp.com",
            ).strip(),
            fcm_web_storage_bucket=os.getenv(
                "FCM_WEB_STORAGE_BUCKET",
                "yabus-111c1.firebasestorage.app",
            ).strip(),
            fcm_web_messaging_sender_id=os.getenv(
                "FCM_WEB_MESSAGING_SENDER_ID",
                "1011547280811",
            ).strip(),
            fcm_web_app_id=os.getenv(
                "FCM_WEB_APP_ID",
                "1:1011547280811:web:7e1e0c0a1baa160df7aeee",
            ).strip(),
            fcm_web_measurement_id=os.getenv(
                "FCM_WEB_MEASUREMENT_ID",
                "G-RB7WBXXRQN",
            ).strip(),
            fcm_web_vapid_key=os.getenv("FCM_WEB_VAPID_KEY", "").strip(),
            account_sync_max_payload_bytes=int(
                os.getenv("ACCOUNT_SYNC_MAX_PAYLOAD_BYTES", str(512 * 1024))
            ),
            feedback_discord_webhook_url=(
                os.getenv("FEEDBACK_DISCORD_WEBHOOK_URL", "").strip() or None
            ),
            account_sync_max_favorites=int(
                os.getenv("ACCOUNT_SYNC_MAX_FAVORITES", "25")
            ),
            account_sync_max_group_name_length=int(
                os.getenv("ACCOUNT_SYNC_MAX_GROUP_NAME_LENGTH", "120")
            ),
            static_merge_directions=_env_bool(
                os.getenv("STATIC_MERGE_DIRECTIONS"), True
            ),
            account_sync_max_json_depth=int(
                os.getenv("ACCOUNT_SYNC_MAX_JSON_DEPTH", "16")
            ),
            # Unlike the other CSV vars, an explicitly empty value means
            # "no cities" (i.e. legacy SubRouteUID filters everywhere), so the
            # unset/empty distinction matters here.
            tdx_routeuid_filter_cities=(
                ("Taipei", "NewTaipei")
                if os.getenv("TDX_ROUTEUID_FILTER_CITIES") is None
                else _split_csv(os.getenv("TDX_ROUTEUID_FILTER_CITIES"), ())
            ),
            city_buses_cache_ttl=int(os.getenv("CITY_BUSES_CACHE_TTL", "15")),
            city_buses_stale_max_seconds=int(
                os.getenv("CITY_BUSES_STALE_MAX_SECONDS", "120")
            ),
            city_buses_page_size=int(os.getenv("CITY_BUSES_PAGE_SIZE", "1000")),
            city_buses_rate_limit_requests=int(
                os.getenv("CITY_BUSES_RATE_LIMIT_REQUESTS", "30")
            ),
        )

    def require_tdx_credentials(self) -> None:
        if self.tdx_client_id and self.tdx_client_secret:
            return
        raise RuntimeError(
            "TDX_CLIENT_ID and TDX_CLIENT_SECRET must both be set in the environment."
        )


def guess_city_from_routeid(routeid: str, allowed_cities: tuple[str, ...]) -> str | None:
    prefix = routeid[:3].upper()
    city = CITY_PREFIX_TO_NAME.get(prefix)
    if city == INTERCITY_CITY_NAME:
        return city
    if city and city in allowed_cities:
        return city
    return None


def routeid_to_city(routeid: str) -> str | None:
    return CITY_PREFIX_TO_NAME.get(routeid[:3].upper())


def is_intercity_city(city: str) -> bool:
    return city == INTERCITY_CITY_NAME


def to_intercity_routeid(routeid: str) -> str:
    normalized = routeid.strip()
    if not normalized:
        return normalized
    if normalized[:3].upper() == INTERCITY_PREFIX:
        return normalized
    return f"{INTERCITY_PREFIX}{normalized}"


def from_intercity_routeid(routeid: str) -> str:
    normalized = routeid.strip()
    if normalized[:3].upper() == INTERCITY_PREFIX:
        return normalized[3:]
    return normalized


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
