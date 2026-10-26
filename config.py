import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_url: str
    api_key: str
    admin_ids: set[int]
    rate_limit_per_minute: int = 10
    public_access: bool = False
    log_chat_id: int | None = None
    payment_upi_id: str = ""
    payment_account_name: str = ""
    credit_packages: tuple[tuple[int, int], ...] = ((50, 49), (120, 99), (300, 199))
    public_api_url: str = ""
    public_api_enabled: bool = True
    api_user_rate_limit_per_minute: int = 60
    auto_cloudflare_tunnel: bool = False
    cloudflare_tunnel_token: str = ""


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    api_url = os.getenv("API_URL", "").strip().rstrip("/")
    api_key = os.getenv("API_KEY", "").strip()
    admin_raw = os.getenv("ADMIN_IDS", "").strip()
    rate_raw = os.getenv("RATE_LIMIT_PER_MINUTE", "10").strip()
    public_access = os.getenv("PUBLIC_ACCESS", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }
    log_chat_raw = os.getenv("LOG_CHAT_ID", "").strip()
    payment_upi_id = os.getenv("PAYMENT_UPI_ID", "").strip()
    payment_account_name = os.getenv("PAYMENT_ACCOUNT_NAME", "").strip()
    packages_raw = os.getenv("CREDIT_PACKAGES", "50:49,120:99,300:199").strip()
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    public_api_url = os.getenv("PUBLIC_API_URL", "https://api.teamsecret.in").strip().rstrip("/")
    if not public_api_url and railway_domain:
        public_api_url = f"https://{railway_domain}"
    public_api_enabled = os.getenv("ENABLE_PUBLIC_API", "true").strip().lower() in {"1", "true", "yes", "on"}
    api_user_rate_raw = os.getenv("API_USER_RATE_LIMIT_PER_MINUTE", "60").strip()
    auto_cloudflare_tunnel = os.getenv("AUTO_CLOUDFLARE_TUNNEL", "false").strip().lower() in {"1", "true", "yes", "on"}
    cloudflare_tunnel_token = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "").strip()

    packages = []
    for item in packages_raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        credit_raw, price_raw = item.split(":", 1)
        try:
            credit_amount = int(credit_raw.strip())
            price_amount = int(price_raw.strip())
        except ValueError:
            continue
        if credit_amount > 0 and price_amount > 0:
            packages.append((credit_amount, price_amount))
    if not packages:
        packages = [(50, 49), (120, 99), (300, 199)]

    if not bot_token:
        raise RuntimeError("BOT_TOKEN missing in .env")
    if not api_url:
        raise RuntimeError("API_URL missing in .env")
    if not api_key:
        raise RuntimeError("API_KEY missing in .env")

    admin_ids = set()
    for part in admin_raw.split(","):
        part = part.strip()
        if part:
            admin_ids.add(int(part))

    if not admin_ids:
        raise RuntimeError("At least one ADMIN_IDS value is required")

    try:
        rate_limit = max(1, int(rate_raw))
    except ValueError:
        rate_limit = 10

    try:
        api_user_rate_limit = max(1, int(api_user_rate_raw))
    except ValueError:
        api_user_rate_limit = 60

    return Config(
        bot_token=bot_token,
        api_url=api_url,
        api_key=api_key,
        admin_ids=admin_ids,
        rate_limit_per_minute=rate_limit,
        public_access=public_access,
        log_chat_id=int(log_chat_raw) if log_chat_raw else None,
        payment_upi_id=payment_upi_id,
        payment_account_name=payment_account_name,
        credit_packages=tuple(packages),
        public_api_url=public_api_url,
        public_api_enabled=public_api_enabled,
        api_user_rate_limit_per_minute=api_user_rate_limit,
        auto_cloudflare_tunnel=auto_cloudflare_tunnel,
        cloudflare_tunnel_token=cloudflare_tunnel_token,
    )
