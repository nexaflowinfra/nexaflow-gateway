from datetime import datetime, timezone, timedelta
from hashlib import sha256
from html import escape as escape_html
from math import ceil
from pathlib import Path
from time import time
from io import StringIO
import csv
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from openai import APIStatusError, OpenAI
from pydantic import BaseModel, Field

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
MARKETING_DIR = BASE_DIR / "marketing"
CLIENTS_FILE = BASE_DIR / "clients.json"
USAGE_LOGS_FILE = BASE_DIR / "usage_logs.json"
DATABASE_FILE = Path(os.getenv("NEXAFLOW_DB_PATH", BASE_DIR / "nexaflow.db"))
BACKUP_DIR = Path(os.getenv("NEXAFLOW_BACKUP_DIR", DATABASE_FILE.parent / "backups"))
AUTO_BACKUP_ENABLED = os.getenv("NEXAFLOW_AUTO_BACKUP_ENABLED", "true").lower() not in {"0", "false", "no"}
AUTO_BACKUP_INTERVAL_SECONDS = int(os.getenv("NEXAFLOW_AUTO_BACKUP_INTERVAL_SECONDS", "86400"))
AUTO_BACKUP_INITIAL_DELAY_SECONDS = int(os.getenv("NEXAFLOW_AUTO_BACKUP_INITIAL_DELAY_SECONDS", "300"))
AUTO_BACKUP_RETENTION_COUNT = int(os.getenv("NEXAFLOW_BACKUP_RETENTION_COUNT", "14"))

PLANS = {
    "starter": {
        "name": "Starter",
        "monthly_price_usd": 19,
        "included_credits": 10000,
        "default_model": "gpt-4o-mini",
        "allowed_tiers": ["economy"],
        "rate_limit_per_minute": 30,
        "daily_request_limit": 300,
        "daily_credit_limit": 3500,
        "max_request_credits": 50,
        "description": "For testing, small automations, and low-volume customer support.",
    },
    "pro": {
        "name": "Pro",
        "monthly_price_usd": 49,
        "included_credits": 50000,
        "default_model": "gpt-4o-mini",
        "allowed_tiers": ["economy", "standard"],
        "rate_limit_per_minute": 90,
        "daily_request_limit": 1500,
        "daily_credit_limit": 20000,
        "max_request_credits": 200,
        "description": "For production workflows that need stronger reasoning and higher limits.",
    },
    "business": {
        "name": "Business",
        "monthly_price_usd": 149,
        "included_credits": 200000,
        "default_model": "gpt-4o-mini",
        "allowed_tiers": ["economy", "standard", "premium"],
        "premium_tiers": ["premium"],
        "min_gross_margin_ratio": 0.35,
        "rate_limit_per_minute": 240,
        "daily_request_limit": 6000,
        "daily_credit_limit": 75000,
        "max_request_credits": 750,
        "description": "For teams that need larger quotas, controlled premium access, and managed onboarding.",
    },
}

PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "api_key_env": "OPENAI_API_KEY",
        "expected_key_prefix": "sk-",
        "base_url": None,
    },
    "openrouter": {
        "name": "OpenRouter",
        "api_key_env": "OPENROUTER_API_KEY",
        "expected_key_prefix": "sk-or-",
        "base_url": "https://openrouter.ai/api/v1",
    },
}

MODEL_CATALOG = {
    "gpt-4o-mini": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "tier": "economy",
        "input_usd_per_million": 0.15,
        "output_usd_per_million": 0.60,
        "tasks": ["chat", "support", "summary", "classification"],
    },
    "gpt-4.1": {
        "provider": "openai",
        "model": "gpt-4.1",
        "tier": "premium",
        "input_usd_per_million": 2.00,
        "output_usd_per_million": 8.00,
        "tasks": ["chat", "support", "summary", "classification", "reasoning"],
    },
    "openrouter/auto": {
        "provider": "openrouter",
        "model": "openrouter/auto",
        "tier": "standard",
        "input_usd_per_million": 1.00,
        "output_usd_per_million": 3.00,
        "tasks": ["chat", "support", "summary", "classification", "reasoning"],
    },
}

CREDIT_UNIT_TOKENS = 1000
OUTPUT_TOKEN_WEIGHT = 4

request_windows = {}
enquiry_windows = {}
backup_scheduler_state = {
    "enabled": AUTO_BACKUP_ENABLED,
    "interval_seconds": AUTO_BACKUP_INTERVAL_SECONDS,
    "initial_delay_seconds": AUTO_BACKUP_INITIAL_DELAY_SECONDS,
    "retention_count": AUTO_BACKUP_RETENTION_COUNT,
    "started": False,
    "last_run_at": None,
    "last_success_at": None,
    "last_backup": None,
    "last_error": None,
}
backup_scheduler_stop = threading.Event()
backup_scheduler_thread = None

app = FastAPI(
    title="NexaFlow AI Gateway",
    version="1.0.0",
    description="A paid API gateway for metered access to AI chat completions.",
)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "media-src 'self' https:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    ),
}


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.on_event("startup")
def on_startup():
    start_backup_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    stop_backup_scheduler()


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=12000)
    task: str = Field(default="chat", max_length=40)
    model: str | None = None
    provider: str | None = None
    routing_strategy: str = Field(default="profit", pattern="^(profit|cost|default)$")


class CreateClientRequest(BaseModel):
    client_id: str = Field(..., min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    plan: str = "starter"
    credits: int | None = Field(default=None, ge=0)
    billing_email: str | None = None


class UpdateClientRequest(BaseModel):
    plan: str | None = None
    credits: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, pattern="^(active|paused|cancelled)$")
    billing_email: str | None = None


class TopupRequest(BaseModel):
    amount: int = Field(..., gt=0)


class SendApiKeyRequest(BaseModel):
    rotate: bool = True


class PaymentWebhookRequest(BaseModel):
    event_id: str = Field(..., min_length=3, max_length=200)
    event_type: str = Field(..., max_length=80)
    provider: str = Field(default="manual", max_length=80)
    client_id: str | None = Field(default=None, min_length=3, max_length=80)
    plan: str = "starter"
    billing_email: str | None = None
    amount_usd: float | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", max_length=10)
    mode: str = Field(default="subscription", pattern="^(subscription|topup)$")
    credits: int | None = Field(default=None, ge=0)
    stripe_billing_reason: str | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    stripe_subscription_status: str | None = None


class EnquiryCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    phone: str = Field(..., min_length=5, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    business_slug: str | None = Field(default=None, max_length=80)
    business_type: str = Field(default="general", max_length=80)
    message: str = Field(..., min_length=3, max_length=4000)
    source: str = Field(default="web", max_length=80)
    campaign: str = Field(default="", max_length=120)
    referrer: str = Field(default="", max_length=300)
    page_url: str = Field(default="", max_length=500)
    pdpa_consent: bool = False


class MerchantManualEnquiryCreate(BaseModel):
    name: str | None = Field(default="", max_length=120)
    phone: str | None = Field(default="", max_length=40)
    email: str | None = Field(default=None, max_length=200)
    message: str = Field(..., min_length=3, max_length=4000)
    source: str = Field(default="manual", max_length=80)
    campaign: str = Field(default="manual-capture", max_length=120)
    processing_acknowledged: bool = False


class MerchantCopilotAnalyzeRequest(BaseModel):
    name: str | None = Field(default="", max_length=120)
    phone: str | None = Field(default="", max_length=40)
    email: str | None = Field(default=None, max_length=200)
    message: str = Field(..., min_length=3, max_length=4000)
    source: str = Field(default="manual", max_length=80)
    campaign: str = Field(default="copilot-preview", max_length=120)
    processing_acknowledged: bool = False


class BusinessProfileRequest(BaseModel):
    slug: str = Field(..., min_length=3, max_length=80)
    business_name: str = Field(..., min_length=1, max_length=160)
    business_type: str = Field(default="general", max_length=80)
    whatsapp_phone: str = Field(..., min_length=5, max_length=40)
    contact_email: str | None = Field(default=None, max_length=200)
    offer_summary: str = Field(default="", max_length=600)
    reply_tone: str = Field(default="friendly and professional", max_length=120)
    opening_hours: str = Field(default="", max_length=200)
    status: str = Field(default="active", pattern="^(active|paused)$")
    rotate_access_key: bool = False
    auto_followup_enabled: bool = True
    hot_followup_hours: int = Field(default=0, ge=0, le=72)
    standard_followup_days: int = Field(default=1, ge=1, le=30)
    data_retention_days: int = Field(default=365, ge=30, le=2555)


class BusinessProfileSettingsUpdate(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=160)
    business_type: str = Field(default="general", max_length=80)
    whatsapp_phone: str = Field(..., min_length=5, max_length=40)
    contact_email: str | None = Field(default=None, max_length=200)
    offer_summary: str = Field(default="", max_length=600)
    reply_tone: str = Field(default="friendly and professional", max_length=120)
    opening_hours: str = Field(default="", max_length=200)
    auto_followup_enabled: bool = True
    hot_followup_hours: int = Field(default=0, ge=0, le=72)
    standard_followup_days: int = Field(default=1, ge=1, le=30)
    data_retention_days: int = Field(default=365, ge=30, le=2555)


class MerchantSignupRequest(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=160)
    whatsapp_phone: str = Field(..., min_length=5, max_length=40)
    contact_email: str | None = Field(default="", max_length=200)
    business_type: str = Field(default="used_car_dealer", max_length=80)
    market: str = Field(default="my", pattern="^(my|sg|other)$")
    preferred_slug: str | None = Field(default=None, max_length=80)
    monthly_enquiries: str = Field(default="", max_length=80)
    pdpa_consent: bool = False


class EnquiryStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(new|contacted|quoted|won|lost|spam)$")


class MerchantEnquiryUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(new|contacted|quoted|won|lost|spam)$")
    internal_note: str | None = Field(default=None, max_length=1000)
    follow_up_at: str | None = Field(default=None, max_length=80)
    deal_value: float | None = Field(default=None, ge=0, le=1000000000)


class ChannelConnectionUpdate(BaseModel):
    integration_mode: str = Field(
        default="official_api_requested",
        pattern="^(official_api_requested|assisted_capture|smart_link|lead_form)$",
    )
    status: str = Field(default="requested", pattern="^(requested|assisted|paused)$")
    account_label: str = Field(default="", max_length=160)
    external_account_id: str = Field(default="", max_length=160)
    data_processing_acknowledged: bool = False
    notes: str = Field(default="", max_length=500)


class TrialRequestCreate(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=160)
    contact_name: str = Field(..., min_length=1, max_length=120)
    contact_email: str | None = Field(default=None, max_length=200)
    whatsapp_phone: str = Field(..., min_length=5, max_length=40)
    business_type: str = Field(default="service business", max_length=80)
    city: str = Field(default="", max_length=120)
    monthly_enquiries: str = Field(default="", max_length=80)
    lead_source: str = Field(default="", max_length=80)
    campaign: str = Field(default="", max_length=120)
    referrer: str = Field(default="", max_length=300)
    message: str = Field(default="", max_length=1000)
    pdpa_consent: bool = False


class TrialRequestUpdate(BaseModel):
    status: str = Field(..., pattern="^(new|contacted|trial_setup|won|lost|spam)$")
    internal_note: str | None = Field(default=None, max_length=1000)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, data):
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def db_connection():
    connection = sqlite3.connect(DATABASE_FILE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def backup_filename():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"nexaflow-backup-{timestamp}.db"


def backup_path_for_name(name):
    if not name.startswith("nexaflow-backup-") or not name.endswith(".db"):
        raise HTTPException(status_code=400, detail="Invalid backup name")

    resolved = (BACKUP_DIR / name).resolve()
    backup_root = BACKUP_DIR.resolve()
    if backup_root not in resolved.parents and resolved != backup_root:
        raise HTTPException(status_code=400, detail="Invalid backup path")

    return resolved


def backup_manifest_path(path):
    return path.with_suffix(path.suffix + ".json")


def write_backup_manifest(backup):
    manifest = backup_manifest_path(Path(backup["path"]))
    manifest.write_text(json.dumps(backup, indent=2), encoding="utf-8")


def create_sqlite_backup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / backup_filename()

    with db_connection() as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)

    backup = {
        "name": target.name,
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "created_at": datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat(),
    }
    backup["offsite"] = upload_backup_offsite(target)
    write_backup_manifest(backup)
    backup["pruned"] = prune_old_backups()
    return backup


def prune_old_backups(retention_count=None):
    retention_count = AUTO_BACKUP_RETENTION_COUNT if retention_count is None else retention_count
    if retention_count <= 0:
        return []

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = sorted(
        BACKUP_DIR.glob("nexaflow-backup-*.db"),
        key=lambda item: item.name,
        reverse=True,
    )
    pruned = []
    for path in backups[retention_count:]:
        try:
            path.unlink()
            manifest = backup_manifest_path(path)
            if manifest.exists():
                manifest.unlink()
            pruned.append(path.name)
        except OSError:
            continue
    return pruned


def list_sqlite_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = []
    for path in sorted(BACKUP_DIR.glob("nexaflow-backup-*.db"), key=lambda item: item.name, reverse=True):
        manifest_path = backup_manifest_path(path)
        manifest = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
        backups.append(
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "offsite": manifest.get("offsite", {"status": "unknown"}),
            }
        )

    return backups


def backup_scheduler_status():
    return {
        **backup_scheduler_state,
        "backup_path": str(BACKUP_DIR),
        "offsite_configured": offsite_backup_configured(),
        "offsite_config": offsite_backup_config_status(),
    }


def run_scheduled_backup_once():
    backup_scheduler_state["last_run_at"] = now_iso()
    backup_scheduler_state["last_error"] = None
    try:
        backup = create_sqlite_backup()
    except Exception as exc:
        backup_scheduler_state["last_error"] = str(exc)
        return {"status": "failed", "reason": str(exc)}

    backup_scheduler_state["last_success_at"] = now_iso()
    backup_scheduler_state["last_backup"] = {
        "name": backup["name"],
        "size_bytes": backup["size_bytes"],
        "created_at": backup["created_at"],
        "offsite": backup.get("offsite", {}),
    }
    return {"status": "created", "backup": backup}


def backup_scheduler_loop():
    if backup_scheduler_stop.wait(AUTO_BACKUP_INITIAL_DELAY_SECONDS):
        return

    while not backup_scheduler_stop.is_set():
        run_scheduled_backup_once()
        if backup_scheduler_stop.wait(AUTO_BACKUP_INTERVAL_SECONDS):
            break


def start_backup_scheduler():
    global backup_scheduler_thread
    if not AUTO_BACKUP_ENABLED:
        backup_scheduler_state["enabled"] = False
        return

    if backup_scheduler_state["started"]:
        return

    backup_scheduler_stop.clear()
    backup_scheduler_thread = threading.Thread(
        target=backup_scheduler_loop,
        name="nexaflow-backup-scheduler",
        daemon=True,
    )
    backup_scheduler_thread.start()
    backup_scheduler_state["started"] = True


def stop_backup_scheduler():
    backup_scheduler_stop.set()


def offsite_backup_configured():
    return offsite_backup_config_status()["configured"]


def offsite_backup_config_status():
    required = [
        "S3_BACKUP_ENDPOINT_URL",
        "S3_BACKUP_BUCKET",
        "S3_BACKUP_ACCESS_KEY_ID",
        "S3_BACKUP_SECRET_ACCESS_KEY",
    ]
    optional = [
        "S3_BACKUP_REGION",
        "S3_BACKUP_PREFIX",
    ]
    present = [key for key in required if os.getenv(key)]
    missing = [key for key in required if not os.getenv(key)]
    return {
        "configured": not missing,
        "partial": bool(present) and bool(missing),
        "present": present,
        "missing": missing,
        "optional_present": [key for key in optional if os.getenv(key)],
        "endpoint_url": os.getenv("S3_BACKUP_ENDPOINT_URL"),
        "bucket": os.getenv("S3_BACKUP_BUCKET"),
        "region": os.getenv("S3_BACKUP_REGION", "auto"),
        "prefix": os.getenv("S3_BACKUP_PREFIX", "nexaflow"),
    }


def s3_signing_key(secret_key, date_stamp, region, service):
    key_date = hmac.new(("AWS4" + secret_key).encode("utf-8"), date_stamp.encode("utf-8"), sha256).digest()
    key_region = hmac.new(key_date, region.encode("utf-8"), sha256).digest()
    key_service = hmac.new(key_region, service.encode("utf-8"), sha256).digest()
    return hmac.new(key_service, b"aws4_request", sha256).digest()


def upload_file_offsite(path, key_name=None, content_type="application/octet-stream"):
    if not offsite_backup_configured():
        return {
            "status": "not_configured",
            "reason": "Set S3_BACKUP_ENDPOINT_URL, S3_BACKUP_BUCKET, S3_BACKUP_ACCESS_KEY_ID, and S3_BACKUP_SECRET_ACCESS_KEY.",
            "config": offsite_backup_config_status(),
        }

    endpoint = os.getenv("S3_BACKUP_ENDPOINT_URL", "").rstrip("/")
    bucket = os.getenv("S3_BACKUP_BUCKET", "")
    access_key = os.getenv("S3_BACKUP_ACCESS_KEY_ID", "")
    secret_key = os.getenv("S3_BACKUP_SECRET_ACCESS_KEY", "")
    region = os.getenv("S3_BACKUP_REGION", "auto")
    prefix = os.getenv("S3_BACKUP_PREFIX", "nexaflow")
    object_name = key_name or path.name
    key = f"{prefix.strip('/')}/{object_name}" if prefix else object_name

    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return {"status": "failed", "reason": "S3_BACKUP_ENDPOINT_URL must be a full HTTPS URL."}

    object_path = f"/{bucket}/{quote(key)}"
    url = f"{endpoint}{object_path}"
    payload = path.read_bytes()
    payload_hash = sha256(payload).hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    canonical_headers = (
        f"host:{parsed.netloc}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [
            "PUT",
            object_path,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        s3_signing_key(secret_key, date_stamp, region, "s3"),
        string_to_sign.encode("utf-8"),
        sha256,
    ).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    request = urllib.request.Request(
        url,
        data=payload,
        method="PUT",
        headers={
            "Authorization": authorization,
            "Content-Type": content_type,
            "Host": parsed.netloc,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": amz_date,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        return {
            "status": "failed",
            "reason": f"Offsite storage returned HTTP {exc.code}.",
            "provider_error": exc.read().decode("utf-8", errors="replace")[:500],
        }
    except urllib.error.URLError as exc:
        return {
            "status": "failed",
            "reason": f"Offsite storage request failed: {exc.reason}",
        }

    return {
        "status": "uploaded",
        "provider": "s3-compatible",
        "bucket": bucket,
        "key": key,
    }


def upload_backup_offsite(path):
    return upload_file_offsite(path, content_type="application/vnd.sqlite3")


def test_offsite_backup_upload():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    probe_name = f"nexaflow-offsite-check-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.txt"
    probe_path = BACKUP_DIR / probe_name
    probe_path.write_text(
        f"NexaFlow offsite backup check\ncreated_at={now_iso()}\n",
        encoding="utf-8",
    )

    try:
        return upload_file_offsite(
            probe_path,
            key_name=f"system/{probe_name}",
            content_type="text/plain; charset=utf-8",
        )
    finally:
        try:
            probe_path.unlink()
        except OSError:
            pass


def init_db():
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                api_key_hash TEXT NOT NULL,
                api_key_prefix TEXT NOT NULL,
                credits INTEGER NOT NULL,
                plan TEXT NOT NULL,
                status TEXT NOT NULL,
                billing_email TEXT,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                subscription_status TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(connection, "clients", "stripe_customer_id", "TEXT")
        ensure_column(connection, "clients", "stripe_subscription_id", "TEXT")
        ensure_column(connection, "clients", "subscription_status", "TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                model_key TEXT,
                task TEXT,
                routing_strategy TEXT,
                route_score_json TEXT,
                fallback_attempts_json TEXT,
                credits_spent INTEGER NOT NULL DEFAULT 1,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                provider_cost_usd REAL NOT NULL DEFAULT 0,
                revenue_usd REAL NOT NULL DEFAULT 0,
                gross_margin_usd REAL NOT NULL DEFAULT 0,
                overage_credits INTEGER NOT NULL DEFAULT 0,
                message_preview TEXT,
                timestamp TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                client_id TEXT,
                plan TEXT,
                billing_email TEXT,
                amount_usd REAL,
                currency TEXT,
                mode TEXT,
                credits INTEGER,
                processed INTEGER NOT NULL DEFAULT 0,
                delivery_status TEXT,
                delivery_provider TEXT,
                response_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(connection, "payment_events", "delivery_status", "TEXT")
        ensure_column(connection, "payment_events", "delivery_provider", "TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS client_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                notification_type TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                delivery_status TEXT,
                delivery_provider TEXT,
                response_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS enquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_slug TEXT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT,
                business_type TEXT,
                message TEXT NOT NULL,
                source TEXT,
                campaign TEXT,
                referrer TEXT,
                page_url TEXT,
                intent TEXT,
                priority TEXT,
                estimated_value TEXT,
                auto_summary TEXT,
                next_action TEXT,
                follow_up_recommendation TEXT,
                follow_up_signals_json TEXT,
                stuck_point TEXT,
                next_question TEXT,
                follow_up_timing TEXT,
                reply_draft TEXT,
                analysis_source TEXT,
                whatsapp_url TEXT,
                merchant_notification_status TEXT,
                merchant_notification_error TEXT,
                internal_note TEXT,
                follow_up_at TEXT,
                deal_value REAL,
                pdpa_consent INTEGER NOT NULL DEFAULT 0,
                consent_at TEXT,
                consent_notice TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(connection, "enquiries", "business_slug", "TEXT")
        ensure_column(connection, "enquiries", "campaign", "TEXT")
        ensure_column(connection, "enquiries", "referrer", "TEXT")
        ensure_column(connection, "enquiries", "page_url", "TEXT")
        ensure_column(connection, "enquiries", "auto_summary", "TEXT")
        ensure_column(connection, "enquiries", "next_action", "TEXT")
        ensure_column(connection, "enquiries", "follow_up_recommendation", "TEXT")
        ensure_column(connection, "enquiries", "follow_up_signals_json", "TEXT")
        ensure_column(connection, "enquiries", "stuck_point", "TEXT")
        ensure_column(connection, "enquiries", "next_question", "TEXT")
        ensure_column(connection, "enquiries", "follow_up_timing", "TEXT")
        ensure_column(connection, "enquiries", "analysis_source", "TEXT")
        ensure_column(connection, "enquiries", "merchant_notification_status", "TEXT")
        ensure_column(connection, "enquiries", "merchant_notification_error", "TEXT")
        ensure_column(connection, "enquiries", "internal_note", "TEXT")
        ensure_column(connection, "enquiries", "follow_up_at", "TEXT")
        ensure_column(connection, "enquiries", "deal_value", "REAL")
        ensure_column(connection, "enquiries", "pdpa_consent", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(connection, "enquiries", "consent_at", "TEXT")
        ensure_column(connection, "enquiries", "consent_notice", "TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS business_profiles (
                slug TEXT PRIMARY KEY,
                client_id TEXT,
                business_name TEXT NOT NULL,
                business_type TEXT NOT NULL,
                whatsapp_phone TEXT NOT NULL,
                contact_email TEXT,
                offer_summary TEXT,
                reply_tone TEXT,
                opening_hours TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                access_key_hash TEXT,
                access_key_prefix TEXT,
                auto_followup_enabled INTEGER NOT NULL DEFAULT 1,
                hot_followup_hours INTEGER NOT NULL DEFAULT 0,
                standard_followup_days INTEGER NOT NULL DEFAULT 1,
                data_retention_days INTEGER NOT NULL DEFAULT 365,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(connection, "business_profiles", "client_id", "TEXT")
        ensure_column(connection, "business_profiles", "access_key_hash", "TEXT")
        ensure_column(connection, "business_profiles", "access_key_prefix", "TEXT")
        ensure_column(connection, "business_profiles", "auto_followup_enabled", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(connection, "business_profiles", "hot_followup_hours", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(connection, "business_profiles", "standard_followup_days", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(connection, "business_profiles", "data_retention_days", "INTEGER NOT NULL DEFAULT 365")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trial_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_name TEXT NOT NULL,
                contact_name TEXT NOT NULL,
                contact_email TEXT,
                whatsapp_phone TEXT NOT NULL,
                business_type TEXT,
                city TEXT,
                monthly_enquiries TEXT,
                lead_source TEXT,
                campaign TEXT,
                referrer TEXT,
                message TEXT,
                pdpa_consent INTEGER NOT NULL DEFAULT 0,
                consent_at TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                internal_note TEXT,
                notification_status TEXT,
                notification_error TEXT,
                trial_started_at TEXT,
                trial_ends_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(connection, "trial_requests", "monthly_enquiries", "TEXT")
        ensure_column(connection, "trial_requests", "lead_source", "TEXT")
        ensure_column(connection, "trial_requests", "campaign", "TEXT")
        ensure_column(connection, "trial_requests", "referrer", "TEXT")
        ensure_column(connection, "trial_requests", "pdpa_consent", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(connection, "trial_requests", "consent_at", "TEXT")
        ensure_column(connection, "trial_requests", "internal_note", "TEXT")
        ensure_column(connection, "trial_requests", "notification_status", "TEXT")
        ensure_column(connection, "trial_requests", "notification_error", "TEXT")
        ensure_column(connection, "trial_requests", "trial_started_at", "TEXT")
        ensure_column(connection, "trial_requests", "trial_ends_at", "TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_slug TEXT NOT NULL,
                channel TEXT NOT NULL,
                integration_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                account_label TEXT,
                external_account_id TEXT,
                capabilities_json TEXT,
                token_status TEXT NOT NULL DEFAULT 'not_stored',
                data_processing_acknowledged INTEGER NOT NULL DEFAULT 0,
                security_reviewed_at TEXT,
                last_sync_at TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (business_slug, channel)
            )
            """
        )
        ensure_column(connection, "channel_connections", "capabilities_json", "TEXT")
        ensure_column(connection, "channel_connections", "token_status", "TEXT NOT NULL DEFAULT 'not_stored'")
        ensure_column(connection, "channel_connections", "data_processing_acknowledged", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(connection, "channel_connections", "security_reviewed_at", "TEXT")
        ensure_column(connection, "channel_connections", "last_sync_at", "TEXT")
        ensure_column(connection, "channel_connections", "notes", "TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_slug TEXT NOT NULL,
                channel TEXT NOT NULL,
                connection_id INTEGER,
                external_message_id TEXT,
                enquiry_id INTEGER,
                customer_display_name TEXT,
                customer_handle TEXT,
                direction TEXT NOT NULL,
                message_preview TEXT,
                received_at TEXT,
                retention_until TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(connection, "channel_messages", "enquiry_id", "INTEGER")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS data_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                business_slug TEXT,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_enquiries_business_created ON enquiries (business_slug, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_enquiries_business_status ON enquiries (business_slug, status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_enquiries_business_followup ON enquiries (business_slug, follow_up_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_enquiries_business_priority ON enquiries (business_slug, priority)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_business_profiles_client ON business_profiles (client_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_trial_requests_status_created ON trial_requests (status, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_connections_business ON channel_connections (business_slug, channel)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_connections_external ON channel_connections (channel, external_account_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_messages_business_created ON channel_messages (business_slug, created_at)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_messages_external_unique ON channel_messages (channel, external_message_id) WHERE external_message_id IS NOT NULL AND external_message_id != ''")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_data_audit_business_created ON data_audit_events (business_slug, created_at)")


def migration_applied(connection, name):
    row = connection.execute(
        "SELECT name FROM migrations WHERE name = ?",
        (name,),
    ).fetchone()
    return row is not None


def ensure_column(connection, table_name, column_name, column_definition):
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(column["name"] == column_name for column in columns):
        return

    try:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def mark_migration_applied(connection, name):
    connection.execute(
        "INSERT OR REPLACE INTO migrations (name, applied_at) VALUES (?, ?)",
        (name, now_iso()),
    )


def migrate_json_to_sqlite():
    init_db()

    with db_connection() as connection:
        if migration_applied(connection, "json_to_sqlite_v1"):
            return

        clients = load_json(CLIENTS_FILE, {})
        for client_id, client in clients.items():
            normalized = normalize_client(client_id, client)
            normalized.pop("api_key", None)
            connection.execute(
                """
                INSERT OR REPLACE INTO clients (
                    client_id, api_key_hash, api_key_prefix, credits, plan,
                    status, billing_email, stripe_customer_id, stripe_subscription_id,
                    subscription_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    normalized["api_key_hash"],
                    normalized["api_key_prefix"],
                    normalized["credits"],
                    normalized["plan"],
                    normalized["status"],
                    normalized.get("billing_email"),
                    normalized.get("stripe_customer_id"),
                    normalized.get("stripe_subscription_id"),
                    normalized.get("subscription_status"),
                    normalized["created_at"],
                ),
            )

        logs = load_json(USAGE_LOGS_FILE, [])
        for log in logs:
            insert_usage_log(connection, log)

        mark_migration_applied(connection, "json_to_sqlite_v1")


def row_to_client(row):
    return {
        "client_id": row["client_id"],
        "api_key_hash": row["api_key_hash"],
        "api_key_prefix": row["api_key_prefix"],
        "credits": row["credits"],
        "plan": row["plan"],
        "status": row["status"],
        "billing_email": row["billing_email"],
        "stripe_customer_id": row["stripe_customer_id"],
        "stripe_subscription_id": row["stripe_subscription_id"],
        "subscription_status": row["subscription_status"],
        "created_at": row["created_at"],
    }


def row_to_usage_log(row):
    route_score = json.loads(row["route_score_json"]) if row["route_score_json"] else None
    fallback_attempts = json.loads(row["fallback_attempts_json"]) if row["fallback_attempts_json"] else None
    log = {
        "client_id": row["client_id"],
        "provider": row["provider"],
        "model": row["model"],
        "model_key": row["model_key"],
        "task": row["task"],
        "routing_strategy": row["routing_strategy"],
        "credits_spent": row["credits_spent"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "provider_cost_usd": row["provider_cost_usd"],
        "revenue_usd": row["revenue_usd"],
        "gross_margin_usd": row["gross_margin_usd"],
        "overage_credits": row["overage_credits"],
        "message_preview": row["message_preview"],
        "timestamp": row["timestamp"],
    }

    if route_score is not None:
        log["route_score"] = route_score

    if fallback_attempts is not None:
        log["fallback_attempts"] = fallback_attempts

    return log


def load_clients():
    with db_connection() as connection:
        rows = connection.execute("SELECT * FROM clients ORDER BY client_id").fetchall()
        return {row["client_id"]: row_to_client(row) for row in rows}


def save_clients(clients):
    with db_connection() as connection:
        connection.execute("DELETE FROM clients")
        for client_id, client in clients.items():
            normalized = normalize_client(client_id, client)
            connection.execute(
                """
                INSERT INTO clients (
                    client_id, api_key_hash, api_key_prefix, credits, plan,
                    status, billing_email, stripe_customer_id, stripe_subscription_id,
                    subscription_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    normalized["api_key_hash"],
                    normalized["api_key_prefix"],
                    normalized["credits"],
                    normalized["plan"],
                    normalized["status"],
                    normalized.get("billing_email"),
                    normalized.get("stripe_customer_id"),
                    normalized.get("stripe_subscription_id"),
                    normalized.get("subscription_status"),
                    normalized["created_at"],
                ),
            )


def load_logs():
    with db_connection() as connection:
        rows = connection.execute("SELECT * FROM usage_logs ORDER BY id").fetchall()
        return [row_to_usage_log(row) for row in rows]


def save_logs(logs):
    with db_connection() as connection:
        connection.execute("DELETE FROM usage_logs")
        for log in logs:
            insert_usage_log(connection, log)


def insert_usage_log(connection, log):
    connection.execute(
        """
        INSERT INTO usage_logs (
            client_id, provider, model, model_key, task, routing_strategy,
            route_score_json, fallback_attempts_json, credits_spent,
            prompt_tokens, completion_tokens, total_tokens, provider_cost_usd,
            revenue_usd, gross_margin_usd, overage_credits, message_preview,
            timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            log["client_id"],
            log.get("provider"),
            log.get("model"),
            log.get("model_key"),
            log.get("task"),
            log.get("routing_strategy"),
            json.dumps(log.get("route_score")) if log.get("route_score") is not None else None,
            json.dumps(log.get("fallback_attempts")) if log.get("fallback_attempts") is not None else None,
            log.get("credits_spent", 1),
            log.get("prompt_tokens", 0),
            log.get("completion_tokens", 0),
            log.get("total_tokens", 0),
            log.get("provider_cost_usd", 0),
            log.get("revenue_usd", 0),
            log.get("gross_margin_usd", 0),
            log.get("overage_credits", 0),
            log.get("message_preview") or log.get("message", "")[:120],
            log.get("timestamp", now_iso()),
        ),
    )


def query_auth_enabled():
    return os.getenv("NEXAFLOW_ALLOW_QUERY_AUTH", "").lower() in {"1", "true", "yes"}


def admin_guard(admin_key: str | None = Query(default=None), x_admin_key: str | None = Header(default=None)):
    expected = os.getenv("ADMIN_KEY")
    supplied = x_admin_key or (admin_key if query_auth_enabled() else None)

    if not expected:
        raise HTTPException(status_code=503, detail="Server missing ADMIN_KEY")

    if not hmac.compare_digest(supplied or "", expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def api_key_digest(api_key):
    pepper = os.getenv("API_KEY_PEPPER") or os.getenv("ADMIN_KEY") or "development-pepper"
    return sha256(f"{pepper}:{api_key}".encode("utf-8")).hexdigest()


def generate_api_key():
    return f"nf_{secrets.token_urlsafe(32)}"


def generate_business_access_key():
    return f"biz_{secrets.token_urlsafe(32)}"


def generate_client_id(email):
    safe_email = (email or "customer").split("@")[0].lower()
    safe_email = "".join(char if char.isalnum() else "_" for char in safe_email).strip("_")
    safe_email = safe_email[:40] or "customer"
    return f"{safe_email}_{secrets.token_hex(4)}"


def generate_business_slug(seed):
    base = (seed or "business").split("@")[0].lower()
    base = "".join(char if char.isalnum() else "-" for char in base)
    base = "-".join(part for part in base.split("-") if part)[:44] or "business"
    return f"{base}-{secrets.token_hex(3)}"


def business_name_from_email(email):
    local = (email or "Your Business").split("@")[0]
    words = [part for part in "".join(char if char.isalnum() else " " for char in local).split() if part]
    return " ".join(word.capitalize() for word in words) or "Your Business"


def normalize_email(email):
    return email.strip().lower() if email else None


def find_client_id_by_email(clients, email):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None

    for client_id, client in clients.items():
        if normalize_email(client.get("billing_email")) == normalized_email:
            return client_id

    return None


def find_client_id_by_stripe_reference(clients, stripe_customer_id=None, stripe_subscription_id=None):
    for client_id, client in clients.items():
        if stripe_customer_id and client.get("stripe_customer_id") == stripe_customer_id:
            return client_id

        if stripe_subscription_id and client.get("stripe_subscription_id") == stripe_subscription_id:
            return client_id

    return None


def client_created_within(client, seconds):
    created_at = client.get("created_at")
    if not created_at:
        return False

    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False

    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    return (datetime.now(timezone.utc) - created).total_seconds() < seconds


def normalize_client(client_id, client):
    plan = client.get("plan", "starter")
    if plan not in PLANS:
        plan = "starter"

    client.setdefault("credits", 0)
    client["plan"] = plan
    client.setdefault("status", "active")
    client.setdefault("created_at", now_iso())

    if "api_key" in client and "api_key_hash" not in client:
        client["api_key_hash"] = api_key_digest(client["api_key"])

    client.setdefault("api_key_prefix", client.get("api_key", "hidden")[:8])
    client.setdefault("billing_email", None)
    client.setdefault("stripe_customer_id", None)
    client.setdefault("stripe_subscription_id", None)
    client.setdefault("subscription_status", None)
    client.setdefault("client_id", client_id)
    return client


migrate_json_to_sqlite()


def find_client_by_api_key(api_key):
    clients = load_clients()

    for client_id, client in clients.items():
        legacy_api_key = client.get("api_key")
        client = normalize_client(client_id, client)
        legacy_match = legacy_api_key == api_key
        hashed_match = client.get("api_key_hash") == api_key_digest(api_key)

        if legacy_match or hashed_match:
            client.pop("api_key", None)
            clients[client_id] = client
            save_clients(clients)
            return client_id, client

    raise HTTPException(status_code=401, detail="Invalid API key")


def create_client_record(
    client_id,
    plan,
    credits,
    billing_email,
    stripe_customer_id=None,
    stripe_subscription_id=None,
    subscription_status=None,
):
    ensure_plan(plan)
    clients = load_clients()

    if client_id in clients:
        raise HTTPException(status_code=409, detail="Client already exists")

    new_api_key = generate_api_key()
    clients[client_id] = {
        "client_id": client_id,
        "api_key_hash": api_key_digest(new_api_key),
        "api_key_prefix": new_api_key[:8],
        "credits": credits,
        "plan": plan,
        "status": "active",
        "billing_email": billing_email,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "subscription_status": subscription_status,
        "created_at": now_iso(),
    }
    save_clients(clients)

    return clients[client_id], new_api_key


def email_delivery_configured():
    return bool(os.getenv("RESEND_API_KEY") and os.getenv("FROM_EMAIL"))


def delivery_email_body(client_id, api_key, plan, credits):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com")
    app_name = os.getenv("NEXAFLOW_APP_NAME", "NexaFlow AI Gateway")
    return f"""Welcome to {app_name}.

Your API access is active.

Client ID: {client_id}
Plan: {plan}
Credits: {credits}
API key: {api_key}

Customer portal:
{site_url}/portal

API docs:
{site_url}/docs

Send a test request:
curl -X POST "{site_url}/v1/chat" \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: {api_key}" \\
  -d "{{\\"message\\":\\"Write one sentence about NexaFlow.\\",\\"routing_strategy\\":\\"profit\\"}}"

Buy or upgrade:
{site_url}/pricing

Keep this API key private. It will not be shown again.
"""


def send_resend_email(to_email, subject, text_body):
    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("FROM_EMAIL")
    if not api_key or not from_email:
        return {
            "status": "pending_email_setup",
            "reason": "Set RESEND_API_KEY and FROM_EMAIL to deliver API keys automatically.",
        }

    payload = json.dumps(
        {
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "text": text_body,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "NexaFlowGateway/1.0 (+https://api.nexaflowinfra.com)",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return {
            "status": "failed",
            "reason": f"Resend returned HTTP {exc.code}.",
            "provider_error": error_body[:500],
        }
    except urllib.error.URLError as exc:
        return {
            "status": "failed",
            "reason": f"Email provider request failed: {exc.reason}",
        }

    return {
        "status": "sent",
        "provider": "resend",
        "response": json.loads(response_body) if response_body else {},
    }


def low_credit_threshold(plan_key):
    plan = PLANS[plan_key]
    return max(100, ceil(plan["included_credits"] * 0.10))


def low_credit_dedupe_key(client_id, plan_key):
    return f"{client_id}:low_credit:{plan_key}"


def notification_exists(dedupe_key):
    with db_connection() as connection:
        row = connection.execute(
            "SELECT id FROM client_notifications WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
    return row is not None


def record_client_notification(client_id, notification_type, dedupe_key, delivery):
    stored_delivery = {key: value for key, value in delivery.items() if key != "response"}
    if "response" in delivery:
        stored_delivery["response"] = delivery["response"]

    with db_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO client_notifications (
                client_id, notification_type, dedupe_key, delivery_status,
                delivery_provider, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                notification_type,
                dedupe_key,
                delivery.get("status"),
                delivery.get("provider"),
                json.dumps(stored_delivery),
                now_iso(),
            ),
        )


def reset_client_notification(client_id, notification_type):
    with db_connection() as connection:
        connection.execute(
            "DELETE FROM client_notifications WHERE client_id = ? AND notification_type = ?",
            (client_id, notification_type),
        )


def row_to_client_notification(row):
    response = json.loads(row["response_json"]) if row["response_json"] else None
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "notification_type": row["notification_type"],
        "delivery_status": row["delivery_status"],
        "delivery_provider": row["delivery_provider"],
        "response": response,
        "created_at": row["created_at"],
    }


def low_credit_email_body(client_id, client, threshold):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com")
    app_name = os.getenv("NEXAFLOW_APP_NAME", "NexaFlow AI Gateway")
    return f"""Your {app_name} credits are running low.

Client ID: {client_id}
Plan: {client["plan"]}
Remaining credits: {client["credits"]}
Low-credit threshold: {threshold}

Open your portal:
{site_url}/portal

Add credits or upgrade before your automations stop:
{site_url}/pricing
"""


def maybe_send_low_credit_notice(client_id, client):
    client = normalize_client(client_id, client)
    threshold = low_credit_threshold(client["plan"])

    if client["status"] != "active" or client["credits"] > threshold:
        reset_client_notification(client_id, "low_credit")
        return {"status": "not_needed", "threshold": threshold}

    dedupe_key = low_credit_dedupe_key(client_id, client["plan"])
    if notification_exists(dedupe_key):
        return {"status": "already_sent", "threshold": threshold}

    if not client.get("billing_email"):
        delivery = {
            "status": "pending_email",
            "reason": "No billing email is available for low-credit notification.",
            "threshold": threshold,
        }
    else:
        subject = f"{os.getenv('NEXAFLOW_APP_NAME', 'NexaFlow AI Gateway')} credits are running low"
        body = low_credit_email_body(client_id, client, threshold)
        delivery = send_resend_email(client["billing_email"], subject, body)
        delivery["threshold"] = threshold

    record_client_notification(client_id, "low_credit", dedupe_key, delivery)
    return delivery


def lifecycle_notice_type(reason):
    if reason in {"payment_issue", "subscription_past_due", "subscription_unpaid", "subscription_paused", "subscription_incomplete"}:
        return "payment_action_required"

    if reason in {"subscription_deleted", "subscription_canceled", "subscription_incomplete_expired"}:
        return "subscription_cancelled"

    return None


def lifecycle_notice_body(client_id, client, payment, reason):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com")
    app_name = os.getenv("NEXAFLOW_APP_NAME", "NexaFlow AI Gateway")
    return f"""Your {app_name} account needs attention.

Client ID: {client_id}
Plan: {client["plan"]}
Account status: {client["status"]}
Reason: {reason}
Stripe event: {payment.event_type}

Open your portal:
{site_url}/portal

Review plans or restore access:
{site_url}/pricing

If this looks wrong, contact NexaFlow support and include your Client ID.
"""


def maybe_send_lifecycle_notice(client_id, client, payment, reason):
    notification_type = lifecycle_notice_type(reason)
    if not notification_type:
        return {"status": "not_required", "reason": reason}

    dedupe_key = f"{client_id}:{notification_type}:{payment.event_id}"
    if notification_exists(dedupe_key):
        return {"status": "already_sent", "reason": reason}

    if not client.get("billing_email"):
        delivery = {
            "status": "pending_email",
            "reason": "No billing email is available for lifecycle notification.",
        }
    else:
        subject = f"{os.getenv('NEXAFLOW_APP_NAME', 'NexaFlow AI Gateway')} account action required"
        if notification_type == "subscription_cancelled":
            subject = f"{os.getenv('NEXAFLOW_APP_NAME', 'NexaFlow AI Gateway')} subscription cancelled"
        body = lifecycle_notice_body(client_id, client, payment, reason)
        delivery = send_resend_email(client["billing_email"], subject, body)

    delivery["reason"] = reason
    record_client_notification(client_id, notification_type, dedupe_key, delivery)
    return delivery


def deliver_api_key(client_id, client, api_key):
    if not api_key:
        return {"status": "not_required"}

    billing_email = client.get("billing_email")
    if not billing_email:
        return {
            "status": "pending_email",
            "reason": "No billing email was provided by the payment event.",
        }

    subject = f"{os.getenv('NEXAFLOW_APP_NAME', 'NexaFlow AI Gateway')} API access"
    body = delivery_email_body(client_id, api_key, client["plan"], client["credits"])
    return send_resend_email(billing_email, subject, body)


def upsert_paid_client(payment):
    ensure_plan(payment.plan)
    clients = load_clients()
    client_id = payment.client_id or find_client_id_by_stripe_reference(
        clients,
        payment.stripe_customer_id,
        payment.stripe_subscription_id,
    )
    client_id = client_id or find_client_id_by_email(clients, payment.billing_email)
    client_id = client_id or generate_client_id(payment.billing_email)
    credits = payment.credits if payment.credits is not None else PLANS[payment.plan]["included_credits"]

    if client_id not in clients:
        client, api_key = create_client_record(
            client_id,
            payment.plan,
            credits,
            payment.billing_email,
            payment.stripe_customer_id,
            payment.stripe_subscription_id,
            payment.stripe_subscription_status or "active",
        )
        delivery = deliver_api_key(client_id, client, api_key)
        enquiry_setup = create_default_business_profile_for_client(client_id, payment.billing_email)
        return {
            **public_client(client_id, client),
            "api_key": api_key,
            "created": True,
            "delivery": delivery,
            "enquiry_setup": enquiry_setup,
            "warning": "Store this API key now. It will not be shown again.",
        }

    client = normalize_client(client_id, clients[client_id])
    client["status"] = "active"

    is_initial_subscription_event = (
        payment.mode == "subscription"
        and payment.event_type in {"checkout.session.completed", "invoice.paid"}
        and client_created_within(client, 24 * 60 * 60)
    )
    is_initial_subscription_invoice = (
        payment.event_type == "invoice.paid"
        and payment.stripe_billing_reason == "subscription_create"
    )
    should_apply_credits = payment.mode == "topup" or not (
        is_initial_subscription_event or is_initial_subscription_invoice
    )

    if payment.mode == "topup":
        client["credits"] += credits
    else:
        client["plan"] = payment.plan
        if should_apply_credits:
            client["credits"] += credits

    if payment.billing_email:
        client["billing_email"] = payment.billing_email

    if payment.stripe_customer_id:
        client["stripe_customer_id"] = payment.stripe_customer_id

    if payment.stripe_subscription_id:
        client["stripe_subscription_id"] = payment.stripe_subscription_id

    if payment.stripe_subscription_status:
        client["subscription_status"] = payment.stripe_subscription_status

    clients[client_id] = client
    save_clients(clients)
    if client["credits"] > low_credit_threshold(client["plan"]):
        reset_client_notification(client_id, "low_credit")
    enquiry_setup = create_default_business_profile_for_client(client_id, client.get("billing_email"))
    return {
        **public_client(client_id, client),
        "created": False,
        "credits_added": credits,
        "credits_applied": should_apply_credits,
        "delivery": {"status": "not_required"},
        "enquiry_setup": enquiry_setup,
    }


def lifecycle_status_for_event(payment):
    if payment.event_type in {"invoice.payment_failed", "charge.refunded", "charge.dispute.created"}:
        return "paused", "payment_issue"

    if payment.event_type == "customer.subscription.deleted":
        return "cancelled", "subscription_deleted"

    if payment.event_type == "customer.subscription.updated":
        status = payment.stripe_subscription_status
        if status in {"active", "trialing"}:
            return "active", "subscription_active"

        if status in {"past_due", "unpaid", "paused", "incomplete"}:
            return "paused", f"subscription_{status}"

        if status in {"canceled", "incomplete_expired"}:
            return "cancelled", f"subscription_{status}"

    return None, "ignored_lifecycle_status"


def process_stripe_lifecycle_event(payment):
    clients = load_clients()
    client_id = payment.client_id or find_client_id_by_stripe_reference(
        clients,
        payment.stripe_customer_id,
        payment.stripe_subscription_id,
    )
    client_id = client_id or find_client_id_by_email(clients, payment.billing_email)

    if not client_id or client_id not in clients:
        return {
            "processed": False,
            "reason": "client_not_found",
            "event_type": payment.event_type,
            "stripe_customer_id": payment.stripe_customer_id,
            "stripe_subscription_id": payment.stripe_subscription_id,
        }

    next_status, reason = lifecycle_status_for_event(payment)
    if not next_status:
        return {
            "processed": False,
            "reason": reason,
            **public_client(client_id, normalize_client(client_id, clients[client_id])),
        }

    client = normalize_client(client_id, clients[client_id])
    client["status"] = next_status

    if payment.billing_email:
        client["billing_email"] = payment.billing_email

    if payment.stripe_customer_id:
        client["stripe_customer_id"] = payment.stripe_customer_id

    if payment.stripe_subscription_id:
        client["stripe_subscription_id"] = payment.stripe_subscription_id

    if payment.stripe_subscription_status:
        client["subscription_status"] = payment.stripe_subscription_status

    clients[client_id] = client
    save_clients(clients)
    delivery = maybe_send_lifecycle_notice(client_id, client, payment, reason)

    return {
        **public_client(client_id, client),
        "created": False,
        "status_changed": True,
        "reason": reason,
        "delivery": delivery,
    }


def parse_stripe_signature(signature_header):
    parts = {}
    for item in signature_header.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts.setdefault(key, []).append(value)
    return parts


def verify_stripe_signature(raw_body, signature_header, endpoint_secret):
    parts = parse_stripe_signature(signature_header or "")
    timestamps = parts.get("t", [])
    signatures = parts.get("v1", [])

    if not timestamps or not signatures:
        return False

    try:
        timestamp = int(timestamps[0])
    except (TypeError, ValueError):
        return False

    tolerance_seconds = int(os.getenv("STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300"))
    if abs(time() - timestamp) > tolerance_seconds:
        return False

    signed_payload = timestamps[0].encode("utf-8") + b"." + raw_body
    expected = hmac.new(
        endpoint_secret.encode("utf-8"),
        signed_payload,
        "sha256",
    ).hexdigest()

    return any(hmac.compare_digest(expected, signature) for signature in signatures)


def legacy_payment_webhook_enabled():
    return os.getenv("ENABLE_LEGACY_PAYMENT_WEBHOOK", "").lower() in {"1", "true", "yes"}


def verify_meta_signature(raw_body, signature_header, app_secret):
    if not app_secret or not signature_header:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    signature = signature_header[len(prefix):]
    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def stripe_plan_from_payment_link(payment_link):
    if not payment_link:
        return None

    for plan in PLANS:
        configured_values = [
            os.getenv(f"PAYMENT_LINK_{plan.upper()}"),
            os.getenv(f"PAYMENT_LINK_ID_{plan.upper()}"),
        ]
        if any(value and value == payment_link for value in configured_values):
            return plan

    return None


def stripe_plan_from_amount(amount_cents, currency):
    if amount_cents is None or (currency or "").lower() != "usd":
        return None

    matches = [
        plan
        for plan, config in PLANS.items()
        if int(config["monthly_price_usd"] * 100) == int(amount_cents)
    ]

    return matches[0] if len(matches) == 1 else None


def infer_stripe_plan(metadata, data_object, amount_cents):
    metadata_plan = metadata.get("plan")
    if metadata_plan in PLANS:
        return metadata_plan

    payment_link_plan = stripe_plan_from_payment_link(data_object.get("payment_link"))
    if payment_link_plan:
        return payment_link_plan

    amount_plan = stripe_plan_from_amount(amount_cents, data_object.get("currency"))
    if amount_plan:
        return amount_plan

    return "starter"


def stripe_object_to_payment_request(event):
    event_type = event.get("type", "")
    data_object = event.get("data", {}).get("object", {})
    metadata = data_object.get("metadata") or {}
    customer_details = data_object.get("customer_details") or {}
    billing_email = (
        metadata.get("billing_email")
        or customer_details.get("email")
        or data_object.get("customer_email")
    )
    amount_total = data_object.get("amount_total")
    amount_paid = data_object.get("amount_paid")
    amount_cents = amount_total if amount_total is not None else amount_paid
    amount_usd = amount_cents / 100 if amount_cents is not None else None
    credits_raw = metadata.get("credits")
    credits = int(credits_raw) if credits_raw else None
    plan = infer_stripe_plan(metadata, data_object, amount_cents)

    return PaymentWebhookRequest(
        event_id=event["id"],
        event_type=event_type,
        provider="stripe",
        client_id=metadata.get("client_id"),
        plan=plan,
        billing_email=billing_email,
        amount_usd=amount_usd,
        currency=(data_object.get("currency") or "usd").upper(),
        mode=metadata.get("mode", "subscription"),
        credits=credits,
        stripe_billing_reason=data_object.get("billing_reason"),
        stripe_customer_id=data_object.get("customer"),
        stripe_subscription_id=data_object.get("subscription") or data_object.get("id"),
        stripe_subscription_status=data_object.get("status"),
    )


def process_payment_event(req):
    success_events = {"payment.succeeded", "checkout.session.completed", "invoice.paid"}
    lifecycle_events = {
        "invoice.payment_failed",
        "customer.subscription.deleted",
        "customer.subscription.updated",
        "charge.refunded",
        "charge.dispute.created",
    }

    if req.event_type not in success_events | lifecycle_events:
        return {
            "processed": False,
            "reason": "ignored_event_type",
            "event_type": req.event_type,
        }

    with db_connection() as connection:
        existing = connection.execute(
            "SELECT response_json FROM payment_events WHERE event_id = ?",
            (req.event_id,),
        ).fetchone()

        if existing:
            return {
                "processed": False,
                "idempotent": True,
                "result": json.loads(existing["response_json"]) if existing["response_json"] else None,
            }

        result = upsert_paid_client(req) if req.event_type in success_events else process_stripe_lifecycle_event(req)
        stored_result = {key: value for key, value in result.items() if key != "api_key"}
        delivery = result.get("delivery") or {}
        connection.execute(
            """
            INSERT INTO payment_events (
                event_id, event_type, provider, client_id, plan, billing_email,
                amount_usd, currency, mode, credits, processed, delivery_status,
                delivery_provider, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req.event_id,
                req.event_type,
                req.provider,
                result["client_id"],
                req.plan,
                req.billing_email,
                req.amount_usd,
                req.currency,
                req.mode,
                req.credits,
                1,
                delivery.get("status"),
                delivery.get("provider"),
                json.dumps(stored_result),
                now_iso(),
            ),
        )

    return {
        "processed": True,
        "result": result,
    }


def public_client(client_id, client):
    plan = PLANS[client["plan"]]
    return {
        "client_id": client_id,
        "credits": client["credits"],
        "plan": client["plan"],
        "status": client["status"],
        "billing_email": client.get("billing_email"),
        "subscription_status": client.get("subscription_status"),
        "api_key_prefix": client.get("api_key_prefix"),
        "rate_limit_per_minute": plan["rate_limit_per_minute"],
        "daily_request_limit": plan["daily_request_limit"],
        "daily_credit_limit": plan["daily_credit_limit"],
        "max_request_credits": plan["max_request_credits"],
        "created_at": client.get("created_at"),
    }


def customer_guard(api_key=None, x_api_key=None, authorization=None):
    bearer_token = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization[7:].strip()

    supplied = x_api_key or bearer_token or (api_key if query_auth_enabled() else None)
    if not supplied:
        raise HTTPException(status_code=401, detail="Missing customer API key")

    return find_client_by_api_key(supplied)


def client_usage_report(client_id, limit=25):
    logs = [log for log in load_logs() if log["client_id"] == client_id]
    totals = {
        "requests": len(logs),
        "credits_spent": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    provider_counts = {}
    model_counts = {}

    for log in logs:
        totals["credits_spent"] += log.get("credits_spent", 0)
        totals["prompt_tokens"] += log.get("prompt_tokens", 0)
        totals["completion_tokens"] += log.get("completion_tokens", 0)
        totals["total_tokens"] += log.get("total_tokens", 0)
        provider = log.get("provider") or "unknown"
        model = log.get("model_key") or log.get("model") or "unknown"
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        model_counts[model] = model_counts.get(model, 0) + 1

    recent_logs = []
    for log in logs[-limit:]:
        recent_logs.append(
            {
                "timestamp": log.get("timestamp"),
                "task": log.get("task"),
                "provider": log.get("provider"),
                "model": log.get("model"),
                "model_key": log.get("model_key"),
                "credits_spent": log.get("credits_spent", 0),
                "prompt_tokens": log.get("prompt_tokens", 0),
                "completion_tokens": log.get("completion_tokens", 0),
                "total_tokens": log.get("total_tokens", 0),
                "message_preview": log.get("message_preview", ""),
            }
        )

    return {
        "client_id": client_id,
        "totals": totals,
        "provider_counts": provider_counts,
        "model_counts": model_counts,
        "recent_logs": list(reversed(recent_logs)),
    }


def empty_usage_totals():
    return {
        "requests": 0,
        "credits_spent": 0,
        "provider_cost_usd": 0,
        "revenue_usd": 0,
        "gross_margin_usd": 0,
    }


def usage_totals_for_logs(logs):
    totals = empty_usage_totals()
    by_client = {}
    by_provider = {}

    for log in logs:
        client_id = log.get("client_id") or "unknown"
        provider = log.get("provider") or "unknown"
        by_client.setdefault(client_id, empty_usage_totals())
        by_provider.setdefault(provider, empty_usage_totals())

        for bucket in (totals, by_client[client_id], by_provider[provider]):
            bucket["requests"] += 1
            bucket["credits_spent"] += log.get("credits_spent", 0)
            bucket["provider_cost_usd"] += log.get("provider_cost_usd", 0)
            bucket["revenue_usd"] += log.get("revenue_usd", 0)
            bucket["gross_margin_usd"] += log.get("gross_margin_usd", 0)

    totals["gross_margin_ratio"] = (
        round(totals["gross_margin_usd"] / totals["revenue_usd"], 6)
        if totals["revenue_usd"] > 0
        else None
    )

    return {
        "totals": totals,
        "by_client": by_client,
        "by_provider": by_provider,
    }


def list_payment_events_from_db(limit=500):
    with db_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM payment_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_payment_event(row) for row in rows]


def payment_event_is_success(event):
    return event.get("processed") and event.get("event_type") in {
        "payment.succeeded",
        "checkout.session.completed",
        "invoice.paid",
    }


def current_month_start():
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def admin_revenue_report_data():
    clients = load_clients()
    normalized_clients = {
        client_id: normalize_client(client_id, client)
        for client_id, client in clients.items()
    }
    logs = load_logs()
    month_start = current_month_start()
    monthly_logs = []

    for log in logs:
        timestamp = parse_log_timestamp(log.get("timestamp"))
        if timestamp is not None and timestamp >= month_start:
            monthly_logs.append(log)

    usage_all_time = usage_totals_for_logs(logs)
    usage_month = usage_totals_for_logs(monthly_logs)
    payment_events = list_payment_events_from_db()
    successful_payments = [event for event in payment_events if payment_event_is_success(event)]
    monthly_successful_payments = [
        event
        for event in successful_payments
        if (parse_log_timestamp(event.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= month_start
    ]

    status_counts = {}
    plan_counts = {}
    active_mrr = 0
    credits_outstanding = 0
    low_credit_clients = []
    paused_or_cancelled_clients = []
    high_usage_clients = []

    for client_id, client in normalized_clients.items():
        plan_key = client["plan"]
        plan = PLANS[plan_key]
        status_counts[client["status"]] = status_counts.get(client["status"], 0) + 1
        plan_counts[plan_key] = plan_counts.get(plan_key, 0) + 1
        credits_outstanding += client.get("credits", 0)

        if client["status"] == "active":
            active_mrr += plan["monthly_price_usd"]

        threshold = low_credit_threshold(plan_key)
        if client["status"] == "active" and client["credits"] <= threshold:
            low_credit_clients.append(
                {
                    "client_id": client_id,
                    "plan": plan_key,
                    "credits": client["credits"],
                    "threshold": threshold,
                    "billing_email": client.get("billing_email"),
                }
            )

        if client["status"] in {"paused", "cancelled"}:
            paused_or_cancelled_clients.append(public_client(client_id, client))

        daily = usage_window_stats(client_id)
        if (
            daily["requests"] >= plan["daily_request_limit"] * 0.8
            or daily["credits_spent"] >= plan["daily_credit_limit"] * 0.8
        ):
            high_usage_clients.append(
                {
                    "client_id": client_id,
                    "plan": plan_key,
                    "daily_usage": daily,
                    "daily_request_limit": plan["daily_request_limit"],
                    "daily_credit_limit": plan["daily_credit_limit"],
                }
            )

    negative_margin_clients = [
        {
            "client_id": client_id,
            **usage,
        }
        for client_id, usage in usage_all_time["by_client"].items()
        if usage["gross_margin_usd"] < 0
    ]

    return {
        "generated_at": now_iso(),
        "currency": "USD",
        "month_start": month_start.isoformat(),
        "mrr": {
            "active_monthly_recurring_revenue_usd": active_mrr,
            "active_customers": status_counts.get("active", 0),
            "status_counts": status_counts,
            "plan_counts": plan_counts,
        },
        "payments": {
            "successful_events": len(successful_payments),
            "all_time_volume_usd": round(sum(event.get("amount_usd") or 0 for event in successful_payments), 2),
            "month_to_date_volume_usd": round(sum(event.get("amount_usd") or 0 for event in monthly_successful_payments), 2),
        },
        "usage": {
            "all_time": usage_all_time,
            "month_to_date": usage_month,
        },
        "risk": {
            "credits_outstanding": credits_outstanding,
            "low_credit_clients": low_credit_clients,
            "paused_or_cancelled_clients": paused_or_cancelled_clients,
            "high_usage_clients": high_usage_clients,
            "negative_margin_clients": negative_margin_clients,
        },
    }


def delivery_needs_attention(status):
    return status not in {None, "", "sent", "not_required", "not_needed", "already_sent"}


def action_item(item_id, severity, category, title, detail, client_id=None, action=None):
    return {
        "id": item_id,
        "severity": severity,
        "category": category,
        "title": title,
        "detail": detail,
        "client_id": client_id,
        "action": action,
    }


def admin_action_items_data():
    report = admin_revenue_report_data()
    items = []

    for event in list_payment_events_from_db(limit=100):
        if delivery_needs_attention(event.get("delivery_status")):
            items.append(
                action_item(
                    f"payment-event:{event['event_id']}",
                    "high",
                    "fulfillment",
                    "Payment processed but delivery needs attention",
                    f"{event.get('event_type')} for {event.get('billing_email') or event.get('client_id')} has delivery status {event.get('delivery_status')}.",
                    event.get("client_id"),
                    "Rotate and resend API key from the admin client table.",
                )
            )

    with db_connection() as connection:
        notification_rows = connection.execute(
            "SELECT * FROM client_notifications ORDER BY created_at DESC LIMIT 100"
        ).fetchall()

    for notification in [row_to_client_notification(row) for row in notification_rows]:
        if delivery_needs_attention(notification.get("delivery_status")):
            items.append(
                action_item(
                    f"notification:{notification['id']}",
                    "medium",
                    "notification",
                    "Customer notification needs attention",
                    f"{notification.get('notification_type')} for {notification.get('client_id')} has delivery status {notification.get('delivery_status')}.",
                    notification.get("client_id"),
                    "Check Resend configuration or contact the customer directly.",
                )
            )

    for client in report["risk"]["low_credit_clients"]:
        items.append(
            action_item(
                f"low-credit:{client['client_id']}",
                "medium",
                "credits",
                "Customer credits are low",
                f"{client['client_id']} has {client['credits']} credits remaining; threshold is {client['threshold']}.",
                client["client_id"],
                "Confirm the low-credit email was delivered or offer an upgrade/top-up.",
            )
        )

    for client in report["risk"]["high_usage_clients"]:
        items.append(
            action_item(
                f"high-usage:{client['client_id']}",
                "high",
                "usage",
                "Customer is close to daily guardrail",
                f"{client['client_id']} used {client['daily_usage']['credits_spent']} credits and {client['daily_usage']['requests']} requests today.",
                client["client_id"],
                "Review usage pattern and consider upgrade, custom limits, or abuse review.",
            )
        )

    for client in report["risk"]["negative_margin_clients"]:
        items.append(
            action_item(
                f"negative-margin:{client['client_id']}",
                "critical",
                "margin",
                "Customer has negative gross margin",
                f"{client['client_id']} has gross margin {client['gross_margin_usd']:.6f} USD.",
                client["client_id"],
                "Review model route, pricing, and plan limits before scaling this customer.",
            )
        )

    for client in report["risk"]["paused_or_cancelled_clients"]:
        items.append(
            action_item(
                f"account-status:{client['client_id']}",
                "medium",
                "account",
                "Customer account is not active",
                f"{client['client_id']} is {client['status']} with subscription status {client.get('subscription_status') or 'unknown'}.",
                client["client_id"],
                "Check Stripe status and decide whether to restore, follow up, or leave disabled.",
            )
        )

    for merchant in merchant_health_report(limit=200)["merchants"]:
        if merchant["risk_level"] == "high":
            items.append(
                action_item(
                    f"merchant-health:{merchant['business_slug']}",
                    "high",
                    "merchant",
                    "Merchant workflow needs attention",
                    f"{merchant['business_name']} is {merchant['onboarding_status']} with {merchant['due_followups']} due follow-up(s) and {merchant['new_leads']} new lead(s).",
                    merchant.get("business_slug"),
                    merchant["next_action"],
                )
            )

    failed_checks = [check for check in deployment_check_items() if not check["ok"]]
    for check in failed_checks:
        items.append(
            action_item(
                f"deploy-check:{check['name']}",
                "critical",
                "readiness",
                f"Readiness check failed: {check['name']}",
                check["detail"],
                None,
                "Fix the production configuration before public traffic increases.",
            )
        )

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    items = sorted(items, key=lambda item: (severity_order.get(item["severity"], 9), item["category"], item["id"]))

    return {
        "generated_at": now_iso(),
        "counts": {
            "total": len(items),
            "critical": sum(1 for item in items if item["severity"] == "critical"),
            "high": sum(1 for item in items if item["severity"] == "high"),
            "medium": sum(1 for item in items if item["severity"] == "medium"),
            "low": sum(1 for item in items if item["severity"] == "low"),
        },
        "items": items,
    }


def row_to_payment_event(row):
    response = json.loads(row["response_json"]) if row["response_json"] else None
    return {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "provider": row["provider"],
        "client_id": row["client_id"],
        "plan": row["plan"],
        "billing_email": row["billing_email"],
        "amount_usd": row["amount_usd"],
        "currency": row["currency"],
        "mode": row["mode"],
        "credits": row["credits"],
        "processed": bool(row["processed"]),
        "delivery_status": row["delivery_status"],
        "delivery_provider": row["delivery_provider"],
        "response": response,
        "created_at": row["created_at"],
    }


def provider_status(provider_key):
    provider = PROVIDERS[provider_key]
    api_key = (os.getenv(provider["api_key_env"]) or "").strip()
    expected_prefix = provider["expected_key_prefix"]

    return {
        "name": provider["name"],
        "configured": bool(api_key) and api_key.startswith(expected_prefix),
        "key_present": bool(api_key),
        "expected_key_prefix": expected_prefix,
    }


def provider_is_configured(provider_key):
    return provider_status(provider_key)["configured"]


def normalize_phone_for_whatsapp(phone):
    return "".join(char for char in phone if char.isdigit())


def normalize_slug(value):
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    if not 3 <= len(slug) <= 80:
        raise HTTPException(status_code=400, detail="Business slug must be 3-80 URL-safe characters.")
    return slug


def row_to_business_profile(row):
    return {
        "slug": row["slug"],
        "client_id": row["client_id"],
        "business_name": row["business_name"],
        "business_type": row["business_type"],
        "whatsapp_phone": row["whatsapp_phone"],
        "contact_email": row["contact_email"],
        "offer_summary": row["offer_summary"] or "",
        "reply_tone": row["reply_tone"] or "friendly and professional",
        "opening_hours": row["opening_hours"] or "",
        "status": row["status"],
        "access_key_prefix": row["access_key_prefix"],
        "auto_followup_enabled": bool(row["auto_followup_enabled"]) if "auto_followup_enabled" in row.keys() else True,
        "hot_followup_hours": row["hot_followup_hours"] if "hot_followup_hours" in row.keys() else 0,
        "standard_followup_days": row["standard_followup_days"] if "standard_followup_days" in row.keys() else 1,
        "data_retention_days": row["data_retention_days"] if "data_retention_days" in row.keys() else 365,
        "form_url": f"/enquiry/{row['slug']}",
        "inbox_url": f"/inbox/{row['slug']}",
        "embed_url": f"/embed/enquiry/{row['slug']}.js",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def default_enquiry_profile():
    return {
        "slug": "demo",
        "client_id": None,
        "business_name": "Demo Business",
        "business_type": "general",
        "whatsapp_phone": "",
        "contact_email": None,
        "offer_summary": "General customer enquiries.",
        "reply_tone": "friendly and professional",
        "opening_hours": "",
        "status": "active",
        "access_key_prefix": None,
        "auto_followup_enabled": True,
        "hot_followup_hours": 0,
        "standard_followup_days": 1,
        "data_retention_days": 365,
        "form_url": "/enquiry/demo",
        "inbox_url": "/inbox/demo",
        "embed_url": "/embed/enquiry/demo.js",
        "created_at": None,
        "updated_at": None,
    }


def public_business_profile_response(profile):
    return {
        "slug": profile["slug"],
        "business_name": profile["business_name"],
        "business_type": profile["business_type"],
        "offer_summary": profile["offer_summary"] or "",
        "opening_hours": profile["opening_hours"] or "",
        "status": profile["status"],
        "form_url": profile["form_url"],
        "embed_url": profile["embed_url"],
    }


def site_absolute_url(path):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com").rstrip("/")
    return f"{site_url}{path}"


def tracked_enquiry_url(profile, source, campaign="merchant-share"):
    params = {
        "source": source,
        "campaign": campaign,
    }
    return f"{site_absolute_url(profile['form_url'])}?{urlencode(params)}"


def merchant_share_links(profile, campaign="merchant-share"):
    campaign = (campaign or "merchant-share").strip()[:120] or "merchant-share"
    form_url = site_absolute_url(profile["form_url"])
    inbox_url = site_absolute_url(profile["inbox_url"])
    embed_url = site_absolute_url(profile["embed_url"])
    embed_code = f'<script src="{embed_url}"></script>'
    links = {
        "direct": {
            "label": "Direct customer link",
            "source": "direct",
            "url": tracked_enquiry_url(profile, "direct", campaign),
        },
        "whatsapp": {
            "label": "WhatsApp share link",
            "source": "whatsapp",
            "url": tracked_enquiry_url(profile, "whatsapp", campaign),
        },
        "instagram": {
            "label": "Instagram bio or DM link",
            "source": "instagram",
            "url": tracked_enquiry_url(profile, "instagram", campaign),
        },
        "facebook": {
            "label": "Facebook post or group link",
            "source": "facebook",
            "url": tracked_enquiry_url(profile, "facebook", campaign),
        },
        "google_business": {
            "label": "Google Business Profile link",
            "source": "google-business",
            "url": tracked_enquiry_url(profile, "google-business", campaign),
        },
        "website_widget": {
            "label": "Website widget code",
            "source": "website-widget",
            "url": tracked_enquiry_url(profile, "website-widget", campaign),
            "embed_code": embed_code,
        },
    }
    caption = (
        f"Hi, you can send your enquiry to {profile['business_name']} here: "
        f"{links['direct']['url']}"
    )
    return {
        "business_slug": profile["slug"],
        "campaign": campaign,
        "form_url": form_url,
        "inbox_url": inbox_url,
        "embed_url": embed_url,
        "embed_code": embed_code,
        "links": links,
        "copy": {
            "short_caption": caption,
            "whatsapp_text": f"Hi, please submit your enquiry here so we can follow up properly: {links['whatsapp']['url']}",
            "instagram_bio": links["instagram"]["url"],
            "facebook_post": caption,
        },
    }


CHANNEL_CATALOG = {
    "whatsapp": {
        "label": "WhatsApp Business",
        "official_status": "available",
        "default_mode": "official_api_requested",
        "capabilities": ["webhook_inbox", "service_replies", "template_followups"],
        "security_requirements": ["business_verification", "webhook_signature", "least_privilege_tokens"],
        "data_note": "Use WhatsApp Business Platform or Cloud API. Do not paste personal WhatsApp passwords or OTPs.",
    },
    "instagram": {
        "label": "Instagram DM",
        "official_status": "available_with_review",
        "default_mode": "official_api_requested",
        "capabilities": ["webhook_inbox", "reply_window_tracking", "lead_source_mapping"],
        "security_requirements": ["professional_account", "meta_app_review", "webhook_signature"],
        "data_note": "Requires a professional account connected to Meta Business. Personal inbox scraping is not supported.",
    },
    "facebook": {
        "label": "Facebook Page Messenger",
        "official_status": "available_with_review",
        "default_mode": "official_api_requested",
        "capabilities": ["page_inbox", "webhook_inbox", "reply_window_tracking"],
        "security_requirements": ["page_admin_authorization", "meta_app_review", "webhook_signature"],
        "data_note": "Works with Facebook Pages, not personal accounts or unsupported Marketplace private inboxes.",
    },
    "tiktok": {
        "label": "TikTok",
        "official_status": "limited",
        "default_mode": "assisted_capture",
        "capabilities": ["source_tracking", "assisted_capture", "lead_form_handoff"],
        "security_requirements": ["no_scraping", "no_password_collection", "official_partner_review"],
        "data_note": "Treat TikTok DM sync as limited until official business messaging access is approved.",
    },
    "xiaohongshu": {
        "label": "Xiaohongshu",
        "official_status": "limited",
        "default_mode": "assisted_capture",
        "capabilities": ["source_tracking", "assisted_capture", "keyword_handoff"],
        "security_requirements": ["no_scraping", "no_password_collection", "official_platform_review"],
        "data_note": "Use assisted capture or official merchant platform access only. Do not store passwords or cookies.",
    },
}


def channel_catalog_item(channel):
    item = CHANNEL_CATALOG.get(channel)
    if not item:
        raise HTTPException(status_code=404, detail="Channel is not supported")
    return item


META_CHANNEL_ID_FIELDS = {
    "whatsapp": {
        "meta_name": "phone_number_id",
        "label": "WhatsApp phone_number_id",
        "label_zh": "WhatsApp phone_number_id",
        "matched_from": "value.metadata.phone_number_id",
        "where_to_find": "Meta Developer App > WhatsApp > API Setup > Phone number ID",
        "help": "Use the phone number ID that receives buyer messages, not the WABA ID.",
        "help_zh": "使用接收买家私信的 phone number ID，不是 WABA ID。",
    },
    "facebook": {
        "meta_name": "page_id",
        "label": "Facebook Page ID",
        "label_zh": "Facebook Page ID",
        "matched_from": "messaging.recipient.id or entry.id",
        "where_to_find": "Meta Business Suite or Graph API page id for the connected Page",
        "help": "Use the connected Facebook Page ID that receives Messenger enquiries.",
        "help_zh": "使用接收 Messenger 询问的 Facebook 专页 ID。",
    },
    "instagram": {
        "meta_name": "instagram_account_id",
        "label": "Instagram professional account ID",
        "label_zh": "Instagram 专业账号 ID",
        "matched_from": "messaging.recipient.id or entry.id",
        "where_to_find": "Instagram professional account connected to a Facebook Page in Meta Business",
        "help": "Use the professional Instagram account ID connected to the Meta app.",
        "help_zh": "使用已连接到 Meta app 的 Instagram 专业账号 ID。",
    },
}


def channel_external_id_field(channel):
    field = META_CHANNEL_ID_FIELDS.get(channel)
    if field:
        return {
            "storage_key": "external_account_id",
            "required_for_official_sync": True,
            **field,
        }
    return {
        "storage_key": "external_account_id",
        "meta_name": "external_account_id",
        "label": "External account ID",
        "label_zh": "外部账号 ID",
        "matched_from": "",
        "where_to_find": "Optional source identifier for assisted capture.",
        "help": "Optional unless official API sync is approved for this source.",
        "help_zh": "除非这个来源已获官方 API 同步权限，否则这是选填。",
        "required_for_official_sync": False,
    }


def row_to_channel_connection(row):
    capabilities = json.loads(row["capabilities_json"] or "[]")
    return {
        "id": row["id"],
        "business_slug": row["business_slug"],
        "channel": row["channel"],
        "integration_mode": row["integration_mode"],
        "status": row["status"],
        "account_label": row["account_label"] or "",
        "external_account_id": row["external_account_id"] or "",
        "capabilities": capabilities,
        "token_status": row["token_status"] or "not_stored",
        "data_processing_acknowledged": bool(row["data_processing_acknowledged"]),
        "security_reviewed_at": row["security_reviewed_at"] or "",
        "last_sync_at": row["last_sync_at"] or "",
        "notes": row["notes"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_channel_connections(business_slug):
    with db_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM channel_connections WHERE business_slug = ? ORDER BY channel",
            (normalize_slug(business_slug),),
        ).fetchall()
    return {row["channel"]: row_to_channel_connection(row) for row in rows}


def get_saved_channel_connection(business_slug, channel):
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM channel_connections WHERE business_slug = ? AND channel = ?",
            (normalize_slug(business_slug), channel),
        ).fetchone()
    return row_to_channel_connection(row) if row else None


def channel_connection_response(profile):
    saved = list_channel_connections(profile["slug"])
    connections = []
    for channel, item in CHANNEL_CATALOG.items():
        existing = saved.get(channel)
        if existing:
            status = existing["status"]
            integration_mode = existing["integration_mode"]
            account_label = existing["account_label"]
            external_account_id = existing["external_account_id"]
            acknowledged = existing["data_processing_acknowledged"]
            notes = existing["notes"]
            updated_at = existing["updated_at"]
        else:
            status = "setup_required" if item["official_status"] != "limited" else "limited"
            integration_mode = item["default_mode"]
            account_label = ""
            external_account_id = ""
            acknowledged = False
            notes = ""
            updated_at = ""

        connections.append(
            {
                "channel": channel,
                "label": item["label"],
                "official_status": item["official_status"],
                "integration_mode": integration_mode,
                "status": status,
                "account_label": account_label,
                "external_account_id": external_account_id,
                "id_field": channel_external_id_field(channel),
                "capabilities": item["capabilities"],
                "security_requirements": item["security_requirements"],
                "token_status": "not_stored",
                "data_processing_acknowledged": acknowledged,
                "data_note": item["data_note"],
                "notes": notes,
                "updated_at": updated_at,
            }
        )

    return {
        "business": {
            "slug": profile["slug"],
            "business_name": profile["business_name"],
            "data_retention_days": profile.get("data_retention_days", 365),
        },
        "summary": {
            "total": len(connections),
            "official_ready": sum(1 for item in connections if item["official_status"].startswith("available")),
            "limited": sum(1 for item in connections if item["official_status"] == "limited"),
            "configured": sum(1 for item in connections if item["status"] in {"requested", "assisted", "connected"}),
        },
        "security_notice": (
            "NexaFlow stores channel connection metadata only in this setup screen. "
            "Do not paste platform passwords, OTPs, cookies, long-lived access tokens, or customer identity documents."
        ),
        "data_protection": {
            "owner_key_required": True,
            "tokens_stored": False,
            "audit_events": True,
            "retention_days": profile.get("data_retention_days", 365),
        },
        "connections": connections,
    }


def merchant_meta_setup_response(profile):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com").rstrip("/")
    webhook_url = f"{site_url}/webhooks/meta"
    verify_token_configured = bool(os.getenv("META_WEBHOOK_VERIFY_TOKEN"))
    app_secret_configured = bool(os.getenv("META_APP_SECRET"))
    https_callback_url = webhook_url.startswith("https://")
    ready_for_meta_setup = verify_token_configured and app_secret_configured and https_callback_url
    permission_notes = {
        "whatsapp": ["WhatsApp Business Platform access", "webhook messages subscription"],
        "facebook": ["Page admin access", "Messenger webhook events", "Meta app review if required"],
        "instagram": ["Professional Instagram account", "Instagram messaging access", "Meta app review if required"],
    }
    return {
        "business": {
            "slug": profile["slug"],
            "business_name": profile["business_name"],
        },
        "webhook": {
            "url": webhook_url,
            "verify_token_configured": verify_token_configured,
            "app_secret_configured": app_secret_configured,
            "signature_required": True,
            "site_url_configured": bool(os.getenv("NEXAFLOW_SITE_URL")),
            "https_callback_url": https_callback_url,
            "ready_for_meta_setup": ready_for_meta_setup,
        },
        "meta_channels": [
            {
                "channel": channel,
                "label": CHANNEL_CATALOG[channel]["label"],
                "id_field": channel_external_id_field(channel),
                "external_account_id_label": channel_external_id_field(channel)["label"],
                "where_to_find": channel_external_id_field(channel)["where_to_find"],
                "required_permissions": permission_notes[channel],
            }
            for channel in ("whatsapp", "facebook", "instagram")
        ],
        "security": {
            "tokens_stored": False,
            "do_not_paste": ["platform password", "OTP", "cookies", "page access token", "app secret"],
            "notes": "Store Meta app secret and verify token only as Railway environment variables. NexaFlow channel setup stores account IDs only.",
        },
    }


def contains_forbidden_channel_secret(*values):
    text = " ".join(str(value or "") for value in values).lower()
    secret_markers = [
        "access_token",
        "refresh_token",
        "client_secret",
        "app_secret",
        "bearer ",
        "cookie",
        "sessionid",
        "password=",
        "passwd",
        "otp",
        "authorization:",
    ]
    return any(marker in text for marker in secret_markers)


def contains_sensitive_manual_enquiry_content(*values):
    text = " ".join(str(value or "") for value in values).lower()
    sensitive_markers = [
        "bank statement",
        "payslip",
        "pay slip",
        "salary slip",
        "epf statement",
        "kwsp statement",
        "passport",
        "nric",
        "ic number",
        "identity card",
        "id card",
        "kad pengenalan",
        "银行卡",
        "银行账单",
        "银行月结单",
        "薪资单",
        "粮单",
        "身份证",
        "护照",
    ]
    return any(marker in text for marker in sensitive_markers)


def manual_capture_buyer_name(source, name):
    clean_name = (name or "").strip()
    if clean_name:
        return clean_name[:120]
    source_label = {
        "whatsapp": "WhatsApp",
        "instagram": "Instagram",
        "facebook": "Facebook",
        "tiktok": "TikTok",
        "xiaohongshu": "Xiaohongshu",
        "referral": "Referral",
        "manual": "Manual",
    }.get(source, source.replace("-", " ").title() if source else "Manual")
    return f"{source_label} Buyer"[:120]


def manual_capture_contact_identifier(source, name, phone):
    clean_contact = (phone or "").strip()
    if clean_contact:
        return clean_contact[:40]
    handle = re.sub(r"[^a-z0-9@_.-]+", "-", (name or "").strip().lower()).strip("-")
    if not handle:
        handle = "buyer"
    return f"{source}:{handle}"[:40]


def upsert_channel_connection(profile, channel, req):
    catalog = channel_catalog_item(channel)
    external_account_id = req.external_account_id.strip()
    if req.integration_mode == "official_api_requested" and catalog["official_status"] == "limited":
        raise HTTPException(status_code=400, detail="This channel does not support official DM sync in the current setup")
    if req.status == "requested" and not req.data_processing_acknowledged:
        raise HTTPException(status_code=400, detail="Data processing acknowledgement is required before requesting a channel connection")
    if contains_forbidden_channel_secret(req.account_label, req.external_account_id, req.notes):
        raise HTTPException(status_code=400, detail="Do not paste passwords, OTPs, cookies, app secrets, or access tokens into channel setup")

    timestamp = now_iso()
    capabilities_json = json.dumps(catalog["capabilities"], separators=(",", ":"), sort_keys=True)
    with db_connection() as connection:
        if external_account_id:
            owner = connection.execute(
                """
                SELECT business_slug FROM channel_connections
                WHERE channel = ? AND external_account_id = ? AND business_slug != ?
                LIMIT 1
                """,
                (channel, external_account_id, profile["slug"]),
            ).fetchone()
            if owner:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This Meta account ID is already connected to another NexaFlow inbox. "
                        "Check the phone_number_id, page_id, or instagram_account_id before saving."
                    ),
                )
        existing = connection.execute(
            "SELECT * FROM channel_connections WHERE business_slug = ? AND channel = ?",
            (profile["slug"], channel),
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE channel_connections
                SET integration_mode = ?, status = ?, account_label = ?, external_account_id = ?,
                    capabilities_json = ?, token_status = 'not_stored', data_processing_acknowledged = ?,
                    security_reviewed_at = ?, notes = ?, updated_at = ?
                WHERE business_slug = ? AND channel = ?
                """,
                (
                    req.integration_mode,
                    req.status,
                    req.account_label.strip(),
                    external_account_id,
                    capabilities_json,
                    1 if req.data_processing_acknowledged else 0,
                    timestamp if req.data_processing_acknowledged else None,
                    req.notes.strip(),
                    timestamp,
                    profile["slug"],
                    channel,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO channel_connections (
                    business_slug, channel, integration_mode, status, account_label, external_account_id,
                    capabilities_json, token_status, data_processing_acknowledged, security_reviewed_at,
                    notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'not_stored', ?, ?, ?, ?, ?)
                """,
                (
                    profile["slug"],
                    channel,
                    req.integration_mode,
                    req.status,
                    req.account_label.strip(),
                    external_account_id,
                    capabilities_json,
                    1 if req.data_processing_acknowledged else 0,
                    timestamp if req.data_processing_acknowledged else None,
                    req.notes.strip(),
                    timestamp,
                    timestamp,
                ),
            )
        row = connection.execute(
            "SELECT * FROM channel_connections WHERE business_slug = ? AND channel = ?",
            (profile["slug"], channel),
        ).fetchone()
        write_data_audit_event(
            "channel.connection_updated",
            "merchant",
            "channel_connection",
            row["id"],
            business_slug=profile["slug"],
            metadata={
                "channel": channel,
                "integration_mode": req.integration_mode,
                "status": req.status,
                "token_status": "not_stored",
                "data_processing_acknowledged": req.data_processing_acknowledged,
            },
            connection=connection,
        )

    return {
        **row_to_channel_connection(row),
        "label": catalog["label"],
        "official_status": catalog["official_status"],
        "security_requirements": catalog["security_requirements"],
        "data_note": catalog["data_note"],
    }


def meta_message_text(message):
    if not isinstance(message, dict):
        return ""
    text_value = message.get("text")
    if isinstance(text_value, dict) and text_value.get("body"):
        return text_value["body"]
    if text_value:
        return str(text_value)
    if message.get("button", {}).get("text"):
        return message["button"]["text"]
    if message.get("interactive", {}).get("button_reply", {}).get("title"):
        return message["interactive"]["button_reply"]["title"]
    if message.get("interactive", {}).get("list_reply", {}).get("title"):
        return message["interactive"]["list_reply"]["title"]
    if message.get("attachments"):
        return "[Attachment received]"
    message_type = message.get("type") or ""
    if message_type and message_type != "text":
        return f"[{message_type} message received]"
    return ""


def meta_timestamp_to_iso(value):
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return now_iso()
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def extract_meta_inbound_messages(payload):
    events = []
    payload_object = payload.get("object")
    for entry in payload.get("entry", []):
        entry_id = str(entry.get("id") or "")
        if payload_object == "whatsapp_business_account":
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                metadata = value.get("metadata") or {}
                account_id = str(metadata.get("phone_number_id") or metadata.get("display_phone_number") or "")
                contact_names = {
                    str(contact.get("wa_id") or ""): (contact.get("profile") or {}).get("name") or ""
                    for contact in value.get("contacts", [])
                }
                for message in value.get("messages", []):
                    text = meta_message_text(message)
                    if not text:
                        continue
                    customer_handle = str(message.get("from") or "")
                    events.append(
                        {
                            "channel": "whatsapp",
                            "account_id": account_id,
                            "external_message_id": str(message.get("id") or f"whatsapp:{account_id}:{customer_handle}:{message.get('timestamp') or now_iso()}"),
                            "customer_display_name": contact_names.get(customer_handle) or customer_handle or "WhatsApp buyer",
                            "customer_handle": customer_handle,
                            "message": text,
                            "received_at": meta_timestamp_to_iso(message.get("timestamp")),
                            "create_whatsapp_reply": True,
                        }
                    )
        elif payload_object in {"page", "instagram"}:
            channel = "instagram" if payload_object == "instagram" else "facebook"
            for item in entry.get("messaging", []):
                message = item.get("message") or {}
                if message.get("is_echo"):
                    continue
                text = meta_message_text(message)
                if not text:
                    continue
                recipient = item.get("recipient") or {}
                sender = item.get("sender") or {}
                account_id = str(recipient.get("id") or entry_id)
                customer_handle = str(sender.get("id") or "")
                timestamp = item.get("timestamp")
                received_at = meta_timestamp_to_iso(str(int(timestamp / 1000)) if timestamp else None)
                events.append(
                    {
                        "channel": channel,
                        "account_id": account_id,
                        "external_message_id": str(message.get("mid") or f"{channel}:{account_id}:{customer_handle}:{timestamp or now_iso()}"),
                        "customer_display_name": customer_handle or f"{channel.title()} buyer",
                        "customer_handle": customer_handle,
                        "message": text,
                        "received_at": received_at,
                        "create_whatsapp_reply": False,
                    }
                )
    return events


def channel_connection_for_external_account(channel, external_account_id):
    if not external_account_id:
        return None, None
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM channel_connections
            WHERE channel = ? AND external_account_id = ?
                AND data_processing_acknowledged = 1
                AND status != 'paused'
                AND integration_mode = 'official_api_requested'
            """,
            (channel, external_account_id),
        ).fetchall()
    if len(rows) != 1:
        return None, None
    channel_connection = row_to_channel_connection(rows[0])
    profile = get_business_profile(channel_connection["business_slug"])
    if profile["status"] != "active":
        return None, None
    return channel_connection, profile


def channel_message_exists(channel, external_message_id):
    if not external_message_id:
        return False
    with db_connection() as connection:
        row = connection.execute(
            "SELECT id FROM channel_messages WHERE channel = ? AND external_message_id = ?",
            (channel, external_message_id),
        ).fetchone()
    return row is not None


def record_channel_message(connection_info, profile, event, enquiry):
    try:
        retention_until = (
            datetime.now(timezone.utc) + timedelta(days=int(profile.get("data_retention_days") or 365))
        ).date().isoformat()
    except (TypeError, ValueError):
        retention_until = ""
    timestamp = now_iso()
    with db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO channel_messages (
                business_slug, channel, connection_id, external_message_id, enquiry_id,
                customer_display_name, customer_handle, direction, message_preview,
                received_at, retention_until, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'inbound', ?, ?, ?, ?)
            """,
            (
                profile["slug"],
                event["channel"],
                connection_info["id"],
                event["external_message_id"],
                enquiry["id"],
                event["customer_display_name"],
                event["customer_handle"],
                event["message"][:300],
                event["received_at"],
                retention_until,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE channel_connections SET last_sync_at = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, connection_info["id"]),
        )
        write_data_audit_event(
            "channel.message_received",
            "meta_webhook",
            "channel_message",
            cursor.lastrowid,
            business_slug=profile["slug"],
            metadata={
                "channel": event["channel"],
                "external_message_id": event["external_message_id"],
                "enquiry_id": enquiry["id"],
            },
            connection=connection,
        )


def create_enquiry_from_meta_event(connection_info, profile, event, notify_merchant=True):
    if channel_message_exists(event["channel"], event["external_message_id"]):
        return {"status": "duplicate", "external_message_id": event["external_message_id"]}
    phone_or_handle = event["customer_handle"] if event["channel"] == "whatsapp" else f"{event['channel']}:{event['customer_handle'] or event['external_message_id']}"
    enquiry_req = EnquiryCreateRequest(
        business_slug=profile["slug"],
        business_type=profile["business_type"],
        name=event["customer_display_name"] or f"{event['channel'].title()} buyer",
        phone=phone_or_handle[:40],
        email=None,
        message=event["message"],
        source=event["channel"],
        campaign="meta-webhook",
        referrer=f"meta:{event['account_id']}",
        page_url="",
        pdpa_consent=True,
    )
    if event.get("local_pilot"):
        consent_notice = (
            f"Local {event['channel']} pilot test generated by the merchant to preview how NexaFlow turns "
            "a social message into a buyer follow-up card. No real Meta message was sent or received."
        )
    else:
        consent_notice = (
            f"Inbound {event['channel']} message received through a merchant-connected official Meta channel. "
            "The merchant authorized NexaFlow to process this buyer message for reply, quotation, appointment, "
            "loan follow-up, support, security, audit, and retention controls."
        )
    enquiry = create_enquiry_record(
        enquiry_req,
        actor_type="meta_webhook",
        notify_merchant=notify_merchant,
        consent_notice_override=consent_notice,
        create_whatsapp_reply=event["create_whatsapp_reply"],
    )
    record_channel_message(connection_info, profile, event, enquiry)
    return {"status": "created", "enquiry_id": enquiry["id"], "external_message_id": event["external_message_id"]}


def create_meta_pilot_test_event(profile, channel):
    if channel not in {"whatsapp", "facebook", "instagram"}:
        raise HTTPException(status_code=400, detail="Meta pilot test is available for WhatsApp, Facebook, and Instagram only")
    connection_info = get_saved_channel_connection(profile["slug"], channel)
    if not connection_info:
        connection_info = {
            "id": 0,
            "channel": channel,
            "external_account_id": f"local-pilot:{profile['slug']}:{channel}",
        }

    timestamp = now_iso()
    pilot_nonce = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    demo_messages = {
        "whatsapp": "Meta pilot test: buyer asks if loan can be approved, monthly below RM900, and wants to view today.",
        "facebook": "Meta pilot test: buyer asks if the Civic is still available and whether viewing tomorrow is possible.",
        "instagram": "Meta pilot test: buyer asks for lowest deposit on Vios and is comparing another dealer.",
    }
    event = {
        "channel": channel,
        "account_id": connection_info.get("external_account_id") or f"local-pilot:{profile['slug']}:{channel}",
        "external_message_id": f"pilot:{channel}:{connection_info['id']}:{pilot_nonce}",
        "customer_display_name": f"{CHANNEL_CATALOG[channel]['label']} Test Buyer",
        "customer_handle": "60120000000" if channel == "whatsapp" else f"{channel}-test-user",
        "message": demo_messages[channel],
        "received_at": timestamp,
        "create_whatsapp_reply": channel == "whatsapp",
        "local_pilot": True,
    }
    item = create_enquiry_from_meta_event(connection_info, profile, event, notify_merchant=False)
    return {
        **item,
        "channel": channel,
        "business_slug": profile["slug"],
        "message": "Local Meta test buyer created in the buyer inbox. No real Meta DM was sent or received.",
        "inbox_url": profile["inbox_url"],
    }


def mask_external_account_id(value):
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 6:
        return "***"
    return f"{text[:3]}...{text[-3:]}"


def process_meta_webhook_payload(payload):
    events = extract_meta_inbound_messages(payload)
    result = {
        "received": len(events),
        "created": 0,
        "duplicates": 0,
        "unmapped": 0,
        "ignored": 0,
        "items": [],
    }
    for event in events:
        connection_info, profile = channel_connection_for_external_account(event["channel"], event["account_id"])
        if not connection_info:
            result["unmapped"] += 1
            result["items"].append(
                {
                    "status": "unmapped",
                    "channel": event["channel"],
                    "account_id_preview": mask_external_account_id(event["account_id"]),
                    "external_message_id": event["external_message_id"],
                }
            )
            continue
        item = create_enquiry_from_meta_event(connection_info, profile, event)
        result["items"].append(item)
        if item["status"] == "created":
            result["created"] += 1
        elif item["status"] == "duplicate":
            result["duplicates"] += 1
        else:
            result["ignored"] += 1
    return result


def get_business_profile(slug):
    if not slug:
        return default_enquiry_profile()

    normalized_slug = normalize_slug(slug)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM business_profiles WHERE slug = ?",
            (normalized_slug,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Business profile not found")

    return row_to_business_profile(row)


def list_business_profiles(client_id=None):
    with db_connection() as connection:
        if client_id:
            rows = connection.execute(
                "SELECT * FROM business_profiles WHERE client_id = ? ORDER BY business_name",
                (client_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM business_profiles ORDER BY business_name"
            ).fetchall()

    return [row_to_business_profile(row) for row in rows]


def create_default_business_profile_for_client(client_id, billing_email):
    existing = list_business_profiles(client_id=client_id)
    if existing:
        return {
            "created": False,
            "profile": existing[0],
            "delivery": {"status": "not_required", "reason": "Business profile already exists."},
        }

    timestamp = now_iso()
    raw_access_key = generate_business_access_key()
    slug = generate_business_slug(billing_email or client_id)
    with db_connection() as connection:
        while connection.execute("SELECT slug FROM business_profiles WHERE slug = ?", (slug,)).fetchone():
            slug = generate_business_slug(billing_email or client_id)
        connection.execute(
            """
            INSERT INTO business_profiles (
                slug, client_id, business_name, business_type, whatsapp_phone, contact_email,
                offer_summary, reply_tone, opening_hours, status, access_key_hash,
                access_key_prefix, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                client_id,
                business_name_from_email(billing_email),
                "general",
                "",
                normalize_email(billing_email),
                "Customer enquiries for your service business.",
                "friendly and professional",
                "",
                "active",
                api_key_digest(raw_access_key),
                raw_access_key[:12],
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM business_profiles WHERE slug = ?",
            (slug,),
        ).fetchone()

    profile = row_to_business_profile(row)
    profile["business_access_key"] = raw_access_key
    delivery = (
        send_resend_email(
            profile["contact_email"],
            f"{profile['business_name']} NexaFlow Enquiry Inbox is ready",
            merchant_onboarding_email(profile, raw_access_key),
        )
        if profile.get("contact_email")
        else {"status": "pending_email", "reason": "No billing email available for merchant onboarding."}
    )
    return {
        "created": True,
        "profile": {key: value for key, value in profile.items() if key != "business_access_key"},
        "delivery": delivery,
    }


def upsert_business_profile(req, owner_client_id=None):
    slug = normalize_slug(req.slug)
    timestamp = now_iso()
    raw_access_key = None
    with db_connection() as connection:
        existing = connection.execute(
            "SELECT created_at, access_key_hash, access_key_prefix, client_id FROM business_profiles WHERE slug = ?",
            (slug,),
        ).fetchone()
        if existing and owner_client_id and existing["client_id"] != owner_client_id:
            raise HTTPException(status_code=409, detail="Business slug is already used by another customer.")

        created_at = existing["created_at"] if existing else timestamp
        client_id = owner_client_id if owner_client_id is not None else (existing["client_id"] if existing else None)
        access_key_hash = existing["access_key_hash"] if existing else None
        access_key_prefix = existing["access_key_prefix"] if existing else None
        if not access_key_hash or req.rotate_access_key:
            raw_access_key = generate_business_access_key()
            access_key_hash = api_key_digest(raw_access_key)
            access_key_prefix = raw_access_key[:12]
        connection.execute(
            """
            INSERT OR REPLACE INTO business_profiles (
                slug, client_id, business_name, business_type, whatsapp_phone, contact_email,
                offer_summary, reply_tone, opening_hours, status, access_key_hash,
                access_key_prefix, auto_followup_enabled, hot_followup_hours,
                standard_followup_days, data_retention_days, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                client_id,
                req.business_name.strip(),
                req.business_type.strip() or "general",
                req.whatsapp_phone.strip(),
                normalize_email(req.contact_email),
                req.offer_summary.strip(),
                req.reply_tone.strip() or "friendly and professional",
                req.opening_hours.strip(),
                req.status,
                access_key_hash,
                access_key_prefix,
                1 if req.auto_followup_enabled else 0,
                req.hot_followup_hours,
                req.standard_followup_days,
                req.data_retention_days,
                created_at,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM business_profiles WHERE slug = ?",
            (slug,),
        ).fetchone()

    profile = row_to_business_profile(row)
    if raw_access_key:
        profile["business_access_key"] = raw_access_key
    return profile


def business_slug_base(value, fallback="dealer"):
    raw = "".join(char.lower() if char.isalnum() else "-" for char in (value or "").strip())
    base = "-".join(part for part in raw.split("-") if part)
    if len(base) < 3:
        base = fallback
    return base[:64].strip("-") or fallback


def business_slug_is_available(slug, connection=None):
    normalized_slug = normalize_slug(slug)
    query = "SELECT slug FROM business_profiles WHERE slug = ?"
    params = (normalized_slug,)
    if connection is not None:
        return connection.execute(query, params).fetchone() is None
    with db_connection() as lookup_connection:
        return lookup_connection.execute(query, params).fetchone() is None


def merchant_signup_response(profile, access_key):
    safe_profile = {key: value for key, value in profile.items() if key != "business_access_key"}
    return {
        "profile": safe_profile,
        "business_access_key": access_key,
        "form_url": profile["form_url"],
        "inbox_url": profile["inbox_url"],
        "channels_url": f"/channels/{profile['slug']}",
        "login_url": "/merchant-login",
        "security_notice": (
            "Never paste social media passwords, OTPs, cookies, app secrets, or access tokens into NexaFlow. "
            "Use official platform authorization or assisted capture only."
        ),
        "next_steps": [
            "Open your private inbox.",
            "Load demo buyers or add one real DM/call manually.",
            "Request Meta auto-sync later after Meta Developer setup is ready.",
        ],
    }


def create_merchant_workspace(req):
    if not req.pdpa_consent:
        raise HTTPException(status_code=400, detail="Privacy and data-use consent is required to create a workspace.")
    if contains_forbidden_channel_secret(
        req.business_name,
        req.whatsapp_phone,
        req.contact_email,
        req.preferred_slug or "",
        req.monthly_enquiries,
    ):
        raise HTTPException(
            status_code=400,
            detail="Do not paste passwords, OTPs, cookies, app secrets, or access tokens into signup.",
        )
    enforce_enquiry_rate_limit(
        "merchant-signup",
        normalize_email(req.contact_email) or req.whatsapp_phone,
        limit=3,
        window_seconds=3600,
    )

    if req.preferred_slug:
        slug = normalize_slug(req.preferred_slug)
        if not business_slug_is_available(slug):
            raise HTTPException(status_code=409, detail="Dealer link name is already taken.")
    else:
        base = business_slug_base(req.business_name, fallback="dealer")
        slug = base
        with db_connection() as connection:
            while not business_slug_is_available(slug, connection=connection):
                slug = f"{base[:56].strip('-')}-{secrets.token_hex(3)}"

    profile = upsert_business_profile(
        BusinessProfileRequest(
            slug=slug,
            business_name=req.business_name.strip(),
            business_type=req.business_type.strip() or "used_car_dealer",
            whatsapp_phone=req.whatsapp_phone.strip(),
            contact_email=req.contact_email,
            offer_summary=(
                f"{req.business_name.strip()} uses NexaFlow to collect car buyer enquiries, "
                "track missing details, and keep follow-up visible across social channels."
            ),
            reply_tone="friendly, sales-focused, and clear",
            opening_hours="",
            status="active",
            rotate_access_key=True,
            auto_followup_enabled=True,
            hot_followup_hours=2,
            standard_followup_days=1,
            data_retention_days=365,
        )
    )
    write_data_audit_event(
        "business.self_signup_created",
        "merchant",
        "business_profile",
        profile["slug"],
        business_slug=profile["slug"],
        metadata={
            "market": req.market,
            "business_type": req.business_type,
            "monthly_enquiries": req.monthly_enquiries,
            "access_key_prefix": profile.get("access_key_prefix"),
        },
    )
    return merchant_signup_response(profile, profile["business_access_key"])


def rotate_business_access_key(slug, owner_client_id=None):
    normalized_slug = normalize_slug(slug)
    raw_access_key = generate_business_access_key()
    access_key_hash = api_key_digest(raw_access_key)
    access_key_prefix = raw_access_key[:12]
    timestamp = now_iso()

    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM business_profiles WHERE slug = ?",
            (normalized_slug,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Business profile not found")
        if owner_client_id and row["client_id"] != owner_client_id:
            raise HTTPException(status_code=404, detail="Business profile not found")

        connection.execute(
            "UPDATE business_profiles SET access_key_hash = ?, access_key_prefix = ?, updated_at = ? WHERE slug = ?",
            (access_key_hash, access_key_prefix, timestamp, normalized_slug),
        )
        updated = connection.execute(
            "SELECT * FROM business_profiles WHERE slug = ?",
            (normalized_slug,),
        ).fetchone()

    profile = row_to_business_profile(updated)
    profile["business_access_key"] = raw_access_key
    return profile


def update_business_profile_settings(slug, req):
    normalized_slug = normalize_slug(slug)
    timestamp = now_iso()
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM business_profiles WHERE slug = ?",
            (normalized_slug,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Business profile not found")

        connection.execute(
            """
            UPDATE business_profiles
            SET business_name = ?, business_type = ?, whatsapp_phone = ?, contact_email = ?,
                offer_summary = ?, reply_tone = ?, opening_hours = ?, auto_followup_enabled = ?,
                hot_followup_hours = ?, standard_followup_days = ?, data_retention_days = ?, updated_at = ?
            WHERE slug = ?
            """,
            (
                req.business_name.strip(),
                req.business_type.strip() or "general",
                req.whatsapp_phone.strip(),
                normalize_email(req.contact_email),
                req.offer_summary.strip(),
                req.reply_tone.strip() or "friendly and professional",
                req.opening_hours.strip(),
                1 if req.auto_followup_enabled else 0,
                req.hot_followup_hours,
                req.standard_followup_days,
                req.data_retention_days,
                timestamp,
                normalized_slug,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM business_profiles WHERE slug = ?",
            (normalized_slug,),
        ).fetchone()

    return row_to_business_profile(updated)


def merchant_onboarding_email(profile, access_key):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com").rstrip("/")
    form_url = f"{site_url}{profile['form_url']}"
    inbox_url = f"{site_url}{profile['inbox_url']}"
    embed_url = f"{site_url}{profile['embed_url']}"
    embed_code = f'<script src="{embed_url}"></script>'
    return f"""Hi {profile['business_name']},

Your NexaFlow Enquiry setup is ready.

What this does:
NexaFlow helps your dealer team collect buyer enquiries, spot what each buyer is stuck on, prepare WhatsApp follow-up drafts, and remind you who needs follow-up.

Buyer enquiry form:
{form_url}

Private dealer inbox:
{inbox_url}

Inbox password:
{access_key}

Website widget code:
{embed_code}

5-minute setup:
1. Open the private dealer inbox.
2. Paste the inbox password above and click Open Inbox.
3. Open Dealer settings and confirm your WhatsApp phone, email, showroom summary, and opening hours.
4. Copy the buyer enquiry link and share it on WhatsApp, Facebook, Instagram, Google Business Profile, or your website.
5. When buyers arrive, open the inbox, click WhatsApp, save notes, set follow-up dates, and mark each buyer as contacted, quoted, booked, or not proceeding.

Data and privacy:
Buyer enquiry forms include a privacy notice. Your private inbox is protected by this inbox password. Keep it private.

Keep this inbox password private. If it is exposed or lost, ask the NexaFlow operator to rotate it.
"""


def merchant_onboarding_whatsapp_message(profile, access_key):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com").rstrip("/")
    form_url = f"{site_url}{profile['form_url']}"
    inbox_url = f"{site_url}{profile['inbox_url']}"
    return f"""Hi {profile['business_name']}, your NexaFlow Enquiry 30-day trial inbox is ready.

Buyer enquiry link:
{form_url}

Private dealer inbox:
{inbox_url}

Inbox password:
{access_key}

Simple setup:
1. Open the private inbox.
2. Paste the inbox password and open the inbox.
3. Share the enquiry link on WhatsApp, Facebook, Instagram, Google Business Profile, or your website.
4. When a buyer arrives, follow up from the inbox and update the status.

Please keep the inbox password private because it protects your buyer enquiry data."""


def send_business_onboarding(slug, owner_client_id=None):
    profile = rotate_business_access_key(slug, owner_client_id=owner_client_id)
    if not profile.get("contact_email"):
        raise HTTPException(status_code=400, detail="Business profile is missing contact_email.")

    delivery = send_resend_email(
        profile["contact_email"],
        f"{profile['business_name']} NexaFlow Enquiry Inbox is ready",
        merchant_onboarding_email(profile, profile["business_access_key"]),
    )
    return {
        "profile": {key: value for key, value in profile.items() if key != "business_access_key"},
        "delivery": delivery,
        "rotated": True,
        "message": "A new business access key was generated and sent to the merchant contact email.",
    }


def trial_request_followup_metadata(row):
    created = parse_log_timestamp(row["created_at"])
    updated = parse_log_timestamp(row["updated_at"])
    trial_ends_at = parse_log_timestamp(row["trial_ends_at"]) if "trial_ends_at" in row.keys() else None
    now = datetime.now(timezone.utc)
    age_days = max(0, int((now - created).total_seconds() // 86400)) if created else 0
    untouched_days = max(0, int((now - updated).total_seconds() // 86400)) if updated else age_days
    days_until_trial_end = (
        int((trial_ends_at - now).total_seconds() // 86400) + 1
        if trial_ends_at
        else None
    )
    status = row["status"]

    if status == "new":
        if age_days >= 1:
            return age_days, days_until_trial_end, "high", "Contact this merchant today and confirm trial fit."
        return age_days, days_until_trial_end, "medium", "Send first WhatsApp reply and ask for setup details."
    if status == "contacted":
        if untouched_days >= 3:
            return age_days, days_until_trial_end, "medium", "Follow up and offer to create the merchant inbox."
        return age_days, days_until_trial_end, "low", "Wait for merchant response or prepare setup."
    if status == "trial_setup":
        if days_until_trial_end is not None and days_until_trial_end <= 0:
            return age_days, days_until_trial_end, "high", "Trial ended. Ask for feedback and offer paid plan."
        if days_until_trial_end is not None and days_until_trial_end <= 7:
            return age_days, days_until_trial_end, "high", "Trial ending soon. Ask for feedback and discuss paid plan."
        if days_until_trial_end is not None and days_until_trial_end <= 14:
            return age_days, days_until_trial_end, "medium", "Check usage and prepare conversion conversation."
        if age_days >= 7:
            return age_days, days_until_trial_end, "medium", "Check if the merchant has received real enquiries."
        return age_days, days_until_trial_end, "low", "Help merchant complete first test enquiry and share link."
    if status == "won":
        return age_days, days_until_trial_end, "low", "Confirm payment, billing setup, and ongoing support."
    if status == "lost":
        return age_days, days_until_trial_end, "low", "Record reason and archive unless they ask to restart."
    return age_days, days_until_trial_end, "low", "No follow-up required."


def trial_conversion_stage(status, days_until_trial_end):
    if status == "won":
        return "converted"
    if status in {"lost", "spam"}:
        return "closed"
    if status in {"new", "contacted"}:
        return "pre_trial"
    if status == "trial_setup":
        if days_until_trial_end is None:
            return "trial_started"
        if days_until_trial_end <= 0:
            return "trial_ended"
        if days_until_trial_end <= 7:
            return "trial_ending_soon"
        return "active_trial"
    return "unknown"


def trial_conversion_plan_links():
    links = {}
    for plan_key in ("starter", "pro", "business"):
        payment_link = payment_link_for_plan(plan_key)
        links[plan_key] = payment_link or site_absolute_url(f"/billing/checkout?plan={plan_key}")
    return links


def trial_conversion_next_action(stage):
    actions = {
        "pre_trial": "First confirm fit, then create the merchant inbox before discussing paid plans.",
        "active_trial": "Help them receive real enquiries and schedule a mid-trial check-in.",
        "trial_ending_soon": "Ask for feedback, show the value delivered, and offer Starter or Pro before the trial ends.",
        "trial_ended": "Follow up today with the paid plan link or close the trial if they are not a fit.",
        "converted": "Confirm billing, support channel, and next workflow to add.",
        "closed": "Keep the reason for learning and do not follow up unless they ask to restart.",
    }
    return actions.get(stage, "Review the trial manually.")


def trial_conversion_whatsapp_message(request):
    links = trial_conversion_plan_links()
    business_name = request["business_name"]
    contact_name = request["contact_name"]
    days_left = request.get("days_until_trial_end")
    if days_left is None:
        timing = "Your NexaFlow trial is active."
    elif days_left > 0:
        timing = f"Your NexaFlow trial has about {days_left} day(s) left."
    else:
        timing = "Your NexaFlow trial has ended."

    return f"""Hi {contact_name}, quick check-in from NexaFlow.

{timing}

For {business_name}, NexaFlow helps keep enquiries, WhatsApp follow-ups, customer details, and lead status in one private inbox.

If you want to continue after the trial:
Starter: {links['starter']}
Pro: {links['pro']}

You can start with Starter and upgrade later when enquiry volume grows."""


def trial_conversion_whatsapp_url(request):
    if request["status"] not in {"trial_setup", "won"}:
        return None
    if request["status"] == "won":
        return None
    return whatsapp_url_for_phone(
        request["whatsapp_phone"],
        trial_conversion_whatsapp_message(request),
    )


def row_to_trial_request(row):
    age_days, days_until_trial_end, follow_up_priority, next_action = trial_request_followup_metadata(row)
    stage = trial_conversion_stage(row["status"], days_until_trial_end)
    request = {
        "id": row["id"],
        "business_name": row["business_name"],
        "contact_name": row["contact_name"],
        "contact_email": row["contact_email"],
        "whatsapp_phone": row["whatsapp_phone"],
        "business_type": row["business_type"],
        "city": row["city"],
        "monthly_enquiries": row["monthly_enquiries"],
        "lead_source": row["lead_source"] if "lead_source" in row.keys() else "",
        "campaign": row["campaign"] if "campaign" in row.keys() else "",
        "referrer": row["referrer"] if "referrer" in row.keys() else "",
        "message": row["message"],
        "pdpa_consent": bool(row["pdpa_consent"]),
        "consent_at": row["consent_at"],
        "status": row["status"],
        "internal_note": row["internal_note"],
        "notification_status": row["notification_status"],
        "notification_error": row["notification_error"],
        "trial_started_at": row["trial_started_at"] if "trial_started_at" in row.keys() else None,
        "trial_ends_at": row["trial_ends_at"] if "trial_ends_at" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "age_days": age_days,
        "days_until_trial_end": days_until_trial_end,
        "follow_up_priority": follow_up_priority,
        "next_action": next_action,
        "conversion_stage": stage,
        "conversion_next_action": trial_conversion_next_action(stage),
        "conversion_plan_links": trial_conversion_plan_links(),
        "whatsapp_url": sales_whatsapp_url(
            f"Hi {row['contact_name']}, this is NexaFlow. Thanks for requesting the 30-day trial for {row['business_name']}. I can help set up your enquiry inbox."
        ),
    }
    request["conversion_whatsapp_url"] = trial_conversion_whatsapp_url(request)
    return request


def trial_request_notification_email(request):
    return f"""New NexaFlow Enquiry trial request

Business:
{request['business_name']}

Contact:
{request['contact_name']}
{request.get('contact_email') or ''}
{request['whatsapp_phone']}

Business type:
{request.get('business_type') or ''}

City:
{request.get('city') or ''}

Monthly enquiries:
{request.get('monthly_enquiries') or ''}

Lead source:
{request.get('lead_source') or 'unknown'}

Campaign:
{request.get('campaign') or ''}

Referrer:
{request.get('referrer') or ''}

Message:
{request.get('message') or ''}

Admin follow-up:
Open /enquiry-admin and check Trial requests.
"""


def notify_trial_request(request):
    to_email = (
        os.getenv("NEXAFLOW_SALES_EMAIL")
        or os.getenv("SALES_EMAIL")
        or os.getenv("ADMIN_EMAIL")
        or os.getenv("FROM_EMAIL")
    )
    if not to_email:
        return {"status": "skipped", "provider": "none", "reason": "Sales email is not configured."}
    return send_resend_email(
        to_email,
        f"New trial request: {request['business_name']}",
        trial_request_notification_email(request),
    )


def create_trial_request(req):
    if not req.pdpa_consent:
        raise HTTPException(
            status_code=400,
            detail="Consent is required to contact you about the NexaFlow Enquiry trial.",
        )
    enforce_enquiry_rate_limit("trial-request", req.whatsapp_phone, limit=3, window_seconds=3600)

    timestamp = now_iso()
    with db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO trial_requests (
                business_name, contact_name, contact_email, whatsapp_phone, business_type,
                city, monthly_enquiries, lead_source, campaign, referrer, message, pdpa_consent, consent_at, status,
                internal_note, notification_status, notification_error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', '', ?, ?, ?, ?)
            """,
            (
                req.business_name.strip(),
                req.contact_name.strip(),
                normalize_email(req.contact_email),
                req.whatsapp_phone.strip(),
                req.business_type.strip() or "service business",
                req.city.strip(),
                req.monthly_enquiries.strip(),
                req.lead_source.strip() or "direct",
                req.campaign.strip(),
                req.referrer.strip(),
                req.message.strip(),
                1,
                timestamp,
                None,
                None,
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM trial_requests WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

    trial_request = row_to_trial_request(row)
    delivery = notify_trial_request(trial_request)
    status = delivery.get("status")
    error = delivery.get("error") or delivery.get("reason")
    with db_connection() as connection:
        connection.execute(
            "UPDATE trial_requests SET notification_status = ?, notification_error = ?, updated_at = ? WHERE id = ?",
            (status, error, now_iso(), trial_request["id"]),
        )
        updated = connection.execute(
            "SELECT * FROM trial_requests WHERE id = ?",
            (trial_request["id"],),
        ).fetchone()
    return row_to_trial_request(updated)


def list_trial_requests(status=None, limit=100):
    query = "SELECT * FROM trial_requests"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with db_connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [row_to_trial_request(row) for row in rows]


def trial_request_stats(requests):
    return {
        "total": len(requests),
        "new": sum(1 for item in requests if item["status"] == "new"),
        "contacted": sum(1 for item in requests if item["status"] == "contacted"),
        "trial_setup": sum(1 for item in requests if item["status"] == "trial_setup"),
        "won": sum(1 for item in requests if item["status"] == "won"),
        "lost": sum(1 for item in requests if item["status"] == "lost"),
        "urgent": sum(1 for item in requests if item["follow_up_priority"] == "high"),
        "active_trial": sum(1 for item in requests if item["conversion_stage"] == "active_trial"),
        "trial_ended": sum(1 for item in requests if item["conversion_stage"] == "trial_ended"),
        "conversion_due": sum(
            1
            for item in requests
            if item["conversion_stage"] in {"trial_ending_soon", "trial_ended"}
        ),
        "ending_soon": sum(
            1
            for item in requests
            if item["status"] == "trial_setup"
            and item["days_until_trial_end"] is not None
            and item["days_until_trial_end"] <= 7
        ),
    }


def update_trial_request_status(request_id, req):
    timestamp = now_iso()
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM trial_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Trial request not found")
        note = req.internal_note.strip() if req.internal_note is not None else (row["internal_note"] or "")
        connection.execute(
            "UPDATE trial_requests SET status = ?, internal_note = ?, updated_at = ? WHERE id = ?",
            (req.status, note, timestamp, request_id),
        )
        updated = connection.execute(
            "SELECT * FROM trial_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
    return row_to_trial_request(updated)


def trial_request_to_business_profile(request_id):
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM trial_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Trial request not found")

        base_slug = normalize_slug(row["business_name"])
        slug = base_slug
        if connection.execute("SELECT slug FROM business_profiles WHERE slug = ?", (slug,)).fetchone():
            slug = normalize_slug(f"{base_slug}-{request_id}")

    profile = upsert_business_profile(
        BusinessProfileRequest(
            slug=slug,
            business_name=row["business_name"],
            business_type=row["business_type"] or "service business",
            whatsapp_phone=row["whatsapp_phone"],
            contact_email=row["contact_email"],
            offer_summary=(
                row["message"]
                or f"{row['business_name']} customer enquiries and WhatsApp follow-up."
            ),
            reply_tone="friendly and professional",
            opening_hours="",
            status="active",
            rotate_access_key=True,
        )
    )

    trial_started_at = now_iso()
    trial_ends_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    with db_connection() as connection:
        connection.execute(
            "UPDATE trial_requests SET trial_started_at = ?, trial_ends_at = ?, updated_at = ? WHERE id = ?",
            (trial_started_at, trial_ends_at, now_iso(), request_id),
        )

    updated_request = update_trial_request_status(
        request_id,
        TrialRequestUpdate(
            status="trial_setup",
            internal_note=f"Business profile created: {profile['slug']}",
        ),
    )
    onboarding_message = merchant_onboarding_whatsapp_message(
        profile,
        profile["business_access_key"],
    )
    return {
        "trial_request": updated_request,
        "profile": profile,
        "onboarding_message": onboarding_message,
        "onboarding_whatsapp_url": whatsapp_url_for_phone(row["whatsapp_phone"], onboarding_message),
        "conversion_message": trial_conversion_whatsapp_message(updated_request),
        "conversion_whatsapp_url": trial_conversion_whatsapp_url(updated_request),
        "message": "Business profile and merchant inbox created from trial request.",
    }


def extract_bearer_token(authorization):
    if not authorization:
        return None
    prefix = "Bearer "
    if authorization.startswith(prefix):
        return authorization[len(prefix):].strip()
    return None


def get_business_profile_for_access_key(access_key):
    if not access_key:
        raise HTTPException(status_code=401, detail="Business access key required")
    digest = api_key_digest(access_key)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM business_profiles WHERE access_key_hash = ?",
            (digest,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid business access key")
    profile = row_to_business_profile(row)
    if profile["status"] != "active":
        raise HTTPException(status_code=403, detail="Business profile is paused")
    return profile


def business_guard(
    business_slug,
    business_key=None,
    x_business_key=None,
    authorization=None,
):
    supplied = x_business_key or extract_bearer_token(authorization) or (business_key if query_auth_enabled() else None)
    profile = get_business_profile_for_access_key(supplied)
    if normalize_slug(business_slug) != profile["slug"]:
        raise HTTPException(status_code=403, detail="Business key cannot access this inbox")
    return profile


def vehicle_sales_context(message="", business_type=""):
    text = f"{message} {business_type}".replace("_", " ").lower()
    english_keywords = [
        "car",
        "vehicle",
        "auto",
        "automotive",
        "dealer",
        "dealership",
        "motor",
        "used car",
        "recond",
        "trade in",
        "trade-in",
        "test drive",
        "showroom",
    ]
    cjk_keywords = ["车", "汽车", "二手车", "车商", "试驾", "看车"]
    return any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in english_keywords) or any(
        keyword in text for keyword in cjk_keywords
    )


def enquiry_followup_signals(message, business_type=""):
    text = message.lower()
    signals = []

    def add_signal(key, label, detail):
        if not any(item["key"] == key for item in signals):
            signals.append({"key": key, "label": label, "detail": detail})

    if any(keyword in text for keyword in ["loan", "finance", "financing", "bank", "月供", "贷款", "供车", "银行"]):
        add_signal("finance", "Finance / loan", "Ask target monthly payment, down payment, and loan readiness.")
    if any(keyword in text for keyword in ["monthly", "installment", "instalment", "payment", "月供", "供多少", "供期"]):
        add_signal("monthly_payment", "Monthly payment", "Clarify comfortable monthly range before pushing an appointment.")
    if any(keyword in text for keyword in ["budget", "down payment", "downpayment", "deposit", "首付", "头期", "预算"]):
        add_signal("budget", "Budget / down payment", "Ask budget range and cash upfront so the offer fits.")
    if any(keyword in text for keyword in ["compare", "cheaper", "best price", "discount", "other dealer", "same car", "比价", "哪里便宜", "最低", "别家"]):
        add_signal("comparison", "Price comparison", "Ask what offer, spec, and monthly payment they are comparing against.")
    if any(keyword in text for keyword in ["view car", "view today", "view tomorrow", "can view", "see car", "test drive", "showroom", "appointment", "来看车", "看车", "试驾", "预约"]):
        add_signal("appointment", "Viewing / appointment", "Confirm day, time, branch, and whether they need loan support.")
    if any(keyword in text for keyword in ["model", "variant", "year", "mileage", "color", "stock", "available", "unit", "spec", "车型", "车款", "年份", "公里", "颜色", "现车", "库存"]):
        add_signal("vehicle_fit", "Vehicle fit", "Confirm model, year, mileage, color, and must-have specs.")
    if any(keyword in text for keyword in ["income", "salary", "payslip", "epf", "commitment", "薪水", "收入", "粮单", "文件"]):
        add_signal("income_check", "Loan background", "Ask only the needed loan-readiness question and avoid collecting sensitive details too early.")
    if any(keyword in text for keyword in ["urgent", "asap", "today", "tonight", "now", "immediately", "紧急", "今天", "马上", "急"]):
        add_signal("time_sensitive", "Time sensitive", "Reply quickly and set a same-day follow-up if they go quiet.")

    if vehicle_sales_context(message, business_type) and not signals:
        add_signal("discovery", "Needs discovery", "Ask model, budget/monthly payment, loan need, and timeline.")

    return signals[:6]


def classify_enquiry(message, business_type=""):
    text = message.lower()
    urgent_keywords = ["urgent", "asap", "today", "tonight", "emergency", "now", "immediately", "紧急", "今天", "马上", "急"]
    finance_keywords = ["loan", "finance", "financing", "monthly", "installment", "instalment", "down payment", "downpayment", "bank", "income", "salary", "月供", "贷款", "首付", "头期", "供车", "银行", "收入"]
    comparison_keywords = ["compare", "cheaper", "best price", "discount", "other dealer", "same car", "比价", "哪里便宜", "最低", "别家"]
    quote_keywords = ["price", "quote", "quotation", "cost", "how much", "budget", "package", "多少钱", "价格", "报价", "费用"] + finance_keywords + comparison_keywords
    booking_keywords = ["book", "appointment", "schedule", "reserve", "available", "slot", "view car", "view today", "view tomorrow", "can view", "see car", "test drive", "showroom", "预约", "安排", "时间", "看车", "试驾"]
    inventory_keywords = ["stock", "available", "inventory", "in stock", "quantity", "model", "variant", "year", "mileage", "color", "spec", "库存", "现货", "数量", "车型", "车款", "年份", "公里", "颜色"]
    signals = enquiry_followup_signals(message, business_type)
    signal_keys = {item["key"] for item in signals}

    if any(keyword in text for keyword in urgent_keywords) or ("appointment" in signal_keys and "time_sensitive" in signal_keys):
        priority = "hot"
    elif any(keyword in text for keyword in quote_keywords + booking_keywords):
        priority = "warm"
    else:
        priority = "normal"

    if any(keyword in text for keyword in quote_keywords):
        intent = "quotation"
    elif any(keyword in text for keyword in booking_keywords):
        intent = "booking"
    elif any(keyword in text for keyword in inventory_keywords):
        intent = "inventory"
    else:
        intent = "general"

    if priority == "hot":
        estimated_value = "high"
    elif intent in {"quotation", "booking"} or signal_keys.intersection({"finance", "monthly_payment", "budget", "comparison", "appointment"}):
        estimated_value = "medium"
    else:
        estimated_value = "unknown"

    return {
        "intent": intent,
        "priority": priority,
        "estimated_value": estimated_value,
    }


def enquiry_followup_focus_label(priority: str | None):
    return {
        "hot": "answer now",
        "warm": "needs details",
        "normal": "ask next question",
    }.get(priority or "", priority or "unknown")


def enquiry_followup_guidance(message, business_type="", signals=None):
    signals = signals if signals is not None else enquiry_followup_signals(message, business_type)
    signal_keys = {item["key"] for item in signals}
    vehicle_context = vehicle_sales_context(message, business_type)

    if vehicle_context and signal_keys.intersection({"finance", "monthly_payment", "income_check"}):
        return {
            "stuck_point": "Monthly payment or loan readiness",
            "next_question": "Ask their comfortable monthly payment, down payment range, and whether they need loan support.",
            "follow_up_timing": "If they do not reply, follow up within 2 hours with one loan or monthly-payment question.",
        }
    if vehicle_context and "comparison" in signal_keys:
        return {
            "stuck_point": "Comparing price, spec, or monthly payment with another dealer",
            "next_question": "Ask which offer, spec, year, mileage, and monthly payment they are comparing against.",
            "follow_up_timing": "Follow up within 4 hours because comparison buyers go cold when the next step is unclear.",
        }
    if vehicle_context and "appointment" in signal_keys:
        return {
            "stuck_point": "Viewing appointment not confirmed",
            "next_question": "Confirm viewing day, time, branch, preferred model, and whether loan support is needed.",
            "follow_up_timing": "Follow up the same day if the viewing time is not confirmed.",
        }
    if vehicle_context and "vehicle_fit" in signal_keys:
        return {
            "stuck_point": "Still choosing the right car or spec",
            "next_question": "Ask model, year, mileage, budget or monthly range, and must-have specs.",
            "follow_up_timing": "Follow up within 1 business day with one matching option or one short discovery question.",
        }
    if vehicle_context and "budget" in signal_keys:
        return {
            "stuck_point": "Budget or down payment not clear",
            "next_question": "Ask budget range, down payment comfort, monthly payment target, and loan need.",
            "follow_up_timing": "Follow up within 1 business day with an option that fits the stated budget.",
        }
    if vehicle_context:
        return {
            "stuck_point": "Buyer has not shared enough buying background yet",
            "next_question": "Ask model, budget or monthly range, loan need, and viewing timeline.",
            "follow_up_timing": "Follow up within 1 business day if they do not answer the discovery question.",
        }
    if signal_keys:
        first_signal = signals[0]
        return {
            "stuck_point": first_signal["label"],
            "next_question": first_signal["detail"],
            "follow_up_timing": "Follow up within 1 business day if the customer does not reply.",
        }
    return {
        "stuck_point": "Need more context",
        "next_question": "Ask one clear question about what they need, budget, timing, and preferred next step.",
        "follow_up_timing": "Follow up within 2 business days if there is no reply.",
    }


def enquiry_reply_draft(name, business_type, message, classification, profile=None, signals=None):
    profile = profile or default_enquiry_profile()
    service_label = (profile.get("offer_summary") or business_type.replace("_", " ").strip() or "service").strip()
    business_name = profile.get("business_name") or "us"
    tone = profile.get("reply_tone") or "friendly and professional"
    hours = f" Our opening hours are {profile['opening_hours']}." if profile.get("opening_hours") else ""
    signals = signals if signals is not None else enquiry_followup_signals(message, business_type)
    signal_keys = {item["key"] for item in signals}
    if vehicle_sales_context(message, business_type):
        if signal_keys.intersection({"finance", "monthly_payment"}):
            next_step = "Can I check your target monthly payment, down payment range, and whether you need loan support?"
        elif "budget" in signal_keys:
            next_step = "Can you share your budget range, preferred monthly payment, and whether you plan to use loan?"
        elif "comparison" in signal_keys:
            next_step = "Can you share which offer or spec you are comparing against so I can advise the closest option clearly?"
        elif "appointment" in signal_keys:
            next_step = "Can I confirm which day you want to view the car, your preferred branch, and whether you need loan support?"
        elif "vehicle_fit" in signal_keys:
            next_step = "Can you confirm the model, year, budget or monthly range, and must-have specs?"
        else:
            next_step = "Can you share the model you want, budget or monthly range, loan need, and when you hope to view the car?"
    elif classification["intent"] == "quotation":
        next_step = "Can you share your preferred date, budget range, and any photos or details so we can prepare a more accurate quote?"
    elif classification["intent"] == "booking":
        next_step = "Can you share your preferred date and time so we can check availability?"
    elif classification["intent"] == "inventory":
        next_step = "Can you confirm the item/model and quantity you need so we can check availability?"
    else:
        next_step = "Can you share a few more details so we can recommend the best next step?"

    urgency = " This looks time-sensitive, so we will answer the immediate stuck point first." if classification["priority"] == "hot" else ""
    return (
        f"Hi {name}, thanks for contacting {business_name} about {service_label}.{urgency} "
        f"{next_step}{hours} We will reply in a {tone} way shortly."
    )


def enquiry_workflow_summary(name, message, classification, profile=None, signals=None, business_type=None):
    profile = profile or default_enquiry_profile()
    business_name = profile.get("business_name") or "the business"
    business_type = business_type or profile.get("business_type") or "general"
    clean_message = " ".join(message.strip().split())
    if len(clean_message) > 140:
        clean_message = f"{clean_message[:137]}..."

    intent_label = {
        "quotation": "quotation request",
        "booking": "booking request",
        "inventory": "stock or availability question",
        "general": "general enquiry",
    }.get(classification["intent"], "general enquiry")
    priority_label = {
        "hot": "time-sensitive follow-up",
        "warm": "needs-details follow-up",
        "normal": "next-question follow-up",
    }.get(classification["priority"], classification["priority"])

    auto_summary = (
        f"{name} sent a {priority_label} {intent_label} for {business_name}. "
        f"Message: {clean_message}"
    )
    signals = signals if signals is not None else enquiry_followup_signals(message, business_type)
    signal_keys = {item["key"] for item in signals}
    if signals:
        auto_summary += " Signals: " + ", ".join(item["label"] for item in signals) + "."

    if vehicle_sales_context(message, business_type):
        if signal_keys.intersection({"finance", "monthly_payment"}):
            next_action = "Clarify target monthly payment, down payment, and loan need before pushing for viewing."
            follow_up = "If no reply, follow up within 2 hours with one clear loan or monthly-payment question."
        elif "comparison" in signal_keys:
            next_action = "Ask what price, spec, or monthly payment they are comparing, then explain the strongest matching option."
            follow_up = "Follow up within 4 hours; comparison shoppers often go cold when the next step is unclear."
        elif "appointment" in signal_keys:
            next_action = "Confirm viewing time, branch, preferred model, and whether loan support is needed."
            follow_up = "Follow up the same day if the appointment is not confirmed."
        elif "vehicle_fit" in signal_keys:
            next_action = "Confirm model, year, mileage, budget or monthly range, and must-have specs."
            follow_up = "Follow up within 1 business day with one matching option or a short discovery question."
        elif "budget" in signal_keys:
            next_action = "Ask budget range, down payment comfort, and monthly payment target."
            follow_up = "Follow up within 1 business day with an option that fits the stated budget."
        else:
            next_action = "Do discovery first: ask model, budget/monthly range, loan need, and viewing timeline."
            follow_up = "Follow up within 1 business day if the customer does not answer the discovery question."
    elif classification["priority"] == "hot":
        next_action = "Reply as soon as possible, then mark the lead as Contacted."
        follow_up = "Follow up today if the customer has not replied."
    elif classification["intent"] == "quotation":
        next_action = "Ask for missing quote details and prepare a price estimate."
        follow_up = "Follow up within 1 business day."
    elif classification["intent"] == "booking":
        next_action = "Confirm preferred date, time, and availability."
        follow_up = "Follow up within 1 business day."
    elif classification["intent"] == "inventory":
        next_action = "Check stock or availability, then reply with options."
        follow_up = "Follow up within 1 business day."
    else:
        next_action = "Reply with the most relevant service information and ask one clear next question."
        follow_up = "Follow up within 2 business days."

    return {
        "auto_summary": auto_summary,
        "next_action": next_action,
        "follow_up_recommendation": follow_up,
    }


def enquiry_auto_follow_up_date(classification, profile=None):
    profile = profile or default_enquiry_profile()
    if not profile.get("auto_followup_enabled", True):
        return ""

    current = datetime.now(timezone.utc)
    if classification["priority"] == "hot":
        hours = int(profile.get("hot_followup_hours") or 0)
        follow_up_at = current if hours <= 24 else current + timedelta(hours=hours)
    else:
        days = int(profile.get("standard_followup_days") or 1)
        follow_up_at = current + timedelta(days=days)
    return follow_up_at.date().isoformat()


ALLOWED_ENQUIRY_INTENTS = {"quotation", "booking", "inventory", "general"}
ALLOWED_ENQUIRY_PRIORITIES = {"hot", "warm", "normal"}
ALLOWED_ENQUIRY_VALUES = {"high", "medium", "unknown"}
ENQUIRY_SIGNAL_LIBRARY = {
    "finance": ("Finance / loan", "Ask target monthly payment, down payment, and loan readiness."),
    "monthly_payment": ("Monthly payment", "Clarify comfortable monthly range before pushing an appointment."),
    "budget": ("Budget / down payment", "Ask budget range and cash upfront so the offer fits."),
    "comparison": ("Price comparison", "Ask what offer, spec, and monthly payment they are comparing against."),
    "appointment": ("Viewing / appointment", "Confirm day, time, branch, and whether they need loan support."),
    "vehicle_fit": ("Vehicle fit", "Confirm model, year, mileage, color, and must-have specs."),
    "income_check": ("Loan background", "Ask only the needed loan-readiness question and avoid collecting sensitive details too early."),
    "time_sensitive": ("Time sensitive", "Reply quickly and set a same-day follow-up if they go quiet."),
    "discovery": ("Needs discovery", "Ask model, budget/monthly payment, loan need, and timeline."),
}


def env_flag_enabled(name, default=False):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def enquiry_ai_analysis_enabled():
    return env_flag_enabled("NEXAFLOW_ENQUIRY_AI_ANALYSIS_ENABLED", default=False)


def rule_based_enquiry_analysis(name, business_type, message, profile=None):
    profile = profile or default_enquiry_profile()
    analysis_business_type = (business_type or profile.get("business_type") or "general").strip() or "general"
    classification = classify_enquiry(message, analysis_business_type)
    signals = enquiry_followup_signals(message, analysis_business_type)
    guidance = enquiry_followup_guidance(message, analysis_business_type, signals)
    workflow = enquiry_workflow_summary(
        name,
        message,
        classification,
        profile=profile,
        signals=signals,
        business_type=analysis_business_type,
    )
    follow_up_at = enquiry_auto_follow_up_date(classification, profile)
    reply_draft = enquiry_reply_draft(
        name,
        analysis_business_type,
        message,
        classification,
        profile=profile,
        signals=signals,
    )
    return {
        "business_type": analysis_business_type,
        "classification": classification,
        "signals": signals,
        "guidance": guidance,
        "workflow": workflow,
        "follow_up_at": follow_up_at,
        "reply_draft": reply_draft,
        "analysis_source": "rules_v1",
    }


def normalized_enquiry_signal(key, fallback_detail=""):
    normalized_key = re.sub(r"[^a-z0-9_]+", "_", str(key or "").strip().lower()).strip("_")
    label, detail = ENQUIRY_SIGNAL_LIBRARY.get(
        normalized_key,
        (normalized_key.replace("_", " ").title() if normalized_key else "Signal", fallback_detail or ""),
    )
    return {
        "key": normalized_key or "general",
        "label": label[:80],
        "detail": (detail or fallback_detail or "")[:220],
    }


def normalize_ai_enquiry_analysis(ai_payload, fallback):
    if not isinstance(ai_payload, dict):
        return None

    fallback_classification = fallback["classification"]
    classification = {
        "intent": ai_payload.get("intent") if ai_payload.get("intent") in ALLOWED_ENQUIRY_INTENTS else fallback_classification["intent"],
        "priority": ai_payload.get("priority") if ai_payload.get("priority") in ALLOWED_ENQUIRY_PRIORITIES else fallback_classification["priority"],
        "estimated_value": (
            ai_payload.get("estimated_value")
            if ai_payload.get("estimated_value") in ALLOWED_ENQUIRY_VALUES
            else fallback_classification["estimated_value"]
        ),
    }

    raw_signals = ai_payload.get("signals")
    signals = []
    if isinstance(raw_signals, list):
        for item in raw_signals[:6]:
            if isinstance(item, dict):
                signals.append(normalized_enquiry_signal(item.get("key"), item.get("detail", "")))
            elif isinstance(item, str):
                signals.append(normalized_enquiry_signal(item))
    signals = signals or fallback["signals"]

    guidance = {
        "stuck_point": str(ai_payload.get("stuck_point") or fallback["guidance"]["stuck_point"])[:180],
        "next_question": str(ai_payload.get("next_question") or fallback["guidance"]["next_question"])[:300],
        "follow_up_timing": str(ai_payload.get("follow_up_timing") or fallback["guidance"]["follow_up_timing"])[:300],
    }
    workflow = {
        "auto_summary": str(ai_payload.get("auto_summary") or fallback["workflow"]["auto_summary"])[:500],
        "next_action": str(ai_payload.get("next_action") or fallback["workflow"]["next_action"])[:300],
        "follow_up_recommendation": str(
            ai_payload.get("follow_up_recommendation") or fallback["workflow"]["follow_up_recommendation"]
        )[:300],
    }
    reply_draft = str(ai_payload.get("reply_draft") or fallback["reply_draft"])[:900]

    return {
        **fallback,
        "classification": classification,
        "signals": signals,
        "guidance": guidance,
        "workflow": workflow,
        "reply_draft": reply_draft,
        "analysis_source": f"ai:{os.getenv('NEXAFLOW_ENQUIRY_AI_MODEL_KEY', 'gpt-4o-mini')}",
    }


def enquiry_ai_analysis_prompt(name, business_type, message, profile, fallback):
    context = {
        "buyer_name": name,
        "business_name": profile.get("business_name"),
        "business_type": business_type,
        "offer_summary": profile.get("offer_summary"),
        "reply_tone": profile.get("reply_tone"),
        "opening_hours": profile.get("opening_hours"),
        "buyer_message": message,
        "rules_fallback": {
            "intent": fallback["classification"]["intent"],
            "priority": fallback["classification"]["priority"],
            "estimated_value": fallback["classification"]["estimated_value"],
            "signals": [item["key"] for item in fallback["signals"]],
            "stuck_point": fallback["guidance"]["stuck_point"],
            "next_question": fallback["guidance"]["next_question"],
        },
    }
    return (
        "Analyze this customer enquiry for a merchant inbox. Return compact JSON only. "
        "Use these exact enum values: intent=quotation|booking|inventory|general, "
        "priority=hot|warm|normal, estimated_value=high|medium|unknown. "
        "signals must use keys from finance, monthly_payment, budget, comparison, appointment, "
        "vehicle_fit, income_check, time_sensitive, discovery. "
        "Do not ask for sensitive documents or collect unnecessary personal data. "
        "Keep reply_draft concise and suitable for the merchant to send or adapt.\n"
        f"{json.dumps(context, ensure_ascii=False)}"
    )


def ai_enquiry_analysis(name, business_type, message, profile, fallback):
    if not enquiry_ai_analysis_enabled():
        return None
    model_key = os.getenv("NEXAFLOW_ENQUIRY_AI_MODEL_KEY", "gpt-4o-mini")
    catalog = model_catalog()
    model_config = catalog.get(model_key)
    if not model_config:
        return None
    provider_client = get_provider_client(model_config["provider"])
    if provider_client is None:
        return None

    try:
        response = provider_client.chat.completions.create(
            model=model_config["model"],
            messages=[
                {
                    "role": "developer",
                    "content": (
                        "You analyze merchant enquiries for NexaFlow. "
                        "Return only valid JSON with no markdown."
                    ),
                },
                {"role": "user", "content": enquiry_ai_analysis_prompt(name, business_type, message, profile, fallback)},
            ],
            response_format={"type": "json_object"},
            extra_headers={
                "HTTP-Referer": os.getenv("NEXAFLOW_SITE_URL", "http://localhost:8000"),
                "X-Title": os.getenv("NEXAFLOW_APP_NAME", "NexaFlow AI Gateway"),
            } if model_config["provider"] == "openrouter" else None,
        )
        content = response.choices[0].message.content
        parsed = json.loads(content)
        return normalize_ai_enquiry_analysis(parsed, fallback)
    except (APIStatusError, json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError):
        return None


def analyze_enquiry(name, business_type, message, profile=None):
    profile = profile or default_enquiry_profile()
    fallback = rule_based_enquiry_analysis(name, business_type, message, profile)
    ai_analysis = ai_enquiry_analysis(name, fallback["business_type"], message, profile, fallback)
    return ai_analysis or fallback


def copilot_contact_timing(priority, follow_up_at):
    if priority == "hot":
        return "reply_now"
    if follow_up_at:
        try:
            follow_up_date = datetime.fromisoformat(str(follow_up_at)[:10]).date()
            if follow_up_date <= datetime.now(timezone.utc).date():
                return "follow_up_today"
        except ValueError:
            pass
    if priority == "warm":
        return "reply_today"
    return "ask_next_question"


def copilot_decision_payload(analysis):
    classification = analysis["classification"]
    guidance = analysis["guidance"]
    workflow = analysis["workflow"]
    priority = classification["priority"]
    timing = copilot_contact_timing(priority, analysis.get("follow_up_at"))
    labels = {
        "reply_now": "Reply first",
        "follow_up_today": "Follow up today",
        "reply_today": "Reply today",
        "ask_next_question": "Ask next question",
    }
    return {
        "contact_timing": timing,
        "contact_label": labels.get(timing, "Review"),
        "recommended_action": workflow["next_action"],
        "recommended_question": guidance["next_question"],
        "send_policy": "human_review_required",
        "send_policy_label": "AI drafts only. Merchant must review before sending.",
    }


def merchant_copilot_analysis_response(profile, req):
    source = re.sub(r"[^a-z0-9_-]+", "-", (req.source or "manual").strip().lower()).strip("-") or "manual"
    buyer_name = manual_capture_buyer_name(source, req.name)
    analysis = analyze_enquiry(buyer_name, profile["business_type"], req.message, profile)
    classification = analysis["classification"]
    return {
        "status": "ok",
        "mode": "ai_copilot",
        "business_slug": profile["slug"],
        "business_name": profile["business_name"],
        "customer": {
            "name": buyer_name,
            "source": source,
            "campaign": (req.campaign or "").strip(),
        },
        "classification": classification,
        "priority": classification["priority"],
        "intent": classification["intent"],
        "estimated_value": classification["estimated_value"],
        "signals": analysis["signals"],
        "guidance": analysis["guidance"],
        "workflow": analysis["workflow"],
        "decision": copilot_decision_payload(analysis),
        "reply_draft": analysis["reply_draft"],
        "follow_up_at": analysis["follow_up_at"],
        "analysis_source": analysis["analysis_source"],
        "safety": {
            "stores_message": False,
            "auto_sends": False,
            "requires_human_review": True,
            "note": "This Copilot preview analyzes the message and prepares a draft only. It does not send a message.",
        },
    }


def whatsapp_reply_url(phone, reply_draft):
    digits = normalize_phone_for_whatsapp(phone)
    if not digits or len(digits) < 5:
        return None
    return f"https://wa.me/{digits}?text={quote(reply_draft)}"


def enforce_enquiry_rate_limit(business_slug, phone, limit=10, window_seconds=3600):
    identity = normalize_phone_for_whatsapp(phone) or "unknown"
    key = f"{business_slug}:{identity}"
    current_time = time()
    window_start = current_time - window_seconds
    timestamps = [ts for ts in enquiry_windows.get(key, []) if ts >= window_start]
    if len(timestamps) >= limit:
        raise HTTPException(
            status_code=429,
            detail="Too many enquiries submitted. Please try again later.",
        )
    timestamps.append(current_time)
    enquiry_windows[key] = timestamps


def write_data_audit_event(event_type, actor_type, entity_type, entity_id=None, business_slug=None, metadata=None, connection=None):
    safe_metadata = metadata or {}
    params = (
        event_type,
        actor_type,
        normalize_slug(business_slug) if business_slug else None,
        entity_type,
        str(entity_id) if entity_id is not None else None,
        json.dumps(safe_metadata, separators=(",", ":"), sort_keys=True),
        now_iso(),
    )
    statement = """
        INSERT INTO data_audit_events (
            event_type, actor_type, business_slug, entity_type, entity_id, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    if connection is not None:
        connection.execute(statement, params)
        return
    with db_connection() as audit_connection:
        audit_connection.execute(statement, params)


def list_data_audit_events(business_slug=None, event_type=None, limit=100):
    query = "SELECT * FROM data_audit_events"
    filters = []
    params = []
    if business_slug:
        filters.append("business_slug = ?")
        params.append(normalize_slug(business_slug))
    if event_type:
        filters.append("event_type = ?")
        params.append(event_type)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with db_connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "actor_type": row["actor_type"],
            "business_slug": row["business_slug"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def row_to_enquiry(row):
    stored_signals = []
    if "follow_up_signals_json" in row.keys() and row["follow_up_signals_json"]:
        try:
            parsed_signals = json.loads(row["follow_up_signals_json"])
            if isinstance(parsed_signals, list):
                stored_signals = [
                    item for item in parsed_signals
                    if isinstance(item, dict) and item.get("key") and item.get("label")
                ]
        except json.JSONDecodeError:
            stored_signals = []
    follow_up_signals = stored_signals or enquiry_followup_signals(row["message"], row["business_type"])
    follow_up_guidance = enquiry_followup_guidance(row["message"], row["business_type"], follow_up_signals)
    stored_stuck_point = row["stuck_point"] if "stuck_point" in row.keys() else ""
    stored_next_question = row["next_question"] if "next_question" in row.keys() else ""
    stored_follow_up_timing = row["follow_up_timing"] if "follow_up_timing" in row.keys() else ""
    return {
        "id": row["id"],
        "business_slug": row["business_slug"],
        "name": row["name"],
        "phone": row["phone"],
        "email": row["email"],
        "business_type": row["business_type"],
        "message": row["message"],
        "source": row["source"],
        "campaign": row["campaign"] if "campaign" in row.keys() else "",
        "referrer": row["referrer"] if "referrer" in row.keys() else "",
        "page_url": row["page_url"] if "page_url" in row.keys() else "",
        "intent": row["intent"],
        "priority": row["priority"],
        "estimated_value": row["estimated_value"],
        "follow_up_signals": follow_up_signals,
        "stuck_point": stored_stuck_point or follow_up_guidance["stuck_point"],
        "next_question": stored_next_question or follow_up_guidance["next_question"],
        "follow_up_timing": stored_follow_up_timing or follow_up_guidance["follow_up_timing"],
        "analysis_source": row["analysis_source"] if "analysis_source" in row.keys() else "rules_v1",
        "auto_summary": row["auto_summary"] if "auto_summary" in row.keys() else "",
        "next_action": row["next_action"] if "next_action" in row.keys() else "",
        "follow_up_recommendation": row["follow_up_recommendation"] if "follow_up_recommendation" in row.keys() else "",
        "reply_draft": row["reply_draft"],
        "whatsapp_url": row["whatsapp_url"],
        "merchant_notification_status": row["merchant_notification_status"],
        "merchant_notification_error": row["merchant_notification_error"],
        "internal_note": row["internal_note"] or "",
        "follow_up_at": row["follow_up_at"] or "",
        "deal_value": row["deal_value"],
        "pdpa_consent": bool(row["pdpa_consent"]),
        "consent_at": row["consent_at"],
        "consent_notice": row["consent_notice"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def merchant_enquiry_notification_email(profile, enquiry):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com").rstrip("/")
    inbox_url = f"{site_url}{profile['inbox_url']}"
    whatsapp_line = f"\nWhatsApp follow-up:\n{enquiry['whatsapp_url']}\n" if enquiry.get("whatsapp_url") else ""
    return f"""New enquiry for {profile['business_name']}

Lead:
{enquiry['name']}
{enquiry['phone']}
{enquiry.get('email') or ''}

Intent: {enquiry['intent']}
Follow-up focus: {enquiry_followup_focus_label(enquiry['priority'])}
Follow-up signal: {enquiry['estimated_value']}

Auto-organized summary:
{enquiry.get('auto_summary') or 'Not available'}

Recommended next action:
{enquiry.get('next_action') or 'Review this enquiry in your inbox.'}

Follow-up suggestion:
{enquiry.get('follow_up_recommendation') or 'Set a follow-up date after replying.'}

Auto follow-up date:
{enquiry.get('follow_up_at') or 'Not scheduled'}

Message:
{enquiry['message']}

Suggested reply:
{enquiry['reply_draft']}
{whatsapp_line}
Open your inbox:
{inbox_url}
"""


def notify_merchant_new_enquiry(profile, enquiry):
    if not profile.get("contact_email"):
        return {
            "status": "skipped",
            "reason": "Business profile has no contact_email.",
        }

    return send_resend_email(
        profile["contact_email"],
        f"New enquiry - {enquiry_followup_focus_label(enquiry['priority'])}: {enquiry['name']}",
        merchant_enquiry_notification_email(profile, enquiry),
    )


def merchant_followup_digest_email(profile, enquiries):
    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com").rstrip("/")
    inbox_url = f"{site_url}{profile['inbox_url']}"
    lines = [
        f"Due follow-ups for {profile['business_name']}",
        "",
        f"You have {len(enquiries)} lead(s) due for follow-up.",
        "",
    ]
    for index, enquiry in enumerate(enquiries, start=1):
        value = enquiry.get("deal_value")
        value_line = f"Recorded sale value: {value}" if value else "Recorded sale value: not set"
        whatsapp_line = f"WhatsApp: {enquiry['whatsapp_url']}" if enquiry.get("whatsapp_url") else "WhatsApp: not available"
        note_line = f"Note: {enquiry['internal_note']}" if enquiry.get("internal_note") else "Note: none"
        lines.extend(
            [
                f"{index}. {enquiry['name']} ({enquiry['phone']})",
                f"Follow-up date: {enquiry.get('follow_up_at') or 'not set'}",
                f"Intent: {enquiry['intent']} | Follow-up focus: {enquiry_followup_focus_label(enquiry['priority'])} | Status: {enquiry['status']}",
                value_line,
                note_line,
                whatsapp_line,
                "",
            ]
        )

    lines.extend(["Open your inbox:", inbox_url, ""])
    return "\n".join(lines)


def send_due_followup_digest(business_slug=None, dry_run=False):
    profiles = [get_business_profile(business_slug)] if business_slug else list_business_profiles()
    results = []
    sent = 0
    skipped = 0
    for profile in profiles:
        if profile["status"] != "active":
            skipped += 1
            results.append(
                {
                    "business_slug": profile["slug"],
                    "status": "skipped",
                    "reason": "Business profile is paused.",
                    "due_count": 0,
                }
            )
            continue

        due_enquiries = list_enquiry_records(
            business_slug=profile["slug"],
            follow_up="due",
            limit=100,
        )
        if not due_enquiries:
            skipped += 1
            results.append(
                {
                    "business_slug": profile["slug"],
                    "status": "skipped",
                    "reason": "No due follow-ups.",
                    "due_count": 0,
                }
            )
            continue

        if not profile.get("contact_email"):
            skipped += 1
            results.append(
                {
                    "business_slug": profile["slug"],
                    "status": "skipped",
                    "reason": "Business profile has no contact_email.",
                    "due_count": len(due_enquiries),
                }
            )
            continue

        if dry_run:
            delivery = {"status": "dry_run", "reason": "Email not sent."}
        else:
            delivery = send_resend_email(
                profile["contact_email"],
                f"{profile['business_name']}: {len(due_enquiries)} follow-up(s) due today",
                merchant_followup_digest_email(profile, due_enquiries),
            )
        if delivery.get("status") in {"sent", "dry_run"}:
            sent += 1
        else:
            skipped += 1
        results.append(
            {
                "business_slug": profile["slug"],
                "business_name": profile["business_name"],
                "contact_email": profile.get("contact_email"),
                "due_count": len(due_enquiries),
                "delivery": delivery,
            }
        )

    return {
        "processed": len(results),
        "sent": sent,
        "skipped": skipped,
        "dry_run": dry_run,
        "results": results,
    }


def cleanup_expired_enquiries(business_slug=None, dry_run=True, limit_per_business=500):
    profiles = [get_business_profile(business_slug)] if business_slug else list_business_profiles()
    results = []
    total_expired = 0
    total_deleted = 0
    total_expired_channel_messages = 0
    total_deleted_channel_messages = 0
    current = datetime.now(timezone.utc)

    for profile in profiles:
        retention_days = int(profile.get("data_retention_days") or 365)
        cutoff = current - timedelta(days=retention_days)
        cutoff_iso = cutoff.isoformat()
        cutoff_date = current.date().isoformat()

        with db_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, status, created_at
                FROM enquiries
                WHERE business_slug = ? AND created_at < ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (profile["slug"], cutoff_iso, limit_per_business),
            ).fetchall()
            ids = [row["id"] for row in rows]
            expired_count = len(ids)
            deleted_count = 0
            expired_channel_messages_count = 0
            deleted_channel_messages_count = 0

            message_where = "business_slug = ? AND retention_until IS NOT NULL AND retention_until != '' AND retention_until < ?"
            message_params = [profile["slug"], cutoff_date]
            if ids:
                message_placeholders = ",".join("?" for _ in ids)
                message_where = f"business_slug = ? AND ((retention_until IS NOT NULL AND retention_until != '' AND retention_until < ?) OR enquiry_id IN ({message_placeholders}))"
                message_params = [profile["slug"], cutoff_date, *ids]
            message_rows = connection.execute(
                f"""
                SELECT id
                FROM channel_messages
                WHERE {message_where}
                ORDER BY created_at ASC
                LIMIT ?
                """,
                [*message_params, limit_per_business],
            ).fetchall()
            message_ids = [row["id"] for row in message_rows]
            expired_channel_messages_count = len(message_ids)

            if message_ids and not dry_run:
                message_delete_placeholders = ",".join("?" for _ in message_ids)
                connection.execute(
                    f"DELETE FROM channel_messages WHERE business_slug = ? AND id IN ({message_delete_placeholders})",
                    [profile["slug"], *message_ids],
                )
                deleted_channel_messages_count = expired_channel_messages_count

            if ids and not dry_run:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM enquiries WHERE business_slug = ? AND id IN ({placeholders})",
                    [profile["slug"], *ids],
                )
                deleted_count = expired_count
                write_data_audit_event(
                    "enquiry.retention_deleted",
                    "automation",
                    "enquiry",
                    business_slug=profile["slug"],
                    metadata={
                        "count": deleted_count,
                        "channel_messages_deleted": deleted_channel_messages_count,
                        "retention_days": retention_days,
                        "cutoff": cutoff.date().isoformat(),
                    },
                    connection=connection,
                )

        total_expired += expired_count
        total_deleted += deleted_count
        total_expired_channel_messages += expired_channel_messages_count
        total_deleted_channel_messages += deleted_channel_messages_count
        results.append(
            {
                "business_slug": profile["slug"],
                "retention_days": retention_days,
                "cutoff": cutoff.date().isoformat(),
                "expired_count": expired_count,
                "deleted_count": deleted_count,
                "expired_channel_messages_count": expired_channel_messages_count,
                "deleted_channel_messages_count": deleted_channel_messages_count,
                "dry_run": dry_run,
            }
        )

    return {
        "dry_run": dry_run,
        "processed": len(results),
        "expired": total_expired,
        "deleted": total_deleted,
        "expired_channel_messages": total_expired_channel_messages,
        "deleted_channel_messages": total_deleted_channel_messages,
        "limit_per_business": limit_per_business,
        "results": results,
    }


def trial_automation_report(limit=100):
    requests = list_trial_requests(limit=limit)
    urgent = [
        {
            "id": item["id"],
            "business_name": item["business_name"],
            "contact_name": item["contact_name"],
            "contact_email": item["contact_email"],
            "whatsapp_phone": item["whatsapp_phone"],
            "status": item["status"],
            "follow_up_priority": item["follow_up_priority"],
            "days_until_trial_end": item["days_until_trial_end"],
            "next_action": item["next_action"],
        }
        for item in requests
        if item["follow_up_priority"] == "high"
    ]
    return {
        "stats": trial_request_stats(requests),
        "urgent_count": len(urgent),
        "urgent": urgent[:20],
    }


def deployment_automation_report():
    checks = deployment_check_items()
    failed = [item for item in checks if not item["ok"]]
    return {
        "ok": len(failed) == 0,
        "total": len(checks),
        "failed_count": len(failed),
        "failed": failed,
    }


def run_backend_automation_once(dry_run=True, include_backup=False):
    tasks = {}
    tasks["deployment_checks"] = deployment_automation_report()
    tasks["trial_requests"] = trial_automation_report(limit=200)
    tasks["merchant_health"] = merchant_health_report(limit=200)
    tasks["followup_digest"] = send_due_followup_digest(dry_run=dry_run)
    tasks["data_retention"] = cleanup_expired_enquiries(dry_run=dry_run)

    if include_backup:
        if dry_run:
            tasks["backup"] = {
                "status": "dry_run",
                "reason": "Backup not created while dry_run=true.",
                "scheduler": backup_scheduler_status(),
            }
        else:
            tasks["backup"] = {
                "status": "created",
                "backup": create_sqlite_backup(),
                "scheduler": backup_scheduler_status(),
            }
    else:
        tasks["backup"] = {
            "status": "skipped",
            "reason": "Set include_backup=true to create a backup during this automation run.",
            "scheduler": backup_scheduler_status(),
        }

    return {
        "ran_at": now_iso(),
        "dry_run": dry_run,
        "include_backup": include_backup,
        "tasks": tasks,
        "next_step": (
            "Review dry_run output, then schedule this endpoint with dry_run=false when email and backup settings are ready."
            if dry_run
            else "Automation run completed. Review task results for skipped or failed items."
        ),
    }


def public_enquiry_response(enquiry):
    return {
        "id": enquiry["id"],
        "business_slug": enquiry["business_slug"],
        "intent": enquiry["intent"],
        "priority": enquiry["priority"],
        "status": enquiry["status"],
        "created_at": enquiry["created_at"],
    }


def create_enquiry_record(
    req,
    actor_type="public_form",
    notify_merchant=True,
    consent_notice_override=None,
    create_whatsapp_reply=True,
):
    profile = get_business_profile(req.business_slug) if req.business_slug else default_enquiry_profile()
    if profile["status"] != "active":
        raise HTTPException(status_code=403, detail="This enquiry form is not accepting new enquiries.")
    if not req.pdpa_consent:
        raise HTTPException(
            status_code=400,
            detail="Consent is required to collect and use your contact details for enquiry follow-up.",
        )
    enforce_enquiry_rate_limit(profile["slug"], req.phone)

    business_type = profile.get("business_type") or req.business_type
    analysis = analyze_enquiry(req.name, business_type, req.message, profile)
    business_type = analysis["business_type"]
    classification = analysis["classification"]
    workflow = analysis["workflow"]
    follow_up_at = analysis["follow_up_at"]
    reply_draft = analysis["reply_draft"]
    # The inbox WhatsApp action is for the merchant to reply to the buyer.
    # Dealer/profile WhatsApp is used for merchant notifications and setup, not this follow-up link.
    reply_phone = req.phone if create_whatsapp_reply else ""
    whatsapp_url = whatsapp_reply_url(reply_phone, reply_draft)
    timestamp = now_iso()
    consent_notice = consent_notice_override or (
        "I agree that my name, contact details, and enquiry message may be collected, used, "
        "and disclosed to the business and NexaFlow service providers for enquiry follow-up, "
        "customer support, security, and record keeping."
    )

    with db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO enquiries (
                business_slug, name, phone, email, business_type, message, source, campaign,
                referrer, page_url, intent,
                priority, estimated_value, auto_summary, next_action, follow_up_recommendation,
                follow_up_signals_json, stuck_point, next_question, follow_up_timing,
                reply_draft, analysis_source, whatsapp_url, merchant_notification_status,
                merchant_notification_error, follow_up_at, pdpa_consent, consent_at, consent_notice,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile["slug"],
                req.name.strip(),
                req.phone.strip(),
                normalize_email(req.email),
                business_type.strip() or "general",
                req.message.strip(),
                req.source.strip() or "web",
                req.campaign.strip(),
                req.referrer.strip(),
                req.page_url.strip(),
                classification["intent"],
                classification["priority"],
                classification["estimated_value"],
                workflow["auto_summary"],
                workflow["next_action"],
                workflow["follow_up_recommendation"],
                json.dumps(analysis["signals"], separators=(",", ":"), ensure_ascii=False),
                analysis["guidance"]["stuck_point"],
                analysis["guidance"]["next_question"],
                analysis["guidance"]["follow_up_timing"],
                reply_draft,
                analysis["analysis_source"],
                whatsapp_url,
                "pending",
                None,
                follow_up_at,
                1,
                timestamp,
                consent_notice,
                "new",
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM enquiries WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

    enquiry = row_to_enquiry(row)
    write_data_audit_event(
        "enquiry.created",
        actor_type,
        "enquiry",
        enquiry["id"],
        business_slug=profile["slug"],
        metadata={
            "intent": enquiry["intent"],
            "priority": enquiry["priority"],
            "source": enquiry.get("source") or "web",
            "campaign": enquiry.get("campaign") or "",
            "pdpa_consent": enquiry["pdpa_consent"],
            "auto_followup_set": bool(enquiry.get("follow_up_at")),
        },
    )
    if notify_merchant:
        delivery = notify_merchant_new_enquiry(profile, enquiry)
    else:
        delivery = {
            "status": "not_required",
            "reason": "Merchant manually captured this enquiry in the private inbox.",
        }
    status = delivery.get("status", "unknown")
    error = delivery.get("reason") or delivery.get("provider_error")
    with db_connection() as connection:
        connection.execute(
            """
            UPDATE enquiries
            SET merchant_notification_status = ?, merchant_notification_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, error, now_iso(), enquiry["id"]),
        )
        updated = connection.execute(
            "SELECT * FROM enquiries WHERE id = ?",
            (enquiry["id"],),
        ).fetchone()

    return row_to_enquiry(updated)


MERCHANT_DEMO_REFERRER = "nexaflow-demo-pack"
MERCHANT_DEMO_ENQUIRIES = [
    {
        "name": "TikTok Civic Buyer",
        "phone": "demo-tiktok-001",
        "source": "tiktok",
        "campaign": "Civic TikTok DM",
        "buyer_request": "2018 Honda Civic, loan check, monthly below RM900",
        "buyer_request_zh": "2018 Honda Civic，想查贷款，月供低过 RM900",
        "message": "Saw your 2018 Honda Civic on TikTok. Can loan? Monthly below RM900, can view today?",
        "reply_draft_zh": "你好，感谢你询问这台 2018 Honda Civic。我们先帮你确认月供和贷款方向。可以告诉我你舒服的月供、首付范围，以及是否需要贷款吗？如果合适，我们再安排今天看车。",
        "status": "new",
        "internal_note": "Demo: high intent, asks loan, monthly payment, and viewing today.",
        "follow_up_days": 0,
        "deal_value": 68800,
    },
    {
        "name": "Instagram Vios Buyer",
        "phone": "demo-instagram-002",
        "source": "instagram",
        "campaign": "Vios Instagram DM",
        "buyer_request": "Toyota Vios, best price, lowest deposit",
        "buyer_request_zh": "Toyota Vios，问最低价和最低首付",
        "message": "Still got Toyota Vios? I saw another dealer cheaper. What is your best price and lowest deposit?",
        "reply_draft_zh": "你好，Toyota Vios 还有。为了给你比较准确的方案，可以发我你看到的年份、spec、mileage、价格和月供吗？我帮你对比最接近、最划算的选择。",
        "status": "contacted",
        "internal_note": "Demo: buyer is comparing dealers and needs price confidence.",
        "follow_up_days": 0,
        "deal_value": 49800,
    },
    {
        "name": "Facebook Alza Family",
        "phone": "demo-facebook-003",
        "source": "facebook",
        "campaign": "Alza Facebook Messenger",
        "buyer_request": "Family car, loan advice with variable income",
        "buyer_request_zh": "家庭车，收入不固定，需要贷款建议",
        "message": "Looking for family car. Need loan but income not fixed every month. Can you advise what documents first?",
        "reply_draft_zh": "你好，感谢你询问家庭车。贷款方面我们可以先简单了解收入类型、预算月供和首付范围，再建议需要准备什么文件。你大概想控制在多少月供？",
        "status": "new",
        "internal_note": "Demo: loan qualification is unclear; ask income and documents before pushing appointment.",
        "follow_up_days": 1,
        "deal_value": 72800,
    },
    {
        "name": "WhatsApp Mazda Viewer",
        "phone": "demo-whatsapp-004",
        "source": "whatsapp",
        "campaign": "Mazda WhatsApp",
        "buyer_request": "Mazda 3 viewing, location, 7-year monthly estimate",
        "buyer_request_zh": "Mazda 3，看车地点和 7 年月供预算",
        "message": "Can I view the Mazda 3 tomorrow morning? Send location and total monthly for 7 years please.",
        "reply_draft_zh": "你好，可以。我先帮你确认明早看车时间和地点。你方便几点到？同时我可以准备 7 年供期的月供估算，也想确认你是否需要贷款。",
        "status": "quoted",
        "internal_note": "Demo: appointment intent is clear; confirm time and showroom location.",
        "follow_up_days": 1,
        "deal_value": 83800,
    },
    {
        "name": "Xiaohongshu Bezza Browser",
        "phone": "demo-xhs-005",
        "source": "xiaohongshu",
        "campaign": "Bezza Xiaohongshu DM",
        "buyer_request": "Bezza around RM700 monthly, maybe next month",
        "buyer_request_zh": "Bezza，月供约 RM700，可能下个月才买",
        "message": "Hi, just checking. Any Bezza around RM700 monthly? Not urgent, maybe next month only.",
        "reply_draft_zh": "你好，有机会可以找 RM700 左右月供的 Bezza 方向。你比较接受多少首付？想新一点还是月供低一点？如果你是下个月看车，我可以先帮你留意合适单位。",
        "status": "new",
        "internal_note": "Demo: lower urgency; ask budget and timeline without sounding pushy.",
        "follow_up_days": 3,
        "deal_value": 35800,
    },
    {
        "name": "Referral Trade-in Buyer",
        "phone": "demo-referral-006",
        "source": "referral",
        "campaign": "Trade-in referral call",
        "buyer_request": "Trade in Myvi, upgrade to HR-V, around RM1200 monthly",
        "buyer_request_zh": "Myvi trade in，升级 HR-V，月供约 RM1200",
        "message": "My friend bought from you. I want to trade in my Myvi and upgrade to HR-V. Budget around RM1200 monthly, can call tonight?",
        "reply_draft_zh": "你好，感谢朋友介绍。HR-V 和 Myvi trade in 可以一起评估。你今晚方便几点通话？我会先确认你的 Myvi 年份、mileage、目标月供 RM1200 和贷款需求。",
        "status": "new",
        "internal_note": "Demo: referral with trade-in and monthly payment target.",
        "follow_up_days": 0,
        "deal_value": 118800,
    },
    {
        "name": "Direct Link Booked Buyer",
        "phone": "demo-direct-007",
        "source": "direct",
        "campaign": "Buyer enquiry link",
        "buyer_request": "Honda City Saturday viewing, loan estimate",
        "buyer_request_zh": "Honda City，星期六看车，准备贷款估算",
        "message": "Thanks, I already booked the Saturday viewing slot for the City. Please prepare the loan estimate.",
        "reply_draft_zh": "你好，星期六 Honda City 看车已经记录。我会先准备贷款估算。到时也可以一起确认首付、供期和你舒服的月供范围。",
        "status": "won",
        "internal_note": "Demo: booked buyer kept in the pipeline but not shown as urgent.",
        "follow_up_days": 2,
        "deal_value": 76800,
    },
]


DEMO_TEXT_ZH = {
    "Reply first": "优先回复",
    "Check details": "确认资料",
    "Ask next": "问下一句",
    "Unknown": "未知",
    "New": "新客户",
    "Contacted": "已联系",
    "Quoted": "已报价",
    "Booked": "已预约",
    "Direct": "直接询问",
    "Referral": "介绍",
    "Xiaohongshu": "小红书",
    "Demo pipeline stage": "Demo 阶段",
    "Monthly payment or loan readiness": "月供或贷款条件还没确认",
    "Comparing price, spec, or monthly payment with another dealer": "正在和其他车商比价或比月供",
    "Viewing appointment not confirmed": "看车时间还没确认",
    "Still choosing the right car or spec": "还在选车型或规格",
    "Budget or down payment not clear": "预算或首付还不清楚",
    "Buyer has not shared enough buying background yet": "买家背景资料还不够",
    "Need more context": "需要更多客户背景",
    "Clarify target monthly payment, down payment, and loan need before pushing for viewing.": "先确认目标月供、首付和贷款需求，再推进看车。",
    "Ask what price, spec, or monthly payment they are comparing, then explain the strongest matching option.": "先问他在比较什么价格、规格或月供，再解释最接近的选择。",
    "Confirm viewing time, branch, preferred model, and whether loan support is needed.": "确认看车时间、分行、车型，以及是否需要贷款协助。",
    "Confirm model, year, mileage, budget or monthly range, and must-have specs.": "确认车型、年份、mileage、预算或月供范围，以及必备规格。",
    "Ask budget range, down payment comfort, and monthly payment target.": "询问预算范围、可接受首付和目标月供。",
    "Do discovery first: ask model, budget/monthly range, loan need, and viewing timeline.": "先了解需求：车型、预算/月供、贷款需求和看车时间。",
    "If no reply, follow up within 2 hours with one clear loan or monthly-payment question.": "如果没回复，2 小时内用一个贷款或月供问题跟进。",
    "Follow up within 4 hours; comparison shoppers often go cold when the next step is unclear.": "4 小时内跟进；比价客户如果下一步不清楚，很容易冷掉。",
    "Follow up the same day if the appointment is not confirmed.": "如果看车时间没确认，当天再跟进。",
    "Follow up within 1 business day with one matching option or a short discovery question.": "1 个工作日内用一个合适选择或一个简短问题跟进。",
    "Follow up within 1 business day with an option that fits the stated budget.": "1 个工作日内用一个符合预算的选择跟进。",
    "Follow up within 1 business day if the customer does not answer the discovery question.": "如果客户没回答需求问题，1 个工作日内跟进。",
    "Follow up within 2 business days if there is no reply.": "如果没回复，2 个工作日内跟进。",
}


DEMO_SIGNAL_ZH = {
    "finance": {
        "label": "贷款 / 银行",
        "detail": "询问目标月供、首付和贷款准备情况。",
    },
    "monthly_payment": {
        "label": "月供",
        "detail": "先确认舒服的月供范围，再推进看车。",
    },
    "budget": {
        "label": "预算 / 首付",
        "detail": "询问预算范围和可接受首付，让方案更贴近客户。",
    },
    "comparison": {
        "label": "正在比价",
        "detail": "问清楚客户在比较的价格、规格和月供。",
    },
    "appointment": {
        "label": "看车 / 预约",
        "detail": "确认日期、时间、地点，以及是否需要贷款协助。",
    },
    "vehicle_fit": {
        "label": "车型匹配",
        "detail": "确认车型、年份、mileage、颜色和必要规格。",
    },
    "income_check": {
        "label": "贷款背景",
        "detail": "只问必要的贷款准备问题，不太早收敏感资料。",
    },
    "time_sensitive": {
        "label": "需要快回",
        "detail": "先快速回复，如果客户安静下来，当天再跟进。",
    },
    "discovery": {
        "label": "需要了解需求",
        "detail": "询问车型、预算/月供、贷款需求和看车时间。",
    },
}


def demo_zh_text(value):
    return DEMO_TEXT_ZH.get(value or "", value or "")


def demo_signal_zh(signal):
    mapped = DEMO_SIGNAL_ZH.get(signal.get("key") or "", {})
    return {
        "key": signal.get("key") or "",
        "label": mapped.get("label") or signal.get("label") or signal.get("key") or "",
        "detail": mapped.get("detail") or signal.get("detail") or "",
    }


def seed_merchant_demo_enquiries(profile):
    with db_connection() as connection:
        existing_count = connection.execute(
            "SELECT COUNT(*) AS count FROM enquiries WHERE business_slug = ? AND referrer = ?",
            (profile["slug"], MERCHANT_DEMO_REFERRER),
        ).fetchone()["count"]
    if existing_count:
        return {
            "status": "already_loaded",
            "created": 0,
            "existing": existing_count,
            "message": "Demo buyers are already loaded for this inbox.",
            "enquiries": list_enquiry_records(business_slug=profile["slug"], search="Demo:", limit=20),
        }

    today = datetime.now(timezone.utc).date()
    created = []
    consent_notice = (
        "Demo sample created by the merchant to evaluate NexaFlow. "
        "This is fictional buyer data for product demonstration only."
    )
    for sample in MERCHANT_DEMO_ENQUIRIES:
        enquiry_req = EnquiryCreateRequest(
            business_slug=profile["slug"],
            business_type=profile["business_type"],
            name=sample["name"],
            phone=sample["phone"],
            email=None,
            message=sample["message"],
            source=sample["source"],
            campaign=sample["campaign"],
            referrer=MERCHANT_DEMO_REFERRER,
            page_url=site_absolute_url(profile["inbox_url"]),
            pdpa_consent=True,
        )
        enquiry = create_enquiry_record(
            enquiry_req,
            actor_type="merchant_demo",
            notify_merchant=False,
            consent_notice_override=consent_notice,
            create_whatsapp_reply=False,
        )
        follow_up_at = (today + timedelta(days=sample["follow_up_days"])).isoformat()
        with db_connection() as connection:
            connection.execute(
                """
                UPDATE enquiries
                SET status = ?, internal_note = ?, follow_up_at = ?, deal_value = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    sample["status"],
                    sample["internal_note"],
                    follow_up_at,
                    sample["deal_value"],
                    now_iso(),
                    enquiry["id"],
                ),
            )
            updated = connection.execute("SELECT * FROM enquiries WHERE id = ?", (enquiry["id"],)).fetchone()
        created.append(row_to_enquiry(updated))

    return {
        "status": "created",
        "created": len(created),
        "existing": 0,
        "message": "Demo buyers loaded. Use them to show daily follow-up, loan, monthly payment, viewing, and comparison workflows.",
        "enquiries": created,
    }


def demo_dealer_enquiry_cards():
    profile = {
        "slug": "dealer-demo",
        "business_name": "NexaFlow Demo Dealer",
        "business_type": "used_car_dealer",
        "offer_summary": "used cars with loan support, trade-in advice, and viewing appointments",
        "reply_tone": "friendly, sales-focused, and clear",
        "opening_hours": "Mon-Sat 10am-7pm",
    }
    today = datetime.now(timezone.utc).date()
    cards = []
    for index, sample in enumerate(MERCHANT_DEMO_ENQUIRIES, start=1):
        classification = classify_enquiry(sample["message"], profile["business_type"])
        workflow = enquiry_workflow_summary(sample["name"], sample["message"], classification, profile)
        signals = enquiry_followup_signals(sample["message"], profile["business_type"])
        guidance = enquiry_followup_guidance(sample["message"], profile["business_type"], signals)
        cards.append(
            {
                "id": index,
                "name": sample["name"],
                "source": sample["source"],
                "campaign": sample["campaign"],
                "message": sample["message"],
                "status": sample["status"],
                "priority": classification["priority"],
                "intent": classification["intent"],
                "buyer_request": sample["buyer_request"],
                "buyer_request_zh": sample["buyer_request_zh"],
                "deal_value": sample["deal_value"],
                "follow_up_at": (today + timedelta(days=sample["follow_up_days"])).isoformat(),
                "stuck_point": guidance["stuck_point"],
                "stuck_point_zh": demo_zh_text(guidance["stuck_point"]),
                "next_question": guidance["next_question"],
                "follow_up_timing": guidance["follow_up_timing"],
                "follow_up_timing_zh": demo_zh_text(guidance["follow_up_timing"]),
                "reply_draft": enquiry_reply_draft(sample["name"], profile["business_type"], sample["message"], classification, profile),
                "reply_draft_zh": sample["reply_draft_zh"],
                "next_action": workflow["next_action"],
                "next_action_zh": demo_zh_text(workflow["next_action"]),
                "auto_summary": workflow["auto_summary"],
                "follow_up_recommendation": workflow["follow_up_recommendation"],
                "signals": signals,
                "signals_zh": [demo_signal_zh(signal) for signal in signals],
            }
        )
    return cards


def dealer_demo_page_body():
    cards = demo_dealer_enquiry_cards()
    actionable = [card for card in cards if card["status"] not in {"won", "lost", "spam"}]
    hot_count = sum(1 for card in actionable if card["priority"] == "hot")
    today_iso = datetime.now(timezone.utc).date().isoformat()
    due_count = sum(1 for card in actionable if card["follow_up_at"] <= today_iso)
    pipeline = {}
    sources = {}
    for card in cards:
        pipeline[card["status"]] = pipeline.get(card["status"], 0) + 1
        sources[card["source"]] = sources.get(card["source"], 0) + 1

    def lang_span(en, zh):
        return (
            f'<span data-lang="en">{escape_html(en)}</span>'
            f'<span data-lang="zh" class="lang-hidden">{escape_html(zh)}</span>'
        )

    source_badges = "".join(
        f'<span class="lead-badge">{lang_span(source.title(), demo_zh_text(source.title()))} · {count}</span>'
        for source, count in sorted(sources.items())
    )
    pipeline_cards = "".join(
        f"""
        <div class="stage-card">
            <strong>{lang_span(status.title(), demo_zh_text(status.title()))} <span>{count}</span></strong>
            <span>{lang_span("Demo pipeline stage", "Demo 阶段")}</span>
        </div>
        """
        for status, count in sorted(pipeline.items())
    )
    display_cards = actionable[:6]

    def demo_priority_label(value):
        return {
            "hot": "Reply first",
            "warm": "Check details",
            "normal": "Ask next",
        }.get(value or "", value or "Unknown")

    def demo_priority_label_zh(value):
        return demo_zh_text(demo_priority_label(value))

    def demo_status_label(value):
        return {
            "new": "New",
            "contacted": "Contacted",
            "quoted": "Quoted",
            "won": "Booked",
        }.get(value or "", (value or "Unknown").title())

    def demo_status_label_zh(value):
        return demo_zh_text(demo_status_label(value))

    def demo_source_label(value):
        return {
            "tiktok": "TikTok",
            "instagram": "Instagram",
            "facebook": "Facebook",
            "whatsapp": "WhatsApp",
            "xiaohongshu": "Xiaohongshu",
            "referral": "Referral",
            "direct": "Direct",
        }.get(value or "", (value or "Unknown").replace("_", " ").title())

    def demo_source_label_zh(value):
        return demo_zh_text(demo_source_label(value))

    lead_cards = "".join(
        f"""
        <button type="button" class="demo-queue-card {"active" if index == 0 else ""}" data-demo-id="{card["id"]}" onclick="selectDemoLead({card["id"]})">
            <span class="demo-queue-top">
                <span class="demo-queue-title">
                    <strong>{escape_html(card["name"])}</strong>
                    <span class="demo-queue-meta">{lang_span(demo_source_label(card["source"]), demo_source_label_zh(card["source"]))} · {lang_span(demo_status_label(card["status"]), demo_status_label_zh(card["status"]))}</span>
                </span>
                <span class="lead-badge {"hot" if card["priority"] == "hot" or card["follow_up_at"] <= today_iso else ""}">{lang_span(demo_priority_label(card["priority"]), demo_priority_label_zh(card["priority"]))}</span>
            </span>
            <span class="demo-queue-focus">{lang_span(card["stuck_point"], card["stuck_point_zh"])}</span>
        </button>
        """
        for index, card in enumerate(display_cards)
    )
    demo_cards_json = (
        json.dumps(display_cards)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )

    return f"""
    <style>
        @media (max-width: 520px) {{
            .nav-contact {{ display: none; }}
        }}
    </style>
    <section class="hero compact">
        <div>
            <div class="product-controls demo-controls">
                <div class="glass-control label-free demo-glass-control">
                    <div class="language-toggle" aria-label="Language" id="demoLangToggle">
                        <button type="button" class="active" onclick="setDealerDemoLang('en')" id="demoLangEn">EN</button>
                        <button type="button" onclick="setDealerDemoLang('zh')" id="demoLangZh">中文</button>
                    </div>
                </div>
            </div>
            <div class="eyebrow">{lang_span("Dealer Demo", "车商 Demo")}</div>
            <h1>{lang_span("A sales queue for used car enquiries.", "二手车询问的销售队列。")}</h1>
            <p class="lead">{lang_span("Open the queue, pick the first buyer, and copy the next reply your sales team can send.", "打开队列，从第一个买家开始，直接复制销售可以发的下一句回复。")}</p>
            <div class="actions">
                <a class="btn" href="/merchant-signup">{lang_span("Create Trial Inbox", "创建试用 Inbox")}</a>
                <a class="text-link" href="#demoNotes">{lang_span("Test with a real DM", "用真实私信测试")}</a>
            </div>
        </div>
    </section>
    <section class="form-card">
        <div class="section-head">
            <div>
                <h2>{lang_span("Today's buyer queue", "今天的买家队列")}</h2>
                <p>{lang_span("Start from the top. Demo data only. No message is sent.", "从最上面开始处理。这里是 Demo 资料，不会发送任何消息。")}</p>
            </div>
        </div>
        <div class="demo-kpi-strip" aria-label="Demo queue summary">
            <span><strong>{hot_count}</strong>{lang_span("Reply first", "优先回复")}</span>
            <span><strong>{due_count}</strong>{lang_span("Due today", "今天到期")}</span>
            <span><strong>{len(display_cards)}</strong>{lang_span("In queue", "队列中")}</span>
        </div>
        <div class="dealer-demo-board">
            <div class="demo-queue" aria-label="Demo buyer queue">{lead_cards}</div>
            <aside class="demo-detail-panel" id="demoLeadDetail" aria-live="polite"></aside>
        </div>
    </section>
    <script>
        const dealerDemoLeads = {demo_cards_json};
        let selectedDemoLeadId = dealerDemoLeads.length ? dealerDemoLeads[0].id : null;

        function escapeDemoHtml(value) {{
            return String(value ?? "").replace(/[&<>"']/g, char => ({{
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#039;"
            }}[char]));
        }}
        function currentDealerDemoLang() {{
            return localStorage.getItem("nexaflow_dealer_demo_lang") || "en";
        }}
        function demoText(en, zh) {{
            return currentDealerDemoLang() === "zh" ? zh : en;
        }}
        function demoLangSpan(en, zh) {{
            return `<span data-lang="en">${{escapeDemoHtml(en)}}</span><span data-lang="zh" class="lang-hidden">${{escapeDemoHtml(zh)}}</span>`;
        }}
        function setDealerDemoLang(lang, options = {{}}) {{
            document.querySelectorAll("[data-lang]").forEach(item => {{
                item.classList.toggle("lang-hidden", item.dataset.lang !== lang);
            }});
            const enButton = document.getElementById("demoLangEn");
            const zhButton = document.getElementById("demoLangZh");
            const toggle = document.getElementById("demoLangToggle");
            if (enButton && zhButton) {{
                enButton.classList.toggle("active", lang === "en");
                zhButton.classList.toggle("active", lang === "zh");
            }}
            if (toggle) {{
                toggle.classList.toggle("is-second", lang === "zh");
            }}
            if (options.persist !== false) {{
                localStorage.setItem("nexaflow_dealer_demo_lang", lang);
            }}
        }}
        function demoSourceLabel(value) {{
            const labels = {{
                tiktok: "TikTok",
                instagram: "Instagram",
                facebook: "Facebook",
                whatsapp: "WhatsApp",
                xiaohongshu: "Xiaohongshu",
                referral: "Referral",
                direct: "Direct"
            }};
            return labels[value] || String(value || "Unknown").replace(/_/g, " ");
        }}
        function demoSourceLabelZh(value) {{
            const labels = {{
                tiktok: "TikTok",
                instagram: "Instagram",
                facebook: "Facebook",
                whatsapp: "WhatsApp",
                xiaohongshu: "小红书",
                referral: "介绍",
                direct: "直接询问"
            }};
            return labels[value] || String(value || "未知").replace(/_/g, " ");
        }}
        function demoPriorityLabelEn(value) {{
            const labels = {{ hot: "Reply first", warm: "Check details", normal: "Ask next" }};
            return labels[value] || value || "Unknown";
        }}
        function demoPriorityLabelZh(value) {{
            const labels = {{ hot: "优先回复", warm: "确认资料", normal: "问下一句" }};
            return labels[value] || value || "未知";
        }}
        function demoStatusLabelEn(value) {{
            const labels = {{ new: "New", contacted: "Contacted", quoted: "Quoted", won: "Booked" }};
            return labels[value] || value || "Unknown";
        }}
        function demoStatusLabelZh(value) {{
            const labels = {{ new: "新客户", contacted: "已联系", quoted: "已报价", won: "已预约" }};
            return labels[value] || value || "未知";
        }}
        function renderDemoSignals(lead) {{
            const urgent = new Set(["finance", "monthly_payment", "budget", "comparison", "appointment", "time_sensitive"]);
            const signals = Array.isArray(lead.signals) && lead.signals.length
                ? lead.signals.slice(0, 5)
                : [{{ key: "discovery", label: "Needs discovery" }}];
            const signalsZh = Array.isArray(lead.signals_zh) && lead.signals_zh.length
                ? lead.signals_zh.slice(0, 5)
                : [{{ key: "discovery", label: "需要了解需求", detail: "询问车型、预算/月供、贷款需求和看车时间。" }}];
            return signals.map(signal => `
                <span class="lead-badge ${{urgent.has(signal.key) ? "hot" : ""}}" title="${{escapeDemoHtml(signal.detail || "")}}">
                    ${{demoLangSpan(signal.label || signal.key, (signalsZh.find(item => item.key === signal.key) || {{}}).label || signal.label || signal.key)}}
                </span>
            `).join("");
        }}
        function selectDemoLead(id) {{
            const lead = dealerDemoLeads.find(item => item.id === id) || dealerDemoLeads[0];
            if (!lead) {{
                return;
            }}
            selectedDemoLeadId = lead.id;
            document.querySelectorAll(".demo-queue-card").forEach(card => {{
                card.classList.toggle("active", Number(card.dataset.demoId) === lead.id);
            }});
            const panel = document.getElementById("demoLeadDetail");
            panel.innerHTML = `
                <div class="demo-detail-head">
                    <div>
                        <span class="demo-detail-kicker">${{demoLangSpan(demoSourceLabel(lead.source), demoSourceLabelZh(lead.source))}} · ${{escapeDemoHtml(lead.campaign)}}</span>
                        <h3>${{escapeDemoHtml(lead.name)}}</h3>
                    </div>
                    <span class="lead-badge ${{lead.priority === "hot" ? "hot" : ""}}">${{demoLangSpan(demoPriorityLabelEn(lead.priority), demoPriorityLabelZh(lead.priority))}}</span>
                </div>
                <div class="demo-copilot-card">
                    <div>
                        <strong>${{demoLangSpan("AI Copilot", "AI Copilot")}}</strong>
                        <span>${{demoLangSpan("Finds the stuck point and prepares the next reply. Salesperson reviews before sending.", "先找出客户卡点并准备下一句回复。销售确认后才发送。")}}</span>
                    </div>
                    <div class="lead-badges">
                        <span class="lead-badge ${{lead.priority === "hot" ? "hot" : ""}}">${{demoLangSpan(demoPriorityLabelEn(lead.priority), demoPriorityLabelZh(lead.priority))}}</span>
                        <span class="lead-badge">${{demoLangSpan("Human approval", "人工确认")}}</span>
                    </div>
                </div>
                <div class="demo-reply-box primary">
                    <strong>${{demoLangSpan("Next reply to send", "建议发送的下一句")}}</strong>
                    <p>${{demoLangSpan(lead.reply_draft, lead.reply_draft_zh || lead.reply_draft)}}</p>
                </div>
                <div class="demo-panel-actions">
                    <button type="button" class="btn" onclick="copyDemoReply()">${{demoLangSpan("Copy reply", "复制回复")}}</button>
                    <span id="demoPanelStatus">${{demoLangSpan("Demo only: no message is sent.", "Demo 不会发送任何消息。")}}</span>
                </div>
                <div class="demo-detail-grid">
                    <div class="demo-detail-box">
                        <strong>${{demoLangSpan("Buyer wants", "客户想要")}}</strong>
                        <span>${{demoLangSpan(lead.buyer_request || lead.message, lead.buyer_request_zh || lead.buyer_request || lead.message)}}</span>
                    </div>
                    <div class="demo-detail-box">
                        <strong>${{demoLangSpan("Stuck on", "客户卡点")}}</strong>
                        <span>${{demoLangSpan(lead.stuck_point, lead.stuck_point_zh || lead.stuck_point)}}</span>
                    </div>
                </div>
                <details class="demo-inline-details">
                    <summary>${{demoLangSpan("More buyer details", "更多买家资料")}}</summary>
                    <div class="lead-badges">${{renderDemoSignals(lead)}}</div>
                    <div class="demo-detail-grid demo-extra-grid">
                        <div class="demo-detail-box">
                            <strong>${{demoLangSpan("Next move", "下一步")}}</strong>
                            <span>${{demoLangSpan(lead.next_action, lead.next_action_zh || lead.next_action)}}</span>
                        </div>
                        <div class="demo-detail-box">
                            <strong>${{demoLangSpan("When to chase", "什么时候再追")}}</strong>
                            <span>${{demoLangSpan(lead.follow_up_timing || lead.follow_up_recommendation, lead.follow_up_timing_zh || lead.follow_up_timing || lead.follow_up_recommendation)}}</span>
                        </div>
                    </div>
                    <div class="demo-message-box">
                        <strong>${{demoLangSpan("Buyer message", "买家原始留言")}}</strong>
                        <p>${{escapeDemoHtml(lead.message)}}</p>
                    </div>
                </details>
            `;
            setDealerDemoLang(currentDealerDemoLang(), {{ persist: false }});
        }}
        function copyDemoReply() {{
            const lead = dealerDemoLeads.find(item => item.id === selectedDemoLeadId);
            const status = document.getElementById("demoPanelStatus");
            if (!lead || !status) {{
                return;
            }}
            const draft = currentDealerDemoLang() === "zh" && lead.reply_draft_zh ? lead.reply_draft_zh : lead.reply_draft;
            if (navigator.clipboard && draft) {{
                navigator.clipboard.writeText(draft)
                    .then(() => {{ status.textContent = demoText("Reply copied for demo.", "回复已复制。"); }})
                    .catch(() => {{ status.textContent = demoText("Copy manually from the suggested reply.", "请手动复制上面的建议回复。"); }});
            }} else {{
                status.textContent = demoText("Copy manually from the suggested reply.", "请手动复制上面的建议回复。");
            }}
        }}
        setDealerDemoLang(currentDealerDemoLang(), {{ persist: false }});
        selectDemoLead(selectedDemoLeadId);
    </script>
    <details class="form-card" id="demoNotes">
        <summary>{lang_span("Demo notes", "Demo 说明")}</summary>
        <section class="demo-notes-grid">
            <div>
                <h3>{lang_span("Open today's queue", "打开今天队列")}</h3>
                <p>{lang_span("See who should be replied first, who needs a loan check, and who is waiting for a viewing confirmation.", "先看谁要优先回复、谁要查贷款、谁在等看车确认。")}</p>
                <div class="lead-badges">
                    <span class="lead-badge hot">{lang_span("Reply first", "优先回复")} · {hot_count}</span>
                    <span class="lead-badge hot">{lang_span("Due today", "今天到期")} · {due_count}</span>
                    <span class="lead-badge">{lang_span("Demo buyers", "Demo 买家")} · {len(cards)}</span>
                </div>
            </div>
            <div>
                <h3>{lang_span("All sources, one list", "所有来源，一个列表")}</h3>
                <p>{lang_span("WhatsApp, Instagram, Facebook, TikTok, Xiaohongshu, referral, and direct link enquiries can be worked from the same sales queue.", "WhatsApp、Instagram、Facebook、TikTok、小红书、介绍和 direct link 来的客户，都可以在同一个销售队列处理。")}</p>
                <div class="lead-badges">{source_badges}</div>
            </div>
        </section>
    </details>
    <details class="form-card">
        <summary>{lang_span("Buyer progress", "买家进度")}</summary>
        <section class="pipeline-board">{pipeline_cards}</section>
    </details>
    """


def list_enquiry_records(
    status=None,
    business_slug=None,
    limit=50,
    priority=None,
    intent=None,
    source=None,
    search=None,
    follow_up=None,
):
    query = "SELECT * FROM enquiries"
    params = []
    filters = []
    if status:
        filters.append("status = ?")
        params.append(status)
    if priority:
        filters.append("priority = ?")
        params.append(priority)
    if intent:
        filters.append("intent = ?")
        params.append(intent)
    if source:
        filters.append("lower(source) = lower(?)")
        params.append(source.strip())
    if business_slug:
        filters.append("business_slug = ?")
        params.append(normalize_slug(business_slug))
    if search:
        filters.append("(name LIKE ? OR phone LIKE ? OR email LIKE ? OR message LIKE ? OR internal_note LIKE ?)")
        term = f"%{search.strip()}%"
        params.extend([term, term, term, term, term])
    if follow_up == "scheduled":
        filters.append("follow_up_at IS NOT NULL AND follow_up_at != ''")
    elif follow_up == "due":
        filters.append(
            "follow_up_at IS NOT NULL AND follow_up_at != '' "
            "AND date(follow_up_at) <= date('now') "
            "AND status NOT IN ('won', 'lost', 'spam')"
        )
    elif follow_up == "none":
        filters.append("(follow_up_at IS NULL OR follow_up_at = '')")
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with db_connection() as connection:
        rows = connection.execute(query, params).fetchall()

    return [row_to_enquiry(row) for row in rows]


def enquiries_to_csv(enquiries):
    output = StringIO()
    fieldnames = [
        "created_at",
        "name",
        "phone",
        "email",
        "intent",
        "priority",
        "status",
        "message",
        "reply_draft",
        "internal_note",
        "follow_up_at",
        "deal_value",
        "source",
        "campaign",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for enquiry in enquiries:
        writer.writerow({key: csv_safe_cell(enquiry.get(key, "")) for key in fieldnames})
    return output.getvalue()


def csv_safe_cell(value):
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def enquiry_stats(business_slug=None):
    query = "SELECT status, priority, intent, source, deal_value, follow_up_at FROM enquiries"
    params = []
    if business_slug:
        query += " WHERE business_slug = ?"
        params.append(normalize_slug(business_slug))
    with db_connection() as connection:
        rows = connection.execute(query, params).fetchall()

    stats = {
        "total": len(rows),
        "by_status": {},
        "by_priority": {},
        "by_intent": {},
        "by_source": {},
        "pipeline_value": 0,
        "won_value": 0,
        "scheduled_followups": 0,
        "due_followups": 0,
    }
    for row in rows:
        stats["by_status"][row["status"]] = stats["by_status"].get(row["status"], 0) + 1
        stats["by_priority"][row["priority"]] = stats["by_priority"].get(row["priority"], 0) + 1
        stats["by_intent"][row["intent"]] = stats["by_intent"].get(row["intent"], 0) + 1
        source = row["source"] or "unknown"
        stats["by_source"][source] = stats["by_source"].get(source, 0) + 1
        value = float(row["deal_value"] or 0)
        if row["status"] != "lost":
            stats["pipeline_value"] += value
        if row["status"] == "won":
            stats["won_value"] += value
        follow_up_at = row["follow_up_at"] or ""
        if follow_up_at:
            stats["scheduled_followups"] += 1
            if row["status"] not in {"won", "lost", "spam"}:
                try:
                    due_date = datetime.fromisoformat(follow_up_at[:10]).date()
                    if due_date <= datetime.now(timezone.utc).date():
                        stats["due_followups"] += 1
                except ValueError:
                    pass

    return stats


def business_profile_onboarding_status(profile):
    stats = enquiry_stats(business_slug=profile["slug"])
    checks = [
        {
            "key": "business_profile",
            "label": "Business profile",
            "done": bool(profile.get("business_name") and profile.get("business_type") and profile.get("offer_summary")),
            "detail": "Business name, service type, and offer summary are filled.",
        },
        {
            "key": "whatsapp",
            "label": "WhatsApp number",
            "done": bool(normalize_phone_for_whatsapp(profile.get("whatsapp_phone") or "")),
            "detail": "Merchant WhatsApp number is ready for follow-up links.",
        },
        {
            "key": "notification_email",
            "label": "Notification email",
            "done": bool(profile.get("contact_email")),
            "detail": "Merchant can receive new enquiry and follow-up email alerts.",
        },
        {
            "key": "business_access_key",
            "label": "Private inbox access",
            "done": bool(profile.get("access_key_prefix")),
            "detail": "Business access key has been generated.",
        },
        {
            "key": "first_enquiry",
            "label": "First enquiry received",
            "done": stats["total"] > 0,
            "detail": "At least one test or customer enquiry has reached the inbox.",
        },
        {
            "key": "followup_ready",
            "label": "Follow-up workflow",
            "done": stats["scheduled_followups"] > 0 or stats["due_followups"] > 0 or stats["total"] > 0,
            "detail": "Leads can be tracked with statuses, notes, and follow-up dates.",
        },
    ]
    completed = sum(1 for item in checks if item["done"])
    total = len(checks)
    required_done = all(
        item["done"]
        for item in checks
        if item["key"] in {"business_profile", "whatsapp", "notification_email", "business_access_key"}
    )
    if required_done and stats["total"] > 0:
        status = "live"
        next_action = "Share the enquiry link with real customers and monitor follow-ups."
    elif required_done:
        status = "ready_for_test"
        next_action = "Submit one test enquiry before sending the link to customers."
    else:
        status = "needs_setup"
        next_action = "Complete the missing setup items before promotion."

    missing = [item for item in checks if not item["done"]]
    return {
        "status": status,
        "completed": completed,
        "total": total,
        "percent": round((completed / total) * 100) if total else 0,
        "next_action": next_action,
        "missing_keys": [item["key"] for item in missing],
        "checks": checks,
    }


def merchant_health_report(limit=100, business_slug=None):
    merchants = []
    profiles = [get_business_profile(business_slug)] if business_slug else list_business_profiles()
    for profile in profiles:
        stats = enquiry_stats(business_slug=profile["slug"])
        onboarding = business_profile_onboarding_status(profile)
        due_followups = int(stats.get("due_followups") or 0)
        new_leads = int((stats.get("by_status") or {}).get("new") or 0)
        total_leads = int(stats.get("total") or 0)
        score = 100
        score -= max(0, onboarding["total"] - onboarding["completed"]) * 12
        score -= min(due_followups * 12, 36)
        score -= min(new_leads * 6, 24)
        if profile["status"] != "active":
            score -= 30
        score = max(0, min(100, score))

        if profile["status"] != "active":
            risk_level = "high"
            next_action = "Merchant profile is paused. Confirm whether to restore or keep disabled."
        elif onboarding["status"] == "needs_setup":
            risk_level = "high"
            next_action = onboarding["next_action"]
        elif due_followups > 0:
            risk_level = "high"
            next_action = f"{due_followups} follow-up(s) are due. Contact these leads before they go cold."
        elif onboarding["status"] == "ready_for_test":
            risk_level = "medium"
            next_action = "Ask the merchant to submit one test enquiry, then share the link with real customers."
        elif new_leads > 0:
            risk_level = "medium"
            next_action = f"{new_leads} new lead(s) need first reply."
        else:
            risk_level = "low"
            next_action = "Merchant is operational. Review pipeline value and conversion weekly."

        merchants.append(
            {
                "business_slug": profile["slug"],
                "business_name": profile["business_name"],
                "status": profile["status"],
                "health_score": score,
                "risk_level": risk_level,
                "onboarding_status": onboarding["status"],
                "onboarding_percent": onboarding["percent"],
                "missing_setup": onboarding["missing_keys"],
                "total_leads": total_leads,
                "new_leads": new_leads,
                "due_followups": due_followups,
                "pipeline_value": stats.get("pipeline_value") or 0,
                "won_value": stats.get("won_value") or 0,
                "next_action": next_action,
            }
        )

    severity_order = {"high": 0, "medium": 1, "low": 2}
    merchants = sorted(
        merchants,
        key=lambda item: (
            severity_order.get(item["risk_level"], 9),
            item["health_score"],
            item["business_name"].lower(),
        ),
    )
    visible_merchants = merchants[:limit]
    return {
        "generated_at": now_iso(),
        "summary": {
            "total": len(visible_merchants),
            "high": sum(1 for item in visible_merchants if item["risk_level"] == "high"),
            "medium": sum(1 for item in visible_merchants if item["risk_level"] == "medium"),
            "low": sum(1 for item in visible_merchants if item["risk_level"] == "low"),
            "needs_setup": sum(1 for item in visible_merchants if item["onboarding_status"] == "needs_setup"),
            "ready_for_test": sum(1 for item in visible_merchants if item["onboarding_status"] == "ready_for_test"),
            "live": sum(1 for item in visible_merchants if item["onboarding_status"] == "live"),
            "due_followups": sum(item["due_followups"] for item in visible_merchants),
            "new_leads": sum(item["new_leads"] for item in visible_merchants),
        },
        "merchants": visible_merchants,
    }


def stripe_secret_key():
    return (os.getenv("STRIPE_SECRET_KEY") or "").strip()


def stripe_billing_portal_configured():
    return bool(stripe_secret_key()) or bool(os.getenv("STRIPE_BILLING_PORTAL_TEST_URL"))


def create_stripe_billing_portal_session(stripe_customer_id, return_url):
    test_url = os.getenv("STRIPE_BILLING_PORTAL_TEST_URL")
    if test_url:
        return {
            "id": "bps_test",
            "url": test_url,
            "livemode": False,
            "test_mode": True,
        }

    secret = stripe_secret_key()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="Stripe billing portal is not configured. Set STRIPE_SECRET_KEY in Railway.",
        )

    payload = urlencode(
        {
            "customer": stripe_customer_id,
            "return_url": return_url,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.stripe.com/v1/billing_portal/sessions",
        data=payload,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "NexaFlowGateway/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Stripe returned HTTP {exc.code} while creating a billing portal session.",
                "provider_error": error_body[:500],
            },
        ) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach Stripe billing portal API: {exc.reason}",
        ) from exc

    session = json.loads(response_body)
    if not session.get("url"):
        raise HTTPException(status_code=502, detail="Stripe billing portal response did not include a URL.")

    return {
        "id": session.get("id"),
        "url": session["url"],
        "livemode": session.get("livemode"),
        "test_mode": False,
    }


def get_provider_client(provider_key):
    provider = PROVIDERS[provider_key]
    api_key = (os.getenv(provider["api_key_env"]) or "").strip()

    if not api_key:
        return None

    if provider["base_url"]:
        return OpenAI(api_key=api_key, base_url=provider["base_url"])

    return OpenAI(api_key=api_key)


def model_catalog():
    override = os.getenv("MODEL_CATALOG_JSON")

    if not override:
        return MODEL_CATALOG

    try:
        custom_models = json.loads(override)
    except json.JSONDecodeError:
        return MODEL_CATALOG

    return {**MODEL_CATALOG, **custom_models}


def ensure_plan(plan):
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {plan}")


def enforce_rate_limit(client_id, limit):
    current = time()
    window_start = current - 60
    timestamps = [ts for ts in request_windows.get(client_id, []) if ts >= window_start]

    if len(timestamps) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    timestamps.append(current)
    request_windows[client_id] = timestamps


def parse_log_timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)

    return timestamp


def usage_window_stats(client_id, window_seconds=86400):
    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
    stats = {
        "requests": 0,
        "credits_spent": 0,
    }

    for log in load_logs():
        if log.get("client_id") != client_id:
            continue

        timestamp = parse_log_timestamp(log.get("timestamp"))
        if timestamp is None:
            continue

        if timestamp.timestamp() >= cutoff:
            stats["requests"] += 1
            stats["credits_spent"] += log.get("credits_spent", 0)

    return stats


def enforce_usage_guard(client_id, client, plan, req):
    estimate = estimate_credits_for_request(req.message, req.task)
    window = usage_window_stats(client_id)

    if estimate["credits_spent"] > plan["max_request_credits"]:
        raise HTTPException(
            status_code=413,
            detail={
                "message": "Request is too large for this plan.",
                "estimated_credits": estimate["credits_spent"],
                "max_request_credits": plan["max_request_credits"],
            },
        )

    if estimate["credits_spent"] > client["credits"]:
        raise HTTPException(
            status_code=402,
            detail={
                "message": "Insufficient credits for the estimated request size.",
                "estimated_credits": estimate["credits_spent"],
                "remaining_credits": client["credits"],
            },
        )

    if window["requests"] >= plan["daily_request_limit"]:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily request limit exceeded.",
                "daily_request_limit": plan["daily_request_limit"],
            },
        )

    if window["credits_spent"] + estimate["credits_spent"] > plan["daily_credit_limit"]:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily credit limit exceeded.",
                "daily_credit_limit": plan["daily_credit_limit"],
                "credits_used_today": window["credits_spent"],
                "estimated_credits": estimate["credits_spent"],
            },
        )

    return {
        "estimate": estimate,
        "daily_usage": window,
    }


def model_is_allowed(model_key, plan, task):
    catalog = model_catalog()
    model = catalog[model_key]
    return model["tier"] in plan["allowed_tiers"] and task in model["tasks"]


def estimate_tokens_for_message(message):
    return max(1, ceil(len(message) / 4))


def estimate_completion_tokens(task, prompt_tokens):
    task_multiplier = {
        "classification": 0.15,
        "support": 0.7,
        "summary": 0.4,
        "reasoning": 1.2,
        "chat": 0.8,
    }.get(task, 0.8)

    return max(32, ceil(prompt_tokens * task_multiplier))


def estimate_credits_for_request(message, task):
    prompt_tokens = estimate_tokens_for_message(message)
    completion_tokens = estimate_completion_tokens(task, prompt_tokens)
    weighted_tokens = prompt_tokens + (completion_tokens * OUTPUT_TOKEN_WEIGHT)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "credits_spent": max(1, ceil(weighted_tokens / CREDIT_UNIT_TOKENS)),
    }


def score_model_for_route(model, plan, estimate):
    estimated_cost_usd = calculate_provider_cost_usd(
        model,
        estimate["prompt_tokens"],
        estimate["completion_tokens"],
    )
    estimated_revenue_usd = calculate_credit_revenue_usd(plan, estimate["credits_spent"])

    gross_margin_usd = round(estimated_revenue_usd - estimated_cost_usd, 8)
    gross_margin_ratio = (
        round(gross_margin_usd / estimated_revenue_usd, 6)
        if estimated_revenue_usd > 0
        else 0
    )

    return {
        "estimated_provider_cost_usd": estimated_cost_usd,
        "estimated_revenue_usd": estimated_revenue_usd,
        "estimated_gross_margin_usd": gross_margin_usd,
        "estimated_gross_margin_ratio": gross_margin_ratio,
    }


def profit_guard_enabled():
    return os.getenv("ALLOW_UNPROFITABLE_MODEL_ROUTES", "").lower() not in {"1", "true", "yes"}


def route_meets_margin(plan, score):
    minimum = plan.get("min_gross_margin_ratio", 0)
    return score["estimated_gross_margin_ratio"] >= minimum


def route_margin_detail(plan, score):
    minimum = plan.get("min_gross_margin_ratio", 0)
    return (
        f"Estimated gross margin ratio {score['estimated_gross_margin_ratio']:.2%} "
        f"is below required {minimum:.2%}."
    )


def worst_case_margin_score(model, plan, credits_spent=10):
    weighted_tokens = credits_spent * CREDIT_UNIT_TOKENS
    completion_tokens = weighted_tokens // OUTPUT_TOKEN_WEIGHT
    prompt_tokens = 0
    estimated_cost_usd = calculate_provider_cost_usd(model, prompt_tokens, completion_tokens)
    estimated_revenue_usd = calculate_credit_revenue_usd(plan, credits_spent)
    gross_margin_usd = round(estimated_revenue_usd - estimated_cost_usd, 8)
    gross_margin_ratio = (
        round(gross_margin_usd / estimated_revenue_usd, 6)
        if estimated_revenue_usd > 0
        else 0
    )

    return {
        "estimated_provider_cost_usd": estimated_cost_usd,
        "estimated_revenue_usd": estimated_revenue_usd,
        "estimated_gross_margin_usd": gross_margin_usd,
        "estimated_gross_margin_ratio": gross_margin_ratio,
    }


def model_meets_plan_margin(model, plan, score):
    return route_meets_margin(plan, score) and route_meets_margin(
        plan,
        worst_case_margin_score(model, plan),
    )


def rank_model_candidates(req, plan):
    catalog = model_catalog()
    task = req.task or "chat"

    if req.model:
        if req.model not in catalog:
            raise HTTPException(status_code=400, detail=f"Unknown model: {req.model}")

        model = catalog[req.model]

        if req.provider and req.provider != model["provider"]:
            raise HTTPException(status_code=400, detail="Requested provider does not match requested model")

        if not model_is_allowed(req.model, plan, task):
            raise HTTPException(status_code=403, detail="Model is not allowed for this plan or task")

        if not provider_is_configured(model["provider"]):
            raise HTTPException(status_code=503, detail=f"Provider is not configured: {model['provider']}")

        estimate = estimate_credits_for_request(req.message, task)
        score = score_model_for_route(model, plan, estimate)
        if profit_guard_enabled() and not model_meets_plan_margin(model, plan, score):
            raise HTTPException(
                status_code=402,
                detail=f"Requested model is not available on this plan at current pricing. {route_margin_detail(plan, score)}",
            )
        return [(req.model, model, score)]

    candidates = []

    for model_key, model in catalog.items():
        if req.provider and model["provider"] != req.provider:
            continue

        if model_is_allowed(model_key, plan, task) and provider_is_configured(model["provider"]):
            candidates.append((model_key, model))

    if not candidates:
        allowed = ", ".join(plan["allowed_tiers"])
        raise HTTPException(
            status_code=503,
            detail=f"No configured provider has a model for task '{task}' in tiers: {allowed}",
        )

    estimate = estimate_credits_for_request(req.message, task)
    scored_candidates = []

    for model_key, model in candidates:
        score = score_model_for_route(model, plan, estimate)
        if not profit_guard_enabled() or model_meets_plan_margin(model, plan, score):
            scored_candidates.append((model_key, model, score))

    default_model = plan["default_model"]
    if not scored_candidates:
        raise HTTPException(
            status_code=503,
            detail="No configured model can satisfy this plan's margin guard at current pricing.",
        )

    if req.routing_strategy == "default":
        for model_key, model, score in scored_candidates:
            if model_key == default_model:
                default_candidate = (model_key, model, score)
                other_candidates = [
                    candidate for candidate in scored_candidates if candidate[0] != default_model
                ]
                return [default_candidate] + other_candidates

    if req.routing_strategy == "cost":
        return sorted(scored_candidates, key=lambda item: item[2]["estimated_provider_cost_usd"])

    return sorted(
        scored_candidates,
        key=lambda item: (
            item[2]["estimated_gross_margin_usd"],
            -item[2]["estimated_provider_cost_usd"],
        ),
        reverse=True,
    )


def choose_model(req, plan):
    return rank_model_candidates(req, plan)[0]


def payment_link_for_plan(plan):
    specific = os.getenv(f"PAYMENT_LINK_{plan.upper()}")
    if specific:
        return specific

    links_json = os.getenv("PAYMENT_LINKS_JSON")
    if not links_json:
        return None

    try:
        links = json.loads(links_json)
    except json.JSONDecodeError:
        return None

    return links.get(plan)


def plan_billing_links():
    links = {}
    for plan_key, plan in PLANS.items():
        links[plan_key] = {
            "plan": plan_key,
            "name": plan["name"],
            "monthly_price_usd": plan["monthly_price_usd"],
            "included_credits": plan["included_credits"],
            "rate_limit_per_minute": plan["rate_limit_per_minute"],
            "daily_request_limit": plan["daily_request_limit"],
            "daily_credit_limit": plan["daily_credit_limit"],
            "max_request_credits": plan["max_request_credits"],
            "payment_link": payment_link_for_plan(plan_key),
            "checkout_url": f"/billing/checkout?plan={plan_key}",
        }
    return links


def calculate_provider_cost_usd(model, prompt_tokens, completion_tokens):
    input_cost = (prompt_tokens / 1_000_000) * model["input_usd_per_million"]
    output_cost = (completion_tokens / 1_000_000) * model["output_usd_per_million"]
    return round(input_cost + output_cost, 8)


def calculate_credit_revenue_usd(plan, credits_spent):
    unit_price = plan["monthly_price_usd"] / plan["included_credits"]
    return round(unit_price * credits_spent, 8)


def calculate_credits_spent(usage):
    if usage is None:
        return 1

    prompt_tokens = getattr(usage, "prompt_tokens", None) or 0
    completion_tokens = getattr(usage, "completion_tokens", None) or 0
    weighted_tokens = prompt_tokens + (completion_tokens * OUTPUT_TOKEN_WEIGHT)

    return max(1, ceil(weighted_tokens / CREDIT_UNIT_TOKENS))


def plan_margin_report():
    estimate = {
        "prompt_tokens": 1000,
        "completion_tokens": 250,
        "credits_spent": 2,
    }
    report = {}
    catalog = model_catalog()

    for plan_key, plan in PLANS.items():
        plan_report = []
        for model_key, model in catalog.items():
            if not model_is_allowed(model_key, plan, "chat"):
                continue

            score = score_model_for_route(model, plan, estimate)
            worst_case = worst_case_margin_score(model, plan)
            plan_report.append(
                {
                    "model": model_key,
                    "tier": model["tier"],
                    "gross_margin_ratio": score["estimated_gross_margin_ratio"],
                    "worst_case_gross_margin_ratio": worst_case["estimated_gross_margin_ratio"],
                    "gross_margin_usd_per_sample": score["estimated_gross_margin_usd"],
                    "meets_guard": model_meets_plan_margin(model, plan, score),
                }
            )

        report[plan_key] = plan_report

    return report


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "nexaflow-gateway",
    }


def deployment_check_items():
    site_url = os.getenv("NEXAFLOW_SITE_URL", "")
    stripe_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    offsite_status = offsite_backup_config_status()
    configured_providers = [
        provider_key for provider_key in PROVIDERS if provider_is_configured(provider_key)
    ]
    payment_links = {
        plan_key: bool(payment_link_for_plan(plan_key))
        for plan_key in PLANS
    }
    margin_report = plan_margin_report()
    margin_guard_ok = all(
        any(model["meets_guard"] for model in models)
        for models in margin_report.values()
    )
    usage_guards_ok = all(
        plan.get("rate_limit_per_minute", 0) > 0
        and plan.get("daily_request_limit", 0) > 0
        and plan.get("daily_credit_limit", 0) > 0
        and plan.get("max_request_credits", 0) > 0
        and plan["max_request_credits"] <= plan["daily_credit_limit"]
        for plan in PLANS.values()
    )
    checks = [
        {
            "name": "ADMIN_KEY",
            "ok": bool(os.getenv("ADMIN_KEY")),
            "detail": "Required for admin API and dashboard.",
        },
        {
            "name": "API_KEY_PEPPER",
            "ok": bool(os.getenv("API_KEY_PEPPER")),
            "detail": "Required for stable customer API key hashing.",
        },
        {
            "name": "PAYMENT_WEBHOOK_SECRET",
            "ok": bool(os.getenv("PAYMENT_WEBHOOK_SECRET")),
            "detail": "Required before receiving payment webhooks.",
        },
        {
            "name": "STRIPE_WEBHOOK_SECRET",
            "ok": stripe_secret.startswith("whsec_"),
            "detail": "Required before receiving Stripe webhook events. Use the whsec_ value from Stripe.",
        },
        {
            "name": "STRIPE_BILLING_PORTAL",
            "ok": True,
            "detail": (
                "Configured for customer self-service."
                if stripe_billing_portal_configured()
                else "Optional next step: set STRIPE_SECRET_KEY to enable customer self-service billing portal."
            ),
        },
        {
            "name": "MODEL_PROVIDER",
            "ok": len(configured_providers) > 0,
            "detail": f"Configured providers: {', '.join(configured_providers) or 'none'}.",
        },
        {
            "name": "NEXAFLOW_SITE_URL",
            "ok": site_url.startswith("https://"),
            "detail": "Use https://api.nexaflowinfra.com or another HTTPS production URL.",
        },
        {
            "name": "PAYMENT_LINKS",
            "ok": all(payment_links.values()),
            "detail": f"Configured payment links: {payment_links}.",
        },
        {
            "name": "EMAIL_DELIVERY",
            "ok": email_delivery_configured(),
            "detail": "Set RESEND_API_KEY and FROM_EMAIL so paid customers receive API keys automatically.",
        },
        {
            "name": "MARGIN_GUARD",
            "ok": margin_guard_ok,
            "detail": f"Plan model margin guard report: {margin_report}.",
        },
        {
            "name": "USAGE_GUARDS",
            "ok": usage_guards_ok,
            "detail": "Every plan must have per-minute, per-day, daily-credit, and per-request credit guards.",
        },
        {
            "name": "PERSISTENT_SQLITE_PATH",
            "ok": "NEXAFLOW_DB_PATH" in os.environ,
            "detail": "Set NEXAFLOW_DB_PATH to a persistent disk path in production.",
        },
        {
            "name": "BACKUP_PATH",
            "ok": str(BACKUP_DIR).startswith(str(DATABASE_FILE.parent)),
            "detail": f"Backups are stored in {BACKUP_DIR}. Use external object storage for off-platform backups later.",
        },
        {
            "name": "AUTO_BACKUP",
            "ok": AUTO_BACKUP_ENABLED and AUTO_BACKUP_INTERVAL_SECONDS > 0 and AUTO_BACKUP_RETENTION_COUNT > 0,
            "detail": f"Enabled={AUTO_BACKUP_ENABLED}, interval={AUTO_BACKUP_INTERVAL_SECONDS}s, retention={AUTO_BACKUP_RETENTION_COUNT}.",
        },
        {
            "name": "OFFSITE_BACKUP",
            "ok": not offsite_status["partial"],
            "detail": (
                "Configured."
                if offsite_status["configured"]
                else f"Optional but recommended: missing {offsite_status['missing']}."
            ),
        },
    ]
    return checks


@app.get("/admin/deploy-check")
def deploy_check(admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    checks = deployment_check_items()
    return {
        "ready": all(check["ok"] for check in checks),
        "checks": checks,
    }


@app.post("/admin/automation/run")
def run_admin_backend_automation(
    dry_run: bool = True,
    include_backup: bool = False,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return run_backend_automation_once(dry_run=dry_run, include_backup=include_backup)


@app.post("/admin/data-retention/cleanup")
def run_admin_data_retention_cleanup(
    business_slug: str | None = None,
    dry_run: bool = True,
    limit_per_business: int = Query(default=500, ge=1, le=5000),
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return cleanup_expired_enquiries(
        business_slug=business_slug,
        dry_run=dry_run,
        limit_per_business=limit_per_business,
    )


@app.get("/admin/data-audit-events")
def get_admin_data_audit_events(
    business_slug: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return {
        "events": list_data_audit_events(
            business_slug=business_slug,
            event_type=event_type,
            limit=limit,
        )
    }


def money(amount):
    return f"${amount:,.2f}"


def base_html(title, body):
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{title}</title>
            <style>
                :root {{
                    color-scheme: light;
                    --ink: #17202a;
                    --muted: #5b6673;
                    --line: #d8dee6;
                    --soft: #f5f7fa;
                    --brand: #116149;
                    --accent: #b5332a;
                    --gold: #c58b16;
                }}
                * {{ box-sizing: border-box; }}
                body {{
                    margin: 0;
                    font-family: Arial, Helvetica, sans-serif;
                    color: var(--ink);
                    background: #ffffff;
                    line-height: 1.5;
                }}
                header {{
                    border-bottom: 1px solid var(--line);
                    background: #ffffff;
                    position: sticky;
                    top: 0;
                    z-index: 10;
                }}
                nav {{
                    max-width: 1120px;
                    margin: 0 auto;
                    padding: 14px 20px;
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    gap: 16px;
                }}
                .brand {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    font-weight: 700;
                    color: var(--ink);
                    text-decoration: none;
                }}
                .mark {{
                    width: 28px;
                    height: 28px;
                    border-radius: 6px;
                    background: linear-gradient(135deg, var(--brand), var(--gold));
                    display: inline-block;
                }}
                .nav-links {{
                    display: flex;
                    gap: 14px;
                    align-items: center;
                    flex-wrap: wrap;
                }}
                a {{ color: var(--brand); }}
                .nav-links a {{
                    color: var(--muted);
                    text-decoration: none;
                    font-size: 14px;
                }}
                main {{
                    max-width: 1120px;
                    margin: 0 auto;
                    padding: 42px 20px 64px;
                }}
                .hero {{
                    display: grid;
                    grid-template-columns: minmax(0, 1fr) minmax(320px, 430px);
                    gap: 34px;
                    align-items: center;
                    padding: 20px 0 38px;
                }}
                h1 {{
                    font-size: 46px;
                    line-height: 1.08;
                    margin: 0 0 16px;
                    max-width: 760px;
                }}
                h2 {{
                    font-size: 26px;
                    margin: 34px 0 14px;
                }}
                h3 {{ margin: 0 0 8px; }}
                p {{ color: var(--muted); margin: 0 0 16px; }}
                .actions {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 22px; }}
                .btn {{
                    appearance: none;
                    border: 1px solid var(--brand);
                    background: var(--brand);
                    color: white;
                    padding: 10px 14px;
                    border-radius: 6px;
                    text-decoration: none;
                    font-weight: 700;
                    cursor: pointer;
                    font-size: 14px;
                }}
                .btn.secondary {{
                    background: white;
                    color: var(--brand);
                }}
                .product-panel {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    overflow: hidden;
                    background: #ffffff;
                    box-shadow: 0 12px 30px rgba(23, 32, 42, .08);
                }}
                .panel-top {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    padding: 12px 14px;
                    background: var(--soft);
                    border-bottom: 1px solid var(--line);
                    font-size: 13px;
                    color: var(--muted);
                }}
                .metrics {{
                    display: grid;
                    grid-template-columns: repeat(3, 1fr);
                    gap: 1px;
                    background: var(--line);
                }}
                .metric {{
                    background: white;
                    padding: 14px;
                    min-height: 86px;
                }}
                .metric strong {{
                    display: block;
                    font-size: 22px;
                    color: var(--ink);
                }}
                .flow {{
                    padding: 16px;
                    display: grid;
                    gap: 10px;
                }}
                .flow-row {{
                    display: grid;
                    grid-template-columns: 110px 1fr 80px;
                    gap: 10px;
                    align-items: center;
                    font-size: 13px;
                }}
                .bar {{
                    height: 10px;
                    border-radius: 6px;
                    background: var(--line);
                    overflow: hidden;
                }}
                .bar span {{
                    display: block;
                    height: 100%;
                    background: var(--brand);
                }}
                .grid {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 16px;
                }}
                .card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 18px;
                    background: white;
                }}
                .price {{
                    font-size: 34px;
                    font-weight: 800;
                    color: var(--ink);
                    margin: 10px 0;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 12px;
                    font-size: 14px;
                }}
                th, td {{
                    border-bottom: 1px solid var(--line);
                    text-align: left;
                    padding: 10px 8px;
                    vertical-align: top;
                }}
                th {{ color: var(--muted); font-weight: 700; }}
                input, select, textarea {{
                    width: 100%;
                    border: 1px solid var(--line);
                    border-radius: 6px;
                    padding: 10px;
                    font: inherit;
                }}
                textarea {{ min-height: 110px; resize: vertical; }}
                label {{
                    display: grid;
                    gap: 6px;
                    color: var(--muted);
                    font-size: 13px;
                    margin-bottom: 10px;
                }}
                .toolbar {{
                    display: grid;
                    grid-template-columns: minmax(260px, 1fr) auto;
                    gap: 10px;
                    align-items: end;
                    margin-bottom: 18px;
                }}
                .status {{
                    white-space: pre-wrap;
                    background: var(--soft);
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 12px;
                    min-height: 46px;
                    color: var(--muted);
                    font-size: 13px;
                }}
                .legal-doc {{
                    max-width: 840px;
                }}
                .legal-doc ul {{
                    color: var(--muted);
                    padding-left: 20px;
                }}
                footer {{
                    max-width: 1120px;
                    margin: 0 auto;
                    padding: 22px 20px 34px;
                    border-top: 1px solid var(--line);
                    display: flex;
                    justify-content: space-between;
                    gap: 16px;
                    flex-wrap: wrap;
                    color: var(--muted);
                    font-size: 13px;
                }}
                footer a {{
                    color: var(--muted);
                    text-decoration: none;
                    margin-right: 12px;
                }}
                @media (max-width: 820px) {{
                    .hero {{ grid-template-columns: 1fr; }}
                    .grid {{ grid-template-columns: 1fr; }}
                    .metrics {{ grid-template-columns: 1fr; }}
                    .toolbar {{ grid-template-columns: 1fr; }}
                    h1 {{ font-size: 34px; }}
                }}
            </style>
        </head>
        <body>
            <header>
                <nav>
                    <a class="brand" href="/"><span class="mark"></span><span>NexaFlow</span></a>
                    <div class="nav-links">
                        <a href="/pricing">Pricing</a>
                        <a href="/ai-enquiry">Enquiry App</a>
                        <a href="/portal">Portal</a>
                        <a href="/admin/dashboard">Admin</a>
                        <a href="/docs">API Docs</a>
                        <a href="/terms">Terms</a>
                    </div>
                </nav>
            </header>
            <main>{body}</main>
            <footer>
                <div>NexaFlow AI Gateway</div>
                <div>
                    <a href="/terms">Terms</a>
                    <a href="/privacy">Privacy</a>
                    <a href="/refund-policy">Refunds</a>
                    <a href="/acceptable-use">Acceptable Use</a>
                </div>
            </footer>
        </body>
        </html>
        """
    )


def sales_whatsapp_url(message="Hi NexaFlow, I want to know more about NexaFlow Enquiry"):
    phone = os.getenv("NEXAFLOW_SALES_WHATSAPP_PHONE") or os.getenv("NEXAFLOW_WHATSAPP_PHONE") or "60176731323"
    return whatsapp_url_for_phone(phone, message)


def whatsapp_url_for_phone(phone, message):
    digits = normalize_phone_for_whatsapp(phone)
    if not digits:
        return None
    return f"https://wa.me/{digits}?text={quote(message)}"


@app.get("/assets/brand/nexaflow-final.png")
def nexaflow_brand_final_image():
    path = MARKETING_DIR / "nexaflow-brand-final.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Brand asset not found.")
    return FileResponse(path, media_type="image/png")


@app.get("/assets/brand/nexaflow-icon.png")
@app.get("/favicon.ico")
def nexaflow_brand_icon_image():
    path = MARKETING_DIR / "nexaflow-social-avatar-icon.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Brand icon not found.")
    return FileResponse(path, media_type="image/png")


@app.get("/assets/demo/nexaflow-dealer-walkthrough.webm")
def nexaflow_dealer_walkthrough_video():
    path = MARKETING_DIR / "demo" / "nexaflow-dealer-walkthrough.webm"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Demo video not found.")
    return FileResponse(path, media_type="video/webm")


def merchant_html(title, business_name, body, show_sales_contact=False, show_floating_contact=True):
    safe_title = escape_html(title)
    safe_business_name = escape_html(business_name)
    whatsapp_url = sales_whatsapp_url() if show_sales_contact else None
    sales_contact_nav = (
        f'<a class="nav-contact" target="_blank" rel="noopener" href="{escape_html(whatsapp_url)}">WhatsApp</a>'
        if whatsapp_url
        else ""
    )
    sales_contact_float = (
        f'<a class="floating-whatsapp" target="_blank" rel="noopener" href="{escape_html(whatsapp_url)}">WhatsApp Us</a>'
        if whatsapp_url and show_floating_contact
        else ""
    )
    merchant_login_nav = '<a class="nav-login" href="/merchant-login">Merchant Login</a>'
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <meta name="description" content="NexaFlow Enquiry helps merchants collect scattered enquiries, capture missing customer details, and manage follow-up in a private WhatsApp-ready inbox.">
            <meta property="og:title" content="{safe_title}">
            <meta property="og:description" content="One enquiry workspace for scattered customer enquiries, missing details, reply drafts, and WhatsApp-ready follow-up.">
            <meta property="og:image" content="https://api.nexaflowinfra.com/assets/brand/nexaflow-final.png">
            <meta property="og:type" content="website">
            <link rel="icon" type="image/png" href="/assets/brand/nexaflow-icon.png">
            <link rel="apple-touch-icon" href="/assets/brand/nexaflow-icon.png">
            <title>{safe_title}</title>
            <style>
                :root {{
                    color-scheme: dark;
                    --bg: #000000;
                    --surface: #0c0c0d;
                    --surface-2: #141312;
                    --surface-3: #191715;
                    --ink: #f7f3ea;
                    --muted: #aaa39a;
                    --line: rgba(255, 255, 255, .095);
                    --soft: #18140d;
                    --brand: #f3c76a;
                    --brand-strong: #ffe3a0;
                    --accent: #f3c76a;
                    --teal: #45d5c7;
                    --gold: #f3c76a;
                    --danger: #ef4444;
                }}
                * {{ box-sizing: border-box; }}
                body {{
                    margin: 0;
                    font-family: Inter, Segoe UI, Arial, sans-serif;
                    background:
                        linear-gradient(180deg, #070707 0%, #030303 42%, #000000 100%);
                    color: var(--ink);
                    line-height: 1.5;
                    overflow-x: hidden;
                }}
                body::before {{
                    content: "";
                    position: fixed;
                    inset: 0;
                    z-index: 0;
                    pointer-events: none;
                    background:
                        linear-gradient(90deg, rgba(255,255,255,.028) 0 1px, transparent 1px 120px),
                        linear-gradient(180deg, rgba(243,199,106,.055), transparent 34%);
                    mask-image: linear-gradient(180deg, rgba(0,0,0,.52), transparent 72%);
                }}
                header, main, footer {{
                    position: relative;
                    z-index: 1;
                }}
                header {{
                    border-bottom: 1px solid var(--line);
                    background: rgba(0, 0, 0, .82);
                    backdrop-filter: blur(14px);
                    position: sticky;
                    top: 0;
                    z-index: 10;
                }}
                nav {{
                    max-width: 1120px;
                    margin: 0 auto;
                    padding: 16px 20px;
                    display: flex;
                    justify-content: space-between;
                    gap: 16px;
                    align-items: center;
                }}
                .nav-actions {{
                    display: flex;
                    align-items: center;
                    justify-content: flex-end;
                    gap: 12px;
                    min-width: 0;
                }}
                .nav-login {{
                    color: var(--muted);
                    text-decoration: none;
                    font-size: 14px;
                    font-weight: 800;
                    white-space: nowrap;
                }}
                .nav-login:hover {{ color: var(--ink); }}
                .nav-contact {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border: 1px solid rgba(243,199,106,.44);
                    border-radius: 999px;
                    padding: 8px 13px;
                    color: var(--ink);
                    text-decoration: none;
                    font-size: 14px;
                    font-weight: 800;
                    background: rgba(243,199,106,.08);
                }}
                .nav-contact:hover {{ background: var(--soft); }}
                .brand {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    font-weight: 800;
                    color: var(--ink);
                    text-decoration: none;
                }}
                .mark {{
                    width: 34px;
                    height: 34px;
                    border-radius: 8px;
                    display: inline-block;
                    object-fit: cover;
                    box-shadow: 0 0 18px rgba(243,199,106,.18);
                }}
                main {{
                    max-width: 1160px;
                    width: 100%;
                    margin: 0 auto;
                    padding: 38px 20px 64px;
                }}
                .hero {{
                    display: grid;
                    grid-template-columns: minmax(0, .98fr) minmax(380px, 1.02fr);
                    gap: 34px;
                    align-items: center;
                    padding: 18px 0 34px;
                    min-width: 0;
                    max-width: 100%;
                }}
                .hero.product-hero {{
                    position: relative;
                    padding: 44px 0 48px;
                    isolation: isolate;
                }}
                .hero.product-hero::before {{
                    content: none;
                }}
                .hero.product-hero::after {{
                    content: none;
                }}
                .hero.compact {{ grid-template-columns: minmax(0, 1fr); max-width: 820px; }}
                h1 {{ font-size: 48px; line-height: 1.04; margin: 0 0 14px; letter-spacing: 0; }}
                h2 {{ font-size: 24px; margin: 30px 0 14px; }}
                h3 {{ margin: 0 0 8px; }}
                p {{ color: var(--muted); line-height: 1.6; margin: 0 0 14px; }}
                .lead {{ font-size: 18px; max-width: 680px; }}
                .hero-copy {{
                    max-width: 650px;
                    min-width: 0;
                }}
                .eyebrow {{
                    color: var(--brand-strong);
                    font-size: 13px;
                    font-weight: 800;
                    margin-bottom: 10px;
                    text-transform: uppercase;
                }}
                .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 20px; }}
                .product-controls {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 10px;
                    align-items: center;
                    margin: 0 0 20px;
                    min-width: 0;
                    max-width: 100%;
                }}
                .glass-control {{
                    display: inline-flex;
                    align-items: center;
                    gap: 8px;
                    border: 1px solid rgba(255,255,255,.18);
                    border-radius: 999px;
                    padding: 5px 6px 5px 12px;
                    background:
                        linear-gradient(135deg, rgba(255,255,255,.14), rgba(255,255,255,.045)),
                        rgba(8,8,8,.56);
                    box-shadow:
                        inset 0 1px 0 rgba(255,255,255,.22),
                        inset 0 -14px 28px rgba(0,0,0,.14),
                        0 18px 46px rgba(0,0,0,.24);
                    backdrop-filter: blur(22px) saturate(170%);
                    -webkit-backdrop-filter: blur(22px) saturate(170%);
                }}
                .control-label {{
                    color: rgba(247,243,234,.62);
                    font-size: 11px;
                    font-weight: 900;
                    text-transform: uppercase;
                    letter-spacing: 0;
                    white-space: nowrap;
                }}
                .glass-control.label-free {{
                    gap: 0;
                    padding: 5px;
                }}
                .language-toggle {{
                    display: inline-flex;
                    position: relative;
                    max-width: 100%;
                    min-width: 0;
                    gap: 4px;
                    border: 1px solid rgba(255,255,255,.2);
                    border-radius: 999px;
                    padding: 4px;
                    margin-bottom: 18px;
                    overflow: hidden;
                    background:
                        linear-gradient(135deg, rgba(255,255,255,.2), rgba(255,255,255,.045)),
                        rgba(255,255,255,.045);
                    box-shadow:
                        inset 0 1px 0 rgba(255,255,255,.24),
                        inset 0 -10px 22px rgba(0,0,0,.16),
                        0 12px 32px rgba(0,0,0,.22);
                    backdrop-filter: blur(20px) saturate(170%);
                    -webkit-backdrop-filter: blur(20px) saturate(170%);
                }}
                .language-toggle::before {{
                    content: "";
                    position: absolute;
                    inset: 1px 1px auto 1px;
                    height: 48%;
                    border-radius: 999px;
                    pointer-events: none;
                    background: linear-gradient(180deg, rgba(255,255,255,.18), rgba(255,255,255,0));
                }}
                .language-toggle button {{
                    position: relative;
                    z-index: 1;
                    border: 0;
                    border-radius: 999px;
                    padding: 7px 11px;
                    background: transparent;
                    color: rgba(247,243,234,.72);
                    cursor: pointer;
                    font-weight: 850;
                    white-space: nowrap;
                }}
                .language-toggle button.active {{
                    background:
                        linear-gradient(135deg, rgba(255,255,255,.96), rgba(243,199,106,.82));
                    color: #050505;
                    box-shadow:
                        inset 0 1px 0 rgba(255,255,255,.9),
                        0 6px 18px rgba(0,0,0,.24),
                        0 0 0 1px rgba(255,255,255,.18);
                }}
                .glass-control .language-toggle {{
                    margin-bottom: 0;
                    background: rgba(255,255,255,.05);
                    box-shadow: inset 0 1px 0 rgba(255,255,255,.14);
                    display: grid;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    gap: 0;
                    isolation: isolate;
                }}
                .glass-control .language-toggle::after {{
                    content: "";
                    position: absolute;
                    z-index: 1;
                    top: 4px;
                    bottom: 4px;
                    left: 4px;
                    width: calc((100% - 8px) / 2);
                    border-radius: 999px;
                    pointer-events: none;
                    background:
                        radial-gradient(circle at 28% 18%, rgba(255,255,255,.98), rgba(255,255,255,.38) 34%, transparent 62%),
                        linear-gradient(135deg, rgba(255,255,255,.88), rgba(243,199,106,.82) 56%, rgba(255,255,255,.68));
                    box-shadow:
                        inset 0 1px 0 rgba(255,255,255,.98),
                        inset 0 -10px 18px rgba(243,199,106,.22),
                        0 6px 18px rgba(0,0,0,.34),
                        0 0 0 1px rgba(255,255,255,.18);
                    backdrop-filter: blur(18px) saturate(190%);
                    -webkit-backdrop-filter: blur(18px) saturate(190%);
                    transform: translateX(0);
                    transition:
                        transform .42s cubic-bezier(.22, 1.18, .36, 1),
                        box-shadow .24s ease,
                        filter .24s ease;
                }}
                .glass-control .language-toggle.is-second::after {{
                    transform: translateX(100%);
                }}
                .glass-control .language-toggle:active::after {{
                    filter: brightness(1.04);
                    box-shadow:
                        inset 0 1px 0 rgba(255,255,255,.98),
                        inset 0 -8px 18px rgba(243,199,106,.2),
                        0 4px 14px rgba(0,0,0,.28),
                        0 0 0 1px rgba(255,255,255,.2);
                }}
                .glass-control .language-toggle button {{
                    background: transparent !important;
                    box-shadow: none !important;
                    z-index: 2;
                    transition: color .2s ease, transform .2s ease;
                }}
                .glass-control .language-toggle button.active {{
                    color: #050505;
                }}
                .glass-control .language-toggle button:not(.active) {{
                    color: rgba(247,243,234,.68);
                }}
                .glass-control .language-toggle button:active {{
                    transform: scale(.985);
                }}
                .lang-hidden, .market-hidden {{ display: none !important; }}
                .btn {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border: 0;
                    border-radius: 8px;
                    padding: 10px 14px;
                    background: var(--brand);
                    color: #000000;
                    font-weight: 700;
                    text-decoration: none;
                    cursor: pointer;
                    min-height: 40px;
                }}
                .btn:hover {{ background: linear-gradient(135deg, var(--brand-strong), #ffffff); }}
                .btn.secondary {{
                    background: rgba(255,255,255,.025);
                    color: var(--ink);
                    border: 1px solid var(--line);
                }}
                .btn.secondary:hover {{ background: var(--soft); }}
                .text-link {{
                    display: inline-flex;
                    align-items: center;
                    color: var(--muted);
                    text-decoration: none;
                    font-size: 14px;
                    font-weight: 700;
                    min-height: 40px;
                    padding: 0 4px;
                }}
                .text-link:hover {{ color: var(--ink); }}
                .product-panel, .form-card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    overflow: hidden;
                    background:
                        linear-gradient(135deg, rgba(255,255,255,.03), rgba(243,199,106,.025)),
                        var(--surface);
                    box-shadow: 0 24px 70px rgba(0,0,0,.28);
                }}
                .panel-top {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    gap: 12px;
                    padding: 12px 14px;
                    background: var(--surface-2);
                    border-bottom: 1px solid var(--line);
                    color: var(--muted);
                    font-size: 13px;
                }}
                .signal-list {{
                    display: grid;
                    gap: 1px;
                    background: var(--line);
                }}
                .signal-row {{
                    display: grid;
                    grid-template-columns: 88px 1fr auto;
                    gap: 12px;
                    align-items: start;
                    background: var(--surface);
                    padding: 14px;
                    font-size: 14px;
                }}
                .signal-row strong {{ display: block; color: var(--ink); }}
                .pill {{
                    display: inline-flex;
                    align-items: center;
                    border: 1px solid var(--line);
                    border-radius: 999px;
                    padding: 4px 9px;
                    background: #0a0a0a;
                    color: var(--muted);
                    font-size: 12px;
                    font-weight: 700;
                    white-space: nowrap;
                }}
                .pill.hot {{ color: #ffffff; border-color: #525252; background: #1f1f1f; }}
                .pill.good {{ color: var(--gold); border-color: rgba(243,199,106,.44); background: #181205; }}
                .hero-side {{
                    display: grid;
                    gap: 14px;
                    min-width: 0;
                    max-width: 100%;
                }}
                .hero-preview {{
                    border: 1px solid rgba(255,255,255,.11);
                    border-radius: 8px;
                    overflow: hidden;
                    background:
                        linear-gradient(180deg, rgba(255,255,255,.055), transparent 42%),
                        #080808;
                    box-shadow: 0 28px 90px rgba(0,0,0,.34);
                    max-width: 100%;
                    min-width: 0;
                }}
                .preview-top {{
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    gap: 12px;
                    padding: 13px 14px;
                    border-bottom: 1px solid var(--line);
                    background: rgba(255,255,255,.025);
                    color: var(--muted);
                    font-size: 13px;
                    font-weight: 800;
                }}
                .preview-board {{
                    display: grid;
                    grid-template-columns: minmax(170px, .78fr) minmax(220px, 1fr);
                    gap: 1px;
                    background: var(--line);
                }}
                .preview-list,
                .preview-detail {{
                    background: rgba(12,12,13,.96);
                    padding: 12px;
                    min-width: 0;
                }}
                .preview-list {{
                    display: grid;
                    gap: 8px;
                }}
                .preview-lead {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 10px;
                    background: rgba(255,255,255,.025);
                }}
                .preview-lead.active {{
                    border-color: rgba(243,199,106,.58);
                    background: rgba(243,199,106,.08);
                }}
                .preview-lead strong {{
                    display: block;
                    color: var(--ink);
                    font-size: 13px;
                    margin-bottom: 4px;
                }}
                .preview-lead span {{
                    display: block;
                    color: var(--muted);
                    font-size: 12px;
                    line-height: 1.35;
                }}
                .preview-detail h3 {{
                    font-size: 18px;
                    margin: 2px 0 10px;
                }}
                .preview-detail-card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 10px;
                    background: rgba(0,0,0,.24);
                    margin-bottom: 9px;
                }}
                .preview-detail-card strong {{
                    display: block;
                    color: var(--ink);
                    font-size: 12px;
                    margin-bottom: 4px;
                }}
                .preview-detail-card span,
                .preview-detail-card p {{
                    display: block;
                    color: var(--muted);
                    font-size: 12px;
                    line-height: 1.42;
                    margin: 0;
                }}
                .preview-reply {{
                    border-color: rgba(243,199,106,.34);
                    background: rgba(243,199,106,.07);
                }}
                .brand-visual {{
                    position: relative;
                    overflow: hidden;
                    border: 1px solid var(--line);
                    border-radius: 10px;
                    background: #050505;
                    min-height: 190px;
                }}
                .brand-visual img {{
                    display: block;
                    width: 100%;
                    height: 100%;
                    min-height: 190px;
                    object-fit: cover;
                    object-position: center;
                    opacity: .9;
                }}
                .brand-visual::after {{
                    content: "";
                    position: absolute;
                    inset: 0;
                    background: linear-gradient(180deg, transparent 48%, rgba(0,0,0,.42));
                    pointer-events: none;
                }}
                .ecosystem-grid {{
                    display: grid;
                    grid-template-columns: repeat(5, minmax(0, 1fr));
                    gap: 10px;
                    margin-top: 12px;
                }}
                .ecosystem-pill {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    background:
                        linear-gradient(135deg, rgba(45,212,191,.07), rgba(243,199,106,.07)),
                        var(--surface);
                    padding: 14px 10px;
                    text-align: center;
                    font-weight: 800;
                    color: var(--ink);
                    min-height: 54px;
                    display: grid;
                    place-items: center;
                }}
                .grid {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 16px;
                    min-width: 0;
                }}
                .card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 18px;
                    background: rgba(12,12,13,.86);
                    min-width: 0;
                }}
                .card.accent-card {{
                    background:
                        linear-gradient(135deg, rgba(255,255,255,.08), transparent 56%),
                        var(--surface);
                }}
                .card p:last-child {{ margin-bottom: 0; }}
                .pricing-grid {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 14px;
                    margin-top: 12px;
                }}
                .price-card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 18px;
                    background: rgba(12,12,13,.88);
                    display: grid;
                    gap: 10px;
                    align-content: start;
                    min-width: 0;
                }}
                .price-card.highlight {{
                    border-color: #525252;
                    background:
                        linear-gradient(135deg, rgba(255,255,255,.08), transparent 58%),
                        var(--surface);
                }}
                .price-card.trial {{
                    grid-column: 1 / -1;
                    grid-template-columns: minmax(0, 1fr) minmax(220px, auto);
                    align-items: center;
                    border-color: rgba(243,199,106,.46);
                    background:
                        linear-gradient(135deg, rgba(45,212,191,.11), rgba(243,199,106,.13) 54%, transparent 70%),
                        #120d05;
                }}
                .price-card h3 {{ margin: 0; }}
                .plan-price {{
                    color: var(--ink);
                    font-size: 28px;
                    font-weight: 900;
                    line-height: 1.1;
                    margin: 2px 0;
                }}
                .plan-price span {{
                    color: var(--muted);
                    font-size: 13px;
                    font-weight: 500;
                }}
                .plan-price [data-market] {{
                    color: var(--ink);
                    font-size: inherit;
                    font-weight: 900;
                }}
                .price-card ul {{
                    margin: 0;
                    padding-left: 18px;
                    color: var(--muted);
                    font-size: 14px;
                    line-height: 1.55;
                }}
                .steps {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 12px;
                    counter-reset: steps;
                }}
                .step {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 16px;
                    background: var(--surface);
                }}
                .step::before {{
                    counter-increment: steps;
                    content: counter(steps);
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    width: 26px;
                    height: 26px;
                    border-radius: 999px;
                    background: var(--ink);
                    color: #000000;
                    font-weight: 800;
                    margin-bottom: 10px;
                }}
                input, select, textarea {{
                    width: 100%;
                    border: 1px solid var(--line);
                    border-radius: 6px;
                    padding: 10px;
                    font: inherit;
                    background: #050505;
                    color: var(--ink);
                }}
                input:focus, select:focus, textarea:focus {{
                    outline: 2px solid rgba(255, 255, 255, .14);
                    border-color: #737373;
                }}
                input[type="checkbox"] {{
                    width: auto;
                    margin-right: 8px;
                }}
                .checkbox-label {{
                    display: flex;
                    align-items: flex-start;
                    gap: 10px;
                    margin: 12px 0 14px;
                    color: var(--muted);
                    font-size: 13px;
                    line-height: 1.55;
                }}
                .checkbox-label input[type="checkbox"] {{
                    flex: 0 0 auto;
                    margin: 3px 0 0;
                }}
                .checkbox-label span {{
                    min-width: 0;
                }}
                textarea {{ min-height: 110px; resize: vertical; }}
                label {{
                    display: grid;
                    gap: 6px;
                    color: var(--muted);
                    font-size: 13px;
                    margin-bottom: 10px;
                }}
                .toolbar {{
                    display: grid;
                    grid-template-columns: minmax(260px, 1fr) auto;
                    gap: 10px;
                    align-items: end;
                    margin-bottom: 18px;
                }}
                .status {{
                    white-space: pre-wrap;
                    background: var(--surface-2);
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 12px;
                    min-height: 46px;
                    color: var(--muted);
                    font-size: 13px;
                }}
                .mini-note {{
                    margin: -2px 0 12px;
                    color: var(--muted);
                    font-size: 12px;
                    line-height: 1.45;
                }}
                pre {{
                    white-space: pre-wrap;
                    word-break: break-word;
                    background: #050505;
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 12px;
                    max-height: 320px;
                    overflow: auto;
                    color: var(--muted);
                    font-family: Consolas, Menlo, monospace;
                    font-size: 13px;
                }}
                .price {{
                    font-size: 34px;
                    font-weight: 800;
                    color: var(--ink);
                    margin: 10px 0;
                }}
                .section-head {{
                    display: flex;
                    justify-content: space-between;
                    gap: 16px;
                    align-items: end;
                    margin: 28px 0 12px;
                }}
                .section-head h2 {{ margin: 0; }}
                .section-head.compact-section {{
                    margin-top: 18px;
                }}
                .trust-strip {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 16px;
                    background: rgba(255,255,255,.025);
                    display: grid;
                    grid-template-columns: minmax(0, 1fr) auto;
                    gap: 16px;
                    align-items: center;
                }}
                .trust-strip p {{
                    margin-bottom: 0;
                }}
                .form-card {{
                    padding: 18px;
                    overflow: hidden;
                    min-width: 0;
                }}
                .setup-panel {{
                    display: grid;
                    grid-template-columns: repeat(4, minmax(0, 1fr));
                    gap: 12px;
                    margin-bottom: 18px;
                }}
                .setup-step {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 14px;
                    background: var(--surface);
                }}
                .setup-step strong {{ display: block; color: var(--ink); margin-bottom: 6px; }}
                .setup-step span {{ color: var(--muted); font-size: 13px; }}
                .onboarding-head {{
                    margin-top: 0;
                }}
                .checklist {{
                    display: grid;
                    grid-template-columns: repeat(4, minmax(0, 1fr));
                    gap: 12px;
                }}
                .check-item {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 14px;
                    background: #070707;
                    min-height: 138px;
                    display: flex;
                    flex-direction: column;
                    gap: 8px;
                }}
                .check-item.done {{
                    border-color: rgba(45,212,191,.55);
                    background: linear-gradient(135deg, rgba(45,212,191,.10), rgba(243,199,106,.04)), #070707;
                }}
                .check-status {{
                    width: 24px;
                    height: 24px;
                    border-radius: 999px;
                    border: 1px solid var(--line);
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    color: var(--muted);
                    font-size: 13px;
                    font-weight: 800;
                }}
                .check-item.done .check-status {{
                    background: var(--teal);
                    border-color: var(--teal);
                    color: #00110f;
                }}
                .check-item strong {{ color: var(--ink); }}
                .check-item span {{ color: var(--muted); font-size: 13px; }}
                .action-center {{
                    display: grid;
                    grid-template-columns: minmax(0, 1.2fr) minmax(280px, .8fr);
                    gap: 14px;
                    margin: 18px 0;
                    min-width: 0;
                }}
                .action-card {{
                    border: 1px solid rgba(243,199,106,.42);
                    border-radius: 8px;
                    padding: 18px;
                    background:
                        linear-gradient(135deg, rgba(243,199,106,.11), rgba(45,212,191,.055)),
                        var(--surface);
                    min-width: 0;
                }}
                .action-card h3 {{ margin-bottom: 6px; }}
                .action-card code {{
                    display: block;
                    max-width: 100%;
                    white-space: pre-wrap;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}
                .copilot-preview {{
                    border: 1px solid rgba(243,199,106,.32);
                    border-radius: 8px;
                    padding: 14px;
                    margin: 12px 0;
                    background:
                        linear-gradient(135deg, rgba(243,199,106,.10), rgba(45,212,191,.04)),
                        var(--surface);
                    color: var(--muted);
                    display: grid;
                    gap: 8px;
                    min-width: 0;
                }}
                .copilot-preview > strong,
                .copilot-preview-head strong {{
                    color: var(--ink);
                    font-size: 15px;
                }}
                .copilot-preview-head {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    gap: 10px;
                }}
                .copilot-preview-grid {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 8px;
                }}
                .copilot-preview-grid div {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 10px;
                    background: rgba(0,0,0,.18);
                    min-width: 0;
                }}
                .copilot-preview-grid small {{
                    display: block;
                    color: var(--muted);
                    font-size: 11px;
                    font-weight: 900;
                    text-transform: uppercase;
                    margin-bottom: 5px;
                }}
                .copilot-preview-grid b {{
                    display: block;
                    color: var(--ink);
                    font-size: 13px;
                    line-height: 1.38;
                    overflow-wrap: anywhere;
                }}
                .action-list {{
                    display: grid;
                    gap: 10px;
                    margin-top: 12px;
                }}
                .action-item {{
                    display: grid;
                    grid-template-columns: 28px 1fr;
                    gap: 10px;
                    align-items: start;
                    color: var(--muted);
                    font-size: 14px;
                }}
                .action-item strong {{
                    display: block;
                    color: var(--ink);
                    margin-bottom: 2px;
                }}
                .action-dot {{
                    width: 28px;
                    height: 28px;
                    border-radius: 999px;
                    display: inline-grid;
                    place-items: center;
                    color: #000000;
                    background: linear-gradient(135deg, var(--gold), #ffffff);
                    font-size: 13px;
                    font-weight: 900;
                }}
                .pipeline-board {{
                    display: grid;
                    grid-template-columns: repeat(5, minmax(0, 1fr));
                    gap: 10px;
                    margin: 14px 0 18px;
                }}
                .stage-card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    background: var(--surface);
                    padding: 13px;
                    min-height: 96px;
                }}
                .stage-card strong {{
                    display: flex;
                    justify-content: space-between;
                    gap: 8px;
                    color: var(--ink);
                    margin-bottom: 8px;
                }}
                .stage-card span {{
                    color: var(--muted);
                    font-size: 12px;
                }}
                .lead-badges {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 6px;
                    margin-top: 8px;
                }}
                .lead-badge {{
                    display: inline-flex;
                    align-items: center;
                    border-radius: 999px;
                    border: 1px solid var(--line);
                    padding: 3px 8px;
                    color: var(--muted);
                    font-size: 12px;
                    font-weight: 700;
                    background: rgba(255,255,255,.03);
                }}
                .lead-badge.hot {{ color: #ffffff; border-color: rgba(243,199,106,.5); background: rgba(243,199,106,.12); }}
                .demo-controls {{
                    margin-bottom: 18px;
                }}
                .demo-glass-control {{
                    padding-left: 10px;
                }}
                .demo-kpi-strip {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 1px;
                    overflow: hidden;
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    background: var(--line);
                    margin: 0 0 14px;
                }}
                .demo-kpi-strip span {{
                    display: grid;
                    gap: 3px;
                    min-width: 0;
                    background: rgba(0,0,0,.22);
                    padding: 12px;
                    color: var(--muted);
                    font-size: 12px;
                    font-weight: 800;
                }}
                .demo-kpi-strip strong {{
                    color: var(--ink);
                    font-size: 22px;
                    line-height: 1;
                }}
                .next-action {{
                    display: block;
                    margin-top: 8px;
                    color: var(--brand-strong);
                    font-size: 12px;
                    font-weight: 800;
                }}
                .simple-lead-list {{
                    display: grid;
                    gap: 12px;
                }}
                .simple-lead-card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    background: var(--surface);
                    padding: 14px;
                    display: grid;
                    grid-template-columns: minmax(180px, .8fr) minmax(240px, 1.2fr) minmax(180px, .8fr);
                    gap: 12px;
                    align-items: start;
                }}
                .simple-lead-card strong {{
                    color: var(--ink);
                    display: block;
                    margin-bottom: 4px;
                }}
                .simple-lead-card small {{
                    color: var(--muted);
                    display: block;
                    line-height: 1.45;
                }}
                .dealer-demo-board {{
                    display: grid;
                    grid-template-columns: minmax(270px, .72fr) minmax(390px, 1.28fr);
                    gap: 14px;
                    align-items: start;
                }}
                .demo-queue {{
                    display: grid;
                    gap: 8px;
                    min-width: 0;
                }}
                .demo-queue-card {{
                    width: 100%;
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    background: rgba(255,255,255,.025);
                    color: var(--ink);
                    padding: 12px;
                    display: grid;
                    gap: 7px;
                    text-align: left;
                    cursor: pointer;
                    transition: border-color .16s ease, background .16s ease, transform .16s ease, box-shadow .16s ease;
                    min-width: 0;
                }}
                .demo-queue-card:hover,
                .demo-queue-card.active {{
                    border-color: rgba(243,199,106,.7);
                    background: rgba(243,199,106,.07);
                }}
                .demo-queue-card.active {{
                    box-shadow: inset 3px 0 0 var(--gold);
                }}
                .demo-queue-card:hover {{
                    transform: translateY(-1px);
                }}
                .demo-queue-top {{
                    display: flex;
                    justify-content: space-between;
                    align-items: flex-start;
                    gap: 10px;
                }}
                .demo-queue-title {{
                    display: grid;
                    gap: 3px;
                    min-width: 0;
                }}
                .demo-queue-top strong {{
                    color: var(--ink);
                    font-size: 15px;
                    line-height: 1.25;
                }}
                .demo-queue-meta {{
                    color: var(--muted);
                    font-size: 12px;
                    line-height: 1.4;
                }}
                .demo-queue-focus {{
                    color: var(--brand-strong);
                    font-size: 13px;
                    font-weight: 800;
                    line-height: 1.35;
                    overflow-wrap: anywhere;
                }}
                .demo-detail-panel {{
                    border: 1px solid rgba(243,199,106,.28);
                    border-radius: 8px;
                    background: rgba(8,8,8,.78);
                    padding: 16px;
                    min-height: 0;
                    position: sticky;
                    top: 88px;
                    min-width: 0;
                }}
                .demo-detail-head {{
                    display: flex;
                    justify-content: space-between;
                    gap: 12px;
                    align-items: flex-start;
                    margin-bottom: 8px;
                }}
                .demo-detail-head h3 {{
                    margin: 4px 0 0;
                    font-size: 22px;
                }}
                .demo-detail-kicker {{
                    color: var(--muted);
                    font-size: 12px;
                    font-weight: 800;
                    text-transform: uppercase;
                    letter-spacing: 0;
                }}
                .demo-copilot-card {{
                    border: 1px solid rgba(45,212,191,.28);
                    border-radius: 8px;
                    padding: 12px;
                    background:
                        linear-gradient(135deg, rgba(45,212,191,.09), rgba(243,199,106,.055)),
                        rgba(0,0,0,.2);
                    display: flex;
                    justify-content: space-between;
                    gap: 12px;
                    align-items: flex-start;
                }}
                .demo-copilot-card strong {{
                    display: block;
                    color: var(--ink);
                    margin-bottom: 4px;
                }}
                .demo-copilot-card span {{
                    color: var(--muted);
                    font-size: 12px;
                    line-height: 1.45;
                }}
                .demo-detail-grid {{
                    display: grid;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    gap: 10px;
                    margin: 12px 0;
                }}
                .demo-detail-box,
                .demo-message-box,
                .demo-reply-box {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    background: rgba(0,0,0,.22);
                    padding: 12px;
                    min-width: 0;
                }}
                .demo-detail-box strong,
                .demo-message-box strong,
                .demo-reply-box strong {{
                    display: block;
                    color: var(--ink);
                    margin-bottom: 6px;
                    font-size: 13px;
                }}
                .demo-detail-box span,
                .demo-message-box p,
                .demo-reply-box p {{
                    margin: 0;
                    color: var(--muted);
                    font-size: 13px;
                    line-height: 1.48;
                }}
                .demo-reply-box {{
                    margin-top: 10px;
                    border-color: rgba(243,199,106,.42);
                    border-left: 3px solid var(--gold);
                    background: rgba(243,199,106,.075);
                }}
                .demo-reply-box.primary {{
                    margin-top: 12px;
                    background:
                        linear-gradient(135deg, rgba(243,199,106,.13), rgba(255,255,255,.025)),
                        rgba(0,0,0,.24);
                }}
                .demo-reply-box p {{
                    color: var(--ink);
                    font-size: 14px;
                    overflow-wrap: anywhere;
                }}
                .demo-inline-details {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    background: rgba(0,0,0,.16);
                    padding: 12px;
                    margin-top: 12px;
                }}
                .demo-inline-details summary {{
                    cursor: pointer;
                    color: var(--ink);
                    font-size: 13px;
                    font-weight: 900;
                }}
                .demo-inline-details[open] summary {{
                    margin-bottom: 10px;
                }}
                .demo-extra-grid {{
                    margin-bottom: 10px;
                }}
                .demo-panel-actions {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 10px;
                    align-items: center;
                    margin-top: 12px;
                }}
                .demo-panel-actions span {{
                    color: var(--muted);
                    font-size: 12px;
                }}
                .demo-notes-grid {{
                    display: grid;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    gap: 16px;
                }}
                .demo-notes-grid h3 {{
                    margin: 0 0 6px;
                    color: var(--ink);
                    font-size: 15px;
                }}
                .demo-notes-grid p {{
                    margin: 0;
                    color: var(--muted);
                    font-size: 13px;
                    line-height: 1.5;
                }}
                .simple-actions {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 8px;
                    align-items: center;
                }}
                details.form-card summary {{
                    cursor: pointer;
                    color: var(--ink);
                    font-weight: 800;
                }}
                details.form-card summary + * {{ margin-top: 16px; }}
                details.form-card .action-center {{
                    margin-bottom: 0;
                }}
                .share-links {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 12px;
                    margin-top: 14px;
                }}
                .share-link-box {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 14px;
                    background: var(--surface-2);
                    min-width: 0;
                }}
                .share-link-box code {{
                    display: block;
                    white-space: pre-wrap;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    color: var(--muted);
                    margin: 8px 0 10px;
                }}
                .setup-package {{
                    display: grid;
                    gap: 14px;
                    color: var(--ink);
                }}
                .setup-package p {{
                    margin: 0;
                }}
                .setup-package-head {{
                    display: flex;
                    justify-content: space-between;
                    align-items: flex-start;
                    gap: 12px;
                }}
                .setup-package-head h3 {{
                    margin: 0 0 4px;
                }}
                .setup-package-grid {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 10px;
                }}
                .setup-package-box {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 12px;
                    background: rgba(255,255,255,.025);
                    min-width: 0;
                }}
                .setup-package-box span {{
                    display: block;
                    color: var(--muted);
                    font-size: 11px;
                    font-weight: 900;
                    text-transform: uppercase;
                    margin-bottom: 6px;
                }}
                .setup-package-box code {{
                    display: block;
                    white-space: pre-wrap;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    color: var(--ink);
                    margin-bottom: 10px;
                }}
                .setup-package-message {{
                    border: 1px solid rgba(243,199,106,.32);
                    border-radius: 8px;
                    padding: 12px;
                    background: rgba(243,199,106,.045);
                }}
                .setup-package-message pre {{
                    margin: 8px 0 10px;
                    max-height: 220px;
                }}
                .setup-checklist {{
                    display: grid;
                    grid-template-columns: repeat(4, minmax(0, 1fr));
                    gap: 8px;
                }}
                .setup-check {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 10px;
                    background: var(--surface);
                    color: var(--muted);
                    font-size: 12px;
                    line-height: 1.45;
                }}
                .setup-check strong {{
                    display: block;
                    color: var(--ink);
                    font-size: 13px;
                    margin-bottom: 3px;
                }}
                .setup-copy-status {{
                    color: var(--brand-strong);
                    font-size: 12px;
                    font-weight: 800;
                }}
                .admin-split {{
                    display: grid;
                    grid-template-columns: minmax(0, 380px) minmax(0, 1fr);
                    gap: 18px;
                    align-items: start;
                }}
                .trial-request-list {{
                    display: grid;
                    gap: 14px;
                    margin-top: 14px;
                }}
                .trial-request-card {{
                    display: grid;
                    grid-template-columns: minmax(0, 1fr) minmax(260px, 320px);
                    gap: 16px;
                    border: 1px solid var(--line);
                    border-radius: 10px;
                    padding: 16px;
                    background:
                        linear-gradient(135deg, rgba(45,212,191,.04), rgba(243,199,106,.035)),
                        var(--surface);
                }}
                .trial-request-head {{
                    display: flex;
                    align-items: flex-start;
                    justify-content: space-between;
                    gap: 12px;
                    margin-bottom: 12px;
                }}
                .trial-request-title {{
                    margin: 0 0 4px;
                    font-size: 19px;
                }}
                .trial-request-subtitle {{
                    color: var(--muted);
                    font-size: 13px;
                    line-height: 1.45;
                }}
                .trial-contact {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 10px;
                    margin: 12px 0;
                }}
                .trial-field {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 10px;
                    background: rgba(255,255,255,.025);
                    min-width: 0;
                }}
                .trial-field span {{
                    display: block;
                    color: var(--muted);
                    font-size: 11px;
                    font-weight: 800;
                    text-transform: uppercase;
                    margin-bottom: 4px;
                }}
                .trial-field strong, .trial-field code {{
                    color: var(--ink);
                    font-size: 13px;
                    overflow-wrap: anywhere;
                }}
                .trial-message {{
                    border: 1px solid rgba(243,199,106,.28);
                    border-radius: 8px;
                    padding: 12px;
                    background: rgba(243,199,106,.045);
                }}
                .trial-message span {{
                    display: block;
                    color: var(--brand-strong);
                    font-size: 12px;
                    font-weight: 900;
                    margin-bottom: 5px;
                }}
                .trial-message p {{
                    margin: 0;
                    color: var(--ink);
                }}
                .trial-meta {{
                    display: grid;
                    grid-template-columns: repeat(4, minmax(0, 1fr));
                    gap: 8px;
                    margin-top: 12px;
                }}
                .trial-followup {{
                    border-left: 1px solid var(--line);
                    padding-left: 16px;
                    display: grid;
                    align-content: start;
                    gap: 12px;
                }}
                .trial-followup h3 {{
                    margin: 0;
                    font-size: 16px;
                }}
                .trial-followup p {{
                    margin: 0;
                    color: var(--ink);
                }}
                .trial-actions {{
                    display: grid;
                    grid-template-columns: 1fr;
                    gap: 8px;
                    margin-top: 0;
                }}
                .trial-actions .btn {{
                    width: 100%;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 12px;
                    font-size: 14px;
                }}
                th, td {{
                    border-bottom: 1px solid var(--line);
                    text-align: left;
                    padding: 10px 8px;
                    vertical-align: top;
                }}
                th {{ color: var(--muted); font-weight: 700; }}
                footer {{
                    max-width: 1120px;
                    margin: 0 auto;
                    padding: 22px 20px 34px;
                    border-top: 1px solid var(--line);
                    color: var(--muted);
                    font-size: 13px;
                }}
                .floating-whatsapp {{
                    position: fixed;
                    right: 22px;
                    bottom: 22px;
                    z-index: 20;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 48px;
                    padding: 12px 18px;
                    border-radius: 999px;
                    background: linear-gradient(135deg, var(--gold), #ffffff);
                    color: #000000;
                    text-decoration: none;
                    font-weight: 900;
                    box-shadow: 0 16px 40px rgba(0,0,0,.38);
                }}
                .floating-whatsapp:hover {{ background: linear-gradient(135deg, var(--brand-strong), #ffffff); }}
                @media (max-width: 820px) {{
                    .hero, .preview-board, .grid, .ecosystem-grid, .pricing-grid, .steps, .toolbar, .admin-split, .setup-panel, .share-links, .action-center, .pipeline-board, .checklist, .trial-request-card, .trial-contact, .trial-meta, .setup-package-grid, .setup-checklist, .simple-lead-card, .dealer-demo-board, .demo-detail-grid, .demo-notes-grid, .copilot-preview-grid {{ grid-template-columns: 1fr; }}
                    h1 {{ font-size: 32px; }}
                    table {{ display: block; overflow-x: auto; }}
                    nav {{ padding: 13px 14px; gap: 8px; }}
                    .brand {{ gap: 8px; min-width: 0; }}
                    .brand span {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
                    .mark {{ width: 30px; height: 30px; }}
                    .nav-actions {{ gap: 8px; }}
                    .nav-login {{ display: none; }}
                    .nav-contact {{ padding: 7px 10px; font-size: 13px; max-width: 94px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
                    .product-controls {{ align-items: flex-start; gap: 8px; }}
                    .glass-control {{
                        width: 100%;
                        min-width: 0;
                        overflow: hidden;
                        display: grid;
                        grid-template-columns: 66px minmax(0, 1fr);
                        justify-content: stretch;
                    }}
                    .glass-control.label-free {{
                        width: fit-content;
                        grid-template-columns: minmax(0, 1fr);
                        display: inline-flex;
                        padding: 4px;
                    }}
                    .control-label {{ font-size: 10px; }}
                    .glass-control .language-toggle {{ width: 100%; min-width: 0; }}
                    .language-toggle button {{ flex: 1 1 0; min-width: 0; padding: 7px 7px; font-size: 13px; overflow: hidden; text-overflow: ellipsis; }}
                    .glass-control.label-free .language-toggle {{ width: auto; }}
                    .glass-control.label-free .language-toggle button {{
                        flex: 0 0 auto;
                        min-width: 58px;
                        padding: 6px 10px;
                        font-size: 12px;
                    }}
                    .glass-control.label-free #marketToggle button {{
                        min-width: 88px;
                        padding-left: 10px;
                        padding-right: 10px;
                    }}
                    .price-card.trial {{ grid-template-columns: 1fr; }}
                    .trust-strip {{ grid-template-columns: 1fr; }}
                    .signal-row {{ grid-template-columns: 1fr; }}
                    .setup-package-head {{ display: grid; }}
                    .trial-request-head {{ display: grid; }}
                    .trial-followup {{ border-left: 0; border-top: 1px solid var(--line); padding-left: 0; padding-top: 14px; }}
                    .demo-detail-panel {{ position: static; min-height: 0; }}
                    .demo-kpi-strip span {{ padding: 10px 8px; }}
                    .demo-queue-card:nth-of-type(n+4) {{ display: none; }}
                    .hero.compact,
                    .hero.compact h1,
                    .hero.compact .lead,
                    .form-card,
                    .demo-glass-control {{
                        max-width: calc(100vw - 40px);
                    }}
                    .form-card, .action-card, .share-link-box {{ width: 100%; }}
                    .floating-whatsapp {{ right: 14px; bottom: 14px; min-height: 44px; padding: 10px 14px; max-width: calc(100vw - 28px); }}
                }}
                @media (max-width: 430px) {{
                    main {{ padding-left: 20px; padding-right: 20px; }}
                    .hero.product-hero {{ padding-top: 32px; }}
                }}
                @media (max-width: 520px) {{
                    .hero.compact,
                    .hero.compact h1,
                    .hero.compact .lead,
                    .form-card {{
                        width: 100%;
                        max-width: min(350px, calc(100vw - 40px));
                    }}
                }}
            </style>
        </head>
        <body>
            <header>
                <nav>
                    <a class="brand" href="/"><img class="mark" src="/assets/brand/nexaflow-icon.png" alt="NexaFlow logo"><span>{safe_business_name}</span></a>
                    <div class="nav-actions">{merchant_login_nav}{sales_contact_nav}</div>
                </nav>
            </header>
            <main>{body}</main>
            <footer>Powered by NexaFlow</footer>
            {sales_contact_float}
        </body>
        </html>
        """
    )


def plan_card(plan_key, plan):
    checkout_url = f"/billing/checkout?plan={plan_key}"
    tiers = ", ".join(plan["allowed_tiers"])
    return f"""
    <section class="card">
        <h3>{plan["name"]}</h3>
        <div class="price">${plan["monthly_price_usd"]}<span style="font-size:14px;color:var(--muted);font-weight:400;"> / mo</span></div>
        <p>{plan["included_credits"]:,} token credits</p>
        <p>{plan["rate_limit_per_minute"]} requests/min</p>
        <p>{plan["daily_request_limit"]:,} requests/day guard</p>
        <p>{plan["daily_credit_limit"]:,} credits/day guard</p>
        <p>{plan["max_request_credits"]:,} credits/request max</p>
        <p>{tiers}</p>
        <a class="btn" href="{checkout_url}">Checkout</a>
    </section>
    """


def legal_page(title, updated, sections):
    section_html = ""
    for heading, paragraphs in sections:
        section_html += f"<h2>{heading}</h2>"
        for paragraph in paragraphs:
            if isinstance(paragraph, list):
                items = "".join(f"<li>{item}</li>" for item in paragraph)
                section_html += f"<ul>{items}</ul>"
            else:
                section_html += f"<p>{paragraph}</p>"

    return base_html(
        title,
        f"""
        <article class="legal-doc">
            <h1>{title}</h1>
            <p>Last updated: {updated}</p>
            {section_html}
        </article>
        """
    )


@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    return legal_page(
        "Terms of Service",
        "May 30, 2026",
        [
            (
                "Service Scope",
                [
                    "NexaFlow provides hosted AI infrastructure and business tools, including AI model routing, usage tracking, customer credits, billing support, enquiry forms, merchant inboxes, WhatsApp follow-up links, email notifications, CSV exports, and related account tools.",
                    "The Enquiry product is a business-to-business tool for merchants to collect and manage customer enquiries. NexaFlow is not a party to the merchant's sale, quotation, appointment, delivery, refund, warranty, or customer service relationship.",
                    "We may improve, modify, limit, suspend, or discontinue parts of the service where needed for security, reliability, legal compliance, provider availability, or sustainable operation.",
                ],
            ),
            (
                "Accounts, Access Keys, and Merchant Inbox",
                [
                    [
                        "Keep API keys secret and rotate them immediately if exposed.",
                        "Keep business access keys private. Anyone with a valid business access key may access the relevant merchant inbox and lead data.",
                        "You may not resell, share, scrape, overload, reverse engineer, or abuse access outside your authorized business use.",
                        "You are responsible for all activity under your account, API keys, business access keys, payment links, enquiry links, embeds, and integrations.",
                        "We may suspend or restrict accounts for non-payment, suspicious activity, policy violations, security risk, legal risk, spam, abuse, or use that may harm NexaFlow, merchants, end customers, infrastructure providers, or third parties.",
                    ]
                ],
            ),
            (
                "Merchant Responsibilities",
                [
                    [
                        "Merchants are responsible for the accuracy of their business name, service description, prices, availability, WhatsApp number, email address, opening hours, offers, and follow-up messages.",
                        "Merchants must review AI-generated classifications, suggested replies, summaries, and follow-up drafts before relying on or sending them.",
                        "Merchants are responsible for responding to customers, providing the actual goods or services, fulfilling quotations and appointments, handling disputes, refunds, complaints, warranties, taxes, licenses, and consumer obligations.",
                        "Merchants must not use NexaFlow for unlawful, misleading, harmful, discriminatory, fraudulent, high-risk, or spam activity.",
                        "Merchants must ensure their use of customer data, exported CSV files, internal notes, and follow-up actions complies with applicable law, including personal data protection and marketing rules.",
                    ]
                ],
            ),
            (
                "Customer Enquiries and Personal Data",
                [
                    "Customer enquiry forms may collect names, phone numbers, email addresses, messages, consent records, intent labels, follow-up focus labels, reply drafts, internal notes, follow-up dates, and follow-up signal values.",
                    "Merchants may use enquiry data only for the stated enquiry follow-up, service, support, security, and record-keeping purposes. Customer data must not be sold as a marketing list or used for unrelated purposes unless the merchant has a lawful basis and required consent.",
                    "NexaFlow provides technical safeguards such as consent capture, business access keys, private merchant inboxes, CSV export controls, and public response minimisation. Merchants remain responsible for their own handling of customer data after access or export.",
                    "Merchants should promptly notify NexaFlow if a business access key, exported file, mailbox, or connected system is exposed or compromised.",
                ],
            ),
            (
                "AI Outputs and Automation",
                [
                    "AI features may classify enquiries, suggest follow-up focus, generate reply drafts, route model requests, or assist with business workflows. AI outputs can be inaccurate, incomplete, delayed, biased, or unsuitable for a particular situation.",
                    "NexaFlow does not guarantee that any enquiry will convert into a sale, that a lead is genuine, that a suggested reply is correct, or that a workflow will produce a specific business result.",
                    "Do not rely on the service as the sole basis for legal, financial, medical, safety-critical, employment, credit, insurance, immigration, or other high-risk decisions.",
                ],
            ),
            (
                "Third-Party Services",
                [
                    "The service may depend on third-party providers such as hosting, payment, email, domain, backup, AI model, analytics, WhatsApp, and messaging providers.",
                    "Third-party services may have their own terms, outages, limits, fees, rate limits, review processes, and data handling practices. NexaFlow is not responsible for third-party service interruptions, policy changes, delivery failures, or provider decisions.",
                    "WhatsApp links and email notifications are convenience features. NexaFlow does not guarantee message delivery, open rates, response times, or recipient action.",
                ],
            ),
            (
                "Billing, Payments, and Refunds",
                [
                    "Plans include a monthly credit allowance. Credits are consumed based on estimated and actual token usage, including heavier weighting for model output tokens.",
                    "Payments are processed by third-party payment providers. NexaFlow does not store payment card data.",
                    "Unless otherwise stated, subscriptions renew automatically and access may be limited, suspended, or cancelled if payment fails, a chargeback occurs, fraud is suspected, or the account is used in breach of these Terms.",
                    "Refunds are handled under the Refund Policy. Credits, usage, setup work, third-party charges, and consumed service periods may be non-refundable unless required by law or approved case by case.",
                ],
            ),
            (
                "Availability, Backups, and Data Loss",
                [
                    "The service depends on third-party infrastructure and AI model providers. We aim for reliable operation, but do not guarantee uninterrupted availability.",
                    "We may maintain backups and operational logs, but backups are not a substitute for the merchant's own exports and records. Merchants should export important leads regularly.",
                    "We are not responsible for data loss caused by merchant error, exposed access keys, deleted records, failed third-party services, browser or device issues, malware, or events outside our reasonable control.",
                ],
            ),
            (
                "Acceptable Use",
                [
                    [
                        "Do not submit illegal, abusive, infringing, deceptive, harassing, obscene, or harmful content.",
                        "Do not use the service to send spam, unsolicited marketing, scams, phishing, malware, or messages that violate platform rules or applicable law.",
                        "Do not attempt to bypass rate limits, access another merchant's data, probe security controls, or interfere with service operation.",
                        "Do not upload or process sensitive personal data unless you have a lawful basis and the service plan expressly supports that use.",
                    ]
                ],
            ),
            (
                "Disclaimers",
                [
                    "The service is provided on an as-is and as-available basis to the fullest extent permitted by law.",
                    "NexaFlow disclaims warranties of merchantability, fitness for a particular purpose, uninterrupted operation, non-infringement, accuracy of AI output, lead quality, conversion rate, revenue, profit, deliverability, or suitability for any specific business.",
                    "Nothing in these Terms excludes liability that cannot lawfully be excluded under applicable law.",
                ],
            ),
            (
                "Limitation of Liability",
                [
                    "To the maximum extent allowed by law, NexaFlow is not liable for indirect, incidental, special, consequential, punitive, exemplary, lost-profit, lost-revenue, lost-data, loss-of-goodwill, business interruption, or third-party damages.",
                    "To the maximum extent allowed by law, NexaFlow's total liability for claims relating to the service is limited to the amount paid by the customer to NexaFlow for the affected service during the three months before the event giving rise to the claim.",
                    "The limitations apply whether the claim is based on contract, tort, negligence, statute, strict liability, or any other legal theory, even if a remedy fails of its essential purpose.",
                ],
            ),
            (
                "Indemnity",
                [
                    "You agree to defend, indemnify, and hold NexaFlow, its operator, suppliers, and service providers harmless from claims, losses, liabilities, damages, penalties, costs, and expenses arising from your business, your customer relationships, your content, your data handling, your breach of these Terms, your violation of law, or your misuse of the service.",
                ],
            ),
            (
                "Termination",
                [
                    "You may stop using the service at any time. We may suspend or terminate access where reasonably necessary for security, compliance, non-payment, provider requirements, suspected misuse, or material breach.",
                    "After termination, we may retain records as needed for billing, tax, legal, security, backup, dispute, and operational purposes, subject to the Privacy Policy and applicable law.",
                ],
            ),
            (
                "Governing Law and Disputes",
                [
                    "These Terms are intended to be governed by the laws of Singapore unless a mandatory law requires otherwise.",
                    "Before starting formal proceedings, the parties should first try to resolve account, billing, or service disputes by contacting nexaflowinfra@gmail.com with reasonable details of the issue.",
                ],
            ),
            (
                "Contact",
                [
                    "For account, billing, or compliance questions, contact the operator at nexaflowinfra@gmail.com.",
                ],
            ),
        ],
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return legal_page(
        "Privacy Policy",
        "May 29, 2026",
        [
            (
                "Data We Process",
                [
                    [
                        "Account identifiers, billing email, plan, subscription status, and API key prefix.",
                        "Usage metadata such as timestamps, provider, model, token counts, credit spend, costs, and request previews.",
                        "Enquiry product data such as lead name, phone, email, enquiry message, intent, follow-up focus, reply draft, follow-up date, deal value, internal merchant notes, consent timestamp, and consent notice.",
                        "Operational records such as webhook deliveries, customer notifications, backups, and admin actions.",
                    ]
                ],
            ),
            (
                "Why We Process Data",
                [
                    "We process data to provide the service, bill customers, handle customer enquiries, notify merchants, prepare follow-up drafts, prevent abuse, monitor reliability, send account notifications, diagnose incidents, and maintain required business records.",
                    "Enquiry form submissions are used only for enquiry handling, merchant follow-up, support, security, and record keeping. They are not sold as marketing lists.",
                ],
            ),
            (
                "PDPA-Style Notice for Enquiry Forms",
                [
                    "Before submitting an enquiry, individuals are shown a notice explaining that their contact details and message will be collected, used, and disclosed to the relevant business and NexaFlow service providers for enquiry follow-up, support, security, and record keeping.",
                    "The service records the consent status, consent timestamp, and consent notice used at the time of submission.",
                    "Merchants should use exported leads and internal notes only for the stated enquiry follow-up purpose and should handle any access, correction, withdrawal, or deletion request in accordance with applicable law.",
                ],
            ),
            (
                "Subprocessors",
                [
                    "The service may use infrastructure, payment, email, model, and backup providers such as Railway, Stripe, Resend, OpenAI, OpenRouter, Cloudflare, and GitHub.",
                ],
            ),
            (
                "Retention and Security",
                [
                    "API keys are stored as hashes where possible and are not shown again after creation or rotation.",
                    "Merchant inboxes are protected by business access keys. Public enquiry responses do not expose internal reply drafts, WhatsApp follow-up links, internal notes, lead value, or follow-up dates.",
                    "Operational data is retained as needed for billing, support, security, taxes, and product improvement. Backups may retain data until their scheduled retention window expires.",
                ],
            ),
            (
                "Customer Choices",
                [
                    "Customers may request account help, correction, export, or deletion where legally and operationally possible by contacting nexaflowinfra@gmail.com.",
                ],
            ),
        ],
    )


@app.get("/refund-policy", response_class=HTMLResponse)
def refund_policy_page():
    return legal_page(
        "Refund Policy",
        "May 29, 2026",
        [
            (
                "General Policy",
                [
                    "Subscription payments cover hosted access, infrastructure, model routing, account automation, and included credits for the selected billing period.",
                    "Refunds are reviewed case by case. Approved refunds may result in account suspension, credit reversal, or API key deactivation.",
                ],
            ),
            (
                "Eligible Cases",
                [
                    [
                        "Duplicate accidental payments.",
                        "A verified service activation failure where no meaningful usage occurred.",
                        "Billing errors caused by platform configuration.",
                    ]
                ],
            ),
            (
                "Usually Not Refundable",
                [
                    [
                        "Used credits, completed API usage, or third-party provider costs already incurred.",
                        "Policy violations, abuse, spam, fraud, chargebacks, or suspended accounts.",
                        "Results generated by AI models that require customer review or refinement.",
                    ]
                ],
            ),
            (
                "How To Request",
                [
                    "Send the payment email, plan, date, and reason to nexaflowinfra@gmail.com within 7 days of the charge.",
                ],
            ),
        ],
    )


@app.get("/acceptable-use", response_class=HTMLResponse)
def acceptable_use_page():
    return legal_page(
        "Acceptable Use Policy",
        "May 29, 2026",
        [
            (
                "Prohibited Uses",
                [
                    [
                        "Illegal activity, fraud, scams, credential theft, impersonation, or evasion of law enforcement.",
                        "Malware, phishing, spam, unauthorized scraping, vulnerability exploitation, or abuse of third-party systems.",
                        "Harassment, hateful abuse, sexual exploitation, or content involving minors in unsafe or exploitative contexts.",
                        "Attempts to bypass safety systems, rate limits, payment controls, usage limits, or provider policies.",
                    ]
                ],
            ),
            (
                "High-Risk Decisions",
                [
                    "Do not use the service as the sole basis for legal, medical, financial, employment, housing, insurance, or safety-critical decisions. Human review is required.",
                ],
            ),
            (
                "Enforcement",
                [
                    "We may throttle, suspend, cancel, or permanently remove access when usage creates legal, security, provider, payment, or reputational risk.",
                ],
            ),
            (
                "Reporting Abuse",
                [
                    "Report suspected abuse or security issues to nexaflowinfra@gmail.com.",
                ],
            ),
        ],
    )


@app.get("/", response_class=HTMLResponse)
def landing_page():
    contact_url = sales_whatsapp_url("Hi NexaFlow, I want to create an enquiry link for my business")
    request_link = escape_html(contact_url or "/contact-trial")
    return merchant_html(
        "NexaFlow Enquiry",
        "NexaFlow",
        f"""
        <style>
            .home-sales-os {{
                position: relative;
                display: grid;
                grid-template-columns: minmax(0, .82fr) minmax(520px, 1.18fr);
                gap: 24px;
                align-items: center;
                padding: 42px 0 48px;
                isolation: isolate;
            }}
            .home-sales-copy {{
                min-width: 0;
            }}
            .home-sales-os .option-kicker {{
                display: block;
                color: var(--brand-strong);
                font-size: 12px;
                font-weight: 900;
                text-transform: uppercase;
                margin-bottom: 4px;
            }}
            .home-sales-copy h1 {{
                max-width: 680px;
                font-size: clamp(40px, 6vw, 76px);
                line-height: .96;
                margin-bottom: 16px;
            }}
            .home-sales-copy .lead {{
                max-width: 620px;
            }}
            .home-os-preview {{
                position: relative;
                display: grid;
                grid-template-columns: 176px minmax(0, 1fr);
                gap: 12px;
                min-height: 480px;
                border: 1px solid rgba(255,255,255,.12);
                border-radius: 14px;
                padding: 14px;
                overflow: hidden;
                background:
                    linear-gradient(90deg, rgba(243,199,106,.055), transparent 36%),
                    #050505;
                box-shadow: 0 28px 90px rgba(0,0,0,.34);
                isolation: isolate;
            }}
            .home-os-preview::before {{
                content: "";
                position: absolute;
                inset: -36%;
                pointer-events: none;
                z-index: 0;
                background:
                    radial-gradient(circle at 18% 30%, rgba(243,199,106,.16), transparent 24%),
                    radial-gradient(circle at 82% 20%, rgba(69,213,199,.12), transparent 25%);
                opacity: .62;
                transform: translate3d(-1.5%, 0, 0);
                animation: homeAmbientDrift 11s ease-in-out infinite alternate;
            }}
            .home-os-preview::after {{
                content: "";
                position: absolute;
                inset: 0;
                pointer-events: none;
                z-index: 0;
                background: linear-gradient(105deg, transparent 0%, transparent 38%, rgba(243,199,106,.08) 48%, transparent 62%, transparent 100%);
                transform: translateX(-120%);
                animation: homeScanPass 6.8s ease-in-out infinite;
            }}
            .home-os-sidebar,
            .home-os-workspace {{
                position: relative;
                z-index: 1;
            }}
            .home-os-sidebar {{
                display: grid;
                align-content: start;
                gap: 11px;
                border: 1px solid rgba(255,255,255,.12);
                border-radius: 12px;
                padding: 13px;
                background: rgba(10,10,10,.84);
                backdrop-filter: blur(16px);
            }}
            .home-os-sidebar strong {{
                color: var(--ink);
                font-size: 17px;
            }}
            .home-os-nav-item {{
                display: flex;
                justify-content: space-between;
                gap: 10px;
                align-items: center;
                min-height: 36px;
                border-radius: 8px;
                padding: 7px 9px;
                color: var(--muted);
                font-size: 12px;
                font-weight: 850;
            }}
            .home-os-nav-item.active {{
                color: var(--ink);
                background: rgba(243,199,106,.12);
            }}
            .home-os-workspace {{
                display: grid;
                grid-template-rows: auto auto minmax(0, 1fr);
                gap: 12px;
                min-width: 0;
            }}
            .home-os-topbar {{
                display: flex;
                justify-content: space-between;
                gap: 12px;
                align-items: flex-start;
                animation: homeFadeUp .62s cubic-bezier(.22, 1, .36, 1) both;
            }}
            .home-os-topbar h2 {{
                margin: 0;
                font-size: clamp(25px, 3vw, 38px);
                line-height: 1.02;
            }}
            .home-os-topbar p {{
                margin: 6px 0 0;
                font-size: 13px;
            }}
            .home-live-strip {{
                display: flex;
                align-items: center;
                gap: 8px;
                width: fit-content;
                max-width: 100%;
                min-height: 38px;
                border: 1px solid rgba(243,199,106,.22);
                border-radius: 999px;
                padding: 7px 10px;
                background: rgba(0,0,0,.26);
                color: var(--muted);
                font-size: 12px;
                font-weight: 800;
                animation: homeFadeUp .62s cubic-bezier(.22, 1, .36, 1) .08s both;
            }}
            .home-live-strip strong {{
                color: var(--ink);
            }}
            .home-live-dot {{
                width: 7px;
                height: 7px;
                border-radius: 999px;
                background: var(--teal);
                box-shadow: 0 0 0 rgba(69,213,199,.48);
                animation: homeLivePulse 1.8s ease-out infinite;
            }}
            .home-source-chip {{
                border: 1px solid rgba(255,255,255,.12);
                border-radius: 999px;
                padding: 3px 7px;
                color: var(--brand-strong);
                background: rgba(243,199,106,.07);
                white-space: nowrap;
            }}
            .home-os-board {{
                position: relative;
                display: grid;
                grid-template-columns: minmax(200px, .78fr) minmax(245px, 1fr) minmax(210px, .74fr);
                gap: 1px;
                border: 1px solid rgba(255,255,255,.12);
                border-radius: 12px;
                overflow: hidden;
                background: rgba(255,255,255,.12);
                animation: homeFadeUp .7s cubic-bezier(.22, 1, .36, 1) .14s both;
            }}
            .home-os-board::before {{
                content: "";
                position: absolute;
                z-index: 2;
                left: 7%;
                right: 7%;
                top: 49%;
                height: 1px;
                pointer-events: none;
                background: linear-gradient(90deg, transparent, rgba(243,199,106,.62), rgba(69,213,199,.42), transparent);
                opacity: 0;
                transform: translateX(-18%);
                animation: homeConnectionBeam 4.6s ease-in-out infinite;
            }}
            .home-os-column {{
                display: grid;
                align-content: start;
                gap: 10px;
                padding: 13px;
                background: rgba(12,12,13,.97);
            }}
            .home-os-column-title {{
                color: var(--muted);
                font-size: 12px;
                font-weight: 900;
                text-transform: uppercase;
            }}
            .home-ticket,
            .home-os-panel {{
                border: 1px solid rgba(255,255,255,.12);
                border-radius: 8px;
                background: rgba(255,255,255,.025);
            }}
            .home-ticket {{
                position: relative;
                padding: 11px;
                transition: transform .22s cubic-bezier(.22, 1, .36, 1), border-color .22s ease, background .22s ease, box-shadow .22s ease;
                animation: homeTicketIn .52s cubic-bezier(.22, 1, .36, 1) both;
                animation-delay: calc(var(--i, 0) * 70ms + 220ms);
            }}
            .home-ticket.priority {{
                border-color: rgba(243,199,106,.6);
                background: rgba(243,199,106,.08);
                transform: translateY(-2px);
                box-shadow: 0 14px 34px rgba(0,0,0,.28), 0 0 0 1px rgba(243,199,106,.08);
            }}
            .home-ticket.priority::after {{
                content: "";
                position: absolute;
                inset: -2px;
                border-radius: 10px;
                border: 1px solid rgba(243,199,106,.38);
                opacity: 0;
                animation: homePriorityPulse 2.4s ease-out infinite;
            }}
            .home-ticket strong,
            .home-os-panel strong {{
                display: block;
                color: var(--ink);
                margin-bottom: 4px;
            }}
            .home-ticket span,
            .home-os-panel span,
            .home-os-panel p {{
                display: block;
                color: var(--muted);
                font-size: 12px;
                line-height: 1.45;
                margin: 0;
            }}
            .home-os-panel {{
                padding: 12px;
                background: rgba(0,0,0,.26);
                transition: border-color .22s ease, background .22s ease, transform .22s cubic-bezier(.22, 1, .36, 1);
                animation: homePanelIn .52s cubic-bezier(.22, 1, .36, 1) both;
                animation-delay: calc(var(--i, 0) * 80ms + 320ms);
            }}
            .home-os-panel.next {{
                border-color: rgba(243,199,106,.36);
                background: rgba(243,199,106,.075);
                animation-name: homePanelIn, homeReplyGlow;
                animation-duration: .52s, 3.4s;
                animation-delay: calc(var(--i, 0) * 80ms + 320ms), 1s;
                animation-timing-function: cubic-bezier(.22, 1, .36, 1), ease-in-out;
                animation-fill-mode: both, none;
                animation-iteration-count: 1, infinite;
            }}
            .home-os-panel.is-refreshing {{
                transform: translateY(-2px);
                border-color: rgba(69,213,199,.34);
            }}
            @keyframes homeAmbientDrift {{
                from {{ transform: translate3d(-1.5%, -1%, 0) scale(1); }}
                to {{ transform: translate3d(1.5%, 1%, 0) scale(1.03); }}
            }}
            @keyframes homeScanPass {{
                0%, 18% {{ transform: translateX(-120%); opacity: 0; }}
                36%, 58% {{ opacity: .9; }}
                78%, 100% {{ transform: translateX(120%); opacity: 0; }}
            }}
            @keyframes homeLivePulse {{
                0% {{ box-shadow: 0 0 0 0 rgba(69,213,199,.46); }}
                72%, 100% {{ box-shadow: 0 0 0 10px rgba(69,213,199,0); }}
            }}
            @keyframes homeFadeUp {{
                from {{ opacity: 0; transform: translateY(12px); filter: blur(6px); }}
                to {{ opacity: 1; transform: translateY(0); filter: blur(0); }}
            }}
            @keyframes homeTicketIn {{
                from {{ opacity: 0; transform: translateY(10px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}
            @keyframes homePanelIn {{
                from {{ opacity: 0; transform: translateY(10px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}
            @keyframes homePriorityPulse {{
                0% {{ opacity: .5; transform: scale(.99); }}
                76%, 100% {{ opacity: 0; transform: scale(1.045); }}
            }}
            @keyframes homeReplyGlow {{
                0%, 100% {{ box-shadow: 0 0 0 rgba(243,199,106,0); }}
                50% {{ box-shadow: 0 0 28px rgba(243,199,106,.14); }}
            }}
            @keyframes homeConnectionBeam {{
                0%, 24% {{ opacity: 0; transform: translateX(-18%); }}
                38%, 58% {{ opacity: .72; }}
                82%, 100% {{ opacity: 0; transform: translateX(18%); }}
            }}
            @media (max-width: 980px) {{
                .home-sales-os {{
                    grid-template-columns: 1fr;
                    padding-top: 30px;
                }}
                .home-os-preview {{
                    grid-template-columns: 1fr;
                    min-height: 0;
                }}
                .home-os-sidebar {{
                    display: none;
                }}
            }}
            @media (max-width: 680px) {{
                .home-sales-copy h1 {{
                    font-size: 36px;
                }}
                .home-os-preview {{
                    padding: 12px;
                }}
                .home-os-topbar {{
                    display: grid;
                }}
                .home-os-board {{
                    grid-template-columns: 1fr;
                }}
                .home-os-column {{
                    padding: 12px;
                }}
                .home-os-column.ai-note .home-os-panel:not(.next) {{
                    display: none;
                }}
                .home-live-strip {{
                    width: 100%;
                }}
            }}
            @media (prefers-reduced-motion: reduce) {{
                .home-os-preview::before,
                .home-os-preview::after,
                .home-os-board::before,
                .home-live-dot,
                .home-os-topbar,
                .home-live-strip,
                .home-os-board,
                .home-ticket,
                .home-ticket.priority::after,
                .home-os-panel,
                .home-os-panel.next {{
                    animation: none !important;
                    transition-duration: .01ms !important;
                    transform: none !important;
                    filter: none !important;
                }}
            }}
        </style>
        <section class="home-sales-os">
            <div class="home-sales-copy">
                <div class="product-controls">
                    <div class="glass-control label-free">
                        <div class="language-toggle" aria-label="Language" id="langToggle">
                            <button type="button" class="active" onclick="setProductLang('en')" id="langEn">EN</button>
                            <button type="button" onclick="setProductLang('zh')" id="langZh">中文</button>
                        </div>
                    </div>
                    <div class="glass-control label-free">
                        <div class="language-toggle" aria-label="Market" id="marketToggle">
                            <button type="button" class="active" onclick="setProductMarket('sg')" id="marketSg">Singapore</button>
                            <button type="button" onclick="setProductMarket('my')" id="marketMy">Malaysia</button>
                        </div>
                    </div>
                </div>
                <div class="eyebrow">NexaFlow Enquiry</div>
                <h1><span data-lang="en">Know who to follow up next.</span><span data-lang="zh" class="lang-hidden">知道下一位该跟进谁。</span></h1>
                <p class="lead"><span data-lang="en">One sales inbox for scattered customer enquiries. NexaFlow shows priority, customer needs, missing details, and the next reply direction.</span><span data-lang="zh" class="lang-hidden">把分散的客户询问集中到一个销售 inbox。NexaFlow 会显示优先级、客户需求、还缺什么，以及下一句回复方向。</span></p>
                <p class="lead"><span data-market="sg">Built for Singapore businesses that receive enquiries from WhatsApp, Instagram, Facebook, TikTok, Xiaohongshu, calls, and referrals.</span><span data-market="my" class="market-hidden">Built for Malaysia businesses that receive enquiries from WhatsApp, Instagram, Facebook, TikTok, Xiaohongshu, calls, and referrals.</span></p>
                <div class="actions">
                    <a class="btn" href="/merchant-signup"><span data-lang="en">Create Enquiry Inbox</span><span data-lang="zh" class="lang-hidden">创建询盘 inbox</span></a>
                    <a class="btn secondary" href="/dealer-demo"><span data-lang="en">See Sales Demo</span><span data-lang="zh" class="lang-hidden">看销售 Demo</span></a>
                </div>
            </div>
            <div class="home-os-preview" id="homeSalesPreview" aria-label="NexaFlow sales inbox preview">
                <aside class="home-os-sidebar" aria-hidden="true">
                    <strong>NexaFlow</strong>
                    <div class="home-os-nav-item active"><span>Today</span><span>12</span></div>
                    <div class="home-os-nav-item"><span>Need reply</span><span>5</span></div>
                    <div class="home-os-nav-item"><span>Appointments</span><span>3</span></div>
                    <div class="home-os-nav-item"><span>Follow-up</span><span>8</span></div>
                </aside>
                <div class="home-os-workspace">
                    <div class="home-os-topbar">
                        <div>
                            <span class="option-kicker">Today&apos;s queue</span>
                            <h2><span data-lang="en">Customer enquiries, sorted by next action.</span><span data-lang="zh" class="lang-hidden">客户询问，按下一步排序。</span></h2>
                            <p><span data-lang="en">The first view is not settings. It is the next customer to handle.</span><span data-lang="zh" class="lang-hidden">第一眼看到的不是设置，而是下一位要处理的客户。</span></p>
                        </div>
                        <span class="pill good">Live demo</span>
                    </div>
                    <div class="home-live-strip" aria-live="polite"><span class="home-live-dot"></span><span id="homeLiveText"><span data-lang="en">Live sorting <strong>WhatsApp Quote Lead</strong></span><span data-lang="zh" class="lang-hidden">正在排序 <strong>WhatsApp 报价客户</strong></span></span><span class="home-source-chip" id="homeLiveSource">WhatsApp</span></div>
                    <div class="home-os-board">
                        <div class="home-os-column">
                            <div class="home-os-column-title"><span data-lang="en">Customer queue</span><span data-lang="zh" class="lang-hidden">客户队列</span></div>
                            <div class="home-ticket priority" data-home-ticket="0" style="--i:0">
                                <strong><span data-lang="en">WhatsApp Quote Lead</span><span data-lang="zh" class="lang-hidden">WhatsApp 报价客户</span></strong>
                                <span><span data-lang="en">Reply first · price / timing</span><span data-lang="zh" class="lang-hidden">优先回复 · 价格 / 时间</span></span>
                            </div>
                            <div class="home-ticket" data-home-ticket="1" style="--i:1">
                                <strong><span data-lang="en">Instagram Service Lead</span><span data-lang="zh" class="lang-hidden">Instagram 服务询问</span></strong>
                                <span><span data-lang="en">Comparing packages</span><span data-lang="zh" class="lang-hidden">正在比较配套</span></span>
                            </div>
                            <div class="home-ticket" data-home-ticket="2" style="--i:2">
                                <strong><span data-lang="en">TikTok Appointment Lead</span><span data-lang="zh" class="lang-hidden">TikTok 预约客户</span></strong>
                                <span><span data-lang="en">Booking not confirmed</span><span data-lang="zh" class="lang-hidden">预约还没确认</span></span>
                            </div>
                        </div>
                        <div class="home-os-column ai-note">
                            <div class="home-os-column-title"><span data-lang="en">AI note</span><span data-lang="zh" class="lang-hidden">AI 重点</span></div>
                            <div class="home-os-panel" data-home-dynamic="request" style="--i:0">
                                <strong><span data-lang="en">Customer needs</span><span data-lang="zh" class="lang-hidden">客户需求</span></strong>
                                <span><span data-lang="en">Package price, available slot, and whether a deposit is needed.</span><span data-lang="zh" class="lang-hidden">配套价格、可预约时间，以及是否需要订金。</span></span>
                            </div>
                            <div class="home-os-panel" data-home-dynamic="stuck" style="--i:1">
                                <strong><span data-lang="en">Stuck on</span><span data-lang="zh" class="lang-hidden">客户卡点</span></strong>
                                <span><span data-lang="en">Price range and appointment timing are not confirmed.</span><span data-lang="zh" class="lang-hidden">价格范围和预约时间还没确认。</span></span>
                            </div>
                            <div class="home-os-panel next" data-home-dynamic="next" style="--i:2">
                                <strong><span data-lang="en">Next question</span><span data-lang="zh" class="lang-hidden">下一句问题</span></strong>
                                <p><span data-lang="en">Ask for date, budget range, and package preference before pushing for appointment.</span><span data-lang="zh" class="lang-hidden">先问日期、预算范围和想了解的配套，再推进预约。</span></p>
                            </div>
                        </div>
                        <div class="home-os-column">
                            <div class="home-os-column-title"><span data-lang="en">Follow-up</span><span data-lang="zh" class="lang-hidden">跟进</span></div>
                            <div class="home-os-panel next" data-home-dynamic="reply" style="--i:0">
                                <strong><span data-lang="en">Next reply</span><span data-lang="zh" class="lang-hidden">下一句回复</span></strong>
                                <p><span data-lang="en">Hi, can I confirm your preferred date and budget range first? Then I can quote the right package.</span><span data-lang="zh" class="lang-hidden">你好，可以先确认你想预约的日期和预算范围吗？我再帮你报价合适的配套。</span></p>
                            </div>
                            <a class="btn" href="/dealer-demo"><span data-lang="en">Open sample queue</span><span data-lang="zh" class="lang-hidden">打开示例队列</span></a>
                            <a class="btn secondary" href="/merchant-login"><span data-lang="en">Merchant Login</span><span data-lang="zh" class="lang-hidden">商家登录</span></a>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <div class="section-head compact-section" id="services">
            <div>
                <h2><span data-lang="en">What your sales team sees every day</span><span data-lang="zh" class="lang-hidden">销售每天看到什么</span></h2>
                <p><span data-lang="en">A simple queue for businesses that need fewer missed messages and clearer follow-up.</span><span data-lang="zh" class="lang-hidden">给商家和销售团队用的简单队列：少漏消息，也知道下一步怎么跟。</span></p>
            </div>
        </div>
        <section class="grid">
            <div class="card">
                <h3><span data-lang="en">All messages in one queue</span><span data-lang="zh" class="lang-hidden">所有消息进一个队列</span></h3>
                <p><span data-lang="en">WhatsApp, Instagram, Facebook, TikTok, Xiaohongshu, calls, and referrals can be worked from one place.</span><span data-lang="zh" class="lang-hidden">WhatsApp、Instagram、Facebook、TikTok、小红书、电话和介绍来的客户，都在一个地方处理。</span></p>
                <a class="btn" href="/merchant-signup"><span data-lang="en">Create My Enquiry Inbox</span><span data-lang="zh" class="lang-hidden">创建我的询盘 inbox</span></a>
            </div>
            <div class="card">
                <h3><span data-lang="en">Know what is missing</span><span data-lang="zh" class="lang-hidden">知道还缺什么</span></h3>
                <p><span data-lang="en">AI shows whether the customer is stuck on budget, timing, quotation, requirements, documents, or comparison.</span><span data-lang="zh" class="lang-hidden">AI 告诉你客户是卡在预算、时间、报价、需求、资料，还是比较不同选择。</span></p>
                <a class="btn secondary" href="/dealer-demo"><span data-lang="en">Try Demo</span><span data-lang="zh" class="lang-hidden">试用 Demo</span></a>
            </div>
            <div class="card">
                <h3><span data-lang="en">Next reply ready</span><span data-lang="zh" class="lang-hidden">下一句回复准备好</span></h3>
                <p><span data-lang="en">Sales get a clear next message, then mark replied, follow up later, or book an appointment.</span><span data-lang="zh" class="lang-hidden">销售拿到下一句回复，再标记已回、之后跟进，或安排预约。</span></p>
                <a class="btn secondary" href="/merchant-login"><span data-lang="en">Open Inbox</span><span data-lang="zh" class="lang-hidden">打开 Inbox</span></a>
            </div>
        </section>

        <div class="section-head">
            <div>
                <h2><span data-lang="en">Bring customer messages into the queue</span><span data-lang="zh" class="lang-hidden">把客户消息带进销售队列</span></h2>
                <p><span data-lang="en">For the pilot, use buyer links plus manual or assisted DM capture today. Meta auto-sync for WhatsApp, Facebook, and Instagram can be switched on after Meta Developer setup is approved.</span><span data-lang="zh" class="lang-hidden">试用阶段今天先用买家 link，加上手动或辅助导入私信。WhatsApp、Facebook 和 Instagram 的 Meta 自动同步，等 Meta Developer 设置通过后再开启。</span></p>
            </div>
        </div>
        <section class="grid">
            <div class="card accent-card">
                <h3><span data-lang="en">Meta sync after approval</span><span data-lang="zh" class="lang-hidden">Meta 通过后自动同步</span></h3>
                <p><span data-lang="en">Official WhatsApp Business, Instagram, and Facebook messaging sync requires Meta account access, webhook setup, and approval. Until then, the sales queue still works with manual capture.</span><span data-lang="zh" class="lang-hidden">官方 WhatsApp Business、Instagram 和 Facebook 消息同步需要 Meta 账号权限、webhook 设置和审核。通过前，销售队列仍然可以用手动导入来试。</span></p>
            </div>
            <div class="card">
                <h3><span data-lang="en">Paste or screenshot when needed</span><span data-lang="zh" class="lang-hidden">需要时复制或截图</span></h3>
                <p><span data-lang="en">For TikTok, Xiaohongshu, calls, and referrals, paste the message or source note so AI can make a follow-up card.</span><span data-lang="zh" class="lang-hidden">TikTok、小红书、电话和介绍来的客户，可以复制内容或来源备注，让 AI 变成跟进卡。</span></p>
            </div>
            <div class="card">
                <h3><span data-lang="en">Reply in the right app</span><span data-lang="zh" class="lang-hidden">回到原平台回复</span></h3>
                <p><span data-lang="en">NexaFlow keeps the source, stuck point, next reply, and reminder while sales reply in WhatsApp or the original app.</span><span data-lang="zh" class="lang-hidden">NexaFlow 记录来源、卡点、下一句回复和提醒；销售再回 WhatsApp 或原平台回复。</span></p>
            </div>
        </section>

        <div class="section-head">
            <div>
                <h2><span data-lang="en">Simple pricing after trial</span><span data-lang="zh" class="lang-hidden">试用后的简单价格</span></h2>
                <p><span data-lang="en">Start with a 30-day trial. Pay only when the enquiry workflow is useful for your business.</span><span data-lang="zh" class="lang-hidden">先试用 30 天。确认询盘流程对你的生意有帮助后再付费。</span></p>
            </div>
        </div>
        <section class="pricing-grid">
            <div class="price-card trial">
                <div>
                    <h3><span data-lang="en">30-day trial</span><span data-lang="zh" class="lang-hidden">30 天试用</span></h3>
                    <div class="plan-price">Free <span>for trial</span></div>
                    <p><span data-lang="en">Test the full enquiry workflow with real customer messages before choosing a monthly plan.</span><span data-lang="zh" class="lang-hidden">先用真实客户消息测试完整询盘流程，再选择月费配套。</span></p>
                </div>
                <div>
                    <ul>
                        <li><span data-lang="en">Private enquiry inbox</span><span data-lang="zh" class="lang-hidden">私密询盘 inbox</span></li>
                        <li><span data-lang="en">AI classification, stuck point, and reply draft</span><span data-lang="zh" class="lang-hidden">AI 分类、客户卡点和回复草稿</span></li>
                        <li><span data-lang="en">Follow-up reminders</span><span data-lang="zh" class="lang-hidden">跟进提醒</span></li>
                    </ul>
                    <a class="btn" href="/merchant-signup"><span data-lang="en">Create Enquiry Inbox</span><span data-lang="zh" class="lang-hidden">创建询盘 inbox</span></a>
                </div>
            </div>
            <div class="price-card">
                <h3><span data-lang="en">Enquiry Starter</span><span data-lang="zh" class="lang-hidden">询盘入门版</span></h3>
                <div class="plan-price"><span data-market="sg">SGD 49</span><span data-market="my" class="market-hidden">MYR 169</span> <span>/ month</span></div>
                <p><span data-lang="en">Full AI follow-up for solo owners and small teams handling up to 100 enquiries per month.</span><span data-lang="zh" class="lang-hidden">完整 AI 跟进功能，适合每月 100 个询盘以内的老板或小团队。</span></p>
                <ul>
                    <li><span data-lang="en">Up to 100 enquiries / month</span><span data-lang="zh" class="lang-hidden">每月最多 100 个询盘</span></li>
                    <li><span data-lang="en">AI priority, category, and stuck-point detection</span><span data-lang="zh" class="lang-hidden">AI 优先级、分类和客户卡点判断</span></li>
                    <li><span data-lang="en">Next reply drafts and follow-up reminders</span><span data-lang="zh" class="lang-hidden">下一句回复草稿和跟进提醒</span></li>
                    <li><span data-lang="en">Manual, paste, screenshot, and source tagging</span><span data-lang="zh" class="lang-hidden">手动新增、复制、截图和来源标记</span></li>
                </ul>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Ask on WhatsApp</span><span data-lang="zh" class="lang-hidden">WhatsApp 咨询</span></a>
            </div>
            <div class="price-card highlight">
                <h3>Enquiry Growth</h3>
                <div class="plan-price"><span data-market="sg">SGD 89</span><span data-market="my" class="market-hidden">MYR 299</span> <span>/ month</span></div>
                <p><span data-lang="en">For teams handling 101-500 enquiries per month with daily follow-up work.</span><span data-lang="zh" class="lang-hidden">适合每月 101-500 个询盘、每天都需要跟进的团队。</span></p>
                <ul>
                    <li><span data-lang="en">101-500 enquiries / month</span><span data-lang="zh" class="lang-hidden">每月 101-500 个询盘</span></li>
                    <li><span data-lang="en">Everything in Starter</span><span data-lang="zh" class="lang-hidden">包含 Starter 全部功能</span></li>
                    <li><span data-lang="en">Shared team queue and follow-up dashboard</span><span data-lang="zh" class="lang-hidden">团队共享队列和跟进看板</span></li>
                    <li><span data-lang="en">Source visibility and setup support</span><span data-lang="zh" class="lang-hidden">来源可视化和设置协助</span></li>
                </ul>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Ask on WhatsApp</span><span data-lang="zh" class="lang-hidden">WhatsApp 咨询</span></a>
            </div>
            <div class="price-card">
                <h3>Business</h3>
                <div class="plan-price"><span data-market="sg">SGD 149+</span><span data-market="my" class="market-hidden">MYR 499+</span> <span>/ month</span></div>
                <p><span data-lang="en">For 500+ enquiries per month, multiple outlets, official channel setup, or custom workflow needs.</span><span data-lang="zh" class="lang-hidden">适合每月 500+ 询盘、多分店、官方渠道接入或客制流程。</span></p>
                <ul>
                    <li><span data-lang="en">500+ enquiries / month or custom volume</span><span data-lang="zh" class="lang-hidden">每月 500+ 询盘或客制用量</span></li>
                    <li><span data-lang="en">Multiple outlets or workspaces</span><span data-lang="zh" class="lang-hidden">多分店或多个 workspace</span></li>
                    <li><span data-lang="en">Official channel setup assistance</span><span data-lang="zh" class="lang-hidden">官方渠道接入协助</span></li>
                    <li><span data-lang="en">Custom workflow, export, and priority support</span><span data-lang="zh" class="lang-hidden">客制流程、导出和优先支持</span></li>
                </ul>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Request Business</span><span data-lang="zh" class="lang-hidden">申请 Business</span></a>
            </div>
        </section>

        <div class="section-head">
            <div>
                <h2><span data-lang="en">The daily flow</span><span data-lang="zh" class="lang-hidden">每天使用流程</span></h2>
                <p><span data-lang="en">No complicated CRM routine. Sales just opens the queue and handles the next customer.</span><span data-lang="zh" class="lang-hidden">不用复杂 CRM 流程。销售打开队列，处理下一位客户。</span></p>
            </div>
        </div>
        <section class="steps">
            <div class="step"><div><strong><span data-lang="en">Open today&apos;s queue</span><span data-lang="zh" class="lang-hidden">打开今天队列</span></strong><p><span data-lang="en">See the customers who need attention first.</span><span data-lang="zh" class="lang-hidden">先看今天最需要处理的客户。</span></p></div></div>
            <div class="step"><div><strong><span data-lang="en">Check the AI note</span><span data-lang="zh" class="lang-hidden">看 AI 提醒</span></strong><p><span data-lang="en">Know what they asked for, what they are stuck on, and what to ask next.</span><span data-lang="zh" class="lang-hidden">知道客户问什么、卡在哪里、下一句问什么。</span></p></div></div>
            <div class="step"><div><strong><span data-lang="en">Reply or set reminder</span><span data-lang="zh" class="lang-hidden">回复或设提醒</span></strong><p><span data-lang="en">Copy the next reply, then mark replied, remind later, or book an appointment.</span><span data-lang="zh" class="lang-hidden">复制下一句回复，然后标记已回、之后提醒，或安排预约。</span></p></div></div>
        </section>

        <div class="section-head">
            <div>
                <h2><span data-lang="en">Trust and data protection</span><span data-lang="zh" class="lang-hidden">信任与资料保护</span></h2>
            </div>
        </div>
        <section class="trust-strip">
            <p><span data-lang="en">Customer enquiry data stays behind a private inbox and should be used only for replies, quotations, appointments, follow-up, support, and required records. Merchants can delete individual enquiries when a record is no longer needed.</span><span data-lang="zh" class="lang-hidden">客户询盘资料会放在私密 inbox 里，并且应只用于回复、报价、预约、跟进、客服和必要记录。当记录不再需要时，商家可以删除单个客户询盘。</span></p>
            <p><a href="/privacy">Privacy Policy</a> · <a href="/terms">Terms</a> · <a href="/refund-policy">Refund Policy</a> · <a href="/acceptable-use">Acceptable Use</a></p>
        </section>
        <script>
            function setProductLang(lang) {{
                document.querySelectorAll("[data-lang]").forEach(item => {{
                    item.classList.toggle("lang-hidden", item.dataset.lang !== lang);
                }});
                document.getElementById("langEn").classList.toggle("active", lang === "en");
                document.getElementById("langZh").classList.toggle("active", lang === "zh");
                document.getElementById("langToggle").classList.toggle("is-second", lang === "zh");
                localStorage.setItem("nexaflow_home_lang", lang);
            }}
            function setProductMarket(market) {{
                document.querySelectorAll("[data-market]").forEach(item => {{
                    item.classList.toggle("market-hidden", item.dataset.market !== market);
                }});
                document.getElementById("marketSg").classList.toggle("active", market === "sg");
                document.getElementById("marketMy").classList.toggle("active", market === "my");
                document.getElementById("marketToggle").classList.toggle("is-second", market === "my");
                localStorage.setItem("nexaflow_home_market", market);
            }}
            setProductLang(localStorage.getItem("nexaflow_home_lang") || "en");
            setProductMarket(localStorage.getItem("nexaflow_home_market") || "sg");
            (function () {{
                const preview = document.getElementById("homeSalesPreview");
                const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                if (!preview || reduce) {{
                    return;
                }}
                const states = [
                    {{
                        source: "WhatsApp",
                        lead: "WhatsApp Quote Lead",
                        leadZh: "WhatsApp 报价客户",
                        requestEn: "Package price, available slot, and whether a deposit is needed.",
                        requestZh: "配套价格、可预约时间，以及是否需要订金。",
                        stuckEn: "Price range and appointment timing are not confirmed.",
                        stuckZh: "价格范围和预约时间还没确认。",
                        nextEn: "Ask for date, budget range, and package preference before pushing for appointment.",
                        nextZh: "先问日期、预算范围和想了解的配套，再推进预约。",
                        replyEn: "Hi, can I confirm your preferred date and budget range first? Then I can quote the right package.",
                        replyZh: "你好，可以先确认你想预约的日期和预算范围吗？我再帮你报价合适的配套。"
                    }},
                    {{
                        source: "Instagram",
                        lead: "Instagram Service Lead",
                        leadZh: "Instagram 服务询问",
                        requestEn: "Asked for package differences, availability, and which option is better.",
                        requestZh: "客户在问配套差别、可预约时间，以及哪个选择比较适合。",
                        stuckEn: "Customer is comparing options and has not chosen a package yet.",
                        stuckZh: "客户还在比较不同选择，还没决定配套。",
                        nextEn: "Ask what they are comparing against and recommend one package based on budget.",
                        nextZh: "先问客户正在比较什么，再根据预算推荐一个配套。",
                        replyEn: "I can help compare. Which package or price are you looking at now, and what budget range should I keep to?",
                        replyZh: "我可以帮你比较。你现在在看哪个配套或价格？预算大概想控制在多少？"
                    }},
                    {{
                        source: "TikTok",
                        lead: "TikTok Appointment Lead",
                        leadZh: "TikTok 预约客户",
                        requestEn: "Interested after seeing a TikTok post but has not confirmed a visit time.",
                        requestZh: "客户看了 TikTok 后有兴趣，但还没确认预约时间。",
                        stuckEn: "Appointment timing is the blocker; do not push too many details yet.",
                        stuckZh: "目前卡在预约时间，不要一次推太多细节。",
                        nextEn: "Offer two simple appointment windows and ask which is easier.",
                        nextZh: "给两个简单的预约时间选择，让客户选比较方便的。",
                        replyEn: "Would today evening or tomorrow afternoon be easier for you? I can reserve a slot first.",
                        replyZh: "今天傍晚或明天下午哪个比较方便？我可以先帮你保留时间。"
                    }}
                ];
                const tickets = Array.from(preview.querySelectorAll("[data-home-ticket]"));
                const liveText = preview.querySelector("#homeLiveText");
                const liveSource = preview.querySelector("#homeLiveSource");
                const panels = {{
                    request: preview.querySelector("[data-home-dynamic='request']"),
                    stuck: preview.querySelector("[data-home-dynamic='stuck']"),
                    next: preview.querySelector("[data-home-dynamic='next']"),
                    reply: preview.querySelector("[data-home-dynamic='reply']")
                }};
                let index = 0;
                function setPanelText(panel, en, zh) {{
                    if (!panel) {{
                        return;
                    }}
                    const body = Array.from(panel.children).find(child => child.tagName === "SPAN" || child.tagName === "P");
                    if (!body) {{
                        return;
                    }}
                    body.innerHTML = '<span data-lang="en">' + en + '</span><span data-lang="zh" class="lang-hidden">' + zh + '</span>';
                    panel.classList.remove("is-refreshing");
                    void panel.offsetWidth;
                    panel.classList.add("is-refreshing");
                    window.setTimeout(() => panel.classList.remove("is-refreshing"), 320);
                }}
                function setHomeState(nextIndex) {{
                    index = nextIndex % states.length;
                    const state = states[index];
                    tickets.forEach((ticket, ticketIndex) => {{
                        ticket.classList.toggle("priority", ticketIndex === index);
                    }});
                    if (liveText) {{
                        liveText.innerHTML = '<span data-lang="en">Live sorting <strong>' + state.lead + '</strong></span><span data-lang="zh" class="lang-hidden">正在排序 <strong>' + state.leadZh + '</strong></span>';
                    }}
                    if (liveSource) {{
                        liveSource.textContent = state.source;
                    }}
                    setPanelText(panels.request, state.requestEn, state.requestZh);
                    setPanelText(panels.stuck, state.stuckEn, state.stuckZh);
                    setPanelText(panels.next, state.nextEn, state.nextZh);
                    setPanelText(panels.reply, state.replyEn, state.replyZh);
                    setProductLang(localStorage.getItem("nexaflow_home_lang") || "en");
                }}
                window.setInterval(() => setHomeState(index + 1), 3400);
            }})();
        </script>
        """,
        show_sales_contact=True,
    )


@app.get("/ui-options", response_class=HTMLResponse)
def ui_options_page():
    return merchant_html(
        "NexaFlow UI Options",
        "NexaFlow",
        """
        <style>
            .ui-options-page {
                --option-line: rgba(255,255,255,.12);
                --option-panel: rgba(12,12,13,.82);
                --option-panel-strong: rgba(20,19,18,.94);
                display: grid;
                gap: 28px;
            }
            .ui-options-intro {
                max-width: 820px;
                padding: 26px 0 4px;
            }
            .ui-options-intro h1 {
                max-width: 760px;
                margin-bottom: 12px;
            }
            .ui-options-intro .lead {
                max-width: 760px;
            }
            .option-nav {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-top: 18px;
            }
            .option-nav a {
                display: inline-flex;
                align-items: center;
                min-height: 36px;
                border: 1px solid var(--option-line);
                border-radius: 999px;
                padding: 7px 11px;
                color: var(--ink);
                text-decoration: none;
                background: rgba(255,255,255,.035);
                font-weight: 800;
                font-size: 13px;
            }
            .option-shell {
                border: 1px solid var(--option-line);
                border-radius: 14px;
                overflow: hidden;
                background:
                    radial-gradient(circle at 12% 18%, rgba(243,199,106,.12), transparent 28%),
                    linear-gradient(135deg, rgba(255,255,255,.055), rgba(255,255,255,.018)),
                    #050505;
                box-shadow: 0 28px 90px rgba(0,0,0,.34);
            }
            .option-head {
                display: grid;
                grid-template-columns: minmax(0, 1fr) auto;
                gap: 14px;
                align-items: start;
                padding: 18px;
                border-bottom: 1px solid var(--option-line);
                background: rgba(255,255,255,.028);
            }
            .option-kicker {
                display: block;
                color: var(--brand-strong);
                font-size: 12px;
                font-weight: 900;
                text-transform: uppercase;
                margin-bottom: 4px;
            }
            .option-head h2 {
                margin: 0 0 6px;
                font-size: 26px;
            }
            .option-head p {
                margin: 0;
                max-width: 760px;
            }
            .option-tag {
                border: 1px solid rgba(243,199,106,.42);
                border-radius: 999px;
                padding: 7px 10px;
                color: var(--brand-strong);
                font-size: 12px;
                font-weight: 900;
                background: rgba(243,199,106,.07);
                white-space: nowrap;
            }
            .option-screen {
                min-height: 560px;
                padding: 24px;
            }
            .option-screen * {
                min-width: 0;
            }
            .tiny-toggle-row {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-bottom: 20px;
            }
            .tiny-glass {
                display: inline-grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                position: relative;
                width: fit-content;
                border: 1px solid rgba(255,255,255,.18);
                border-radius: 999px;
                padding: 3px;
                background:
                    linear-gradient(135deg, rgba(255,255,255,.16), rgba(255,255,255,.035)),
                    rgba(255,255,255,.045);
                box-shadow: inset 0 1px 0 rgba(255,255,255,.2), 0 12px 30px rgba(0,0,0,.22);
                backdrop-filter: blur(18px) saturate(160%);
            }
            .tiny-glass::after {
                content: "";
                position: absolute;
                top: 3px;
                bottom: 3px;
                left: 3px;
                width: calc((100% - 6px) / 2);
                border-radius: 999px;
                background:
                    radial-gradient(circle at 24% 16%, rgba(255,255,255,.95), rgba(255,255,255,.3) 38%, transparent 66%),
                    linear-gradient(135deg, rgba(255,255,255,.9), rgba(243,199,106,.82));
                box-shadow: inset 0 1px 0 rgba(255,255,255,.92), 0 5px 16px rgba(0,0,0,.3);
            }
            .tiny-glass span {
                position: relative;
                z-index: 1;
                display: inline-flex;
                justify-content: center;
                align-items: center;
                min-width: 58px;
                min-height: 30px;
                padding: 4px 10px;
                color: rgba(247,243,234,.68);
                font-size: 12px;
                font-weight: 900;
            }
            .tiny-glass span:first-child {
                color: #050505;
            }
            .tiny-glass.market span {
                min-width: 84px;
            }
            .option-a-grid {
                display: grid;
                grid-template-columns: minmax(0, .92fr) minmax(360px, 1.08fr);
                gap: 26px;
                align-items: center;
            }
            .option-a-copy h3 {
                margin: 0 0 14px;
                font-size: clamp(38px, 6vw, 72px);
                line-height: .98;
                letter-spacing: 0;
            }
            .option-a-copy p {
                font-size: 18px;
                max-width: 600px;
            }
            .option-actions {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                margin-top: 22px;
            }
            .mock-btn {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                min-height: 42px;
                border-radius: 8px;
                padding: 10px 14px;
                text-decoration: none;
                font-weight: 900;
                background: var(--brand);
                color: #000;
            }
            .mock-btn.ghost {
                background: rgba(255,255,255,.035);
                color: var(--ink);
                border: 1px solid var(--option-line);
            }
            .calm-preview {
                border: 1px solid var(--option-line);
                border-radius: 12px;
                overflow: hidden;
                background: rgba(8,8,8,.92);
                box-shadow: 0 24px 80px rgba(0,0,0,.38);
            }
            .calm-preview-top {
                display: flex;
                justify-content: space-between;
                gap: 12px;
                padding: 14px;
                border-bottom: 1px solid var(--option-line);
                color: var(--muted);
                font-size: 13px;
                font-weight: 900;
            }
            .calm-preview-body {
                display: grid;
                grid-template-columns: minmax(150px, .75fr) minmax(220px, 1fr);
                gap: 1px;
                background: var(--option-line);
            }
            .calm-list,
            .calm-detail {
                background: rgba(12,12,13,.98);
                padding: 12px;
            }
            .calm-lead {
                border: 1px solid var(--option-line);
                border-radius: 8px;
                padding: 11px;
                margin-bottom: 8px;
                background: rgba(255,255,255,.025);
            }
            .calm-lead.active {
                border-color: rgba(243,199,106,.58);
                background: rgba(243,199,106,.08);
            }
            .calm-lead strong,
            .calm-detail-card strong {
                display: block;
                color: var(--ink);
                margin-bottom: 4px;
            }
            .calm-lead span,
            .calm-detail-card span,
            .calm-detail-card p {
                display: block;
                color: var(--muted);
                font-size: 12px;
                line-height: 1.45;
                margin: 0;
            }
            .calm-detail h4 {
                margin: 3px 0 10px;
                font-size: 20px;
            }
            .calm-detail-card {
                border: 1px solid var(--option-line);
                border-radius: 8px;
                padding: 10px;
                margin-bottom: 9px;
                background: rgba(0,0,0,.24);
            }
            .calm-detail-card.next {
                border-color: rgba(243,199,106,.34);
                background: rgba(243,199,106,.07);
            }
            .option-b-screen {
                display: grid;
                grid-template-columns: 210px minmax(0, 1fr);
                gap: 14px;
                background:
                    linear-gradient(90deg, rgba(243,199,106,.055), transparent 36%),
                    #050505;
                position: relative;
                overflow: hidden;
                isolation: isolate;
            }
            .option-b-screen::before {
                content: "";
                position: absolute;
                inset: -40%;
                pointer-events: none;
                background:
                    radial-gradient(circle at 18% 30%, rgba(243,199,106,.16), transparent 24%),
                    radial-gradient(circle at 82% 20%, rgba(69,213,199,.12), transparent 25%);
                opacity: .62;
                transform: translate3d(-1.5%, 0, 0);
                animation: bAmbientDrift 11s ease-in-out infinite alternate;
            }
            .option-b-screen::after {
                content: "";
                position: absolute;
                inset: 0;
                pointer-events: none;
                z-index: 0;
                background: linear-gradient(105deg, transparent 0%, transparent 38%, rgba(243,199,106,.08) 48%, transparent 62%, transparent 100%);
                transform: translateX(-120%);
                animation: bScanPass 6.8s ease-in-out infinite;
            }
            .b-sidebar {
                border: 1px solid var(--option-line);
                border-radius: 12px;
                padding: 14px;
                background: rgba(10,10,10,.86);
                display: grid;
                align-content: start;
                gap: 12px;
                position: relative;
                z-index: 1;
                backdrop-filter: blur(16px);
            }
            .b-sidebar strong {
                font-size: 18px;
            }
            .b-live-dot {
                width: 7px;
                height: 7px;
                border-radius: 999px;
                background: var(--teal);
                box-shadow: 0 0 0 rgba(69,213,199,.48);
                animation: bLivePulse 1.8s ease-out infinite;
            }
            .b-nav-item {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
                min-height: 38px;
                padding: 8px 10px;
                border-radius: 8px;
                color: var(--muted);
                font-size: 13px;
                font-weight: 800;
            }
            .b-nav-item.active {
                background: rgba(243,199,106,.12);
                color: var(--ink);
            }
            .b-workspace {
                display: grid;
                grid-template-rows: auto auto minmax(0, 1fr);
                gap: 12px;
                position: relative;
                z-index: 1;
            }
            .b-topbar {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                animation: bFadeUp .62s cubic-bezier(.22, 1, .36, 1) both;
            }
            .b-topbar h3 {
                margin: 0;
                font-size: clamp(28px, 4vw, 46px);
                line-height: 1.02;
            }
            .b-live-strip {
                display: flex;
                align-items: center;
                gap: 8px;
                min-height: 38px;
                border: 1px solid rgba(243,199,106,.22);
                border-radius: 999px;
                padding: 7px 10px;
                background: rgba(0,0,0,.26);
                color: var(--muted);
                font-size: 12px;
                font-weight: 800;
                width: fit-content;
                max-width: 100%;
                animation: bFadeUp .62s cubic-bezier(.22, 1, .36, 1) .08s both;
            }
            .b-live-strip strong {
                color: var(--ink);
                font-weight: 900;
            }
            .b-source-chip {
                border: 1px solid rgba(255,255,255,.12);
                border-radius: 999px;
                padding: 3px 7px;
                color: var(--brand-strong);
                background: rgba(243,199,106,.07);
                white-space: nowrap;
            }
            .b-board {
                display: grid;
                grid-template-columns: minmax(210px, .8fr) minmax(260px, 1fr) minmax(220px, .75fr);
                gap: 1px;
                border: 1px solid var(--option-line);
                border-radius: 12px;
                overflow: hidden;
                background: var(--option-line);
                position: relative;
                animation: bFadeUp .7s cubic-bezier(.22, 1, .36, 1) .14s both;
            }
            .b-board::before {
                content: "";
                position: absolute;
                z-index: 2;
                left: 7%;
                right: 7%;
                top: 49%;
                height: 1px;
                pointer-events: none;
                background: linear-gradient(90deg, transparent, rgba(243,199,106,.62), rgba(69,213,199,.42), transparent);
                opacity: 0;
                transform: translateX(-18%);
                animation: bConnectionBeam 4.6s ease-in-out infinite;
            }
            .b-column {
                background: rgba(12,12,13,.97);
                padding: 13px;
                display: grid;
                align-content: start;
                gap: 10px;
            }
            .b-column-title {
                color: var(--muted);
                font-size: 12px;
                font-weight: 900;
                text-transform: uppercase;
            }
            .b-ticket {
                border: 1px solid var(--option-line);
                border-radius: 8px;
                padding: 11px;
                background: rgba(255,255,255,.025);
                position: relative;
                transition: transform .22s cubic-bezier(.22, 1, .36, 1), border-color .22s ease, background .22s ease, box-shadow .22s ease;
                animation: bTicketIn .52s cubic-bezier(.22, 1, .36, 1) both;
                animation-delay: calc(var(--i, 0) * 70ms + 220ms);
            }
            .b-ticket.priority {
                border-color: rgba(243,199,106,.6);
                background: rgba(243,199,106,.08);
                transform: translateY(-2px);
                box-shadow: 0 14px 34px rgba(0,0,0,.28), 0 0 0 1px rgba(243,199,106,.08);
            }
            .b-ticket.priority::after {
                content: "";
                position: absolute;
                inset: -2px;
                border-radius: 10px;
                border: 1px solid rgba(243,199,106,.38);
                opacity: 0;
                animation: bPriorityPulse 2.4s ease-out infinite;
            }
            .b-ticket strong,
            .b-panel strong {
                display: block;
                color: var(--ink);
                margin-bottom: 4px;
            }
            .b-ticket span,
            .b-panel span,
            .b-panel p {
                color: var(--muted);
                font-size: 12px;
                line-height: 1.45;
                margin: 0;
            }
            .b-panel {
                border: 1px solid var(--option-line);
                border-radius: 8px;
                padding: 12px;
                background: rgba(0,0,0,.26);
                transition: border-color .22s ease, background .22s ease, transform .22s cubic-bezier(.22, 1, .36, 1);
                animation: bPanelIn .52s cubic-bezier(.22, 1, .36, 1) both;
                animation-delay: calc(var(--i, 0) * 80ms + 320ms);
            }
            .b-panel.next {
                border-color: rgba(243,199,106,.36);
                background: rgba(243,199,106,.075);
                animation-name: bPanelIn, bReplyGlow;
                animation-duration: .52s, 3.4s;
                animation-delay: calc(var(--i, 0) * 80ms + 320ms), 1s;
                animation-timing-function: cubic-bezier(.22, 1, .36, 1), ease-in-out;
                animation-fill-mode: both, none;
                animation-iteration-count: 1, infinite;
            }
            .b-panel.is-refreshing {
                transform: translateY(-2px);
                border-color: rgba(69,213,199,.34);
            }
            @keyframes bAmbientDrift {
                from { transform: translate3d(-1.5%, -1%, 0) scale(1); }
                to { transform: translate3d(1.5%, 1%, 0) scale(1.03); }
            }
            @keyframes bScanPass {
                0%, 18% { transform: translateX(-120%); opacity: 0; }
                36%, 58% { opacity: .9; }
                78%, 100% { transform: translateX(120%); opacity: 0; }
            }
            @keyframes bLivePulse {
                0% { box-shadow: 0 0 0 0 rgba(69,213,199,.46); }
                72%, 100% { box-shadow: 0 0 0 10px rgba(69,213,199,0); }
            }
            @keyframes bFadeUp {
                from { opacity: 0; transform: translateY(12px); filter: blur(6px); }
                to { opacity: 1; transform: translateY(0); filter: blur(0); }
            }
            @keyframes bTicketIn {
                from { opacity: 0; transform: translateY(10px); }
                to { opacity: 1; transform: translateY(0); }
            }
            @keyframes bPanelIn {
                from { opacity: 0; transform: translateY(10px); }
                to { opacity: 1; transform: translateY(0); }
            }
            @keyframes bPriorityPulse {
                0% { opacity: .5; transform: scale(.99); }
                76%, 100% { opacity: 0; transform: scale(1.045); }
            }
            @keyframes bReplyGlow {
                0%, 100% { box-shadow: 0 0 0 rgba(243,199,106,0); }
                50% { box-shadow: 0 0 28px rgba(243,199,106,.14); }
            }
            @keyframes bConnectionBeam {
                0%, 24% { opacity: 0; transform: translateX(-18%); }
                38%, 58% { opacity: .72; }
                82%, 100% { opacity: 0; transform: translateX(18%); }
            }
            .option-c-screen {
                position: relative;
                overflow: hidden;
                background:
                    radial-gradient(circle at 50% 24%, rgba(69,213,199,.13), transparent 24%),
                    radial-gradient(circle at 80% 18%, rgba(243,199,106,.14), transparent 28%),
                    #030303;
            }
            .c-hero {
                display: grid;
                grid-template-columns: minmax(0, .9fr) minmax(320px, 1.1fr);
                gap: 26px;
                align-items: center;
            }
            .c-copy h3 {
                margin: 0 0 14px;
                font-size: clamp(40px, 6vw, 76px);
                line-height: .96;
            }
            .c-copy p {
                max-width: 590px;
                font-size: 18px;
            }
            .ecosystem-stage {
                position: relative;
                min-height: 390px;
                border: 1px solid var(--option-line);
                border-radius: 16px;
                background:
                    linear-gradient(135deg, rgba(255,255,255,.06), rgba(255,255,255,.02)),
                    rgba(8,8,8,.84);
                overflow: hidden;
                box-shadow: 0 28px 90px rgba(0,0,0,.36);
            }
            .ecosystem-stage img {
                width: 100%;
                height: 100%;
                min-height: 390px;
                object-fit: cover;
                opacity: .62;
                filter: saturate(1.08) contrast(1.04);
            }
            .ecosystem-overlay {
                position: absolute;
                inset: 0;
                display: grid;
                align-content: end;
                gap: 10px;
                padding: 18px;
                background: linear-gradient(180deg, rgba(0,0,0,.08), rgba(0,0,0,.64));
            }
            .module-row {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
            }
            .module-pill {
                border: 1px solid rgba(255,255,255,.15);
                border-radius: 999px;
                padding: 7px 10px;
                background: rgba(0,0,0,.42);
                color: var(--ink);
                font-size: 12px;
                font-weight: 900;
                backdrop-filter: blur(12px);
            }
            .module-pill.active {
                color: #000;
                background: linear-gradient(135deg, #fff, var(--brand));
            }
            .c-bottom-queue {
                margin-top: 18px;
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 10px;
            }
            .c-mini-card {
                border: 1px solid var(--option-line);
                border-radius: 10px;
                padding: 12px;
                background: rgba(12,12,13,.84);
            }
            .c-mini-card strong {
                display: block;
                color: var(--ink);
                margin-bottom: 4px;
            }
            .c-mini-card span {
                color: var(--muted);
                font-size: 12px;
            }
            .option-decision {
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 12px;
            }
            .decision-card {
                border: 1px solid var(--option-line);
                border-radius: 10px;
                padding: 15px;
                background: rgba(255,255,255,.025);
            }
            .decision-card strong {
                display: block;
                color: var(--ink);
                margin-bottom: 5px;
            }
            .decision-card p {
                margin: 0;
                font-size: 13px;
            }
            @media (max-width: 920px) {
                .option-a-grid,
                .option-b-screen,
                .c-hero,
                .option-decision {
                    grid-template-columns: 1fr;
                }
                .option-screen {
                    min-height: 0;
                    padding: 18px;
                }
                .b-sidebar {
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }
                .b-sidebar strong {
                    grid-column: 1 / -1;
                }
                .b-board,
                .calm-preview-body,
                .c-bottom-queue {
                    grid-template-columns: 1fr;
                }
                .ecosystem-stage,
                .ecosystem-stage img {
                    min-height: 300px;
                }
            }
            @media (max-width: 520px) {
                .ui-options-intro {
                    padding-top: 18px;
                }
                .option-head {
                    grid-template-columns: 1fr;
                    padding: 15px;
                }
                .option-head h2 {
                    font-size: 22px;
                }
                .option-screen {
                    padding: 14px;
                }
                .tiny-glass span {
                    min-width: 48px;
                    min-height: 28px;
                    font-size: 11px;
                    padding: 4px 8px;
                }
                .tiny-glass.market span {
                    min-width: 74px;
                }
                .option-a-copy h3,
                .c-copy h3,
                .b-topbar h3 {
                    font-size: 34px;
                }
                .option-a-copy p,
                .c-copy p {
                    font-size: 16px;
                }
                .b-sidebar {
                    grid-template-columns: 1fr;
                }
                .b-topbar {
                    align-items: flex-start;
                    flex-direction: column;
                }
            }
            @media (prefers-reduced-motion: reduce) {
                .option-b-screen::before,
                .option-b-screen::after,
                .b-board::before,
                .b-live-dot,
                .b-topbar,
                .b-live-strip,
                .b-board,
                .b-ticket,
                .b-ticket.priority::after,
                .b-panel,
                .b-panel.next {
                    animation: none !important;
                    transition-duration: .01ms !important;
                    transform: none !important;
                    filter: none !important;
                }
            }
        </style>
        <div class="ui-options-page">
            <section class="ui-options-intro">
                <span class="option-kicker">Impeccable UI Direction Set</span>
                <h1>3 个 NexaFlow 产品页 UI 方向。</h1>
                <p class="lead">这三个方向都基于现在的产品定位：NexaFlow 是给商家集中询盘、判断客户卡点、准备下一步跟进的 sales inbox。先选方向，再把正式首页往那个方向整理。</p>
                <div class="option-nav">
                    <a href="#option-a">A · Calm Inbox</a>
                    <a href="#option-b">B · Sales OS</a>
                    <a href="#option-c">C · Ecosystem</a>
                </div>
            </section>

            <section class="option-shell" id="option-a">
                <div class="option-head">
                    <div>
                        <span class="option-kicker">Option A</span>
                        <h2>Calm Inbox · 最接近现在主页，但更高级更干净</h2>
                        <p>适合正式主页：一眼讲清楚价值，不让 demo 或设置抢走注意力。重点是 “one inbox + next follow-up”。</p>
                    </div>
                    <span class="option-tag">Homepage safest</span>
                </div>
                <div class="option-screen option-a-grid">
                    <div class="option-a-copy">
                        <div class="tiny-toggle-row">
                            <div class="tiny-glass"><span>EN</span><span>中文</span></div>
                            <div class="tiny-glass market"><span>Singapore</span><span>Malaysia</span></div>
                        </div>
                        <span class="option-kicker">NexaFlow Enquiry</span>
                        <h3>One inbox for every customer enquiry.</h3>
                        <p>See who to reply first, what the customer needs, what is missing, and what to send next.</p>
                        <div class="option-actions">
                            <a class="mock-btn" href="/dealer-demo">See Sales Demo</a>
                            <a class="mock-btn ghost" href="/merchant-signup">Create Enquiry Inbox</a>
                        </div>
                    </div>
                    <div class="calm-preview">
                        <div class="calm-preview-top"><span>Live sales queue preview</span><span class="pill good">Demo</span></div>
                        <div class="calm-preview-body">
                            <div class="calm-list">
                                <div class="calm-lead active"><strong>WhatsApp Quote Lead</strong><span>Reply first · price / timing</span></div>
                                <div class="calm-lead"><strong>Instagram Service Lead</strong><span>Comparing packages</span></div>
                                <div class="calm-lead"><strong>TikTok Appointment Lead</strong><span>Booking not confirmed</span></div>
                            </div>
                            <div class="calm-detail">
                                <span class="option-kicker">WhatsApp · Quote</span>
                                <h4>WhatsApp Quote Lead</h4>
                                <div class="calm-detail-card"><strong>Customer needs</strong><span>Package price, available slot, and deposit details.</span></div>
                                <div class="calm-detail-card"><strong>Stuck on</strong><span>Price range and appointment timing are not confirmed.</span></div>
                                <div class="calm-detail-card next"><strong>Next reply</strong><p>Can I confirm your preferred date, budget range, and the service package you want us to quote?</p></div>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            <section class="option-shell" id="option-b">
                <div class="option-head">
                    <div>
                        <span class="option-kicker">Option B</span>
                        <h2>Sales OS · 产品感最强，直接展示中介每天会用什么</h2>
                        <p>适合给销售型客户看：第一屏就像一个真实 inbox，强调 “今天先回谁、为什么、下一句怎么讲”。</p>
                    </div>
                    <span class="option-tag">Best for demo conversion</span>
                </div>
                <div class="option-screen option-b-screen">
                    <aside class="b-sidebar">
                        <strong>NexaFlow</strong>
                        <div class="b-nav-item active"><span>Today</span><span>12</span></div>
                        <div class="b-nav-item"><span>Need reply</span><span>5</span></div>
                        <div class="b-nav-item"><span>Appointments</span><span>3</span></div>
                        <div class="b-nav-item"><span>Follow-up</span><span>8</span></div>
                    </aside>
                    <div class="b-workspace">
                        <div class="b-topbar">
                            <div>
                                <span class="option-kicker">Today&apos;s queue</span>
                                <h3>Know who to follow up next.</h3>
                            </div>
                            <a class="mock-btn" href="/merchant-signup">Create Inbox</a>
                        </div>
                        <div class="b-live-strip" aria-live="polite"><span class="b-live-dot"></span><span id="bLiveText">Live sorting <strong>WhatsApp Quote Lead</strong></span><span class="b-source-chip" id="bLiveSource">WhatsApp</span></div>
                        <div class="b-board">
                            <div class="b-column">
                                <div class="b-column-title">Buyer queue</div>
                                <div class="b-ticket priority" data-b-ticket="0" style="--i:0"><strong>WhatsApp Quote Lead</strong><span>Price + appointment timing</span></div>
                                <div class="b-ticket" data-b-ticket="1" style="--i:1"><strong>IG Loan Question</strong><span>Income and loan details missing</span></div>
                                <div class="b-ticket" data-b-ticket="2" style="--i:2"><strong>TikTok Comparison</strong><span>Comparing options</span></div>
                            </div>
                            <div class="b-column">
                                <div class="b-column-title">AI note</div>
                                <div class="b-panel" data-b-dynamic="request" style="--i:0"><strong>Customer request</strong><span>Asked for price, availability, and whether booking requires deposit.</span></div>
                                <div class="b-panel" data-b-dynamic="stuck" style="--i:1"><strong>Stuck point</strong><span>Budget range and preferred appointment time are still missing.</span></div>
                                <div class="b-panel next" data-b-dynamic="next" style="--i:2"><strong>Next question</strong><p>Ask for date, budget range, and package preference before pushing for appointment.</p></div>
                            </div>
                            <div class="b-column">
                                <div class="b-column-title">Follow-up</div>
                                <div class="b-panel next" data-b-dynamic="reply" style="--i:0"><strong>Reply draft</strong><p>Hi, can I confirm your preferred date and budget range first? Then I can quote the right package.</p></div>
                                <a class="mock-btn" href="/dealer-demo">Open Demo Queue</a>
                                <a class="mock-btn ghost" href="/merchant-login">Merchant Login</a>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            <section class="option-shell" id="option-c">
                <div class="option-head">
                    <div>
                        <span class="option-kicker">Option C</span>
                        <h2>Ecosystem · 更像 NexaFlow 品牌主页，不只是一款 enquiry 工具</h2>
                        <p>适合你想把 NexaFlow 做成长期 AI business operating system：Enquiry 是第一个入口，后面还能延展 CRM、Billing、Inventory、Automation。</p>
                    </div>
                    <span class="option-tag">Most brand-led</span>
                </div>
                <div class="option-screen option-c-screen">
                    <div class="c-hero">
                        <div class="c-copy">
                            <div class="tiny-toggle-row">
                                <div class="tiny-glass"><span>EN</span><span>中文</span></div>
                                <div class="tiny-glass market"><span>Singapore</span><span>Malaysia</span></div>
                            </div>
                            <span class="option-kicker">NexaFlow Enquiry</span>
                            <h3>The first inbox in your business OS.</h3>
                            <p>Start with customer enquiries. Expand into CRM, billing, inventory, automation, and reporting when the business is ready.</p>
                            <div class="option-actions">
                                <a class="mock-btn" href="/merchant-signup">Start with Enquiry</a>
                                <a class="mock-btn ghost" href="/dealer-demo">View Demo</a>
                            </div>
                        </div>
                        <div class="ecosystem-stage">
                            <img src="/assets/brand/nexaflow-final.png" alt="NexaFlow business ecosystem">
                            <div class="ecosystem-overlay">
                                <div class="module-row">
                                    <span class="module-pill active">Enquiry</span>
                                    <span class="module-pill">CRM</span>
                                    <span class="module-pill">Billing</span>
                                    <span class="module-pill">Inventory</span>
                                    <span class="module-pill">Automation</span>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="c-bottom-queue">
                        <div class="c-mini-card"><strong>Collect</strong><span>WhatsApp, Instagram, Facebook, TikTok, Xiaohongshu, calls, and referrals.</span></div>
                        <div class="c-mini-card"><strong>Understand</strong><span>AI identifies intent, missing details, urgency, and stuck point.</span></div>
                        <div class="c-mini-card"><strong>Follow up</strong><span>Sales gets the next reply and reminder without managing spreadsheets.</span></div>
                    </div>
                </div>
            </section>

            <section class="option-decision">
                <div class="decision-card">
                    <strong>选 A，如果你要最稳的正式主页</strong>
                    <p>清楚、高级、风险最低，适合先上线给不同类型商家看。</p>
                </div>
                <div class="decision-card">
                    <strong>选 B，如果你要最快让中介明白价值</strong>
                    <p>最像真实产品，demo 转化力强，但主页会更偏 sales tool。</p>
                </div>
                <div class="decision-card">
                    <strong>选 C，如果你要做 NexaFlow 长期品牌</strong>
                    <p>品牌野心最大，适合把 Enquiry 放进更大的 business OS 故事里。</p>
                </div>
            </section>
        </div>
        <script>
            (function () {
                const option = document.getElementById("option-b");
                const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                if (!option || reduce) {
                    return;
                }
                const states = [
                    {
                        source: "WhatsApp",
                        lead: "WhatsApp Quote Lead",
                        request: "Asked for price, availability, and whether booking requires deposit.",
                        stuck: "Budget range and preferred appointment time are still missing.",
                        next: "Ask for date, budget range, and package preference before pushing for appointment.",
                        reply: "Hi, can I confirm your preferred date and budget range first? Then I can quote the right package."
                    },
                    {
                        source: "Instagram",
                        lead: "IG Loan Question",
                        request: "Asked whether loan support is available and how monthly payment is calculated.",
                        stuck: "Income range, down payment, and target monthly payment are not clear yet.",
                        next: "Ask income range, down payment amount, and comfortable monthly payment before giving a loan direction.",
                        reply: "Sure, I can guide you. What income range, down payment, and monthly payment are you comfortable with?"
                    },
                    {
                        source: "TikTok",
                        lead: "TikTok Comparison",
                        request: "Comparing the same option with other sellers and checking whether the price is worth it.",
                        stuck: "Customer is comparing price, package, timing, and trust before deciding.",
                        next: "Ask what they are comparing against, then answer the exact concern instead of pushing too early.",
                        reply: "I understand you are comparing. Which package, price, or timing are you comparing with? I can explain the difference clearly."
                    }
                ];
                const tickets = Array.from(option.querySelectorAll("[data-b-ticket]"));
                const liveText = option.querySelector("#bLiveText");
                const liveSource = option.querySelector("#bLiveSource");
                const dynamicPanels = {
                    request: option.querySelector("[data-b-dynamic='request'] span"),
                    stuck: option.querySelector("[data-b-dynamic='stuck'] span"),
                    next: option.querySelector("[data-b-dynamic='next'] p"),
                    reply: option.querySelector("[data-b-dynamic='reply'] p")
                };
                let index = 0;
                function setState(nextIndex) {
                    index = nextIndex % states.length;
                    const state = states[index];
                    tickets.forEach((ticket, ticketIndex) => {
                        ticket.classList.toggle("priority", ticketIndex === index);
                    });
                    if (liveText) {
                        liveText.innerHTML = `Live sorting <strong>${state.lead}</strong>`;
                    }
                    if (liveSource) {
                        liveSource.textContent = state.source;
                    }
                    Object.entries(dynamicPanels).forEach(([key, element]) => {
                        if (!element) {
                            return;
                        }
                        element.textContent = state[key];
                        const panel = element.closest(".b-panel");
                        if (panel) {
                            panel.classList.remove("is-refreshing");
                            void panel.offsetWidth;
                            panel.classList.add("is-refreshing");
                            window.setTimeout(() => panel.classList.remove("is-refreshing"), 320);
                        }
                    });
                }
                window.setInterval(() => setState(index + 1), 3300);
            })();
        </script>
        """,
        show_sales_contact=True,
        show_floating_contact=False,
    )


@app.get("/merchant-login", response_class=HTMLResponse)
def merchant_login_page():
    return merchant_html(
        "NexaFlow Dealer Login",
        "NexaFlow",
        """
        <section class="hero compact">
            <div>
                <div class="language-toggle" aria-label="Language">
                    <button type="button" class="active" onclick="setLoginLang('en')" id="langEn">EN</button>
                    <button type="button" onclick="setLoginLang('zh')" id="langZh">中文</button>
                </div>
                <div class="eyebrow">Dealer Login</div>
                <h1><span data-lang="en">Open your private dealer inbox.</span><span data-lang="zh" class="lang-hidden">打开你的车商私密 inbox。</span></h1>
                <p class="lead"><span data-lang="en">Enter your dealer link name and inbox password. The password is saved only in your browser, not placed in the URL.</span><span data-lang="zh" class="lang-hidden">输入你的车商链接名称和 inbox 密码。密码只会保存在你的浏览器，不会放进 URL。</span></p>
            </div>
        </section>
        <section class="form-card">
            <div class="toolbar">
                <label>Dealer link name<input id="loginSlug" autocomplete="off" placeholder="your-dealer-name"></label>
                <label>Inbox password<input id="loginKey" type="password" autocomplete="current-password" placeholder="biz_..."></label>
            </div>
            <div class="actions">
                <button class="btn" onclick="openMerchantInbox()"><span data-lang="en">Open Inbox</span><span data-lang="zh" class="lang-hidden">打开 Inbox</span></button>
                <a class="btn secondary" href="/merchant-signup"><span data-lang="en">Create Buyer Inbox</span><span data-lang="zh" class="lang-hidden">创建买家 inbox</span></a>
                <a class="btn secondary" href="/"><span data-lang="en">Back to Services</span><span data-lang="zh" class="lang-hidden">返回服务页</span></a>
            </div>
            <div class="status" id="loginStatus"><span data-lang="en">Your inbox password protects buyer enquiries. Do not share it publicly.</span><span data-lang="zh" class="lang-hidden">Inbox 密码会保护买家询盘资料，请不要公开分享。</span></div>
        </section>
        <div class="section-head">
            <div>
                <h2><span data-lang="en">Privacy note</span><span data-lang="zh" class="lang-hidden">隐私提示</span></h2>
                <p><span data-lang="en">Buyer names, phone numbers, messages, notes, and follow-up records are shown only after a valid inbox password is provided.</span><span data-lang="zh" class="lang-hidden">买家姓名、电话、留言、备注和跟进记录，只有输入有效 inbox 密码后才会显示。</span></p>
            </div>
        </div>
        <script>
            function normalizeSlug(value) {
                return String(value || "").trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
            }
            function setLoginLang(lang) {
                document.querySelectorAll("[data-lang]").forEach(item => {
                    item.classList.toggle("lang-hidden", item.dataset.lang !== lang);
                });
                document.getElementById("langEn").classList.toggle("active", lang === "en");
                document.getElementById("langZh").classList.toggle("active", lang === "zh");
                localStorage.setItem("nexaflow_login_lang", lang);
            }
            function openMerchantInbox() {
                const status = document.getElementById("loginStatus");
                const slug = normalizeSlug(document.getElementById("loginSlug").value);
                const key = document.getElementById("loginKey").value.trim();
                if (!slug || !key) {
                    status.textContent = "Please enter both dealer link name and inbox password.";
                    return;
                }
                localStorage.setItem(`nexaflow_business_key_${slug}`, key);
                window.location.href = `/inbox/${slug}`;
            }
            setLoginLang(localStorage.getItem("nexaflow_login_lang") || "en");
        </script>
        """,
        show_sales_contact=True,
    )


@app.get("/merchant-signup", response_class=HTMLResponse)
def merchant_signup_page():
    return merchant_html(
        "Create Buyer Inbox",
        "NexaFlow",
        """
        <section class="hero compact">
            <div>
                <div class="language-toggle" aria-label="Language">
                    <button type="button" class="active" onclick="setSignupLang('en')" id="signupLangEn">EN</button>
                    <button type="button" onclick="setSignupLang('zh')" id="signupLangZh">中文</button>
                </div>
                <div class="eyebrow"><span data-lang="en">Buyer Inbox</span><span data-lang="zh" class="lang-hidden">买家 Inbox</span></div>
                <h1><span data-lang="en">Create your buyer inbox</span><span data-lang="zh" class="lang-hidden">创建你的买家 inbox</span></h1>
                <p class="lead"><span data-lang="en">Collect car buyer enquiries and know who to WhatsApp next. No social media password needed.</span><span data-lang="zh" class="lang-hidden">集中买车询问，并知道下一位该 WhatsApp 谁。不需要社媒密码。</span></p>
            </div>
        </section>
        <section class="form-card">
            <div class="toolbar">
                <label><span data-lang="en">Dealer / showroom name (required)</span><span data-lang="zh" class="lang-hidden">车行 / 展厅名称（必填）</span><input id="signupBusinessName" autocomplete="organization" placeholder="ABC Auto" required></label>
                <label><span data-lang="en">WhatsApp number (required)</span><span data-lang="zh" class="lang-hidden">WhatsApp 号码（必填）</span><input id="signupWhatsapp" autocomplete="tel" placeholder="6012xxxxxxx" required></label>
                <label><span data-lang="en">Owner email</span><span data-lang="zh" class="lang-hidden">老板 Email</span><input id="signupEmail" type="email" autocomplete="email" placeholder="owner@example.com"></label>
            </div>
            <details>
                <summary><span data-lang="en">Optional setup</span><span data-lang="zh" class="lang-hidden">可选设置</span></summary>
                <div class="toolbar">
                    <label><span data-lang="en">Your link name</span><span data-lang="zh" class="lang-hidden">你的链接名称</span><input id="signupSlug" autocomplete="off" placeholder="abc-auto"></label>
                    <label><span data-lang="en">Market</span><span data-lang="zh" class="lang-hidden">市场</span>
                        <select id="signupMarket">
                            <option value="my">Malaysia</option>
                            <option value="sg">Singapore</option>
                            <option value="other">Other / 其他</option>
                        </select>
                    </label>
                </div>
                <div class="toolbar">
                    <label><span data-lang="en">Monthly buyer enquiries</span><span data-lang="zh" class="lang-hidden">每月买家询问</span>
                        <select id="signupMonthly">
                            <option value="under_50">Under 50 / 少过 50</option>
                            <option value="50_200">50 - 200</option>
                            <option value="200_plus">200+</option>
                        </select>
                    </label>
                    <label><span data-lang="en">Dealer type</span><span data-lang="zh" class="lang-hidden">车商类型</span>
                        <select id="signupBusinessType">
                            <option value="used_car_dealer">Used car dealer / 二手车商</option>
                            <option value="auto_dealer">Auto dealer / 汽车销售</option>
                            <option value="service_merchant">Service merchant / 服务商家</option>
                            <option value="general">General / 普通商家</option>
                        </select>
                    </label>
                </div>
            </details>
            <label class="checkbox-label"><input id="signupConsent" type="checkbox"> <span><span data-lang="en">I agree that NexaFlow may create this buyer inbox and process buyer enquiry data for follow-up, security, support, and record keeping under the Privacy Policy.</span><span data-lang="zh" class="lang-hidden">我同意 NexaFlow 创建这个买家 inbox，并根据隐私政策处理买家询盘资料，用于跟进、安全、客服和必要记录。</span></span></label>
            <div class="actions">
                <button class="btn" onclick="createMerchantWorkspace()"><span data-lang="en">Create Dealer Inbox</span><span data-lang="zh" class="lang-hidden">创建车商 Inbox</span></button>
                <a class="btn secondary" href="/merchant-login"><span data-lang="en">I already have an inbox</span><span data-lang="zh" class="lang-hidden">我已经有 inbox</span></a>
            </div>
            <div class="status" id="signupStatus"><span data-lang="en">No social media password needed.</span><span data-lang="zh" class="lang-hidden">不需要社媒密码。</span></div>
        </section>
        <section class="form-card" id="workspaceResult" style="display:none">
            <h2><span data-lang="en">Your buyer inbox is ready</span><span data-lang="zh" class="lang-hidden">你的买家 inbox 已准备好</span></h2>
            <p><span data-lang="en">Save this inbox password now. NexaFlow stores only a protected hash and cannot show it again.</span><span data-lang="zh" class="lang-hidden">请现在保存这个 inbox 密码。NexaFlow 只保存保护后的 hash，之后不会再次显示。</span></p>
            <div class="setup-panel">
                <div class="setup-step"><strong><span data-lang="en">Dealer inbox</span><span data-lang="zh" class="lang-hidden">车商 inbox</span></strong><span id="createdWorkspace">-</span></div>
                <div class="setup-step"><strong><span data-lang="en">Inbox password</span><span data-lang="zh" class="lang-hidden">Inbox 密码</span></strong><span><code id="createdPassword">-</code></span></div>
                <div class="setup-step"><strong><span data-lang="en">Security</span><span data-lang="zh" class="lang-hidden">安全</span></strong><span id="createdSecurity"><span data-lang="en">Never paste platform passwords, OTPs, cookies, or tokens.</span><span data-lang="zh" class="lang-hidden">不要粘贴平台密码、OTP、cookies 或 tokens。</span></span></div>
                <div class="setup-step"><strong><span data-lang="en">Next</span><span data-lang="zh" class="lang-hidden">下一步</span></strong><span><span data-lang="en">Open the inbox, load demo buyers or add one real DM/call, then copy the next reply. Meta auto-sync can be connected later.</span><span data-lang="zh" class="lang-hidden">打开 inbox，加载示例买家或新增一条真实私信/电话内容，然后复制下一句回复。Meta 自动同步之后再接。</span></span></div>
            </div>
            <div class="actions">
                <a class="btn" id="createdInbox" href="/merchant-login"><span data-lang="en">Open Inbox</span><span data-lang="zh" class="lang-hidden">打开 Inbox</span></a>
                <a class="btn secondary" id="createdChannels" href="/merchant-login"><span data-lang="en">Set Sources Later</span><span data-lang="zh" class="lang-hidden">之后设置来源</span></a>
                <a class="btn secondary" id="createdForm" href="/ai-enquiry"><span data-lang="en">Send Test Enquiry</span><span data-lang="zh" class="lang-hidden">发送测试询盘</span></a>
            </div>
            <p class="mini-note"><span data-lang="en">For this pilot, the working flow is buyer link + manual DM/call capture. Do not wait for Meta approval before testing the sales queue.</span><span data-lang="zh" class="lang-hidden">这个试用版先用买家 link + 手动导入私信/电话内容。不需要等 Meta 通过才测试销售队列。</span></p>
        </section>
        <details class="form-card">
            <summary><span data-lang="en">Security and setup notes</span><span data-lang="zh" class="lang-hidden">安全与设置说明</span></summary>
            <section class="grid">
                <div class="card"><h3><span data-lang="en">Private dealer inbox</span><span data-lang="zh" class="lang-hidden">车商私密 inbox</span></h3><p><span data-lang="en">Each dealer gets a separate inbox, password, buyer list, and social source setup.</span><span data-lang="zh" class="lang-hidden">每个车商都有独立 inbox、密码、买家列表和社媒来源设置。</span></p></div>
                <div class="card"><h3><span data-lang="en">No password sharing</span><span data-lang="zh" class="lang-hidden">不共享密码</span></h3><p><span data-lang="en">NexaFlow should use platform authorization or assisted capture, not shared staff passwords.</span><span data-lang="zh" class="lang-hidden">NexaFlow 应使用平台授权或辅助导入，不要求员工共享社媒密码。</span></p></div>
                <div class="card"><h3><span data-lang="en">Start simple</span><span data-lang="zh" class="lang-hidden">先保持简单</span></h3><p><span data-lang="en">The first daily view shows which buyer needs follow-up now, then hides advanced settings until needed.</span><span data-lang="zh" class="lang-hidden">每天先看哪些买家现在要跟进；高级设置先收起来，需要时再打开。</span></p></div>
            </section>
        </details>
        <script>
            function setSignupLang(lang) {
                document.querySelectorAll("[data-lang]").forEach(item => {
                    item.classList.toggle("lang-hidden", item.dataset.lang !== lang);
                });
                document.getElementById("signupLangEn").classList.toggle("active", lang === "en");
                document.getElementById("signupLangZh").classList.toggle("active", lang === "zh");
                localStorage.setItem("nexaflow_signup_lang", lang);
            }
            function signupLang() {
                return localStorage.getItem("nexaflow_signup_lang") || "en";
            }
            function signupText(en, zh) {
                return signupLang() === "zh" ? zh : en;
            }
            function normalizeSignupSlug(value) {
                return String(value || "").trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
            }
            function signupValue(id) {
                return document.getElementById(id).value.trim();
            }
            function signupErrorMessage(result) {
                const fallback = signupText("Could not create buyer inbox.", "无法创建买家 inbox。");
                const detail = result && result.detail;
                if (!detail) {
                    return fallback;
                }
                if (typeof detail === "string") {
                    return detail;
                }
                if (Array.isArray(detail)) {
                    return detail.map(item => {
                        const loc = Array.isArray(item.loc) ? item.loc.filter(part => part !== "body").join(".") : "";
                        const message = item.msg || JSON.stringify(item);
                        return loc ? `${loc}: ${message}` : message;
                    }).join(" ");
                }
                if (detail.msg) {
                    return detail.msg;
                }
                try {
                    return JSON.stringify(detail);
                } catch (error) {
                    return fallback;
                }
            }
            async function createMerchantWorkspace() {
                const status = document.getElementById("signupStatus");
                const businessName = signupValue("signupBusinessName");
                const whatsappPhone = signupValue("signupWhatsapp");
                if (!businessName) {
                    status.textContent = signupText("Dealer / showroom name is required.", "请填写车行 / 展厅名称。");
                    return;
                }
                if (!whatsappPhone) {
                    status.textContent = signupText("WhatsApp number is required.", "请填写 WhatsApp 号码。");
                    return;
                }
                if (!document.getElementById("signupConsent").checked) {
                    status.textContent = signupText("Please agree to the Privacy Policy before creating the inbox.", "创建 inbox 前，请先同意隐私政策。");
                    return;
                }
                status.textContent = signupText("Creating buyer inbox...", "正在创建买家 inbox...");
                const payload = {
                    business_name: businessName,
                    contact_email: signupValue("signupEmail"),
                    whatsapp_phone: whatsappPhone,
                    preferred_slug: normalizeSignupSlug(signupValue("signupSlug")),
                    market: signupValue("signupMarket"),
                    monthly_enquiries: signupValue("signupMonthly"),
                    business_type: signupValue("signupBusinessType"),
                    pdpa_consent: document.getElementById("signupConsent").checked,
                };
                if (!payload.preferred_slug) {
                    payload.preferred_slug = null;
                }
                try {
                    const response = await fetch("/apps/enquiry/api/merchant/signup", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(payload),
                    });
                    const result = await response.json();
                    if (!response.ok) {
                        throw new Error(signupErrorMessage(result));
                    }
                    const slug = result.profile.slug;
                    localStorage.setItem(`nexaflow_business_key_${slug}`, result.business_access_key);
                    document.getElementById("createdWorkspace").textContent = `${result.profile.business_name} / ${slug}`;
                    document.getElementById("createdPassword").textContent = result.business_access_key;
                    document.getElementById("createdSecurity").textContent = result.security_notice;
                    document.getElementById("createdInbox").href = result.inbox_url;
                    document.getElementById("createdChannels").href = result.channels_url;
                    document.getElementById("createdForm").href = result.form_url;
                    document.getElementById("workspaceResult").style.display = "block";
                    status.textContent = signupText("Dealer inbox created. Your inbox password was saved in this browser.", "买家 inbox 已创建。Inbox 密码已保存在这个浏览器。");
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            setSignupLang(localStorage.getItem("nexaflow_signup_lang") || "en");
        </script>
        """,
        show_sales_contact=True,
    )


@app.get("/pricing", response_class=HTMLResponse)
def pricing_page():
    rows = ""
    for plan_key, plan in PLANS.items():
        rows += f"""
        <tr>
            <td>{plan["name"]}</td>
            <td>${plan["monthly_price_usd"]}/mo</td>
            <td>{plan["included_credits"]:,}</td>
            <td>{plan["default_model"]}</td>
            <td>{", ".join(plan["allowed_tiers"])}</td>
            <td><a class="btn" href="/billing/checkout?plan={plan_key}">Checkout</a></td>
        </tr>
        """

    return base_html(
        "NexaFlow Pricing",
        f"""
        <h1>Pricing</h1>
        <p>Credits are token-based and routed for margin-aware model selection.</p>
        <p>By purchasing or using NexaFlow, customers agree to the <a href="/terms">Terms</a>, <a href="/privacy">Privacy Policy</a>, <a href="/refund-policy">Refund Policy</a>, and <a href="/acceptable-use">Acceptable Use Policy</a>.</p>
        <table>
            <thead>
                <tr><th>Plan</th><th>Price</th><th>Credits</th><th>Default</th><th>Tiers</th><th></th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        """
    )


@app.get("/dealer-demo", response_class=HTMLResponse)
def dealer_demo_page():
    return merchant_html(
        "NexaFlow Dealer Demo",
        "NexaFlow",
        dealer_demo_page_body(),
        show_sales_contact=True,
        show_floating_contact=False,
    )


@app.get("/ai-enquiry", response_class=HTMLResponse)
@app.get("/apps/enquiry", response_class=HTMLResponse)
def enquiry_app_page():
    return merchant_html(
        "NexaFlow AI Enquiry Inbox",
        "NexaFlow Enquiry",
        """
        <section class="hero product-hero">
            <div>
                <div class="language-toggle" aria-label="Language">
                    <button type="button" class="active" onclick="setProductLang('en')" id="langEn">EN</button>
                    <button type="button" onclick="setProductLang('zh')" id="langZh">中文</button>
                </div>
                <div class="language-toggle" aria-label="Market">
                    <button type="button" class="active" onclick="setProductMarket('sg')" id="marketSg">Singapore</button>
                    <button type="button" onclick="setProductMarket('my')" id="marketMy">Malaysia</button>
                </div>
                <div class="eyebrow">NexaFlow Enquiry</div>
                <h1><span data-lang="en">One enquiry link. One private inbox. Faster WhatsApp follow-up.</span><span data-lang="zh" class="lang-hidden">一个询盘链接，一个私密 inbox，更快 WhatsApp 跟进。</span></h1>
                <p class="lead">
                    <span data-lang="en">Share one link anywhere. Customers submit their request with consent. NexaFlow organizes the enquiry, highlights urgency, and prepares a WhatsApp-ready follow-up direction.</span>
                    <span data-lang="zh" class="lang-hidden">把一个链接放到任何地方。客户提交询盘并确认隐私提示后，NexaFlow 会整理内容、判断紧急程度，并准备可用于 WhatsApp 跟进的方向。</span>
                </p>
                <p class="lead"><span data-market="sg">For Singapore dealers: PDPA-aware buyer enquiry capture, private inbox, and WhatsApp follow-up.</span><span data-market="my" class="market-hidden">For Malaysia dealers: simple buyer enquiry capture, private inbox, WhatsApp follow-up, and local MYR pricing.</span></p>
                <div class="actions">
                    <a class="btn" href="/merchant-signup"><span data-lang="en">Create My Buyer Inbox</span><span data-lang="zh" class="lang-hidden">创建我的买家 inbox</span></a>
                    <a class="btn secondary" href="#enquiry-form"><span data-lang="en">Try Demo</span><span data-lang="zh" class="lang-hidden">试用 Demo</span></a>
                    <a class="btn secondary" href="/merchant-login"><span data-lang="en">Dealer Login</span><span data-lang="zh" class="lang-hidden">车商登录</span></a>
                </div>
            </div>
            <div class="hero-side">
                <div class="brand-visual"><img src="/assets/brand/nexaflow-final.png" alt="NexaFlow business ecosystem"></div>
                <div class="product-panel">
                    <div class="panel-top"><span data-lang="en">Today</span><span data-lang="zh" class="lang-hidden">今日询盘</span><span class="pill good">WhatsApp-ready</span></div>
                    <div class="signal-list">
                        <div class="signal-row">
                            <span class="pill hot">Answer now</span>
                            <div><strong><span data-lang="en">Quotation request</span><span data-lang="zh" class="lang-hidden">报价询问</span></strong><span data-lang="en">Urgent price request for this week.</span><span data-lang="zh" class="lang-hidden">客户想本周获得报价。</span></div>
                            <span data-lang="en">Reply draft</span><span data-lang="zh" class="lang-hidden">回复草稿</span>
                        </div>
                        <div class="signal-row">
                            <span class="pill">Need details</span>
                            <div><strong><span data-lang="en">Booking request</span><span data-lang="zh" class="lang-hidden">预约询问</span></strong><span data-lang="en">Customer asked for available slots.</span><span data-lang="zh" class="lang-hidden">客户想知道可预约时间。</span></div>
                            <span data-lang="en">Follow up</span><span data-lang="zh" class="lang-hidden">跟进</span>
                        </div>
                        <div class="signal-row">
                            <span class="pill">Ask next</span>
                            <div><strong><span data-lang="en">General enquiry</span><span data-lang="zh" class="lang-hidden">普通询问</span></strong><span data-lang="en">Customer asked for service details.</span><span data-lang="zh" class="lang-hidden">客户想了解服务详情。</span></div>
                            <span data-lang="en">Check</span><span data-lang="zh" class="lang-hidden">查看</span>
                        </div>
                    </div>
                </div>
            </div>
        </section>
        <section class="grid">
            <div class="card accent-card"><h3><span data-lang="en">One link</span><span data-lang="zh" class="lang-hidden">一个链接</span></h3><p><span data-lang="en">Works as a mini landing page for businesses without a website.</span><span data-lang="zh" class="lang-hidden">没有网站的商家，也可以直接用这个链接接收客户询问。</span></p></div>
            <div class="card"><h3><span data-lang="en">Auto sorting</span><span data-lang="zh" class="lang-hidden">自动分类</span></h3><p><span data-lang="en">Labels price, loan, monthly payment, viewing, stock, and general buyer enquiries.</span><span data-lang="zh" class="lang-hidden">自动判断买家是在问价格、贷款、月供、预约看车、库存，还是普通问题。</span></p></div>
            <div class="card"><h3><span data-lang="en">Fast follow-up</span><span data-lang="zh" class="lang-hidden">快速跟进</span></h3><p><span data-lang="en">Shows which buyer to WhatsApp next and prepares a reply direction.</span><span data-lang="zh" class="lang-hidden">显示下一位要 WhatsApp 跟进的买家，并准备回复方向。</span></p></div>
        </section>
        <div class="section-head">
            <div>
                <h2><span data-lang="en">Simple follow-up flow for car dealers</span><span data-lang="zh" class="lang-hidden">二手车商的简单跟进流程</span></h2>
                <p><span data-lang="en">The goal is not to replace WhatsApp. The goal is to make sure every enquiry is captured, understood, and followed up.</span><span data-lang="zh" class="lang-hidden">目标不是取代 WhatsApp，而是确保每个询盘都被记录、理解和及时跟进。</span></p>
            </div>
        </div>
        <section class="steps">
            <div class="step"><div><strong><span data-lang="en">Buyer asks</span><span data-lang="zh" class="lang-hidden">买家询问</span></strong><p><span data-lang="en">A buyer clicks your enquiry link from WhatsApp, Facebook, Instagram, Google Business Profile, or your website.</span><span data-lang="zh" class="lang-hidden">买家从 WhatsApp、Facebook、Instagram、Google 商家资料或网站点击你的询盘链接。</span></p></div></div>
            <div class="step"><div><strong><span data-lang="en">NexaFlow organizes</span><span data-lang="zh" class="lang-hidden">NexaFlow 整理</span></strong><p><span data-lang="en">The private inbox shows buyer details, message, need, urgency, and suggested follow-up direction.</span><span data-lang="zh" class="lang-hidden">私密 inbox 显示买家资料、留言、需求、紧急程度和建议跟进方向。</span></p></div></div>
            <div class="step"><div><strong><span data-lang="en">Dealer follows up</span><span data-lang="zh" class="lang-hidden">车商跟进</span></strong><p><span data-lang="en">Open WhatsApp, reply faster, set a reminder, and keep the buyer visible until the next step is handled.</span><span data-lang="zh" class="lang-hidden">打开 WhatsApp 更快回复，设置提醒，并让买家保持可见直到处理下一步。</span></p></div></div>
        </section>
        <div class="section-head">
            <div>
                <h2><span data-lang="en">Data safety built in</span><span data-lang="zh" class="lang-hidden">内置资料保护</span></h2>
                <p><span data-lang="en">Designed for dealers who need buyer trust before they can win the sale.</span><span data-lang="zh" class="lang-hidden">为需要买家信任才有机会成交的车商而设计。</span></p>
            </div>
        </div>
        <section class="grid">
            <div class="card"><h3><span data-lang="en">Consent before submit</span><span data-lang="zh" class="lang-hidden">提交前同意</span></h3><p><span data-lang="en">Every enquiry records the privacy notice, consent status, and consent time.</span><span data-lang="zh" class="lang-hidden">每个询问都会记录隐私告知、同意状态和同意时间。</span></p></div>
            <div class="card"><h3><span data-lang="en">Private dealer inbox</span><span data-lang="zh" class="lang-hidden">车商私密 inbox</span></h3><p><span data-lang="en">Internal notes, follow-up dates, and WhatsApp links stay behind an inbox password.</span><span data-lang="zh" class="lang-hidden">内部备注、跟进日期和 WhatsApp 链接都由 inbox 密码保护。</span></p></div>
            <div class="card"><h3><span data-lang="en">Delete buyer enquiries</span><span data-lang="zh" class="lang-hidden">删除买家询盘</span></h3><p><span data-lang="en">Dealers can delete individual enquiries when they no longer need the record or receive a valid deletion request.</span><span data-lang="zh" class="lang-hidden">当车商不再需要记录，或收到有效删除请求时，可以删除单个买家询盘。</span></p></div>
            <div class="card"><h3><span data-lang="en">Export and records</span><span data-lang="zh" class="lang-hidden">导出与记录</span></h3><p><span data-lang="en">Dealers can export their buyer list, while Terms and Privacy explain allowed use and responsibilities.</span><span data-lang="zh" class="lang-hidden">车商可以导出买家列表，同时条款和隐私政策说明资料用途和责任。</span></p></div>
        </section>
        <div class="section-head" id="enquiry-pricing">
            <div>
                <h2><span data-lang="en">Start with a 30-day trial</span><span data-lang="zh" class="lang-hidden">先免费试用 30 天</span></h2>
                <p><span data-lang="en">Test the full AI follow-up flow first. After the trial, choose a monthly plan based on enquiry volume and team needs.</span><span data-lang="zh" class="lang-hidden">先测试完整 AI 跟进流程。试用后，再根据每月询盘量和团队需求选择配套。</span></p>
            </div>
        </div>
        <section class="pricing-grid">
            <div class="price-card trial">
                <h3><span data-lang="en">Trial</span><span data-lang="zh" class="lang-hidden">试用</span></h3>
                <div class="plan-price"><span data-lang="en">Free</span><span data-lang="zh" class="lang-hidden">免费</span> <span>/ 30 days</span></div>
                <p><span data-lang="en">Best for trying the full workflow with real enquiries.</span><span data-lang="zh" class="lang-hidden">适合先用真实客户询问测试整套流程。</span></p>
                <ul>
                    <li><span data-lang="en">Private enquiry inbox</span><span data-lang="zh" class="lang-hidden">私密询盘 inbox</span></li>
                    <li><span data-lang="en">Enquiry link and widget</span><span data-lang="zh" class="lang-hidden">询问链接与网站 widget</span></li>
                    <li><span data-lang="en">AI classification, stuck point, and reply draft</span><span data-lang="zh" class="lang-hidden">AI 分类、客户卡点和回复草稿</span></li>
                    <li><span data-lang="en">Follow-up reminders</span><span data-lang="zh" class="lang-hidden">跟进提醒</span></li>
                </ul>
                <a class="btn" href="/merchant-signup"><span data-lang="en">Create Enquiry Inbox</span><span data-lang="zh" class="lang-hidden">创建询盘 inbox</span></a>
            </div>
            <div class="price-card">
                <h3>Starter</h3>
                <div class="plan-price"><span data-market="sg">SGD 49</span><span data-market="my" class="market-hidden">MYR 169</span> <span>/ month</span></div>
                <p><span data-lang="en">Full AI follow-up for solo owners and small teams handling up to 100 enquiries per month.</span><span data-lang="zh" class="lang-hidden">完整 AI 跟进功能，适合每月 100 个询盘以内的老板或小团队。</span></p>
                <ul>
                    <li><span data-lang="en">Up to 100 enquiries / month</span><span data-lang="zh" class="lang-hidden">每月最多 100 个询盘</span></li>
                    <li><span data-lang="en">AI priority, category, and stuck-point detection</span><span data-lang="zh" class="lang-hidden">AI 优先级、分类和客户卡点判断</span></li>
                    <li><span data-lang="en">Next reply drafts and follow-up reminders</span><span data-lang="zh" class="lang-hidden">下一句回复草稿和跟进提醒</span></li>
                    <li><span data-lang="en">Manual, paste, screenshot, and source tagging</span><span data-lang="zh" class="lang-hidden">手动新增、复制、截图和来源标记</span></li>
                </ul>
            </div>
            <div class="price-card highlight">
                <h3>Growth</h3>
                <div class="plan-price"><span data-market="sg">SGD 89</span><span data-market="my" class="market-hidden">MYR 299</span> <span>/ month</span></div>
                <p><span data-lang="en">For teams handling 101-500 enquiries per month with daily follow-up work.</span><span data-lang="zh" class="lang-hidden">适合每月 101-500 个询盘、每天都需要跟进的团队。</span></p>
                <ul>
                    <li><span data-lang="en">Everything in Starter</span><span data-lang="zh" class="lang-hidden">包含 Starter 全部功能</span></li>
                    <li><span data-lang="en">101-500 enquiries / month</span><span data-lang="zh" class="lang-hidden">每月 101-500 个询盘</span></li>
                    <li><span data-lang="en">Shared team queue and follow-up dashboard</span><span data-lang="zh" class="lang-hidden">团队共享队列和跟进看板</span></li>
                    <li><span data-lang="en">Source visibility and setup support</span><span data-lang="zh" class="lang-hidden">来源可视化和设置协助</span></li>
                </ul>
            </div>
            <div class="price-card">
                <h3>Business</h3>
                <div class="plan-price"><span data-market="sg">SGD 149+</span><span data-market="my" class="market-hidden">MYR 499+</span> <span>/ month</span></div>
                <p><span data-lang="en">For 500+ enquiries per month, multiple outlets, official channel setup, or custom workflow needs.</span><span data-lang="zh" class="lang-hidden">适合每月 500+ 询盘、多分店、官方渠道接入或客制流程。</span></p>
                <ul>
                    <li><span data-lang="en">500+ enquiries / month or custom volume</span><span data-lang="zh" class="lang-hidden">每月 500+ 询盘或客制用量</span></li>
                    <li><span data-lang="en">Multiple outlets or workspaces</span><span data-lang="zh" class="lang-hidden">多分店或多个 workspace</span></li>
                    <li><span data-lang="en">Official channel setup assistance</span><span data-lang="zh" class="lang-hidden">官方渠道接入协助</span></li>
                    <li><span data-lang="en">Custom workflow, export, and priority support</span><span data-lang="zh" class="lang-hidden">客制流程、导出和优先支持</span></li>
                </ul>
            </div>
        </section>
        <div class="section-head" id="enquiry-form">
            <div>
                <h2><span data-lang="en">Try the demo</span><span data-lang="zh" class="lang-hidden">试用 Demo</span></h2>
                <p><span data-lang="en">Submit a sample buyer enquiry and see how NexaFlow organizes intent and urgency before the dealer follows up in the private inbox.</span><span data-lang="zh" class="lang-hidden">提交一个示例买家询盘，看看 NexaFlow 如何在车商私密 inbox 跟进前整理买家意向和紧急程度。</span></p>
            </div>
        </div>
        <section class="form-card">
            <div class="toolbar">
                <label><span data-lang="en">Name</span><span data-lang="zh" class="lang-hidden">姓名</span><input id="leadName" value="Alex Tan"></label>
                <label><span data-lang="en">Phone</span><span data-lang="zh" class="lang-hidden">电话</span><input id="leadPhone" value="6591234567"></label>
            </div>
            <div class="toolbar">
                <label>Email<input id="leadEmail" value="alex@example.com"></label>
                <label><span data-lang="en">Service Type</span><span data-lang="zh" class="lang-hidden">服务类型</span>
                    <select id="businessType">
                        <option value="renovation">Renovation</option>
                        <option value="tuition">Tuition</option>
                        <option value="retail">Retail</option>
                        <option value="beauty">Beauty</option>
                        <option value="repair">Repair</option>
                        <option value="general">General</option>
                    </select>
                </label>
            </div>
            <label>Message
                <textarea id="leadMessage">Hi, I need a quotation urgently for this week. How much is your package?</textarea>
            </label>
            <p class="mini-note"><span data-lang="en">Please do not include NRIC/passport numbers, card details, passwords, health details, or other sensitive information in this demo.</span><span data-lang="zh" class="lang-hidden">请不要在 Demo 中填写身份证/护照号码、银行卡资料、密码、健康资料或其他敏感信息。</span></p>
            <label class="checkbox-label"><input id="pdpaConsent" type="checkbox" checked> <span><span data-lang="en">I agree that my contact details and enquiry may be used for follow-up, support, security, and record keeping under the Privacy Policy.</span><span data-lang="zh" class="lang-hidden">我同意根据隐私政策，使用我的联系资料和询问内容作跟进、客服、安全和记录用途。</span></span></label>
            <div class="actions">
                <button class="btn" onclick="submitEnquiry()"><span data-lang="en">Submit Enquiry</span><span data-lang="zh" class="lang-hidden">提交询问</span></button>
            </div>
            <div class="status" id="enquiryStatus">Submit the demo form to create a sample buyer enquiry. Reply drafts are kept inside the private dealer inbox.</div>
        </section>
        <script>
            function setProductLang(lang) {
                document.querySelectorAll("[data-lang]").forEach(item => {
                    item.classList.toggle("lang-hidden", item.dataset.lang !== lang);
                });
                document.getElementById("langEn").classList.toggle("active", lang === "en");
                document.getElementById("langZh").classList.toggle("active", lang === "zh");
                localStorage.setItem("nexaflow_enquiry_lang", lang);
            }
            function setProductMarket(market) {
                document.querySelectorAll("[data-market]").forEach(item => {
                    item.classList.toggle("market-hidden", item.dataset.market !== market);
                });
                document.getElementById("marketSg").classList.toggle("active", market === "sg");
                document.getElementById("marketMy").classList.toggle("active", market === "my");
                localStorage.setItem("nexaflow_enquiry_market", market);
            }
            setProductLang(localStorage.getItem("nexaflow_enquiry_lang") || "en");
            setProductMarket(localStorage.getItem("nexaflow_enquiry_market") || "sg");
            function escapeHtml(value) {
                return String(value ?? "").replace(/[&<>"']/g, char => ({
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }[char]));
            }
            function focusLabel(value) {
                const labels = { hot: "Answer now", warm: "Needs details", normal: "Ask next question" };
                return labels[value] || value || "Unknown";
            }
            async function submitEnquiry() {
                const status = document.getElementById("enquiryStatus");
                if (!document.getElementById("pdpaConsent").checked) {
                    status.textContent = "Please agree to the privacy notice before submitting.";
                    return;
                }
                status.textContent = "Submitting enquiry...";
                try {
                    const response = await fetch("/apps/enquiry/api/enquiries", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            name: document.getElementById("leadName").value,
                            phone: document.getElementById("leadPhone").value,
                            email: document.getElementById("leadEmail").value,
                            business_type: document.getElementById("businessType").value,
                            message: document.getElementById("leadMessage").value,
                            pdpa_consent: document.getElementById("pdpaConsent").checked,
                            source: "demo"
                        })
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    status.innerHTML = `
                        Created enquiry #${result.id}<br>
                        Need: ${escapeHtml(result.intent)} | Follow-up focus: ${escapeHtml(focusLabel(result.priority))}<br>
                        Next: the dealer opens the private inbox, uses the WhatsApp follow-up direction, and updates the buyer status.
                    `;
                } catch (error) {
                    status.textContent = error.message;
                }
            }
        </script>
        """
        ,
        show_sales_contact=True,
    )


@app.get("/contact-trial")
def contact_trial():
    url = sales_whatsapp_url("Hi NexaFlow, I want to start the 30-day trial for NexaFlow Enquiry")
    if not url:
        raise HTTPException(status_code=503, detail="Sales WhatsApp contact is not configured.")
    return RedirectResponse(url)


@app.get("/start-trial", response_class=HTMLResponse)
def start_trial_page():
    whatsapp_url = sales_whatsapp_url("Hi NexaFlow, I submitted the trial request form and want to start the 30-day trial")
    whatsapp_action = (
        f'<a class="btn secondary" target="_blank" rel="noopener" href="{escape_html(whatsapp_url)}">WhatsApp after submit</a>'
        if whatsapp_url
        else ""
    )
    return merchant_html(
        "Start NexaFlow Enquiry Trial",
        "NexaFlow Trial",
        f"""
        <section class="hero compact">
            <div>
                <div class="eyebrow">30-day trial</div>
                <h1>Create your enquiry link.</h1>
                <p class="lead">Tell us your business name, WhatsApp number, and service type. We will prepare your customer link, private inbox, and first demo lead.</p>
                <div class="actions">
                    <a class="btn secondary" href="/ai-enquiry">View product</a>
                    {whatsapp_action}
                </div>
            </div>
        </section>
        <section class="admin-split">
            <div class="form-card">
                <h2>Trial request</h2>
                <p>Best for local service businesses that receive enquiries through WhatsApp, Facebook, Instagram, calls, referrals, or Google Business Profile.</p>
                <label>Business name<input id="trialBusinessName" placeholder="ABC Renovation"></label>
                <label>Your name<input id="trialContactName" placeholder="Alex Tan"></label>
                <label>Email optional<input id="trialEmail" type="email" placeholder="you@example.com"></label>
                <label>WhatsApp phone<input id="trialWhatsapp" placeholder="+65 9123 4567"></label>
                <label>Service type<input id="trialType" placeholder="Renovation, beauty, cleaning, repair..."></label>
                <label>Country / Area<input id="trialCity" placeholder="Singapore, KL, JB..."></label>
                <label>Approx. monthly enquiries
                    <select id="trialVolume">
                        <option value="1-30">1-30</option>
                        <option value="31-100">31-100</option>
                        <option value="101-500">101-500</option>
                        <option value="500+">500+</option>
                        <option value="not sure">Not sure</option>
                    </select>
                </label>
                <label>How did you find NexaFlow?
                    <select id="trialLeadSource">
                        <option value="direct">Direct / not sure</option>
                        <option value="gmail">Email invitation</option>
                        <option value="linkedin">LinkedIn</option>
                        <option value="facebook">Facebook</option>
                        <option value="instagram">Instagram</option>
                        <option value="tiktok">TikTok</option>
                        <option value="whatsapp">WhatsApp</option>
                        <option value="referral">Referral</option>
                    </select>
                </label>
                <label>Where do your enquiries usually come from?<textarea id="trialMessage" rows="4" placeholder="Example: Mostly WhatsApp and Facebook. We often forget to follow up after quoting."></textarea></label>
                <label class="checkbox-label"><input id="trialConsent" type="checkbox"> <span>I agree that NexaFlow may collect and use this information to contact me about the trial, setup, support, security, and service follow-up. See the <a href="/privacy" target="_blank">Privacy Policy</a>.</span></label>
                <div class="actions">
                    <button class="btn" onclick="submitTrialRequest()">Submit trial request</button>
                </div>
                <div class="status" id="trialStatus">Submit this form first. We will follow up with setup steps.</div>
            </div>
            <div class="grid">
                <div class="card accent-card"><h3>1. We create your link</h3><p>You get one customer enquiry link and a private merchant inbox.</p></div>
                <div class="card"><h3>2. You test one enquiry</h3><p>Submit a sample enquiry, check the AI sorting, and open the WhatsApp follow-up.</p></div>
                <div class="card"><h3>3. You share it</h3><p>Use the link on WhatsApp, Facebook, Instagram, Google Business Profile, or your bio link.</p></div>
            </div>
        </section>
        <script>
            function escapeHtml(value) {{
                return String(value ?? "").replace(/[&<>"']/g, char => ({{
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }}[char]));
            }}
            async function submitTrialRequest() {{
                const status = document.getElementById("trialStatus");
                const params = new URLSearchParams(window.location.search);
                const sourceFromUrl = params.get("source") || params.get("utm_source") || "";
                const campaignFromUrl = params.get("campaign") || params.get("utm_campaign") || "";
                const selectedSource = document.getElementById("trialLeadSource").value;
                status.textContent = "Submitting trial request...";
                try {{
                    const response = await fetch("/apps/enquiry/api/trial-requests", {{
                        method: "POST",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{
                            business_name: document.getElementById("trialBusinessName").value,
                            contact_name: document.getElementById("trialContactName").value,
                            contact_email: document.getElementById("trialEmail").value,
                            whatsapp_phone: document.getElementById("trialWhatsapp").value,
                            business_type: document.getElementById("trialType").value,
                            city: document.getElementById("trialCity").value,
                            monthly_enquiries: document.getElementById("trialVolume").value,
                            lead_source: sourceFromUrl || selectedSource,
                            campaign: campaignFromUrl,
                            referrer: document.referrer || "",
                            message: document.getElementById("trialMessage").value,
                            pdpa_consent: document.getElementById("trialConsent").checked
                        }})
                    }});
                    const result = await response.json();
                    if (!response.ok) {{
                        throw new Error(result.detail || JSON.stringify(result));
                    }}
                    status.innerHTML = `Received. Trial request #${{escapeHtml(result.id)}} is recorded. We will contact you on WhatsApp.`;
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            window.addEventListener("DOMContentLoaded", () => {{
                const params = new URLSearchParams(window.location.search);
                const source = params.get("source") || params.get("utm_source");
                const select = document.getElementById("trialLeadSource");
                if (source && [...select.options].some(option => option.value === source)) {{
                    select.value = source;
                }}
            }});
        </script>
        """,
        show_sales_contact=True,
    )


@app.get("/enquiry/{business_slug}", response_class=HTMLResponse)
@app.get("/apps/enquiry/form/{business_slug}", response_class=HTMLResponse)
def public_enquiry_form_page(business_slug: str):
    profile = get_business_profile(business_slug)
    business_name = escape_html(profile["business_name"])
    offer_summary = escape_html(profile["offer_summary"] or "Send an enquiry and the team will follow up.")
    opening_hours = escape_html(profile["opening_hours"] or "Send an enquiry any time. The team will follow up as soon as possible.")
    slug = escape_html(profile["slug"])
    business_type = escape_html(profile["business_type"])
    business_type_label = escape_html(profile["business_type"].replace("_", " ").title())
    return merchant_html(
        f"{business_name} Enquiry Page",
        profile["business_name"],
        f"""
        <section class="hero">
            <div>
                <div class="eyebrow">{business_type_label}</div>
                <h1>{business_name}</h1>
                <p class="lead">{offer_summary}</p>
                <div class="actions">
                    <a class="btn" href="#enquiry-form">Send Enquiry</a>
                    <a class="btn secondary" href="#details">View Details</a>
                </div>
            </div>
            <div class="product-panel" id="details">
                <div class="panel-top"><span>Business enquiry page</span><span class="pill good">AI-assisted</span></div>
                <div class="signal-list">
                    <div class="signal-row">
                        <span class="pill">Ask</span>
                        <div><strong>Price or quotation</strong><span>Share what you need and any important details.</span></div>
                        <span>Step 1</span>
                    </div>
                    <div class="signal-row">
                        <span class="pill">Book</span>
                        <div><strong>Appointment or service</strong><span>Request a date, time, or callback.</span></div>
                        <span>Step 2</span>
                    </div>
                    <div class="signal-row">
                        <span class="pill">Reply</span>
                        <div><strong>Fast follow-up</strong><span>The business receives your enquiry and reply draft.</span></div>
                        <span>Step 3</span>
                    </div>
                </div>
            </div>
        </section>
        <section class="grid">
            <div class="card"><h3>What we do</h3><p>{offer_summary}</p></div>
            <div class="card"><h3>Response</h3><p>Your enquiry is sent directly to the business for follow-up.</p></div>
            <div class="card"><h3>Availability</h3><p>{opening_hours}</p></div>
        </section>
        <section class="form-card" id="enquiry-form">
            <h2>Send an enquiry</h2>
            <p>Leave your contact details and a short message.</p>
            <div class="toolbar">
                <label>Name<input id="leadName" autocomplete="name" placeholder="Your name"></label>
                <label>Phone<input id="leadPhone" autocomplete="tel" placeholder="+65 9123 4567"></label>
            </div>
            <label>Email optional<input id="leadEmail" autocomplete="email" placeholder="you@example.com"></label>
            <label>Message<textarea id="leadMessage">Hi, I would like to enquire about your service.</textarea></label>
            <p class="mini-note">Please do not include NRIC/passport numbers, card details, passwords, health details, or other sensitive information in your message.</p>
            <label class="checkbox-label"><input id="pdpaConsent" type="checkbox"> <span>I agree that my name, contact details, and enquiry message may be used by {business_name} and NexaFlow service providers to respond to this enquiry, provide support, keep records, and protect the service. See the <a href="/privacy" target="_blank">Privacy Policy</a>.</span></label>
            <div class="actions">
                <button class="btn" onclick="submitEnquiry()">Send Enquiry</button>
            </div>
            <div class="status" id="enquiryStatus">Ready when you are.</div>
        </section>
        <script>
            function escapeHtml(value) {{
                return String(value ?? "").replace(/[&<>"']/g, char => ({{
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }}[char]));
            }}
            function focusLabel(value) {{
                const labels = {{ hot: "Answer now", warm: "Needs details", normal: "Ask next question" }};
                return labels[value] || value || "Unknown";
            }}
            async function submitEnquiry() {{
                const status = document.getElementById("enquiryStatus");
                const params = new URLSearchParams(window.location.search);
                const sourceFromUrl = params.get("source") || params.get("utm_source") || "public-form";
                const campaignFromUrl = params.get("campaign") || params.get("utm_campaign") || "";
                const referrerFromUrl = params.get("referrer") || document.referrer || "";
                const pageUrlFromUrl = params.get("page_url") || window.location.href;
                if (!document.getElementById("pdpaConsent").checked) {{
                    status.textContent = "Please agree to the privacy notice before submitting.";
                    return;
                }}
                status.textContent = "Sending...";
                try {{
                    const response = await fetch("/apps/enquiry/api/enquiries", {{
                        method: "POST",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{
                            business_slug: "{slug}",
                            name: document.getElementById("leadName").value,
                            phone: document.getElementById("leadPhone").value,
                            email: document.getElementById("leadEmail").value,
                            business_type: "{business_type}",
                            message: document.getElementById("leadMessage").value,
                            pdpa_consent: document.getElementById("pdpaConsent").checked,
                            source: sourceFromUrl,
                            campaign: campaignFromUrl,
                            referrer: referrerFromUrl,
                            page_url: pageUrlFromUrl
                        }})
                    }});
                    if (!response.ok) {{
                        throw new Error(await response.text());
                    }}
                    const result = await response.json();
                    status.innerHTML = `Enquiry sent. Reference #${{result.id}}. Follow-up focus: ${{escapeHtml(focusLabel(result.priority))}}`;
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
        </script>
        """
    )


@app.get("/inbox/{business_slug}", response_class=HTMLResponse)
@app.get("/apps/enquiry/inbox/{business_slug}", response_class=HTMLResponse)
def merchant_enquiry_inbox_page(business_slug: str):
    profile = get_business_profile(business_slug)
    business_name = escape_html(profile["business_name"])
    slug = escape_html(profile["slug"])
    return merchant_html(
        f"{business_name} Inbox",
        profile["business_name"],
        f"""
        <section class="hero compact">
            <div>
                <div class="language-toggle" aria-label="Language">
                    <button type="button" class="active" onclick="setInboxLang('en')" id="inboxLangEn">EN</button>
                    <button type="button" onclick="setInboxLang('zh')" id="inboxLangZh">中文</button>
                </div>
                <div class="eyebrow"><span data-lang="en">Dealer inbox</span><span data-lang="zh" class="lang-hidden">车商 Inbox</span></div>
                <h1><span data-lang="en">Today&apos;s Buyer Follow-up</span><span data-lang="zh" class="lang-hidden">今日买家跟进</span></h1>
                <p class="lead"><span data-lang="en">Open once a day, see which buyer needs a reply, monthly payment check, loan question, or viewing follow-up.</span><span data-lang="zh" class="lang-hidden">每天打开一次，先看谁要回复、谁要确认月供、谁要问贷款，或谁要约看车。</span></p>
            </div>
        </section>
        <section class="form-card">
            <div class="toolbar">
                <label><span data-lang="en">Inbox password</span><span data-lang="zh" class="lang-hidden">Inbox 密码</span><input id="businessKey" type="password" placeholder="biz_..."></label>
                <button class="btn" onclick="loadMerchantInbox()"><span data-lang="en">Open Buyer List</span><span data-lang="zh" class="lang-hidden">打开买家列表</span></button>
                <button class="btn secondary" onclick="loadDemoBuyers()"><span data-lang="en">Load Demo Buyers</span><span data-lang="zh" class="lang-hidden">加载示例买家</span></button>
                <a class="btn secondary" href="/channels/{slug}"><span data-lang="en">Set Social Sources</span><span data-lang="zh" class="lang-hidden">设置询问来源</span></a>
            </div>
            <div class="status" id="merchantStatus"><span data-lang="en">Enter your owner inbox password to load this private dealer inbox. Do not share this password publicly.</span><span data-lang="zh" class="lang-hidden">输入老板 inbox 密码来打开这个车商私密 inbox。请不要公开分享这个密码。</span></div>
            <p class="mini-note"><span data-lang="en">Buyer data in this inbox should only be used for replies, quotations, viewing appointments, loan follow-up, support, security, and required records.</span><span data-lang="zh" class="lang-hidden">这个 inbox 里的买家资料只应用于回复、报价、预约看车、贷款跟进、客服、安全和必要记录。</span></p>
        </section>
        <section class="form-card" id="merchantManualCapture">
            <div class="section-head onboarding-head">
                <div>
                    <h2><span data-lang="en">Current pilot: add buyer from DM / call</span><span data-lang="zh" class="lang-hidden">当前试用：从私信 / 电话新增买家</span></h2>
                    <p><span data-lang="en">Until Meta auto-sync is connected, this is the main trial flow: paste the buyer&apos;s message from WhatsApp, Instagram, Facebook, TikTok, Xiaohongshu, call, or referral. NexaFlow will turn it into a follow-up card.</span><span data-lang="zh" class="lang-hidden">Meta 自动同步接好之前，这就是主要试用流程：把 WhatsApp、Instagram、Facebook、TikTok、小红书、电话或介绍来的买家内容放进来，NexaFlow 会变成跟进卡。</span></p>
                </div>
            </div>
            <div class="toolbar">
                <label><span data-lang="en">Buyer name</span><span data-lang="zh" class="lang-hidden">买家名字</span><input id="manualLeadName" placeholder="Alex Tan"></label>
                <label><span data-lang="en">Phone / handle optional</span><span data-lang="zh" class="lang-hidden">电话 / 平台 handle（选填）</span><input id="manualLeadPhone" placeholder="6012xxxxxxx or @buyer"></label>
                <label><span data-lang="en">Source</span><span data-lang="zh" class="lang-hidden">来源</span>
                    <select id="manualLeadSource">
                        <option value="whatsapp">WhatsApp</option>
                        <option value="instagram">Instagram</option>
                        <option value="facebook">Facebook</option>
                        <option value="tiktok">TikTok</option>
                        <option value="xiaohongshu">Xiaohongshu</option>
                        <option value="manual">Call / manual</option>
                        <option value="referral">Referral</option>
                    </select>
                </label>
            </div>
            <p class="mini-note"><span data-lang="en">If the buyer has not shared a phone number yet, leave it blank or paste their social handle. NexaFlow will still analyze the DM and show the next follow-up question.</span><span data-lang="zh" class="lang-hidden">如果买家还没给电话号码，可以留空或填写平台 handle。NexaFlow 仍然会分析私信，并显示下一句该怎么跟。</span></p>
            <label><span data-lang="en">Buyer message</span><span data-lang="zh" class="lang-hidden">买家内容</span><textarea id="manualLeadMessage" placeholder="Example: Saw your Civic on TikTok. Can loan? Monthly below RM900, can view today?"></textarea></label>
            <div class="toolbar">
                <label><span data-lang="en">Email optional</span><span data-lang="zh" class="lang-hidden">Email（选填）</span><input id="manualLeadEmail" placeholder="buyer@example.com"></label>
                <label><span data-lang="en">Car / campaign optional</span><span data-lang="zh" class="lang-hidden">车款 / Campaign（选填）</span><input id="manualLeadCampaign" placeholder="Civic TikTok DM"></label>
            </div>
            <label class="checkbox-label"><input id="manualLeadAck" type="checkbox"> <span><span data-lang="en">I confirm this buyer contacted the business and I am not pasting passwords, OTPs, identity documents, bank statements, payslips, or other sensitive files here.</span><span data-lang="zh" class="lang-hidden">我确认这位买家联系过本车行，并且这里没有粘贴密码、OTP、身份证件、银行文件、薪资单或其他敏感文件。</span></span></label>
            <div class="copilot-preview" id="manualCopilotPreview">
                <strong><span data-lang="en">AI Copilot</span><span data-lang="zh" class="lang-hidden">AI Copilot</span></strong>
                <span><span data-lang="en">Preview the stuck point, next question, and reply draft before saving this buyer.</span><span data-lang="zh" class="lang-hidden">保存买家前，先预览客户卡点、下一句问题和回复草稿。</span></span>
            </div>
            <div class="actions">
                <button class="btn secondary" onclick="previewManualCopilot()"><span data-lang="en">AI Copilot Preview</span><span data-lang="zh" class="lang-hidden">AI 先看一下</span></button>
                <button class="btn" onclick="createManualLead()"><span data-lang="en">Add Buyer</span><span data-lang="zh" class="lang-hidden">新增买家</span></button>
            </div>
            <div class="status" id="manualLeadStatus"><span data-lang="en">Use this when buyers DM directly and do not click a link.</span><span data-lang="zh" class="lang-hidden">买家直接私信、不点击 link 的时候，用这里新增。</span></div>
        </section>
        <section class="action-center" id="merchantActionCenter"></section>
        <section class="form-card" id="merchantDailyWork">
            <div class="section-head onboarding-head">
                <div>
                    <h2><span data-lang="en">Buyers to contact now</span><span data-lang="zh" class="lang-hidden">现在要跟进的买家</span></h2>
                    <p><span data-lang="en">Start from the top. Each card shows the buyer, what they are likely stuck on, and the next move.</span><span data-lang="zh" class="lang-hidden">从最上面开始。每张卡会显示买家卡在哪里，以及下一步要做什么。</span></p>
                </div>
            </div>
            <div class="simple-lead-list" id="merchantDailyLeads"></div>
        </section>
        <details class="form-card">
            <summary><span data-lang="en">Advanced tools: social sources, buyer link, setup, and settings</span><span data-lang="zh" class="lang-hidden">进阶工具：来源、买家 link、设置和系统设定</span></summary>
            <section class="grid" id="merchantStats"></section>
            <section class="action-center" id="merchantChannelCenter"></section>
            <section class="form-card" id="merchantQuickShare">
                <div class="section-head onboarding-head">
                    <div>
                        <h2><span data-lang="en">Share your buyer enquiry link</span><span data-lang="zh" class="lang-hidden">分享买家询问 link</span></h2>
                        <p><span data-lang="en">Use one link when a buyer asks about price, monthly payment, loan, car stock, or viewing time.</span><span data-lang="zh" class="lang-hidden">买家问价钱、月供、贷款、库存或看车时间时，就发这个 link。</span></p>
                    </div>
                </div>
                <div id="merchantShareLinks"></div>
            </section>
            <details class="form-card">
                <summary><span data-lang="en">Dealer setup checklist</span><span data-lang="zh" class="lang-hidden">车商设置清单</span></summary>
                <p><span data-lang="en">Complete these once before sending the enquiry link to real buyers.</span><span data-lang="zh" class="lang-hidden">正式发给真实买家前，先完成这些设置。</span></p>
                <button class="btn secondary" onclick="resetMerchantChecklist()"><span data-lang="en">Reset</span><span data-lang="zh" class="lang-hidden">重置</span></button>
                <div class="checklist" id="merchantChecklist"></div>
            </details>
            <details class="form-card">
                <summary><span data-lang="en">Dealer settings</span><span data-lang="zh" class="lang-hidden">车商设置</span></summary>
                <p><span data-lang="en">Update these only when your WhatsApp, email, showroom summary, or opening hours change.</span><span data-lang="zh" class="lang-hidden">只有 WhatsApp、Email、车行简介或营业时间变动时，才需要更新这里。</span></p>
                <div class="toolbar">
                    <label><span data-lang="en">Dealer Name</span><span data-lang="zh" class="lang-hidden">车行名称</span><input id="settingsBusinessName" placeholder="Your showroom"></label>
                    <label><span data-lang="en">Dealer Type</span><span data-lang="zh" class="lang-hidden">车商类型</span>
                        <select id="settingsBusinessType">
                            <option value="used_car_dealer">Used Car Dealer</option>
                            <option value="auto_dealer">Auto Dealer</option>
                            <option value="renovation">Renovation</option>
                            <option value="repair">Repair</option>
                            <option value="tuition">Tuition</option>
                            <option value="beauty">Beauty</option>
                            <option value="retail">Retail</option>
                            <option value="general">General Service</option>
                        </select>
                    </label>
                </div>
                <div class="toolbar">
                    <label><span data-lang="en">WhatsApp Phone</span><span data-lang="zh" class="lang-hidden">WhatsApp 电话</span><input id="settingsWhatsapp" placeholder="+65 9123 4567"></label>
                    <label><span data-lang="en">Notification Email</span><span data-lang="zh" class="lang-hidden">通知 Email</span><input id="settingsEmail" placeholder="owner@example.com"></label>
                </div>
                <label><span data-lang="en">Showroom Summary</span><span data-lang="zh" class="lang-hidden">车行简介</span><textarea id="settingsOffer" placeholder="Tell buyers what cars, loan support, or viewing options you provide."></textarea></label>
                <div class="toolbar">
                    <label><span data-lang="en">Reply Tone</span><span data-lang="zh" class="lang-hidden">回复语气</span><input id="settingsTone" placeholder="friendly and professional"></label>
                    <label><span data-lang="en">Opening Hours</span><span data-lang="zh" class="lang-hidden">营业时间</span><input id="settingsHours" placeholder="Mon-Sat, 9am-6pm"></label>
                </div>
                <div class="toolbar">
                    <label><input id="settingsAutoFollowup" type="checkbox"> <span data-lang="en">Auto-schedule follow-up</span><span data-lang="zh" class="lang-hidden">自动安排跟进</span></label>
                    <label><span data-lang="en">Time-sensitive Follow-up Hours</span><span data-lang="zh" class="lang-hidden">急件买家几小时后跟进</span><input id="settingsHotFollowupHours" type="number" min="0" max="72" step="1"></label>
                    <label><span data-lang="en">Standard Follow-up Days</span><span data-lang="zh" class="lang-hidden">普通买家几天后跟进</span><input id="settingsStandardFollowupDays" type="number" min="1" max="30" step="1"></label>
                    <label><span data-lang="en">Data Retention Days</span><span data-lang="zh" class="lang-hidden">资料保留天数</span><input id="settingsDataRetentionDays" type="number" min="30" max="2555" step="1"></label>
                </div>
                <button class="btn" onclick="saveMerchantSettings()"><span data-lang="en">Save Settings</span><span data-lang="zh" class="lang-hidden">保存设置</span></button>
                <div class="status" id="settingsStatus"><span data-lang="en">Load buyers first, then update your dealer settings here.</span><span data-lang="zh" class="lang-hidden">先加载买家列表，再在这里更新车商设置。</span></div>
            </details>
        </details>
        <details class="form-card">
            <summary><span data-lang="en">Full buyer list and filters</span><span data-lang="zh" class="lang-hidden">完整买家列表和筛选</span></summary>
            <div class="section-head">
                <div>
                    <h2><span data-lang="en">Buyer progress</span><span data-lang="zh" class="lang-hidden">买家进度</span></h2>
                    <p><span data-lang="en">Prioritize buyers asking about monthly payment, loan, comparison, or viewing, then mark the next step.</span><span data-lang="zh" class="lang-hidden">优先处理问月供、贷款、比价或看车的买家，然后标记下一步。</span></p>
                </div>
            </div>
            <section class="pipeline-board" id="merchantPipelineBoard"></section>
            <div class="toolbar">
                <label><span data-lang="en">Status</span><span data-lang="zh" class="lang-hidden">状态</span>
                    <select id="filterStatus">
                        <option value="">All statuses / 全部状态</option>
                        <option value="new">New / 新买家</option>
                        <option value="contacted">Contacted / 已联系</option>
                        <option value="quoted">Quoted / 已报价</option>
                        <option value="won">Booked / Sold / 已预约或成交</option>
                        <option value="lost">Not Proceeding / 不继续</option>
                        <option value="spam">Spam</option>
                    </select>
                </label>
                <label><span data-lang="en">Follow-up focus</span><span data-lang="zh" class="lang-hidden">跟进重点</span>
                    <select id="filterPriority">
                        <option value="">All follow-up focuses / 全部跟进重点</option>
                        <option value="hot">Answer now / 现在回复</option>
                        <option value="warm">Needs details / 需要资料</option>
                        <option value="normal">Ask next question / 问下一题</option>
                    </select>
                </label>
            </div>
            <div class="toolbar">
                <label><span data-lang="en">Buyer need</span><span data-lang="zh" class="lang-hidden">买家需求</span>
                    <select id="filterIntent">
                        <option value="">All intents / 全部需求</option>
                        <option value="quotation">Quotation / 报价</option>
                        <option value="booking">Booking / 预约</option>
                        <option value="inventory">Inventory / 库存</option>
                        <option value="general">General / 普通问题</option>
                    </select>
                </label>
                <label><span data-lang="en">Source</span><span data-lang="zh" class="lang-hidden">来源</span>
                    <select id="filterSource">
                        <option value="">All sources / 全部来源</option>
                        <option value="whatsapp">WhatsApp</option>
                        <option value="instagram">Instagram</option>
                        <option value="facebook">Facebook</option>
                        <option value="tiktok">TikTok</option>
                        <option value="xiaohongshu">Xiaohongshu</option>
                        <option value="direct">Direct link / 直接 link</option>
                        <option value="public-form">Public form / 公开表格</option>
                        <option value="google-business">Google Business Profile</option>
                        <option value="website-widget">Website widget</option>
                        <option value="web">Website</option>
                        <option value="demo">Demo</option>
                        <option value="manual">Manual / call / 手动或电话</option>
                    </select>
                </label>
                <label><span data-lang="en">Follow-up</span><span data-lang="zh" class="lang-hidden">跟进</span>
                    <select id="filterFollowUp">
                        <option value="">All follow-ups / 全部跟进</option>
                        <option value="due">Due now / 现在到期</option>
                        <option value="scheduled">Scheduled / 已安排</option>
                        <option value="none">No follow-up / 未安排</option>
                    </select>
                </label>
            </div>
            <div class="toolbar">
                <label><span data-lang="en">Search</span><span data-lang="zh" class="lang-hidden">搜索</span><input id="filterSearch" placeholder="Name, phone, message, note"></label>
            </div>
            <button class="btn" onclick="loadMerchantInbox()"><span data-lang="en">Apply Filters</span><span data-lang="zh" class="lang-hidden">套用筛选</span></button>
            <button class="btn secondary" onclick="clearMerchantFilters()"><span data-lang="en">Clear</span><span data-lang="zh" class="lang-hidden">清除</span></button>
            <button class="btn secondary" onclick="exportMerchantCsv()"><span data-lang="en">Download Buyer List</span><span data-lang="zh" class="lang-hidden">下载买家列表</span></button>
            <table>
                <thead>
                    <tr><th><span data-lang="en">Time</span><span data-lang="zh" class="lang-hidden">时间</span></th><th><span data-lang="en">Buyer</span><span data-lang="zh" class="lang-hidden">买家</span></th><th><span data-lang="en">Source</span><span data-lang="zh" class="lang-hidden">来源</span></th><th><span data-lang="en">Intent</span><span data-lang="zh" class="lang-hidden">需求</span></th><th><span data-lang="en">Follow-up focus</span><span data-lang="zh" class="lang-hidden">跟进重点</span></th><th><span data-lang="en">Message</span><span data-lang="zh" class="lang-hidden">留言</span></th><th><span data-lang="en">Next move</span><span data-lang="zh" class="lang-hidden">下一步</span></th><th><span data-lang="en">Reply draft</span><span data-lang="zh" class="lang-hidden">回复草稿</span></th><th><span data-lang="en">Follow-up</span><span data-lang="zh" class="lang-hidden">跟进</span></th><th><span data-lang="en">Recorded sale value</span><span data-lang="zh" class="lang-hidden">记录成交额</span></th><th><span data-lang="en">Note</span><span data-lang="zh" class="lang-hidden">备注</span></th><th><span data-lang="en">Status</span><span data-lang="zh" class="lang-hidden">状态</span></th><th><span data-lang="en">Action</span><span data-lang="zh" class="lang-hidden">操作</span></th></tr>
                </thead>
                <tbody id="merchantRows"></tbody>
            </table>
        </details>
        <script>
            const businessSlug = "{slug}";
            const checklistStorageKey = `nexaflow_trial_checklist_${{businessSlug}}`;
            const inboxLangStorageKey = "nexaflow_inbox_lang";
            const checklistSteps = [
                ["loaded", "Load inbox", "Paste your inbox password and load this private buyer inbox.", "打开 inbox", "输入 inbox 密码，打开这个私密买家 inbox。"],
                ["copied_link", "Copy buyer link", "Share this link on WhatsApp, Instagram, Facebook, TikTok, Xiaohongshu, or Google Business Profile.", "复制买家 link", "把这个 link 放到 WhatsApp、Instagram、Facebook、TikTok、小红书或 Google 商家资料。"],
                ["settings", "Review settings", "Confirm dealer name, WhatsApp number, showroom summary, and opening hours.", "检查设置", "确认车行名称、WhatsApp 号码、车行简介和营业时间。"],
                ["first_lead", "Receive first buyer", "Submit one test buyer enquiry before sending the link to real buyers.", "收到第一位买家", "正式发给真实买家前，先提交一笔测试询问。"]
            ];
            function inboxLang() {{
                return localStorage.getItem(inboxLangStorageKey) || "en";
            }}
            function inboxText(en, zh) {{
                return inboxLang() === "zh" ? zh : en;
            }}
            function setInboxLang(lang) {{
                document.querySelectorAll("[data-lang]").forEach(item => {{
                    item.classList.toggle("lang-hidden", item.dataset.lang !== lang);
                }});
                document.getElementById("inboxLangEn").classList.toggle("active", lang === "en");
                document.getElementById("inboxLangZh").classList.toggle("active", lang === "zh");
                localStorage.setItem(inboxLangStorageKey, lang);
            }}
            function escapeHtml(value) {{
                return String(value ?? "").replace(/[&<>"']/g, char => ({{
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }}[char]));
            }}
            function langSpan(en, zh) {{
                return `<span data-lang="en">${{escapeHtml(en)}}</span><span data-lang="zh" class="lang-hidden">${{escapeHtml(zh)}}</span>`;
            }}
            async function merchantApi(path, options = {{}}) {{
                const businessKey = document.getElementById("businessKey").value;
                const headers = {{ "X-Business-Key": businessKey, ...(options.headers || {{}}) }};
                const response = await fetch(path, {{ ...options, headers }});
                if (!response.ok) {{
                    throw new Error(await response.text());
                }}
                return response.json();
            }}
            function resetManualCopilotPreview() {{
                document.getElementById("manualCopilotPreview").innerHTML = `
                    <strong>${{langSpan("AI Copilot", "AI Copilot")}}</strong>
                    <span>${{langSpan("Preview the stuck point, next question, and reply draft before saving this buyer.", "保存买家前，先预览客户卡点、下一句问题和回复草稿。")}}</span>
                `;
                setInboxLang(inboxLang());
            }}
            function renderManualCopilot(result) {{
                const target = document.getElementById("manualCopilotPreview");
                const guidance = result.guidance || {{}};
                const decision = result.decision || {{}};
                const classification = result.classification || {{}};
                target.innerHTML = `
                    <div class="copilot-preview-head">
                        <strong>${{langSpan("AI Copilot preview", "AI Copilot 预览")}}</strong>
                        <span class="lead-badge ${{classification.priority === "hot" ? "hot" : ""}}">${{escapeHtml(priorityLabel(classification.priority))}}</span>
                    </div>
                    <div class="copilot-preview-grid">
                        <div>
                            <small>${{langSpan("Customer stuck point", "客户卡点")}}</small>
                            <b>${{escapeHtml(guidance.stuck_point || "")}}</b>
                        </div>
                        <div>
                            <small>${{langSpan("Next question", "下一句问题")}}</small>
                            <b>${{escapeHtml(guidance.next_question || decision.recommended_question || "")}}</b>
                        </div>
                        <div>
                            <small>${{langSpan("When to follow up", "什么时候再跟")}}</small>
                            <b>${{escapeHtml(guidance.follow_up_timing || "")}}</b>
                        </div>
                    </div>
                    <div class="demo-reply-box primary">
                        <strong>${{langSpan("Reply draft", "回复草稿")}}</strong>
                        <p>${{escapeHtml(result.reply_draft || "")}}</p>
                    </div>
                    <span class="mini-note">${{langSpan("Copilot only prepares the recommendation. The salesperson still reviews and sends the message.", "Copilot 只准备建议，销售仍然需要确认后才发送。")}}</span>
                `;
                setInboxLang(inboxLang());
            }}
            async function previewManualCopilot() {{
                const status = document.getElementById("manualLeadStatus");
                if (!document.getElementById("manualLeadAck").checked) {{
                    status.textContent = inboxText("Please confirm the data protection note before AI Copilot preview.", "请先确认资料保护提醒，再让 AI Copilot 分析。");
                    return;
                }}
                status.textContent = inboxText("AI Copilot is checking the buyer message...", "AI Copilot 正在查看买家内容...");
                try {{
                    const result = await merchantApi(`/apps/enquiry/api/merchant/copilot/analyze?business_slug=${{businessSlug}}`, {{
                        method: "POST",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{
                            name: document.getElementById("manualLeadName").value,
                            phone: document.getElementById("manualLeadPhone").value,
                            email: document.getElementById("manualLeadEmail").value,
                            source: document.getElementById("manualLeadSource").value,
                            campaign: document.getElementById("manualLeadCampaign").value || "copilot-preview",
                            message: document.getElementById("manualLeadMessage").value,
                            processing_acknowledged: document.getElementById("manualLeadAck").checked
                        }})
                    }});
                    renderManualCopilot(result);
                    status.textContent = inboxText("AI Copilot preview ready. Review it, then add the buyer if it looks right.", "AI Copilot 预览好了。确认没问题后，再新增买家。");
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            async function createManualLead() {{
                const status = document.getElementById("manualLeadStatus");
                if (!document.getElementById("manualLeadAck").checked) {{
                    status.textContent = inboxText("Please confirm the data protection note before adding this buyer.", "请先确认资料保护提醒，再新增买家。");
                    return;
                }}
                status.textContent = inboxText("Adding buyer and preparing follow-up...", "正在新增买家并准备跟进建议...");
                try {{
                    const result = await merchantApi(`/apps/enquiry/api/merchant/enquiries?business_slug=${{businessSlug}}`, {{
                        method: "POST",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{
                            name: document.getElementById("manualLeadName").value,
                            phone: document.getElementById("manualLeadPhone").value,
                            email: document.getElementById("manualLeadEmail").value,
                            source: document.getElementById("manualLeadSource").value,
                            campaign: document.getElementById("manualLeadCampaign").value || "manual-capture",
                            message: document.getElementById("manualLeadMessage").value,
                            processing_acknowledged: document.getElementById("manualLeadAck").checked
                        }})
                    }});
                    document.getElementById("manualLeadName").value = "";
                    document.getElementById("manualLeadPhone").value = "";
                    document.getElementById("manualLeadEmail").value = "";
                    document.getElementById("manualLeadCampaign").value = "";
                    document.getElementById("manualLeadMessage").value = "";
                    document.getElementById("manualLeadAck").checked = false;
                    resetManualCopilotPreview();
                    markChecklistStep("first_lead");
                    status.innerHTML = `
                        ${{langSpan("Buyer added.", "买家已新增。")}}
                        <br>${{langSpan("Stuck point", "客户卡点")}}: ${{escapeHtml(result.stuck_point || "")}}
                        <br>${{langSpan("Next question", "下一句")}}: ${{escapeHtml(result.next_question || "")}}
                        <br>${{langSpan("Follow-up timing", "追踪时间")}}: ${{escapeHtml(result.follow_up_timing || "")}}
                    `;
                    await loadMerchantInbox();
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            async function loadDemoBuyers() {{
                const status = document.getElementById("merchantStatus");
                status.textContent = inboxText("Loading demo buyers...", "正在加载示例买家...");
                try {{
                    const result = await merchantApi(`/apps/enquiry/api/merchant/demo-enquiries?business_slug=${{businessSlug}}`, {{
                        method: "POST"
                    }});
                    markChecklistStep("first_lead");
                    await loadMerchantInbox();
                    status.textContent = result.message || inboxText("Demo buyers loaded.", "示例买家已加载。");
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            function loadChecklistState() {{
                try {{
                    return JSON.parse(localStorage.getItem(checklistStorageKey) || "{{}}");
                }} catch (error) {{
                    return {{}};
                }}
            }}
            function saveChecklistState(state) {{
                localStorage.setItem(checklistStorageKey, JSON.stringify(state));
            }}
            function markChecklistStep(step, done = true) {{
                const state = loadChecklistState();
                state[step] = done;
                saveChecklistState(state);
                renderMerchantChecklist();
            }}
            function resetMerchantChecklist() {{
                localStorage.removeItem(checklistStorageKey);
                renderMerchantChecklist();
            }}
            function renderMerchantChecklist() {{
                const state = loadChecklistState();
                document.getElementById("merchantChecklist").innerHTML = checklistSteps.map(([key, title, detail, zhTitle, zhDetail], index) => `
                    <div class="check-item ${{state[key] ? "done" : ""}}">
                        <div class="check-status">${{state[key] ? "✓" : index + 1}}</div>
                        <strong>${{langSpan(title, zhTitle)}}</strong>
                        <span>${{langSpan(detail, zhDetail)}}</span>
                    </div>
                `).join("");
                setInboxLang(inboxLang());
            }}
            async function merchantDownload(path) {{
                const businessKey = document.getElementById("businessKey").value;
                const response = await fetch(path, {{ headers: {{ "X-Business-Key": businessKey }} }});
                if (!response.ok) {{
                    throw new Error(await response.text());
                }}
                return response.blob();
            }}
            function fillMerchantSettings(profile) {{
                document.getElementById("settingsBusinessName").value = profile.business_name || "";
                document.getElementById("settingsBusinessType").value = profile.business_type || "general";
                document.getElementById("settingsWhatsapp").value = profile.whatsapp_phone || "";
                document.getElementById("settingsEmail").value = profile.contact_email || "";
                document.getElementById("settingsOffer").value = profile.offer_summary || "";
                document.getElementById("settingsTone").value = profile.reply_tone || "friendly and professional";
                document.getElementById("settingsHours").value = profile.opening_hours || "";
                document.getElementById("settingsAutoFollowup").checked = profile.auto_followup_enabled !== false;
                document.getElementById("settingsHotFollowupHours").value = profile.hot_followup_hours ?? 0;
                document.getElementById("settingsStandardFollowupDays").value = profile.standard_followup_days ?? 1;
                document.getElementById("settingsDataRetentionDays").value = profile.data_retention_days ?? 365;
            }}
            function absoluteUrl(path) {{
                return new URL(path, window.location.origin).toString();
            }}
            async function copyMerchantText(value, label) {{
                try {{
                    await navigator.clipboard.writeText(value);
                    document.getElementById("merchantStatus").textContent = inboxText(`${{label}} copied.`, `${{label}} 已复制。`);
                    if (label.includes("Buyer enquiry")) markChecklistStep("copied_link");
                }} catch (error) {{
                    document.getElementById("merchantStatus").textContent = inboxText(`Copy failed. Select and copy this manually: ${{value}}`, `复制失败。请手动选择并复制：${{value}}`);
                }}
            }}
            async function copyMerchantElement(id, label) {{
                const value = document.getElementById(id)?.textContent || "";
                await copyMerchantText(value, label);
            }}
            function renderShareLinks(payload) {{
                const links = payload.links || {{}};
                const inboxUrl = payload.inbox_url || absoluteUrl(`/inbox/${{businessSlug}}`);
                const embedCode = payload.embed_code || "";
                const shareRows = [
                    ["merchantShareDirect", "Buyer enquiry link", "买家询问 link", links.direct?.url],
                    ["merchantShareWhatsapp", "WhatsApp share link", "WhatsApp 分享 link", links.whatsapp?.url],
                    ["merchantShareInstagram", "Instagram bio / DM link", "Instagram 简介 / 私信 link", links.instagram?.url],
                    ["merchantShareFacebook", "Facebook post / group link", "Facebook 贴文 / 群组 link", links.facebook?.url],
                    ["merchantShareGoogle", "Google Business Profile link", "Google 商家资料 link", links.google_business?.url],
                ].filter(([, , , value]) => value);
                document.getElementById("merchantShareLinks").innerHTML = `
                    <div class="action-center">
                        <div class="action-card">
                            <h3>${{langSpan("Main buyer link", "主要买家 link")}}</h3>
                            <p>${{langSpan("Send this to buyers when they ask for price, monthly payment, loan, stock, or viewing time.", "买家问价钱、月供、贷款、库存或看车时间时，就发这个 link。")}}</p>
                            <code id="merchantShareDirectPrimary">${{escapeHtml(links.direct?.url || "")}}</code>
                            <div class="toolbar">
                                <button class="btn" onclick="copyMerchantElement('merchantShareDirectPrimary', 'Buyer enquiry link')">${{langSpan("Copy Buyer Link", "复制买家 link")}}</button>
                                <button class="btn secondary" onclick="copyMerchantElement('merchantShareCaption', 'Caption')">${{langSpan("Copy Caption", "复制文案")}}</button>
                            </div>
                        </div>
                        <div class="action-card">
                            <h3>${{langSpan("Suggested caption", "建议文案")}}</h3>
                            <p>${{langSpan("Use this in WhatsApp status, Facebook post, Instagram bio, or buyer chat.", "可以放在 WhatsApp status、Facebook 贴文、Instagram 简介或买家聊天里。")}}</p>
                            <code id="merchantShareCaption">${{escapeHtml(payload.copy?.short_caption || "")}}</code>
                        </div>
                    </div>
                    <div class="share-links">
                        ${{shareRows.map(([id, label, zhLabel, value]) => `
                            <div class="share-link-box">
                                <strong>${{langSpan(label, zhLabel)}}</strong>
                                <code id="${{id}}">${{escapeHtml(value)}}</code>
                                <button class="btn secondary" onclick="copyMerchantElement('${{id}}', '${{escapeHtml(label)}}')">${{langSpan("Copy Link", "复制 Link")}}</button>
                            </div>
                        `).join("")}}
                        <div class="share-link-box">
                            <strong>${{langSpan("Private buyer inbox link", "私密买家 inbox link")}}</strong>
                            <code id="merchantInboxUrl">${{escapeHtml(inboxUrl)}}</code>
                            <button class="btn secondary" onclick="copyMerchantElement('merchantInboxUrl', 'Inbox link')">${{langSpan("Copy Link", "复制 Link")}}</button>
                        </div>
                        <div class="share-link-box">
                            <strong>${{langSpan("Website widget code", "网站 widget 代码")}}</strong>
                            <code id="merchantEmbedCode">${{escapeHtml(embedCode)}}</code>
                            <button class="btn secondary" onclick="copyMerchantElement('merchantEmbedCode', 'Embed code')">${{langSpan("Copy Code", "复制代码")}}</button>
                        </div>
                    </div>
                `;
                setInboxLang(inboxLang());
            }}
            async function loadMerchantShareLinks() {{
                try {{
                    const data = await merchantApi(`/apps/enquiry/api/merchant/share-links?business_slug=${{businessSlug}}&campaign=merchant-share`);
                    renderShareLinks(data);
                }} catch (error) {{
                    document.getElementById("merchantShareLinks").innerHTML = `<div class="status">${{langSpan("Share links could not load:", "分享 link 暂时无法加载：")}} ${{escapeHtml(error.message)}}</div>`;
                    setInboxLang(inboxLang());
                }}
            }}
            async function saveMerchantSettings() {{
                const status = document.getElementById("settingsStatus");
                status.textContent = inboxText("Saving business settings...", "正在保存车商设置...");
                try {{
                    const profile = await merchantApi(`/apps/enquiry/api/merchant/profile?business_slug=${{businessSlug}}`, {{
                        method: "PATCH",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{
                            business_name: document.getElementById("settingsBusinessName").value,
                            business_type: document.getElementById("settingsBusinessType").value,
                            whatsapp_phone: document.getElementById("settingsWhatsapp").value,
                            contact_email: document.getElementById("settingsEmail").value,
                            offer_summary: document.getElementById("settingsOffer").value,
                            reply_tone: document.getElementById("settingsTone").value,
                            opening_hours: document.getElementById("settingsHours").value,
                            auto_followup_enabled: document.getElementById("settingsAutoFollowup").checked,
                            hot_followup_hours: Number(document.getElementById("settingsHotFollowupHours").value || 0),
                            standard_followup_days: Number(document.getElementById("settingsStandardFollowupDays").value || 1),
                            data_retention_days: Number(document.getElementById("settingsDataRetentionDays").value || 365)
                        }})
                    }});
                    fillMerchantSettings(profile);
                    status.textContent = inboxText("Saved. Your public enquiry page and WhatsApp follow-up are updated.", "已保存。公开询问页和 WhatsApp 跟进设置已更新。");
                    markChecklistStep("settings");
                    await loadMerchantInbox();
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            async function exportMerchantCsv() {{
                const status = document.getElementById("merchantStatus");
                if (!confirm(inboxText("This download contains customer personal data. Use it only for replies, quotations, appointments, service follow-up, support, security, or required records. Keep the file secure and delete it when no longer needed.", "这个下载文件包含买家个人资料。只可用于回复、报价、预约、跟进、客服、安全或必要记录。请妥善保管，不需要时删除。"))) return;
                status.textContent = inboxText("Preparing CSV export...", "正在准备 CSV 导出...");
                try {{
                    const blob = await merchantDownload(`/apps/enquiry/api/merchant/enquiries/export.csv?${{merchantQuery(500)}}`);
                    const url = URL.createObjectURL(blob);
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = `${{businessSlug}}-enquiries.csv`;
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                    URL.revokeObjectURL(url);
                    status.textContent = inboxText("CSV export ready.", "CSV 已准备好。");
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            function merchantQuery(limit = 100) {{
                const params = new URLSearchParams({{ business_slug: businessSlug, limit: String(limit) }});
                const status = document.getElementById("filterStatus")?.value;
                const priority = document.getElementById("filterPriority")?.value;
                const intent = document.getElementById("filterIntent")?.value;
                const source = document.getElementById("filterSource")?.value;
                const followUp = document.getElementById("filterFollowUp")?.value;
                const search = document.getElementById("filterSearch")?.value;
                if (status) params.set("status", status);
                if (priority) params.set("priority", priority);
                if (intent) params.set("intent", intent);
                if (source) params.set("source", source);
                if (followUp) params.set("follow_up", followUp);
                if (search) params.set("search", search);
                return params.toString();
            }}
            function clearMerchantFilters() {{
                document.getElementById("filterStatus").value = "";
                document.getElementById("filterPriority").value = "";
                document.getElementById("filterIntent").value = "";
                document.getElementById("filterSource").value = "";
                document.getElementById("filterFollowUp").value = "";
                document.getElementById("filterSearch").value = "";
                loadMerchantInbox();
            }}
            function formatMoney(value) {{
                return Number(value || 0).toLocaleString(undefined, {{ maximumFractionDigits: 0 }});
            }}
            function statusLabel(value) {{
                const labels = {{
                    new: "New",
                    contacted: "Contacted",
                    quoted: "Quoted",
                    won: "Booked / Sold",
                    lost: "Not Proceeding",
                    spam: "Spam"
                }};
                const zhLabels = {{
                    new: "新买家",
                    contacted: "已联系",
                    quoted: "已报价",
                    won: "已预约 / 成交",
                    lost: "不继续",
                    spam: "Spam"
                }};
                return inboxLang() === "zh" ? (zhLabels[value] || value || "未知") : (labels[value] || value || "Unknown");
            }}
            function sourceLabel(value) {{
                const labels = {{
                    whatsapp: "WhatsApp",
                    instagram: "Instagram",
                    facebook: "Facebook",
                    tiktok: "TikTok",
                    xiaohongshu: "Xiaohongshu",
                    direct: "Direct link",
                    "public-form": "Public form",
                    "google-business": "Google Business Profile",
                    "website-widget": "Website widget",
                    web: "Website",
                    demo: "Demo",
                    manual: "Manual / call",
                    referral: "Referral"
                }};
                const zhLabels = {{
                    whatsapp: "WhatsApp",
                    instagram: "Instagram",
                    facebook: "Facebook",
                    tiktok: "TikTok",
                    xiaohongshu: "Xiaohongshu",
                    direct: "直接 link",
                    "public-form": "公开表格",
                    "google-business": "Google 商家资料",
                    "website-widget": "网站 widget",
                    web: "网站",
                    demo: "Demo",
                    manual: "手动 / 电话",
                    referral: "介绍"
                }};
                return inboxLang() === "zh" ? (zhLabels[value] || value || "未知") : (labels[value] || value || "Unknown");
            }}
            function priorityLabel(value) {{
                const labels = {{ hot: "Answer now", warm: "Needs details", normal: "Ask next question" }};
                const zhLabels = {{ hot: "现在回复", warm: "需要资料", normal: "问下一题" }};
                return inboxLang() === "zh" ? (zhLabels[value] || value || "未知") : (labels[value] || value || "Unknown");
            }}
            function signalLabel(signal) {{
                const labels = {{
                    finance: "贷款 / 供车",
                    monthly_payment: "月供",
                    budget: "预算 / 头期",
                    price_comparison: "比价",
                    appointment: "看车 / 预约",
                    vehicle_fit: "车型匹配",
                    time_sensitive: "急件"
                }};
                return inboxLang() === "zh" ? (labels[signal.key] || signal.label) : signal.label;
            }}
            function renderFollowUpSignals(item) {{
                const signals = item.follow_up_signals || [];
                if (!signals.length) {{
                    return `<span class="lead-badge">${{langSpan("Need model / budget", "需要车型 / 预算")}}</span>`;
                }}
                return signals.map(signal => `
                    <span class="lead-badge ${{["finance", "monthly_payment", "appointment", "time_sensitive"].includes(signal.key) ? "hot" : ""}}" title="${{escapeHtml(signal.detail || "")}}">
                        ${{escapeHtml(signalLabel(signal))}}
                    </span>
                `).join("");
            }}
            function chooseNextAction(item) {{
                if (item.next_action) return item.next_action;
                if (item.status === "new" && item.priority === "hot") return inboxText("Reply now and mark Contacted", "现在回复，并标记已联系");
                if (item.status === "new") return inboxText("Send first WhatsApp reply", "先发第一句 WhatsApp");
                if (item.status === "contacted") return inboxText("Set follow-up date or mark Quoted", "设置跟进日期，或标记已报价");
                if (item.status === "quoted") return inboxText("Follow up and mark Booked or Not Proceeding", "继续跟进，并标记预约/成交或不继续");
                if (item.follow_up_at) return inboxText("Review scheduled follow-up", "查看已安排的跟进");
                return inboxText("Add note and next follow-up", "添加备注和下一次跟进");
            }}
            function isDueFollowUp(item) {{
                if (!item.follow_up_at || ["won", "lost", "spam"].includes(item.status)) return false;
                const due = new Date(item.follow_up_at.slice(0, 10) + "T00:00:00");
                const today = new Date();
                today.setHours(0, 0, 0, 0);
                return !Number.isNaN(due.getTime()) && due <= today;
            }}
            function simpleLeadRank(item) {{
                if (isDueFollowUp(item)) return 0;
                if (item.priority === "hot" && !["won", "lost", "spam"].includes(item.status)) return 1;
                if (item.status === "new") return 2;
                if (item.status === "quoted") return 3;
                if (item.status === "contacted") return 4;
                return 9;
            }}
            function renderDailyLeads(leads) {{
                const actionable = (leads || [])
                    .filter(item => !["won", "lost", "spam"].includes(item.status))
                    .sort((a, b) => simpleLeadRank(a) - simpleLeadRank(b))
                    .slice(0, 8);
                const target = document.getElementById("merchantDailyLeads");
                if (!actionable.length) {{
                    target.innerHTML = `
                        <div class="status">${{langSpan("No buyers need action right now. New enquiries and due follow-ups will appear here first.", "现在没有需要处理的买家。新的询问和到期跟进会优先出现在这里。")}}</div>
                    `;
                    setInboxLang(inboxLang());
                    return;
                }}
                target.innerHTML = actionable.map(item => `
                    <div class="simple-lead-card">
                        <div>
                            <strong>${{escapeHtml(item.name)}}</strong>
                            <small>${{escapeHtml(item.phone)}}${{item.email ? ` · ${{escapeHtml(item.email)}}` : ""}}</small>
                            <div class="lead-badges">
                                <span class="lead-badge ${{item.priority === "hot" || isDueFollowUp(item) ? "hot" : ""}}">${{isDueFollowUp(item) ? inboxText("Due now", "现在到期") : escapeHtml(priorityLabel(item.priority))}}</span>
                                <span class="lead-badge">${{escapeHtml(sourceLabel(item.source || "unknown"))}}</span>
                                <span class="lead-badge">${{escapeHtml(statusLabel(item.status))}}</span>
                            </div>
                        </div>
                        <div>
                            <strong>${{escapeHtml(chooseNextAction(item))}}</strong>
                            <small>${{escapeHtml((item.message || "").slice(0, 180))}}</small>
                            <small><strong>${{langSpan("Stuck point", "客户卡点")}}:</strong> ${{escapeHtml(item.stuck_point || "")}}</small>
                            <small><strong>${{langSpan("Next question", "下一句")}}:</strong> ${{escapeHtml(item.next_question || "")}}</small>
                            <div class="lead-badges">${{renderFollowUpSignals(item)}}</div>
                        </div>
                        <div class="simple-actions">
                            ${{item.whatsapp_url ? `<a class="btn" target="_blank" href="${{escapeHtml(item.whatsapp_url)}}">WhatsApp</a>` : ""}}
                            <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'contacted')">${{langSpan("Contacted", "已联系")}}</button>
                            <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'quoted')">${{langSpan("Quoted", "已报价")}}</button>
                            <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'won')">${{langSpan("Booked", "已预约")}}</button>
                        </div>
                    </div>
                `).join("");
                setInboxLang(inboxLang());
            }}
            function renderActionCenter(data) {{
                const stats = data.stats || {{}};
                const leads = data.enquiries || [];
                const onboarding = data.onboarding || {{}};
                const due = stats.due_followups || 0;
                const hot = (stats.by_priority || {{}}).hot || 0;
                const newCount = (stats.by_status || {{}}).new || 0;
                const quoted = (stats.by_status || {{}}).quoted || 0;
                const topLead = leads.find(isDueFollowUp) || leads.find(item => item.priority === "hot" && item.status !== "won" && item.status !== "lost") || leads.find(item => item.status === "new");
                const firstAction = due > 0
                    ? ["Follow up due buyers", `${{due}} buyer(s) need attention today.`, "跟进到期买家", `今天有 ${{due}} 位买家要处理。`]
                    : hot > 0
                        ? ["Answer buyers with stuck points", `${{hot}} buyer(s) need a reply or next question now.`, "先处理卡住的买家", `${{hot}} 位买家现在需要回复或下一题。`]
                        : newCount > 0
                            ? ["Reply to new buyers", `${{newCount}} new buyer(s) are waiting for first reply.`, "回复新买家", `${{newCount}} 位新买家正在等第一句回复。`]
                            : ["Review active buyers", "No urgent enquiries. Check quoted buyers and mark the next step.", "查看进行中的买家", "目前没有紧急询问。检查已报价买家，并标记下一步。"];
                const topLeadLineEn = topLead ? `Start with ${{escapeHtml(topLead.name)}}: ${{escapeHtml(chooseNextAction(topLead))}}.` : "Share your buyer link and wait for new enquiries.";
                const topLeadLineZh = topLead ? `先从 ${{escapeHtml(topLead.name)}} 开始：${{escapeHtml(chooseNextAction(topLead))}}。` : "分享买家 link，等待新的询问进来。";
                document.getElementById("merchantActionCenter").innerHTML = `
                    <div class="action-card">
                        <h3><span data-lang="en">Today&apos;s buyer follow-up</span><span data-lang="zh" class="lang-hidden">今日买家跟进</span></h3>
                        <p>${{langSpan(firstAction[1], firstAction[3])}}</p>
                        <div class="action-list">
                            <div class="action-item"><span class="action-dot">1</span><div><strong>${{langSpan(firstAction[0], firstAction[2])}}</strong><span>${{langSpan(topLeadLineEn, topLeadLineZh)}}</span></div></div>
                            <div class="action-item"><span class="action-dot">2</span><div><strong>${{langSpan("Reply on WhatsApp", "在 WhatsApp 回复")}}</strong><span>${{langSpan("Use the suggested next message, then mark the buyer as Contacted or Quoted.", "使用建议下一句，然后把买家标记为已联系或已报价。")}}</span></div></div>
                            <div class="action-item"><span class="action-dot">3</span><div><strong>${{langSpan("Set next follow-up", "设置下一次跟进")}}</strong><span>${{langSpan("Add a date so the buyer does not disappear inside chat history.", "加一个日期，避免买家消失在聊天记录里。")}}</span></div></div>
                        </div>
                    </div>
                    <div class="action-card">
                        <h3><span data-lang="en">Today&apos;s numbers</span><span data-lang="zh" class="lang-hidden">今日数字</span></h3>
                        <p>${{escapeHtml(onboarding.percent ?? 0)}}% ${{inboxText("ready", "已准备")}} · ${{escapeHtml(onboarding.next_action || inboxText("Complete setup before promotion.", "推广前先完成设置。"))}}</p>
                        <div class="lead-badges">
                            <span class="lead-badge ${{due ? "hot" : ""}}">${{langSpan("Due today", "今天到期")}} · ${{due}}</span>
                            <span class="lead-badge ${{hot ? "hot" : ""}}">${{langSpan("Needs answer", "需要回复")}} · ${{hot}}</span>
                            <span class="lead-badge">${{langSpan("New", "新买家")}} · ${{newCount}}</span>
                            <span class="lead-badge">${{langSpan("Quoted", "已报价")}} · ${{quoted}}</span>
                        </div>
                        <span class="next-action">${{langSpan("Use this page as the daily follow-up list.", "把这个页面当成每天的跟进清单。")}}</span>
                    </div>
                    <div class="action-card">
                        <h3>${{langSpan("Shortcuts", "快捷动作")}}</h3>
                        <p>${{langSpan("Most dealers only need these actions during the day.", "多数车商每天只需要这几个动作。")}}</p>
                        <div class="lead-badges">
                            <button class="btn secondary" onclick="document.getElementById('filterStatus').value='new'; loadMerchantInbox()">${{langSpan("New buyers", "新买家")}}</button>
                            <button class="btn secondary" onclick="document.getElementById('filterFollowUp').value='due'; loadMerchantInbox()">${{langSpan("Due today", "今天到期")}}</button>
                            <button class="btn secondary" onclick="copyMerchantElement('merchantShareDirect', 'Buyer enquiry link')">${{langSpan("Copy buyer link", "复制买家 link")}}</button>
                        </div>
                    </div>
                `;
                setInboxLang(inboxLang());
            }}
            function renderChannelCenter(stats) {{
                const bySource = (stats || {{}}).by_source || {{}};
                const channels = [
                    ["whatsapp", "WhatsApp", "Assisted capture now; Meta auto-sync after setup.", "现在先辅助导入；Meta 设置完成后再自动同步。"],
                    ["instagram", "Instagram", "Use bio link, DM link, or manual assisted capture.", "使用简介 link、私信 link，或手动辅助导入。"],
                    ["facebook", "Facebook", "Track Marketplace, page, group, or Messenger enquiries.", "追踪 Marketplace、专页、群组或 Messenger 询问。"],
                    ["tiktok", "TikTok", "Use profile link or assisted capture for video comments and DMs.", "用主页 link 或辅助导入评论和私信。"],
                    ["xiaohongshu", "Xiaohongshu", "Use link-in-bio or assisted capture for notes and DMs.", "用主页 link 或辅助导入笔记和私信。"],
                    ["direct", "Direct link", "Track buyers who came through the shared enquiry link.", "追踪从共享询问 link 进来的买家。"],
                    ["google-business", "Google Business Profile", "Track enquiries from your Google Business Profile link.", "追踪 Google 商家资料 link 的询问。"],
                    ["website-widget", "Website widget", "Track enquiries that came from the embedded website widget.", "追踪网站 widget 进来的询问。"],
                    ["manual", "Calls / referrals", "Add phone, walk-in, or referral leads without losing follow-up.", "把电话、walk-in 或介绍客户也加进来，不漏跟进。"]
                ];
                const totalSources = Object.values(bySource).reduce((sum, count) => sum + Number(count || 0), 0);
                document.getElementById("merchantChannelCenter").innerHTML = `
                    <div class="action-card">
                        <h3>${{langSpan("Social source inbox", "社媒来源 inbox")}}</h3>
                        <p>${{langSpan("Use this as one daily working list even when the buyer first came from social media, WhatsApp, calls, or referrals.", "无论买家先从社媒、WhatsApp、电话或介绍进来，都集中成每天一个工作清单。")}}</p>
                        <div class="lead-badges">
                            ${{channels.map(([key, label]) => `<span class="lead-badge ${{bySource[key] ? "hot" : ""}}">${{escapeHtml(label)}} · ${{bySource[key] || 0}}</span>`).join("")}}
                        </div>
                        <span class="next-action">${{langSpan("Tracked sources", "已追踪来源")}}: ${{totalSources}} ${{inboxText("enquiry record(s) with source data.", "笔有来源资料的询问记录。")}}</span>
                    </div>
                    <div class="action-card">
                        <h3>${{langSpan("Assisted capture", "辅助导入")}}</h3>
                        <p>${{langSpan("When direct sync is not ready, use Add buyer from DM / call above. Official API sync can be requested later without storing platform passwords here.", "直接同步还没准备好时，先用上面的从私信 / 电话新增买家。之后可申请官方 API 同步，这里不会保存平台密码。")}}</p>
                        <div class="action-list">
                            ${{channels.slice(1, 5).map(([key, label, detail, zhDetail]) => `
                                <div class="action-item"><span class="action-dot">${{bySource[key] || 0}}</span><div><strong>${{escapeHtml(label)}}</strong><span>${{langSpan(detail, zhDetail)}}</span></div></div>
                            `).join("")}}
                        </div>
                    </div>
                `;
                setInboxLang(inboxLang());
            }}
            function renderPipelineBoard(stats) {{
                const byStatus = (stats || {{}}).by_status || {{}};
                const stages = [
                    ["new", "First reply needed", "需要第一句回复"],
                    ["contacted", "Waiting for buyer", "等待买家回应"],
                    ["quoted", "Follow up to close", "跟进成交"],
                    ["won", "Booked or sold", "已预约或成交"],
                    ["lost", "Not proceeding", "不继续"]
                ];
                document.getElementById("merchantPipelineBoard").innerHTML = stages.map(([key, hint, zhHint]) => `
                    <div class="stage-card">
                        <strong>${{statusLabel(key)}} <span>${{byStatus[key] || 0}}</span></strong>
                        <span>${{langSpan(hint, zhHint)}}</span>
                    </div>
                `).join("");
                setInboxLang(inboxLang());
            }}
            async function setMerchantStatus(id, status) {{
                try {{
                    await merchantApi(`/apps/enquiry/api/merchant/enquiries/${{id}}?business_slug=${{businessSlug}}`, {{
                        method: "PATCH",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{ status }})
                    }});
                    await loadMerchantInbox();
                }} catch (error) {{
                    document.getElementById("merchantStatus").textContent = error.message;
                }}
            }}
            async function saveMerchantNote(id) {{
                try {{
                    const note = document.getElementById(`note-${{id}}`).value;
                    const followUpAt = document.getElementById(`follow-up-${{id}}`).value;
                    const dealValueText = document.getElementById(`deal-value-${{id}}`).value;
                    const dealValue = dealValueText === "" ? null : Number(dealValueText);
                    await merchantApi(`/apps/enquiry/api/merchant/enquiries/${{id}}?business_slug=${{businessSlug}}`, {{
                        method: "PATCH",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{ internal_note: note, follow_up_at: followUpAt, deal_value: dealValue }})
                    }});
                    document.getElementById("merchantStatus").textContent = inboxText("Buyer details saved.", "买家资料已保存。");
                    await loadMerchantInbox();
                }} catch (error) {{
                    document.getElementById("merchantStatus").textContent = error.message;
                }}
            }}
            async function deleteMerchantLead(id) {{
                if (!confirm(inboxText("Delete this enquiry? This removes the buyer from this inbox.", "删除这个询问？这个买家会从 inbox 移除。"))) return;
                try {{
                    await merchantApi(`/apps/enquiry/api/merchant/enquiries/${{id}}?business_slug=${{businessSlug}}`, {{
                        method: "DELETE"
                    }});
                    document.getElementById("merchantStatus").textContent = inboxText("Buyer deleted.", "买家已删除。");
                    await loadMerchantInbox();
                }} catch (error) {{
                    document.getElementById("merchantStatus").textContent = error.message;
                }}
            }}
            async function loadMerchantInbox() {{
                const status = document.getElementById("merchantStatus");
                status.textContent = inboxText("Loading buyers...", "正在加载买家...");
                try {{
                    const data = await merchantApi(`/apps/enquiry/api/merchant/enquiries?${{merchantQuery(100)}}`);
                    fillMerchantSettings(data.business);
                    await loadMerchantShareLinks();
                    const stats = data.stats || {{}};
                    document.getElementById("merchantStats").innerHTML = `
                        <section class="card"><h3>${{langSpan("Total buyers", "总买家")}}</h3><div class="price">${{stats.total || 0}}</div></section>
                        <section class="card"><h3>${{langSpan("Need answer now", "现在需要回复")}}</h3><div class="price">${{(stats.by_priority || {{}}).hot || 0}}</div></section>
                        <section class="card"><h3>${{langSpan("Top Source", "最多来源")}}</h3><div class="price">${{escapeHtml(Object.entries(stats.by_source || {{}}).sort((a, b) => b[1] - a[1])[0]?.[0] || "none")}}</div></section>
                        <section class="card"><h3>${{langSpan("Recorded sale value", "记录成交额")}}</h3><div class="price">${{formatMoney(stats.pipeline_value)}}</div></section>
                        <section class="card"><h3>${{langSpan("Due today", "今天到期")}}</h3><div class="price">${{stats.due_followups || 0}}</div></section>
                    `;
                    renderActionCenter(data);
                    renderDailyLeads(data.enquiries || []);
                    renderChannelCenter(stats);
                    renderPipelineBoard(stats);
                    localStorage.setItem(`nexaflow_business_key_${{businessSlug}}`, document.getElementById("businessKey").value);
                    markChecklistStep("loaded");
                    if ((data.enquiries || []).length > 0) markChecklistStep("first_lead");
                    document.getElementById("merchantRows").innerHTML = data.enquiries.map(item => `
                        <tr>
                            <td>${{escapeHtml(item.created_at)}}</td>
                            <td>${{escapeHtml(item.name)}}<br>${{escapeHtml(item.phone)}}<br>${{escapeHtml(item.email || "")}}</td>
                            <td>${{escapeHtml(sourceLabel(item.source || "unknown"))}}${{item.campaign ? `<br>${{escapeHtml(item.campaign)}}` : ""}}${{item.referrer ? `<br><small>${{escapeHtml(item.referrer.slice(0, 80))}}</small>` : ""}}</td>
                            <td>${{escapeHtml(item.intent)}}</td>
                            <td>${{escapeHtml(priorityLabel(item.priority))}}</td>
                            <td>${{escapeHtml(item.message)}}${{item.auto_summary ? `<br><small>${{escapeHtml(item.auto_summary)}}</small>` : ""}}</td>
                            <td>
                                <div class="lead-badges">${{renderFollowUpSignals(item)}}</div>
                                <span class="next-action">${{escapeHtml(item.stuck_point || "")}}</span>
                                <span class="next-action">${{escapeHtml(chooseNextAction(item))}}</span>
                                <small><strong>${{langSpan("Next question", "下一句")}}:</strong> ${{escapeHtml(item.next_question || "")}}</small>
                                <small><strong>${{langSpan("Timing", "时间")}}:</strong> ${{escapeHtml(item.follow_up_timing || "")}}</small>
                                ${{(item.follow_up_signals || []).map(signal => `<small>${{escapeHtml(signal.detail || "")}}</small>`).join("<br>")}}
                            </td>
                            <td>${{escapeHtml(item.reply_draft)}}</td>
                            <td><input id="follow-up-${{item.id}}" type="date" value="${{escapeHtml(item.follow_up_at || "")}}"></td>
                            <td><input id="deal-value-${{item.id}}" type="number" min="0" step="0.01" value="${{item.deal_value ?? ""}}" placeholder="0"></td>
                            <td>
                                <textarea id="note-${{item.id}}" placeholder="Internal follow-up note">${{escapeHtml(item.internal_note || "")}}</textarea>
                                <span class="next-action">${{escapeHtml(chooseNextAction(item))}}</span>
                                ${{item.follow_up_recommendation ? `<span class="next-action">${{escapeHtml(item.follow_up_recommendation)}}</span>` : ""}}
                                <button class="btn secondary" onclick="saveMerchantNote(${{item.id}})">${{langSpan("Save Details", "保存资料")}}</button>
                            </td>
                            <td><span class="lead-badge ${{item.priority === "hot" ? "hot" : ""}}">${{escapeHtml(statusLabel(item.status))}}</span></td>
                            <td>
                                ${{item.whatsapp_url ? `<a class="btn secondary" target="_blank" href="${{escapeHtml(item.whatsapp_url)}}">WhatsApp</a>` : ""}}
                                <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'contacted')">${{langSpan("Contacted", "已联系")}}</button>
                                <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'quoted')">${{langSpan("Quoted", "已报价")}}</button>
                                <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'won')">${{langSpan("Booked", "已预约")}}</button>
                                <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'lost')">${{langSpan("Not Proceeding", "不继续")}}</button>
                                <button class="btn secondary" onclick="deleteMerchantLead(${{item.id}})">${{langSpan("Delete", "删除")}}</button>
                            </td>
                        </tr>
                    `).join("");
                    setInboxLang(inboxLang());
                    status.textContent = inboxText("Buyer list loaded.", "买家列表已加载。");
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            const savedBusinessKey = localStorage.getItem(`nexaflow_business_key_${{businessSlug}}`);
            if (savedBusinessKey) {{
                document.getElementById("businessKey").value = savedBusinessKey;
                loadMerchantInbox();
            }}
            renderMerchantChecklist();
            setInboxLang(localStorage.getItem(inboxLangStorageKey) || "en");
        </script>
        """
    )


@app.get("/channels/{business_slug}", response_class=HTMLResponse)
@app.get("/apps/enquiry/channels/{business_slug}", response_class=HTMLResponse)
def merchant_channel_connections_page(business_slug: str):
    profile = get_business_profile(business_slug)
    business_name = escape_html(profile["business_name"])
    slug = escape_html(profile["slug"])
    return merchant_html(
        f"{business_name} Social Source Setup",
        profile["business_name"],
        f"""
        <section class="hero compact">
            <div>
                <div class="language-toggle" aria-label="Language">
                    <button type="button" class="active" onclick="setChannelsLang('en')" id="channelsLangEn">EN</button>
                    <button type="button" onclick="setChannelsLang('zh')" id="channelsLangZh">中文</button>
                </div>
                <div class="eyebrow"><span data-lang="en">Social sources</span><span data-lang="zh" class="lang-hidden">社媒来源</span></div>
                <h1><span data-lang="en">Social Source Setup</span><span data-lang="zh" class="lang-hidden">询问来源设置</span></h1>
                <p class="lead"><span data-lang="en">Set where buyer messages come from. Start with WhatsApp, Facebook, and Instagram; keep TikTok and Xiaohongshu in assisted mode until official access is ready.</span><span data-lang="zh" class="lang-hidden">设置买家消息从哪里来。先从 WhatsApp、Facebook、Instagram 开始；TikTok 和小红书先用辅助导入，等官方权限准备好再同步。</span></p>
            </div>
        </section>
        <section class="form-card">
            <div class="toolbar">
                <label><span data-lang="en">Inbox password</span><span data-lang="zh" class="lang-hidden">Inbox 密码</span><input id="businessKey" type="password" placeholder="biz_..."></label>
                <button class="btn" onclick="loadChannelConnections()"><span data-lang="en">Open Settings</span><span data-lang="zh" class="lang-hidden">打开设置</span></button>
                <a class="btn secondary" href="/inbox/{slug}"><span data-lang="en">Back to Inbox</span><span data-lang="zh" class="lang-hidden">回到 Inbox</span></a>
            </div>
            <div class="status" id="channelStatus"><span data-lang="en">Enter the owner inbox password to manage social source settings.</span><span data-lang="zh" class="lang-hidden">输入老板 inbox 密码来管理社媒来源设置。</span></div>
            <p class="mini-note"><span data-lang="en">Never paste platform passwords, OTPs, cookies, access tokens, or customer identity documents here. This setup stores connection metadata only.</span><span data-lang="zh" class="lang-hidden">不要在这里粘贴平台密码、OTP、cookies、access token 或客户身份证件。这里仅保存连接设置资料。</span></p>
        </section>
        <section class="grid" id="channelSummary"></section>
        <section class="form-card" id="metaSetupPanel">
            <div class="section-head">
                <div>
                    <h2><span data-lang="en">Meta auto-sync setup request</span><span data-lang="zh" class="lang-hidden">Meta 自动同步设置申请</span></h2>
                    <p><span data-lang="en">Use this after Meta Developer access is ready for WhatsApp Business, Facebook Messenger, and Instagram DM. During the pilot, keep using manual DM/call capture in the inbox. NexaFlow stores account IDs only, not tokens or passwords.</span><span data-lang="zh" class="lang-hidden">等 Meta Developer 权限准备好后，用这里接 WhatsApp Business、Facebook Messenger 和 Instagram DM。试用期间先继续在 inbox 手动导入私信/电话内容。NexaFlow 只保存账号 ID，不保存 token 或密码。</span></p>
                </div>
            </div>
            <div id="metaSetupContent" class="status"><span data-lang="en">Open settings to load Meta setup details.</span><span data-lang="zh" class="lang-hidden">打开设置后会加载 Meta 设置资料。</span></div>
        </section>
        <details class="form-card">
            <summary><span data-lang="en">Security details</span><span data-lang="zh" class="lang-hidden">安全细节</span></summary>
            <div class="section-head">
                <div>
                    <h2><span data-lang="en">Security baseline</span><span data-lang="zh" class="lang-hidden">安全底线</span></h2>
                    <p><span data-lang="en">Direct DM sync must use official APIs, signed webhooks, least-privilege permissions, audit logs, and retention rules. Unsupported inbox scraping is not part of NexaFlow.</span><span data-lang="zh" class="lang-hidden">直接同步私信必须使用官方 API、签名 webhook、最小权限、审计记录和资料保留规则。NexaFlow 不做不受支持的 inbox 抓取。</span></p>
                </div>
            </div>
            <div class="setup-panel">
                <div class="setup-step"><strong><span data-lang="en">No token paste</span><span data-lang="zh" class="lang-hidden">不粘贴 token</span></strong><span><span data-lang="en">OAuth tokens and app secrets belong in server-side secret storage, not merchant forms.</span><span data-lang="zh" class="lang-hidden">OAuth token 和 app secret 应保存在服务器 secret storage，不应放进车商表格。</span></span></div>
                <div class="setup-step"><strong><span data-lang="en">Owner key required</span><span data-lang="zh" class="lang-hidden">需要老板密钥</span></strong><span><span data-lang="en">Only the private owner inbox key can view or update channel connection plans.</span><span data-lang="zh" class="lang-hidden">只有私密老板 inbox 密钥可以查看或更新渠道连接计划。</span></span></div>
                <div class="setup-step"><strong><span data-lang="en">Audit trail</span><span data-lang="zh" class="lang-hidden">审计记录</span></strong><span><span data-lang="en">Every channel request records who changed what, without exposing customer message content.</span><span data-lang="zh" class="lang-hidden">每个渠道请求都会记录谁改了什么，但不暴露客户消息内容。</span></span></div>
                <div class="setup-step"><strong><span data-lang="en">Retention aware</span><span data-lang="zh" class="lang-hidden">资料保留规则</span></strong><span><span data-lang="en">Future synced messages must follow the business data-retention window.</span><span data-lang="zh" class="lang-hidden">未来同步的消息必须遵守商家的资料保留期限。</span></span></div>
            </div>
        </details>
        <section class="grid" id="channelCards"></section>
        <script>
            const businessSlug = "{slug}";
            const channelsLangStorageKey = "nexaflow_channels_lang";
            function channelsLang() {{
                return localStorage.getItem(channelsLangStorageKey) || "en";
            }}
            function channelsText(en, zh) {{
                return channelsLang() === "zh" ? zh : en;
            }}
            function setChannelsLang(lang) {{
                document.querySelectorAll("[data-lang]").forEach(item => {{
                    item.classList.toggle("lang-hidden", item.dataset.lang !== lang);
                }});
                document.getElementById("channelsLangEn").classList.toggle("active", lang === "en");
                document.getElementById("channelsLangZh").classList.toggle("active", lang === "zh");
                localStorage.setItem(channelsLangStorageKey, lang);
            }}
            function escapeHtml(value) {{
                return String(value ?? "").replace(/[&<>"']/g, char => ({{
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }}[char]));
            }}
            function channelLangSpan(en, zh) {{
                return `<span data-lang="en">${{escapeHtml(en)}}</span><span data-lang="zh" class="lang-hidden">${{escapeHtml(zh)}}</span>`;
            }}
            function selected(value, expected) {{
                return value === expected ? "selected" : "";
            }}
            async function copyChannelText(value) {{
                const status = document.getElementById("channelStatus");
                try {{
                    await navigator.clipboard.writeText(value);
                    status.textContent = channelsText("Copied.", "已复制。");
                }} catch (error) {{
                    status.textContent = channelsText(`Copy failed. Select manually: ${{value}}`, `复制失败。请手动选择：${{value}}`);
                }}
            }}
            async function channelApi(path, options = {{}}) {{
                const businessKey = document.getElementById("businessKey").value;
                const headers = {{ "X-Business-Key": businessKey, ...(options.headers || {{}}) }};
                const response = await fetch(path, {{ ...options, headers }});
                if (!response.ok) {{
                    throw new Error(await response.text());
                }}
                return response.json();
            }}
            async function loadMetaSetup() {{
                const target = document.getElementById("metaSetupContent");
                try {{
                    const setup = await channelApi(`/apps/enquiry/api/merchant/meta-setup?business_slug=${{businessSlug}}`);
                    const webhook = setup.webhook || {{}};
                    const channels = setup.meta_channels || [];
                    const statusText = (value) => value ? channelsText("Ready", "已准备") : channelsText("Missing", "缺少");
                    target.className = "";
                    target.innerHTML = `
                        <div class="action-center">
                            <div class="action-card">
                                <h3>${{channelLangSpan("Webhook URL", "Webhook URL")}}</h3>
                                <p>${{channelLangSpan("Paste this URL into your Meta App webhook callback URL after Meta Developer access is ready.", "等 Meta Developer 权限准备好后，把这个 URL 填到 Meta App 的 webhook callback URL。")}}</p>
                                <code id="metaWebhookUrl">${{escapeHtml(webhook.url || "")}}</code>
                                <button class="btn secondary" onclick="copyChannelText(document.getElementById('metaWebhookUrl').textContent)">${{channelLangSpan("Copy URL", "复制 URL")}}</button>
                            </div>
                            <div class="action-card">
                                <h3>${{channelLangSpan("Server status", "服务器状态")}}</h3>
                                <div class="lead-badges">
                                    <span class="lead-badge ${{webhook.ready_for_meta_setup ? "hot" : ""}}">${{channelLangSpan("Meta setup", "Meta 设置")}} · ${{statusText(webhook.ready_for_meta_setup)}}</span>
                                    <span class="lead-badge ${{webhook.verify_token_configured ? "hot" : ""}}">${{channelLangSpan("Verify token", "Verify token")}} · ${{statusText(webhook.verify_token_configured)}}</span>
                                    <span class="lead-badge ${{webhook.app_secret_configured ? "hot" : ""}}">${{channelLangSpan("App secret", "App secret")}} · ${{statusText(webhook.app_secret_configured)}}</span>
                                    <span class="lead-badge ${{webhook.https_callback_url ? "hot" : ""}}">HTTPS · ${{statusText(webhook.https_callback_url)}}</span>
                                    <span class="lead-badge">${{channelLangSpan("Signature required", "需要签名验证")}}</span>
                                </div>
                                <span class="next-action">${{escapeHtml((setup.security || {{}}).notes || "")}}</span>
                            </div>
                        </div>
                        <div class="setup-panel">
                            ${{channels.map(item => `
                                <div class="setup-step">
                                    <strong>${{escapeHtml(item.label)}}</strong>
                                    <span>${{escapeHtml((item.id_field || {{}}).label || item.external_account_id_label)}} · ${{escapeHtml((item.id_field || {{}}).where_to_find || item.where_to_find)}}</span>
                                    <span>${{channelLangSpan("Webhook match", "Webhook 匹配")}}: ${{escapeHtml((item.id_field || {{}}).matched_from || "")}}</span>
                                </div>
                            `).join("")}}
                        </div>
                    `;
                    setChannelsLang(channelsLang());
                }} catch (error) {{
                    target.className = "status";
                    target.textContent = error.message;
                }}
            }}
            function renderChannelConnections(payload) {{
                const summary = payload.summary || {{}};
                const protection = payload.data_protection || {{}};
                const connections = payload.connections || [];
                const mainChannels = connections.filter(item => ["whatsapp", "facebook", "instagram"].includes(item.channel));
                const otherChannels = connections.filter(item => !["whatsapp", "facebook", "instagram"].includes(item.channel));
                document.getElementById("channelSummary").innerHTML = `
                    <section class="card"><h3>${{channelLangSpan("Main sources", "主要来源")}}</h3><div class="price">${{mainChannels.length}}</div><p>WhatsApp, Facebook, Instagram</p></section>
                    <section class="card"><h3>${{channelLangSpan("Setup requested", "已请求设置")}}</h3><div class="price">${{summary.configured || 0}}</div><p>${{channelLangSpan("Saved source settings", "已保存来源设置")}}</p></section>
                    <section class="card"><h3>${{channelLangSpan("Limited sources", "受限来源")}}</h3><div class="price">${{summary.limited || 0}}</div><p>${{channelLangSpan("TikTok / Xiaohongshu assisted mode", "TikTok / 小红书辅助导入模式")}}</p></section>
                `;
                function renderChannelCard(item) {{
                    const idField = item.id_field || {{}};
                    const idFieldLabel = channelLangSpan(
                        idField.label || "External account ID",
                        idField.label_zh || idField.label || "外部账号 ID"
                    );
                    const idFieldHelp = idField.help ? channelLangSpan(idField.help, idField.help_zh || idField.help) : "";
                    const idFieldMatch = idField.matched_from
                        ? `${{channelLangSpan("Webhook match", "Webhook 匹配")}}: ${{escapeHtml(idField.matched_from)}}`
                        : "";
                    return `
                    <section class="card">
                        <h3>${{escapeHtml(item.label)}}</h3>
                        <p>${{escapeHtml(item.data_note || "")}}</p>
                        <div class="lead-badges">
                            <span class="lead-badge ${{item.official_status === "limited" ? "" : "hot"}}">${{item.official_status === "limited" ? channelLangSpan("Assisted only", "只支持辅助导入") : channelLangSpan("Meta sync request", "Meta 同步申请")}}</span>
                            <span class="lead-badge">${{item.data_processing_acknowledged ? channelLangSpan("Confirmed", "已确认") : channelLangSpan("Needs confirm", "需要确认")}}</span>
                        </div>
                        <label>${{channelLangSpan("Current way", "目前方式")}}
                            <select id="mode-${{item.channel}}">
                                <option value="official_api_requested" ${{selected(item.integration_mode, "official_api_requested")}}>Request Meta sync later / 之后申请 Meta 同步</option>
                                <option value="assisted_capture" ${{selected(item.integration_mode, "assisted_capture")}}>Manual / assisted record / 手动或辅助记录</option>
                                <option value="smart_link" ${{selected(item.integration_mode, "smart_link")}}>Link or QR / Link 或 QR</option>
                                <option value="lead_form" ${{selected(item.integration_mode, "lead_form")}}>Lead form / 询问表格</option>
                            </select>
                        </label>
                        <label>${{channelLangSpan("Account or page label", "账号或专页名称")}}<input id="label-${{item.channel}}" value="${{escapeHtml(item.account_label || "")}}" placeholder="@dealer or page name"></label>
                        <label class="checkbox-label"><input id="ack-${{item.channel}}" type="checkbox" ${{item.data_processing_acknowledged ? "checked" : ""}}> <span>${{channelLangSpan("I confirm this is for buyer follow-up and I will not enter passwords or verification codes here.", "我确认这是用于买家跟进，并且不会在这里输入密码或验证码。")}}</span></label>
                        <details>
                            <summary>${{channelLangSpan("Advanced channel details", "高级渠道细节")}}</summary>
                            <label>${{channelLangSpan("Status", "状态")}}
                                <select id="status-${{item.channel}}">
                                    <option value="requested" ${{selected(item.status, "requested")}}>Setup request / 设置请求</option>
                                    <option value="assisted" ${{selected(item.status, "assisted")}}>Assisted capture active / 辅助导入已开启</option>
                                    <option value="paused" ${{selected(item.status, "paused")}}>Paused / 暂停</option>
                                </select>
                            </label>
                            <label>${{idFieldLabel}}<input id="external-${{item.channel}}" value="${{escapeHtml(item.external_account_id || "")}}" placeholder="${{escapeHtml(idField.meta_name || "external_account_id")}}"></label>
                            <span class="mini-note">${{idFieldHelp}}${{idFieldHelp && idFieldMatch ? "<br>" : ""}}${{idFieldMatch}}</span>
                            <label>${{channelLangSpan("Notes", "备注")}}<textarea id="notes-${{item.channel}}" placeholder="Setup notes, never secrets">${{escapeHtml(item.notes || "")}}</textarea></label>
                            <div class="lead-badges">
                                ${{(item.capabilities || []).map(value => `<span class="lead-badge">${{escapeHtml(value)}}</span>`).join("")}}
                            </div>
                            <span class="next-action">${{channelLangSpan("Security", "安全")}}: ${{(item.security_requirements || []).map(value => escapeHtml(value)).join(" · ")}}</span>
                        </details>
                        <button class="btn" onclick="saveChannelConnection('${{item.channel}}')">${{channelLangSpan("Save source", "保存来源")}}</button>
                        ${{["whatsapp", "facebook", "instagram"].includes(item.channel) ? `<button class="btn secondary" onclick="sendMetaPilotTest('${{item.channel}}')">${{channelLangSpan("Create local test buyer", "创建本地测试买家")}}</button>` : ""}}
                    </section>
                    `;
                }}
                document.getElementById("channelCards").innerHTML = `
                    ${{mainChannels.map(renderChannelCard).join("")}}
                    <details class="form-card">
                        <summary>${{channelLangSpan("Other sources: TikTok, Xiaohongshu, website, and assisted capture", "其他来源：TikTok、小红书、网站和辅助导入")}}</summary>
                        <div class="grid">${{otherChannels.map(renderChannelCard).join("")}}</div>
                    </details>
                `;
                setChannelsLang(channelsLang());
            }}
            async function loadChannelConnections() {{
                const status = document.getElementById("channelStatus");
                status.textContent = channelsText("Loading channel connections...", "正在加载渠道连接...");
                try {{
                    const payload = await channelApi(`/apps/enquiry/api/merchant/channel-connections?business_slug=${{businessSlug}}`);
                    renderChannelConnections(payload);
                    await loadMetaSetup();
                    localStorage.setItem(`nexaflow_business_key_${{businessSlug}}`, document.getElementById("businessKey").value);
                    status.textContent = payload.security_notice || channelsText("Loaded.", "已加载。");
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            async function saveChannelConnection(channel) {{
                const status = document.getElementById("channelStatus");
                status.textContent = channelsText(`Saving ${{channel}} connection...`, `正在保存 ${{channel}} 连接...`);
                try {{
                    await channelApi(`/apps/enquiry/api/merchant/channel-connections/${{channel}}?business_slug=${{businessSlug}}`, {{
                        method: "PATCH",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{
                            integration_mode: document.getElementById(`mode-${{channel}}`).value,
                            status: document.getElementById(`status-${{channel}}`).value,
                            account_label: document.getElementById(`label-${{channel}}`).value,
                            external_account_id: document.getElementById(`external-${{channel}}`).value,
                            notes: document.getElementById(`notes-${{channel}}`).value,
                            data_processing_acknowledged: document.getElementById(`ack-${{channel}}`).checked
                        }})
                    }});
                    await loadChannelConnections();
                    status.textContent = channelsText(`${{channel}} connection saved. No passwords or access tokens were stored.`, `${{channel}} 连接已保存。没有保存任何密码或 access token。`);
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            async function sendMetaPilotTest(channel) {{
                const status = document.getElementById("channelStatus");
                status.textContent = channelsText(`Creating local ${{channel}} test buyer...`, `正在创建本地 ${{channel}} 测试买家...`);
                try {{
                    const result = await channelApi(`/apps/enquiry/api/merchant/channel-connections/${{channel}}/pilot-test?business_slug=${{businessSlug}}`, {{
                        method: "POST"
                    }});
                    status.innerHTML = `
                        ${{channelLangSpan("Local test buyer created. This does not send or receive a real Meta DM yet. Open the buyer inbox and confirm the test buyer appears.", "本地测试买家已创建。这还不会发送或接收真实 Meta 私信。打开买家 inbox，确认测试买家已经出现。")}}
                        <br><a href="${{escapeHtml(result.inbox_url || `/inbox/${{businessSlug}}`)}}">${{channelLangSpan("Open buyer inbox", "打开买家 inbox")}}</a>
                    `;
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            const savedBusinessKey = localStorage.getItem(`nexaflow_business_key_${{businessSlug}}`);
            if (savedBusinessKey) {{
                document.getElementById("businessKey").value = savedBusinessKey;
                loadChannelConnections();
            }}
            setChannelsLang(localStorage.getItem(channelsLangStorageKey) || "en");
        </script>
        """
    )


@app.get("/embed/enquiry/{business_slug}.js")
def enquiry_embed_script(business_slug: str):
    profile = get_business_profile(business_slug)
    if profile["status"] != "active":
        raise HTTPException(status_code=404, detail="Business profile not found")

    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com").rstrip("/")
    payload = {
        "slug": profile["slug"],
        "businessName": profile["business_name"],
        "formUrl": f"{site_url}{profile['form_url']}",
    }
    script = f"""
(function () {{
  var config = {json.dumps(payload)};
  if (document.getElementById("nexaflow-enquiry-widget-" + config.slug)) return;

  var root = document.createElement("div");
  root.id = "nexaflow-enquiry-widget-" + config.slug;
  root.style.position = "fixed";
  root.style.right = "20px";
  root.style.bottom = "20px";
  root.style.zIndex = "2147483647";
  root.style.fontFamily = "Arial, Helvetica, sans-serif";

  var button = document.createElement("button");
  button.type = "button";
  button.textContent = "Enquire";
  button.setAttribute("aria-label", "Open enquiry form for " + config.businessName);
  button.style.border = "1px solid #ffffff";
  button.style.borderRadius = "999px";
  button.style.background = "#ffffff";
  button.style.color = "#000000";
  button.style.padding = "12px 18px";
  button.style.fontWeight = "700";
  button.style.cursor = "pointer";
  button.style.boxShadow = "0 12px 28px rgba(0,0,0,.22)";

  var panel = document.createElement("div");
  panel.style.display = "none";
  panel.style.position = "absolute";
  panel.style.right = "0";
  panel.style.bottom = "58px";
  panel.style.width = "min(420px, calc(100vw - 32px))";
  panel.style.height = "min(640px, calc(100vh - 96px))";
  panel.style.border = "1px solid #262626";
  panel.style.borderRadius = "10px";
  panel.style.overflow = "hidden";
  panel.style.background = "#000000";
  panel.style.boxShadow = "0 18px 42px rgba(0,0,0,.35)";

  var topbar = document.createElement("div");
  topbar.style.display = "flex";
  topbar.style.alignItems = "center";
  topbar.style.justifyContent = "space-between";
  topbar.style.gap = "12px";
  topbar.style.padding = "10px 12px";
  topbar.style.background = "#0b0b0d";
  topbar.style.color = "#f5f5f5";
  topbar.style.borderBottom = "1px solid #262626";

  var title = document.createElement("strong");
  title.textContent = config.businessName;
  title.style.fontSize = "14px";

  var close = document.createElement("button");
  close.type = "button";
  close.textContent = "Close";
  close.style.border = "1px solid #404040";
  close.style.borderRadius = "6px";
  close.style.background = "transparent";
  close.style.color = "#f5f5f5";
  close.style.padding = "6px 9px";
  close.style.cursor = "pointer";

  var iframe = document.createElement("iframe");
  iframe.title = config.businessName + " enquiry form";
  var formUrl = new URL(config.formUrl);
  formUrl.searchParams.set("source", "website-widget");
  formUrl.searchParams.set("referrer", window.location.href);
  formUrl.searchParams.set("page_url", window.location.href);
  iframe.src = formUrl.toString();
  iframe.loading = "lazy";
  iframe.style.width = "100%";
  iframe.style.height = "calc(100% - 45px)";
  iframe.style.border = "0";
  iframe.style.background = "#000000";

  function toggle(open) {{
    panel.style.display = open ? "block" : "none";
    button.textContent = open ? "Close enquiry" : "Enquire";
  }}

  button.addEventListener("click", function () {{
    toggle(panel.style.display === "none");
  }});
  close.addEventListener("click", function () {{ toggle(false); }});

  topbar.appendChild(title);
  topbar.appendChild(close);
  panel.appendChild(topbar);
  panel.appendChild(iframe);
  root.appendChild(panel);
  root.appendChild(button);
  document.body.appendChild(root);
}})();
""".strip()
    return Response(
        content=script,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/enquiry-admin", response_class=HTMLResponse)
@app.get("/apps/enquiry/admin", response_class=HTMLResponse)
def enquiry_admin_page():
    return merchant_html(
        "NexaFlow Enquiry Inbox",
        "NexaFlow Enquiry Admin",
        """
        <section class="hero compact">
            <div>
                <div class="eyebrow">Owner admin</div>
                <h1>Set up and manage merchant inboxes</h1>
                <p class="lead">Create a merchant profile once, then give them a public enquiry link and private inbox link.</p>
            </div>
        </section>
        <section class="admin-split">
            <div class="form-card">
                <h2>Create merchant</h2>
                <p>Use a short slug such as <strong>demo-renovation</strong>. The WhatsApp phone receives follow-up links.</p>
                <label>Admin key<input id="adminKey" type="password" placeholder="X-Admin-Key"></label>
                <label>Slug<input id="profileSlug" value="demo-renovation"></label>
                <label>Business Name<input id="businessName" value="Demo Renovation"></label>
                <label>Type<input id="businessType" value="renovation"></label>
                <label>Contact Email<input id="businessEmail" value="merchant@example.com"></label>
                <label>WhatsApp Phone<input id="businessWhatsapp" value="6591234567"></label>
                <label>Offer Summary<input id="offerSummary" value="renovation quotation and consultation"></label>
                <label><input id="rotateAccessKey" type="checkbox"> Generate a new business access key</label>
                <div class="actions">
                    <button class="btn" onclick="saveProfile()">Save Profile</button>
                    <button class="btn secondary" onclick="loadProfiles()">Load Profiles</button>
                </div>
                <div class="status" id="profileStatus">Create a business profile to get dedicated links.</div>
            </div>
            <div>
                <div class="section-head">
                    <div>
                        <h2>Merchant links</h2>
                        <p>Share only the Form and Inbox links with merchants.</p>
                    </div>
                </div>
                <table>
                    <thead><tr><th>Business</th><th>Slug</th><th>Type</th><th>Status</th><th>Readiness</th><th>Key Prefix</th><th>Links</th></tr></thead>
                    <tbody id="profileRows"></tbody>
                </table>
            </div>
        </section>
        <div class="section-head">
            <div>
                <h2>Trial requests</h2>
                <p>Follow up with merchants who requested the 30-day trial from the product page.</p>
            </div>
            <button class="btn" onclick="loadTrialRequests()">Load Trial Requests</button>
        </div>
        <div class="status" id="trialRequestStatus">Enter admin key above to load trial requests.</div>
        <section class="grid" id="trialRequestStats"></section>
        <section class="trial-request-list" id="trialRequestRows"></section>
        <div class="section-head">
            <div>
                <h2>All enquiries</h2>
                <p>Owner view across every merchant profile.</p>
            </div>
            <button class="btn" onclick="loadInbox()">Refresh</button>
        </div>
        <div class="status" id="inboxStatus">Enter admin key above to load enquiries.</div>
        <section class="grid" id="inboxStats"></section>
        <table>
            <thead>
                <tr><th>Time</th><th>Merchant</th><th>Lead</th><th>Source</th><th>Intent</th><th>Follow-up focus</th><th>Message</th><th>Draft</th><th>Status</th><th>Action</th></tr>
            </thead>
            <tbody id="enquiryRows"></tbody>
        </table>
        <script>
            function escapeHtml(value) {
                return String(value ?? "").replace(/[&<>"']/g, char => ({
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }[char]));
            }
            function enquiryFocusLabel(value) {
                return ({ hot: "Answer now", warm: "Needs details", normal: "Ask next question" })[value] || value || "unknown";
            }
            async function adminApi(path, options = {}) {
                const adminKey = document.getElementById("adminKey").value;
                const headers = { "X-Admin-Key": adminKey, ...(options.headers || {}) };
                const response = await fetch(path, { ...options, headers });
                if (!response.ok) {
                    throw new Error(await response.text());
                }
                return response.json();
            }
            function absoluteUrl(path) {
                try {
                    return new URL(path, window.location.origin).toString();
                } catch (error) {
                    return path;
                }
            }
            async function copyAdminText(value, label, statusId) {
                const target = document.getElementById(statusId);
                try {
                    await navigator.clipboard.writeText(value || "");
                    if (target) target.textContent = `${label} copied.`;
                } catch (error) {
                    if (target) target.textContent = `Copy failed. Select and copy manually: ${value || ""}`;
                }
            }
            async function copyAdminElement(elementId, label, statusId) {
                const element = document.getElementById(elementId);
                const value = element?.dataset.copy || element?.textContent?.trim() || "";
                await copyAdminText(value, label, statusId);
            }
            async function saveProfile() {
                const status = document.getElementById("profileStatus");
                status.textContent = "Saving profile...";
                try {
                    const profile = await adminApi("/apps/enquiry/api/business-profiles", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            slug: document.getElementById("profileSlug").value,
                            business_name: document.getElementById("businessName").value,
                            business_type: document.getElementById("businessType").value,
                            contact_email: document.getElementById("businessEmail").value,
                            whatsapp_phone: document.getElementById("businessWhatsapp").value,
                            offer_summary: document.getElementById("offerSummary").value,
                            reply_tone: "friendly and professional",
                            opening_hours: "Mon-Sat, 9am-6pm",
                            status: "active",
                            rotate_access_key: document.getElementById("rotateAccessKey").checked
                        })
                    });
                    const keyMessage = profile.business_access_key
                        ? `<br>Business access key: <strong>${escapeHtml(profile.business_access_key)}</strong><br>Store it now. It will not be shown again.`
                        : "";
                    status.innerHTML = `Saved ${escapeHtml(profile.business_name)}. Public form: <a href="${escapeHtml(profile.form_url)}" target="_blank">${escapeHtml(profile.form_url)}</a>. Inbox: <a href="${escapeHtml(profile.inbox_url)}" target="_blank">${escapeHtml(profile.inbox_url)}</a>. Embed: &lt;script src="${escapeHtml(profile.embed_url)}"&gt;&lt;/script&gt;${keyMessage}`;
                    document.getElementById("rotateAccessKey").checked = false;
                    await loadProfiles();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function sendOnboarding(slug) {
                const status = document.getElementById("profileStatus");
                status.textContent = "Sending onboarding email...";
                try {
                    const result = await adminApi(`/apps/enquiry/api/business-profiles/${slug}/send-onboarding`, {
                        method: "POST"
                    });
                    status.textContent = `Onboarding email ${result.delivery.status}. A new business access key was generated for ${result.profile.business_name}.`;
                    await loadProfiles();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function loadProfiles() {
                const status = document.getElementById("profileStatus");
                try {
                    const data = await adminApi("/apps/enquiry/api/business-profiles");
                    document.getElementById("profileRows").innerHTML = data.profiles.map(profile => `
                        <tr>
                            <td>${escapeHtml(profile.business_name)}</td>
                            <td>${escapeHtml(profile.slug)}</td>
                            <td>${escapeHtml(profile.business_type)}</td>
                            <td>${escapeHtml(profile.status)}</td>
                            <td>
                                <span class="lead-badge ${profile.onboarding?.status === "live" ? "hot" : ""}">${escapeHtml(profile.onboarding?.status || "unknown")}</span>
                                <br><small>${escapeHtml(profile.onboarding?.percent ?? 0)}% · ${escapeHtml(profile.onboarding?.next_action || "")}</small>
                            </td>
                            <td>${escapeHtml(profile.access_key_prefix || "not set")}</td>
                            <td>
                                <a class="btn secondary" href="${escapeHtml(profile.form_url)}" target="_blank">Form</a>
                                <a class="btn secondary" href="${escapeHtml(profile.inbox_url)}" target="_blank">Inbox</a>
                                <button class="btn secondary" onclick="sendOnboarding('${escapeHtml(profile.slug)}')">Send Email</button>
                                <br><code>&lt;script src="${escapeHtml(profile.embed_url)}"&gt;&lt;/script&gt;</code>
                            </td>
                        </tr>
                    `).join("");
                    status.textContent = `Loaded ${data.profiles.length} profiles.`;
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function setStatus(id, status) {
                try {
                    await adminApi(`/apps/enquiry/api/enquiries/${id}`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ status })
                    });
                    await loadInbox();
                } catch (error) {
                    document.getElementById("inboxStatus").textContent = error.message;
                }
            }
            async function setTrialStatus(id, nextStatus) {
                const status = document.getElementById("trialRequestStatus");
                try {
                    await adminApi(`/apps/enquiry/api/trial-requests/${id}`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ status: nextStatus })
                    });
                    await loadTrialRequests();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function createInboxFromTrial(id) {
                const status = document.getElementById("trialRequestStatus");
                status.textContent = "Creating merchant inbox from trial request...";
                try {
                    const result = await adminApi(`/apps/enquiry/api/trial-requests/${id}/create-profile`, {
                        method: "POST"
                    });
                    const profile = result.profile;
                    const packageId = `trialSetup-${String(profile.slug || id).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
                    const formUrl = absoluteUrl(profile.form_url);
                    const inboxUrl = absoluteUrl(profile.inbox_url);
                    const accessKey = profile.business_access_key || "not rotated";
                    const onboardingMessage = result.onboarding_message || "";
                    status.innerHTML = `
                        <section class="setup-package">
                            <div class="setup-package-head">
                                <div>
                                    <h3>Setup package ready: ${escapeHtml(profile.business_name)}</h3>
                                    <p>Send this to the merchant, then help them submit one test enquiry before promotion.</p>
                                </div>
                                <div class="lead-badges">
                                    <span class="lead-badge hot">Private owner key generated</span>
                                    <span class="lead-badge">Trial setup</span>
                                </div>
                            </div>
                            <div class="setup-package-grid">
                                <div class="setup-package-box">
                                    <span>Customer enquiry link</span>
                                    <code id="${packageId}-form" data-copy="${escapeHtml(formUrl)}">${escapeHtml(formUrl)}</code>
                                    <button class="btn secondary" onclick="copyAdminElement('${packageId}-form', 'Customer enquiry link', '${packageId}-copy-status')">Copy Form Link</button>
                                </div>
                                <div class="setup-package-box">
                                    <span>Merchant private inbox</span>
                                    <code id="${packageId}-inbox" data-copy="${escapeHtml(inboxUrl)}">${escapeHtml(inboxUrl)}</code>
                                    <button class="btn secondary" onclick="copyAdminElement('${packageId}-inbox', 'Merchant inbox link', '${packageId}-copy-status')">Copy Inbox Link</button>
                                </div>
                                <div class="setup-package-box">
                                    <span>Owner inbox password</span>
                                    <code id="${packageId}-key" data-copy="${escapeHtml(accessKey)}">${escapeHtml(accessKey)}</code>
                                    <button class="btn secondary" onclick="copyAdminElement('${packageId}-key', 'Owner inbox password', '${packageId}-copy-status')">Copy Password</button>
                                </div>
                            </div>
                            <div class="setup-package-message">
                                <strong>WhatsApp setup message</strong>
                                <pre id="${packageId}-message" data-copy="${escapeHtml(onboardingMessage)}">${escapeHtml(onboardingMessage)}</pre>
                                <div class="actions">
                                    <button class="btn secondary" onclick="copyAdminElement('${packageId}-message', 'Setup message', '${packageId}-copy-status')">Copy Message</button>
                                    ${result.onboarding_whatsapp_url ? `<a class="btn" target="_blank" href="${escapeHtml(result.onboarding_whatsapp_url)}">Send Setup WhatsApp</a>` : ""}
                                    ${result.conversion_whatsapp_url ? `<a class="btn secondary" target="_blank" href="${escapeHtml(result.conversion_whatsapp_url)}">Prepare Upgrade WhatsApp</a>` : ""}
                                </div>
                            </div>
                            <div class="setup-checklist">
                                <div class="setup-check"><strong>1. Send setup</strong>Send WhatsApp message with the form link, inbox link, and owner password.</div>
                                <div class="setup-check"><strong>2. Submit test enquiry</strong>Open the customer form and submit one sample lead for the merchant.</div>
                                <div class="setup-check"><strong>3. Open inbox</strong>Use the owner password and confirm the lead appears in the private inbox.</div>
                                <div class="setup-check"><strong>4. Share link</strong>Ask merchant to place the enquiry link on WhatsApp, Facebook, Instagram, or Google profile.</div>
                            </div>
                            <div class="setup-copy-status" id="${packageId}-copy-status">Keep the owner password private. Share it only with the merchant owner.</div>
                        </section>
                    `;
                    await loadTrialRequests();
                    await loadProfiles();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            function shortDate(value) {
                if (!value) return "Not started";
                return String(value).slice(0, 10);
            }
            function statusLabel(value) {
                return ({
                    new: "New request",
                    contacted: "Contacted",
                    trial_setup: "Trial setup",
                    won: "Converted",
                    lost: "Not proceeding",
                    spam: "Spam"
                })[value] || value;
            }
            function priorityClass(value) {
                return value === "high" ? "hot" : "";
            }
            function trialEndLabel(item) {
                if (!item.trial_ends_at) return "Not started";
                return `${escapeHtml(item.days_until_trial_end)} day(s) left - ${escapeHtml(shortDate(item.trial_ends_at))}`;
            }
            async function loadTrialRequests() {
                const status = document.getElementById("trialRequestStatus");
                status.textContent = "Loading trial requests...";
                try {
                    const data = await adminApi("/apps/enquiry/api/trial-requests?limit=100");
                    const stats = data.stats || {};
                    document.getElementById("trialRequestStats").innerHTML = `
                        <section class="card"><h3>Total Trial Requests</h3><div class="price">${stats.total || 0}</div></section>
                        <section class="card"><h3>Urgent Follow-up</h3><div class="price">${stats.urgent || 0}</div></section>
                        <section class="card"><h3>Ending Soon</h3><div class="price">${stats.ending_soon || 0}</div></section>
                        <section class="card"><h3>Conversion Due</h3><div class="price">${stats.conversion_due || 0}</div></section>
                        <section class="card"><h3>Won</h3><div class="price">${stats.won || 0}</div></section>
                    `;
                    document.getElementById("trialRequestRows").innerHTML = data.trial_requests.map(item => `
                        <section class="trial-request-card">
                            <div>
                                <div class="trial-request-head">
                                    <div>
                                        <div class="eyebrow">Trial request #${escapeHtml(item.id)} - ${escapeHtml(shortDate(item.created_at))}</div>
                                        <h3 class="trial-request-title">${escapeHtml(item.business_name)}</h3>
                                        <div class="trial-request-subtitle">
                                            ${escapeHtml(item.city || "No area")} - ${escapeHtml(item.business_type || "Service business")} - ${escapeHtml(item.monthly_enquiries || "Unknown volume")} enquiries
                                        </div>
                                    </div>
                                    <div class="lead-badges">
                                        <span class="lead-badge">${escapeHtml(statusLabel(item.status))}</span>
                                        <span class="lead-badge ${priorityClass(item.follow_up_priority)}">${escapeHtml(item.follow_up_priority)} follow-up</span>
                                    </div>
                                </div>
                                <div class="trial-contact">
                                    <div class="trial-field">
                                        <span>Contact</span>
                                        <strong>${escapeHtml(item.contact_name)}</strong><br>
                                        <code>${escapeHtml(item.whatsapp_phone)}</code>
                                    </div>
                                    <div class="trial-field">
                                        <span>Email</span>
                                        <code>${escapeHtml(item.contact_email || "Not provided")}</code>
                                    </div>
                                    <div class="trial-field">
                                        <span>Source</span>
                                        <strong>${escapeHtml(item.lead_source || "direct")}</strong>
                                        ${item.campaign ? `<br><code>${escapeHtml(item.campaign)}</code>` : ""}
                                    </div>
                                </div>
                                <div class="trial-message">
                                    <span>Merchant pain point / message</span>
                                    <p>${escapeHtml(item.message || "No message provided.")}</p>
                                </div>
                                <div class="trial-meta">
                                    <div class="trial-field">
                                        <span>Age</span>
                                        <strong>${escapeHtml(item.age_days)} day(s)</strong>
                                    </div>
                                    <div class="trial-field">
                                        <span>Trial End</span>
                                        <strong>${trialEndLabel(item)}</strong>
                                    </div>
                                    <div class="trial-field">
                                        <span>Stage</span>
                                        <strong>${escapeHtml(item.conversion_stage || "manual_review")}</strong>
                                    </div>
                                    <div class="trial-field">
                                        <span>Referrer</span>
                                        <code>${escapeHtml(item.referrer ? item.referrer.slice(0, 80) : "None")}</code>
                                    </div>
                                </div>
                            </div>
                            <aside class="trial-followup">
                                <div>
                                    <h3>Next action</h3>
                                    <p>${escapeHtml(item.next_action || "Review manually.")}</p>
                                    <span class="next-action">${escapeHtml(item.conversion_next_action || "")}</span>
                                </div>
                                <div class="trial-actions">
                                ${item.whatsapp_url ? `<a class="btn secondary" target="_blank" href="${escapeHtml(item.whatsapp_url)}">WhatsApp</a>` : ""}
                                ${item.conversion_whatsapp_url ? `<a class="btn secondary" target="_blank" href="${escapeHtml(item.conversion_whatsapp_url)}">Upgrade WhatsApp</a>` : ""}
                                <button class="btn secondary" onclick="createInboxFromTrial(${item.id})">${item.status === "trial_setup" ? "Refresh Inbox" : "Create Inbox"}</button>
                                <button class="btn secondary" onclick="setTrialStatus(${item.id}, 'contacted')">Contacted</button>
                                <button class="btn secondary" onclick="setTrialStatus(${item.id}, 'trial_setup')">Trial Setup</button>
                                <button class="btn secondary" onclick="setTrialStatus(${item.id}, 'won')">Converted</button>
                                <button class="btn secondary" onclick="setTrialStatus(${item.id}, 'lost')">Not Proceeding</button>
                                </div>
                            </aside>
                        </section>
                    `).join("");
                    status.textContent = `Loaded ${data.trial_requests.length} trial requests.`;
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function loadInbox() {
                const status = document.getElementById("inboxStatus");
                status.textContent = "Loading enquiries...";
                try {
                    const data = await adminApi("/apps/enquiry/api/enquiries?limit=50");
                    const stats = data.stats || {};
                    document.getElementById("inboxStats").innerHTML = `
                        <section class="card"><h3>Total</h3><div class="price">${stats.total || 0}</div></section>
                        <section class="card"><h3>Need answer now</h3><div class="price">${(stats.by_priority || {}).hot || 0}</div></section>
                        <section class="card"><h3>New</h3><div class="price">${(stats.by_status || {}).new || 0}</div></section>
                        <section class="card"><h3>Top Source</h3><div class="price">${escapeHtml(Object.entries(stats.by_source || {}).sort((a, b) => b[1] - a[1])[0]?.[0] || "none")}</div></section>
                    `;
                    document.getElementById("enquiryRows").innerHTML = data.enquiries.map(item => `
                        <tr>
                            <td>${escapeHtml(item.created_at)}</td>
                            <td>${escapeHtml(item.business_slug || "")}</td>
                            <td>${escapeHtml(item.name)}<br>${escapeHtml(item.phone)}<br>${escapeHtml(item.email || "")}</td>
                            <td>${escapeHtml(item.source || "unknown")}${item.campaign ? `<br>${escapeHtml(item.campaign)}` : ""}${item.referrer ? `<br><small>${escapeHtml(item.referrer.slice(0, 80))}</small>` : ""}</td>
                            <td>${escapeHtml(item.intent)}</td>
                            <td>${escapeHtml(enquiryFocusLabel(item.priority))}</td>
                            <td>${escapeHtml(item.message)}</td>
                            <td>${escapeHtml(item.reply_draft)}</td>
                            <td>${escapeHtml(item.status)}</td>
                            <td>
                                ${item.whatsapp_url ? `<a class="btn secondary" target="_blank" href="${escapeHtml(item.whatsapp_url)}">WhatsApp</a>` : ""}
                                <button class="btn secondary" onclick="setStatus(${item.id}, 'contacted')">Contacted</button>
                                <button class="btn secondary" onclick="setStatus(${item.id}, 'won')">Won</button>
                                <button class="btn secondary" onclick="setStatus(${item.id}, 'lost')">Lost</button>
                            </td>
                        </tr>
                    `).join("");
                    status.textContent = "Loaded.";
                } catch (error) {
                    status.textContent = error.message;
                }
            }
        </script>
        """
    )


@app.get("/portal", response_class=HTMLResponse)
def customer_portal():
    return base_html(
        "NexaFlow Customer Portal",
        """
        <h1>Customer Portal</h1>
        <p>Check account status, remaining credits, and recent gateway usage with your NexaFlow API key.</p>
        <div class="toolbar">
            <label>API key
                <input id="apiKey" type="password" autocomplete="off" placeholder="nf_..." />
            </label>
            <button class="btn" onclick="loadPortal()">Refresh</button>
        </div>
        <div class="status" id="portalStatus">Enter your API key to load your account.</div>
        <section class="grid" id="summary"></section>
        <h2>Billing</h2>
        <div class="toolbar">
            <button class="btn" onclick="openBillingPortal()">Manage Billing</button>
            <button class="btn secondary" onclick="loadPortal()">Refresh Billing</button>
        </div>
        <div class="status" id="billingStatus">Use Manage Billing to update payment method, view invoices, or manage subscription in Stripe.</div>
        <section class="grid" id="billingLinks"></section>
        <h2>Enquiry Inbox</h2>
        <p>Create your customer enquiry form and merchant inbox without waiting for manual setup.</p>
        <div class="toolbar">
            <label>Business slug<input id="customerBusinessSlug" value="my-business"></label>
            <label>Business name<input id="customerBusinessName" value="My Business"></label>
        </div>
        <div class="toolbar">
            <label>Business type<input id="customerBusinessType" value="retail"></label>
            <label>WhatsApp phone<input id="customerBusinessWhatsapp" value="6591234567"></label>
        </div>
        <label>Contact email<input id="customerBusinessEmail" placeholder="owner@example.com"></label>
        <label>Offer summary<input id="customerOfferSummary" value="customer enquiries and quotation requests"></label>
        <div class="actions">
            <button class="btn" onclick="saveCustomerBusinessProfile()">Create / Update Enquiry Inbox</button>
            <button class="btn secondary" onclick="sendCustomerBusinessOnboarding()">Email Setup Links</button>
        </div>
        <div class="status" id="enquirySetupStatus">Create your business profile to get a public form and private inbox.</div>
        <section class="grid" id="customerBusinessProfiles"></section>
        <h2>Test Request</h2>
        <div class="toolbar">
            <label>Message
                <input id="testMessage" autocomplete="off" value="Write a one-sentence NexaFlow onboarding message." />
            </label>
            <button class="btn" onclick="sendTestRequest()">Send Test</button>
        </div>
        <div class="status" id="testStatus">A test request spends credits and appears in usage history.</div>
        <h2>Security</h2>
        <div class="toolbar">
            <button class="btn secondary" onclick="rotateCustomerKey()">Rotate API Key</button>
            <button class="btn secondary" onclick="forgetApiKey()">Forget Key</button>
        </div>
        <div class="status" id="securityStatus">Rotate your key if it may have been exposed. The old key stops working immediately.</div>
        <h2>Recent Usage</h2>
        <table>
            <thead>
                <tr><th>Time</th><th>Task</th><th>Provider</th><th>Model</th><th>Credits</th><th>Preview</th></tr>
            </thead>
            <tbody id="usageRows"></tbody>
        </table>
        <script>
            const savedKey = localStorage.getItem("nexaflow_api_key");
            if (savedKey) {
                document.getElementById("apiKey").value = savedKey;
            }
            function escapeHtml(value) {
                return String(value ?? "").replace(/[&<>"']/g, char => ({
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }[char]));
            }
            async function customerApi(path, apiKey) {
                const response = await fetch(path, { headers: { "X-API-Key": apiKey } });
                if (!response.ok) {
                    throw new Error(await response.text());
                }
                return response.json();
            }
            async function customerRequest(path, apiKey, options = {}) {
                const response = await fetch(path, {
                    ...options,
                    headers: { "X-API-Key": apiKey, ...(options.headers || {}) }
                });
                if (!response.ok) {
                    throw new Error(await response.text());
                }
                return response.json();
            }
            async function openBillingPortal() {
                const apiKey = document.getElementById("apiKey").value.trim();
                const status = document.getElementById("billingStatus");
                if (!apiKey) {
                    status.textContent = "Enter your API key first.";
                    return;
                }
                status.textContent = "Creating Stripe billing portal session...";
                try {
                    const response = await fetch("/customer/billing-portal", {
                        method: "POST",
                        headers: { "X-API-Key": apiKey }
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    status.textContent = "Opening Stripe billing portal...";
                    window.location.href = result.url;
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            function renderBillingLinks(account, billing) {
                const currentPlan = account?.plan || "";
                const links = Object.values(billing.links || {});
                document.getElementById("billingLinks").innerHTML = links.map(item => {
                    const disabled = !item.payment_link;
                    const current = item.plan === currentPlan ? "Current plan" : "Buy / upgrade";
                    const href = item.checkout_url || `/billing/checkout?plan=${encodeURIComponent(item.plan)}`;
                    return `
                        <section class="card">
                            <h3>${escapeHtml(item.name)}</h3>
                            <div class="price">$${escapeHtml(item.monthly_price_usd)}</div>
                            <p>${escapeHtml(new Intl.NumberFormat().format(item.included_credits))} credits / month</p>
                            <a class="btn ${disabled ? "secondary" : ""}" href="${escapeHtml(href)}">${disabled ? "Unavailable" : current}</a>
                        </section>
                    `;
                }).join("");
            }
            function renderCustomerBusinessProfiles(profiles) {
                document.getElementById("customerBusinessProfiles").innerHTML = profiles.map(profile => `
                    <section class="card">
                        <h3>${escapeHtml(profile.business_name)}</h3>
                        <p>${escapeHtml(profile.slug)} · ${escapeHtml(profile.status)}</p>
                        <p>Key prefix: ${escapeHtml(profile.access_key_prefix || "not set")}</p>
                        <a class="btn secondary" href="${escapeHtml(profile.form_url)}" target="_blank">Form</a>
                        <a class="btn secondary" href="${escapeHtml(profile.inbox_url)}" target="_blank">Inbox</a>
                        <p>Embed code:</p>
                        <code>&lt;script src="${escapeHtml(profile.embed_url)}"&gt;&lt;/script&gt;</code>
                    </section>
                `).join("");
            }
            async function loadCustomerBusinessProfiles(apiKey) {
                const result = await customerApi("/customer/enquiry/business-profiles", apiKey);
                renderCustomerBusinessProfiles(result.profiles || []);
                return result;
            }
            async function saveCustomerBusinessProfile() {
                const apiKey = document.getElementById("apiKey").value.trim();
                const status = document.getElementById("enquirySetupStatus");
                if (!apiKey) {
                    status.textContent = "Enter your API key first.";
                    return;
                }
                status.textContent = "Creating enquiry inbox...";
                try {
                    const profile = await customerRequest("/customer/enquiry/business-profiles", apiKey, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            slug: document.getElementById("customerBusinessSlug").value,
                            business_name: document.getElementById("customerBusinessName").value,
                            business_type: document.getElementById("customerBusinessType").value,
                            whatsapp_phone: document.getElementById("customerBusinessWhatsapp").value,
                            contact_email: document.getElementById("customerBusinessEmail").value,
                            offer_summary: document.getElementById("customerOfferSummary").value,
                            reply_tone: "friendly and professional",
                            opening_hours: "",
                            status: "active"
                        })
                    });
                    status.innerHTML = `Created ${escapeHtml(profile.business_name)}.<br>Form: <a href="${escapeHtml(profile.form_url)}" target="_blank">${escapeHtml(profile.form_url)}</a><br>Inbox: <a href="${escapeHtml(profile.inbox_url)}" target="_blank">${escapeHtml(profile.inbox_url)}</a><br>Embed: &lt;script src="${escapeHtml(profile.embed_url)}"&gt;&lt;/script&gt;`;
                    await loadCustomerBusinessProfiles(apiKey);
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function sendCustomerBusinessOnboarding() {
                const apiKey = document.getElementById("apiKey").value.trim();
                const slug = document.getElementById("customerBusinessSlug").value.trim();
                const status = document.getElementById("enquirySetupStatus");
                if (!apiKey || !slug) {
                    status.textContent = "Enter your API key and business slug first.";
                    return;
                }
                status.textContent = "Sending setup email...";
                try {
                    const result = await customerRequest(`/customer/enquiry/business-profiles/${encodeURIComponent(slug)}/send-onboarding`, apiKey, {
                        method: "POST"
                    });
                    status.textContent = `Setup email ${result.delivery.status}. A fresh business access key was generated.`;
                    await loadCustomerBusinessProfiles(apiKey);
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function sendTestRequest() {
                const apiKey = document.getElementById("apiKey").value.trim();
                const message = document.getElementById("testMessage").value.trim();
                const status = document.getElementById("testStatus");
                if (!apiKey || !message) {
                    status.textContent = "Enter your API key and a message first.";
                    return;
                }
                status.textContent = "Sending test request...";
                try {
                    const response = await fetch("/v1/chat", {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "X-API-Key": apiKey
                        },
                        body: JSON.stringify({
                            message,
                            task: "chat",
                            routing_strategy: "profit"
                        })
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    status.textContent = `Reply: ${result.reply}\nCredits spent: ${result.credits_spent}\nRemaining credits: ${result.remaining_credits}`;
                    await loadPortal();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function rotateCustomerKey() {
                const apiKeyInput = document.getElementById("apiKey");
                const apiKey = apiKeyInput.value.trim();
                const status = document.getElementById("securityStatus");
                if (!apiKey) {
                    status.textContent = "Enter your current API key first.";
                    return;
                }
                if (!window.confirm("Rotate this API key? The current key will stop working immediately.")) {
                    return;
                }
                status.textContent = "Rotating API key...";
                try {
                    const response = await fetch("/customer/rotate-api-key", {
                        method: "POST",
                        headers: { "X-API-Key": apiKey }
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    apiKeyInput.value = result.api_key;
                    localStorage.setItem("nexaflow_api_key", result.api_key);
                    status.textContent = `New API key generated. Store it now:\n${result.api_key}\nPrefix: ${result.api_key_prefix}`;
                    await loadPortal();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            function forgetApiKey() {
                localStorage.removeItem("nexaflow_api_key");
                document.getElementById("apiKey").value = "";
                document.getElementById("summary").innerHTML = "";
                document.getElementById("billingLinks").innerHTML = "";
                document.getElementById("usageRows").innerHTML = "";
                document.getElementById("securityStatus").textContent = "API key removed from this browser.";
            }
            async function loadPortal() {
                const apiKey = document.getElementById("apiKey").value.trim();
                const status = document.getElementById("portalStatus");
                if (!apiKey) {
                    status.textContent = "Please enter your NexaFlow API key.";
                    return;
                }
                localStorage.setItem("nexaflow_api_key", apiKey);
                status.textContent = "Loading account...";
                try {
                    const [account, usage] = await Promise.all([
                        customerApi("/customer/me", apiKey),
                        customerApi("/customer/usage?limit=25", apiKey)
                    ]);
                    const billing = await customerApi("/customer/billing-links", apiKey);
                    const fmt = new Intl.NumberFormat();
                    document.getElementById("billingStatus").textContent = billing.billing_portal.available
                        ? "Billing portal is available for this account."
                        : (billing.billing_portal.reason || "Billing portal is not available for this account yet.");
                    document.getElementById("summary").innerHTML = `
                        <section class="card"><h3>Account</h3><p>${escapeHtml(account.client_id)}</p><p>${escapeHtml(account.billing_email || "")}</p></section>
                        <section class="card"><h3>Status</h3><div class="price">${escapeHtml(account.status)}</div><p>${escapeHtml(account.subscription_status || "subscription")}</p></section>
                        <section class="card"><h3>Credits</h3><div class="price">${fmt.format(account.credits)}</div><p>${fmt.format(usage.totals.credits_spent)} used</p></section>
                    `;
                    document.getElementById("usageRows").innerHTML = usage.recent_logs.map(log => `
                        <tr>
                            <td>${escapeHtml(log.timestamp || "")}</td>
                            <td>${escapeHtml(log.task || "")}</td>
                            <td>${escapeHtml(log.provider || "")}</td>
                            <td>${escapeHtml(log.model_key || log.model || "")}</td>
                            <td>${fmt.format(log.credits_spent || 0)}</td>
                            <td>${escapeHtml(log.message_preview || "")}</td>
                        </tr>
                    `).join("");
                    renderBillingLinks(account, billing);
                    await loadCustomerBusinessProfiles(apiKey);
                    status.textContent = `Loaded ${fmt.format(usage.totals.requests)} requests.`;
                } catch (error) {
                    status.textContent = error.message;
                    document.getElementById("summary").innerHTML = "";
                    document.getElementById("billingLinks").innerHTML = "";
                    document.getElementById("customerBusinessProfiles").innerHTML = "";
                    document.getElementById("usageRows").innerHTML = "";
                }
            }
        </script>
        """
    )


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard():
    return base_html(
        "NexaFlow Admin",
        """
        <h1>Admin</h1>
        <div class="toolbar">
            <label>Admin key
                <input id="adminKey" type="password" placeholder="X-Admin-Key">
            </label>
            <button class="btn" onclick="loadAdmin()">Refresh</button>
        </div>
        <section class="grid">
            <div class="card"><h3>Total Requests</h3><div class="price" id="totalRequests">-</div></div>
            <div class="card"><h3>Revenue</h3><div class="price" id="revenue">-</div></div>
            <div class="card"><h3>Gross Margin</h3><div class="price" id="margin">-</div></div>
            <div class="card"><h3>MRR Run Rate</h3><div class="price" id="mrr">-</div></div>
            <div class="card"><h3>Active Customers</h3><div class="price" id="activeCustomers">-</div></div>
            <div class="card"><h3>Payments MTD</h3><div class="price" id="paymentsMtd">-</div></div>
        </section>
        <h2>Revenue Report</h2>
        <div class="status" id="revenueReport">Revenue report will appear here.</div>
        <h2>Action Items</h2>
        <div class="status" id="actionItemSummary">Action items will appear here.</div>
        <table>
            <thead>
                <tr><th>Severity</th><th>Category</th><th>Client</th><th>Issue</th><th>Action</th></tr>
            </thead>
            <tbody id="actionItems"></tbody>
        </table>
        <h2>Merchant Health</h2>
        <div class="status" id="merchantHealthSummary">Merchant health will appear here.</div>
        <table>
            <thead>
                <tr><th>Risk</th><th>Merchant</th><th>Readiness</th><th>Leads</th><th>Follow-ups</th><th>Next action</th></tr>
            </thead>
            <tbody id="merchantHealthRows"></tbody>
        </table>
        <h2>Clients</h2>
        <div class="status" id="status">Enter the admin key and refresh.</div>
        <table>
            <thead>
                <tr><th>Client</th><th>Plan</th><th>Credits</th><th>Status</th><th>Email</th><th>Key</th><th>Action</th></tr>
            </thead>
            <tbody id="clients"></tbody>
        </table>
        <h2>Payment Events</h2>
        <table>
            <thead>
                <tr><th>Time</th><th>Event</th><th>Email</th><th>Client</th><th>Plan</th><th>Delivery</th></tr>
            </thead>
            <tbody id="paymentEvents"></tbody>
        </table>
        <h2>Customer Notifications</h2>
        <table>
            <thead>
                <tr><th>Time</th><th>Client</th><th>Type</th><th>Status</th><th>Provider</th></tr>
            </thead>
            <tbody id="notifications"></tbody>
        </table>
        <h2>Margin Guard</h2>
        <div class="status" id="readiness">Readiness checks will appear here.</div>
        <h2>Backend Automation</h2>
        <div class="status" id="automationStatus">Run a dry preview before enabling scheduled automation.</div>
        <div class="toolbar">
            <button class="btn" onclick="runAutomation(true, false)">Dry Run Preview</button>
            <button class="btn secondary" onclick="runAutomation(true, true)">Dry Run + Backup Check</button>
            <button class="btn secondary" onclick="runAutomation(false, true)">Run + Create Backup</button>
        </div>
        <h2>Data Protection</h2>
        <div class="status" id="retentionStatus">Run a retention dry-run before deleting old enquiry data.</div>
        <div class="toolbar">
            <label>Business slug
                <input id="retentionBusinessSlug" placeholder="optional">
            </label>
            <button class="btn secondary" onclick="runRetentionCleanup(true)">Retention Dry Run</button>
            <button class="btn secondary" onclick="runRetentionCleanup(false)">Delete Expired Data</button>
            <button class="btn secondary" onclick="loadAuditEvents()">Refresh Audit Log</button>
        </div>
        <table>
            <thead>
                <tr><th>Time</th><th>Event</th><th>Actor</th><th>Business</th><th>Entity</th><th>Metadata</th></tr>
            </thead>
            <tbody id="auditEvents"></tbody>
        </table>
        <h2>Backups</h2>
        <div class="status" id="backupScheduler">Backup scheduler status will appear here.</div>
        <div class="toolbar">
            <button class="btn" onclick="createBackup()">Create Backup</button>
            <button class="btn secondary" onclick="testOffsiteBackup()">Test Offsite</button>
            <button class="btn secondary" onclick="loadAdmin()">Refresh</button>
        </div>
        <table>
            <thead>
                <tr><th>Backup</th><th>Created</th><th>Size</th><th>Offsite</th><th>Action</th></tr>
            </thead>
            <tbody id="backups"></tbody>
        </table>
        <script>
            const fmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });
            function money(value) {
                return "$" + fmt.format(value || 0);
            }
            async function api(path, adminKey) {
                const response = await fetch(path, { headers: { "X-Admin-Key": adminKey } });
                if (!response.ok) {
                    throw new Error(await response.text());
                }
                return response.json();
            }
            function escapeHtml(value) {
                return String(value ?? "").replace(/[&<>"']/g, char => ({
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#39;"
                })[char]);
            }
            async function resendApiKey(clientId) {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("status");
                status.textContent = "Sending new API key...";
                try {
                    const response = await fetch(`/admin/clients/${encodeURIComponent(clientId)}/send-api-key`, {
                        method: "POST",
                        headers: {
                            "X-Admin-Key": adminKey,
                            "Content-Type": "application/json"
                        },
                        body: JSON.stringify({ rotate: true })
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    status.textContent = `API key rotated for ${result.client_id}. Delivery: ${result.delivery?.status || "unknown"}`;
                    await loadAdmin();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function createBackup() {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("status");
                status.textContent = "Creating backup...";
                try {
                    const response = await fetch("/admin/backups", {
                        method: "POST",
                        headers: { "X-Admin-Key": adminKey }
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    status.textContent = `Backup created: ${result.backup.name}`;
                    await loadAdmin();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function testOffsiteBackup() {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("status");
                status.textContent = "Testing offsite backup storage...";
                try {
                    const response = await fetch("/admin/backups/offsite-test", {
                        method: "POST",
                        headers: { "X-Admin-Key": adminKey }
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    status.textContent = `Offsite test: ${result.result.status}\\n${result.result.reason || result.result.key || ""}`;
                    await loadAdmin();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function downloadBackup(backupName) {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("status");
                status.textContent = "Downloading backup...";
                try {
                    const response = await fetch(`/admin/backups/${encodeURIComponent(backupName)}`, {
                        headers: { "X-Admin-Key": adminKey }
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const blob = await response.blob();
                    const url = URL.createObjectURL(blob);
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = backupName;
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                    URL.revokeObjectURL(url);
                    status.textContent = `Downloaded backup: ${backupName}`;
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            function renderAutomationResult(result) {
                const tasks = result.tasks || {};
                const deploy = tasks.deployment_checks || {};
                const trials = tasks.trial_requests || {};
                const followups = tasks.followup_digest || {};
                const retention = tasks.data_retention || {};
                const backup = tasks.backup || {};
                const merchantHealth = tasks.merchant_health || {};
                const merchantSummary = merchantHealth.summary || {};
                document.getElementById("automationStatus").textContent = [
                    `Ran at: ${result.ran_at || "unknown"}`,
                    `Dry run: ${result.dry_run}`,
                    `Deployment checks ok: ${deploy.ok} (${deploy.failed_count || 0} failed)`,
                    `Trial urgent items: ${trials.urgent_count || 0}`,
                    `Merchant health: ${merchantSummary.high || 0} high risk, ${merchantSummary.due_followups || 0} due follow-up(s)`,
                    `Follow-up digest: processed ${followups.processed || 0}, sent ${followups.sent || 0}, skipped ${followups.skipped || 0}`,
                    `Data retention: expired ${retention.expired || 0}, deleted ${retention.deleted || 0}`,
                    `Channel messages: expired ${retention.expired_channel_messages || 0}, deleted ${retention.deleted_channel_messages || 0}`,
                    `Backup: ${backup.status || "unknown"} - ${backup.reason || backup.backup?.name || ""}`,
                    `Next: ${result.next_step || ""}`
                ].join("\\n");
            }
            async function runAutomation(dryRun = true, includeBackup = false) {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("automationStatus");
                status.textContent = "Running backend automation...";
                try {
                    const response = await fetch(`/admin/automation/run?dry_run=${dryRun}&include_backup=${includeBackup}`, {
                        method: "POST",
                        headers: { "X-Admin-Key": adminKey }
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    renderAutomationResult(result);
                    if (!dryRun || includeBackup) {
                        await loadAdmin();
                        renderAutomationResult(result);
                    }
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            function renderAuditEvents(events) {
                document.getElementById("auditEvents").innerHTML = events.map(event => `
                    <tr>
                        <td>${escapeHtml(event.created_at || "")}</td>
                        <td>${escapeHtml(event.event_type || "")}</td>
                        <td>${escapeHtml(event.actor_type || "")}</td>
                        <td>${escapeHtml(event.business_slug || "")}</td>
                        <td>${escapeHtml(event.entity_type || "")}${event.entity_id ? `<br>${escapeHtml(event.entity_id)}` : ""}</td>
                        <td><small>${escapeHtml(JSON.stringify(event.metadata || {}))}</small></td>
                    </tr>
                `).join("");
            }
            async function loadAuditEvents() {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("retentionStatus");
                status.textContent = "Loading audit events...";
                try {
                    const businessSlug = document.getElementById("retentionBusinessSlug").value.trim();
                    const params = new URLSearchParams({ limit: "50" });
                    if (businessSlug) params.set("business_slug", businessSlug);
                    const data = await api(`/admin/data-audit-events?${params.toString()}`, adminKey);
                    renderAuditEvents(data.events || []);
                    status.textContent = `Loaded ${(data.events || []).length} audit event(s).`;
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function runRetentionCleanup(dryRun = true) {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("retentionStatus");
                status.textContent = dryRun ? "Previewing retention cleanup..." : "Deleting expired enquiry data...";
                try {
                    const businessSlug = document.getElementById("retentionBusinessSlug").value.trim();
                    const params = new URLSearchParams({ dry_run: String(dryRun), limit_per_business: "500" });
                    if (businessSlug) params.set("business_slug", businessSlug);
                    const response = await fetch(`/admin/data-retention/cleanup?${params.toString()}`, {
                        method: "POST",
                        headers: { "X-Admin-Key": adminKey }
                    });
                    if (!response.ok) {
                        throw new Error(await response.text());
                    }
                    const result = await response.json();
                    status.textContent = [
                        `Dry run: ${result.dry_run}`,
                        `Businesses processed: ${result.processed}`,
                        `Expired enquiries: ${result.expired}`,
                        `Deleted enquiries: ${result.deleted}`
                    ].join("\\n");
                    await loadAuditEvents();
                } catch (error) {
                    status.textContent = error.message;
                }
            }
            async function loadAdmin() {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("status");
                status.textContent = "Loading...";
                try {
                    const [clients, stats, report, actions, merchantHealth, events, notifications, checks, backups, audit] = await Promise.all([
                        api("/admin/clients", adminKey),
                        api("/admin/usage-stats", adminKey),
                        api("/admin/revenue-report", adminKey),
                        api("/admin/action-items", adminKey),
                        api("/admin/merchant-health", adminKey),
                        api("/admin/payment-events?limit=12", adminKey),
                        api("/admin/notifications?limit=12", adminKey),
                        api("/admin/deploy-check", adminKey),
                        api("/admin/backups", adminKey),
                        api("/admin/data-audit-events?limit=50", adminKey)
                    ]);
                    let totalRequests = stats.total_requests || 0;
                    let revenue = 0;
                    let margin = 0;
                    for (const value of Object.values(stats.client_usage || {})) {
                        revenue += value.revenue_usd || 0;
                        margin += value.gross_margin_usd || 0;
                    }
                    document.getElementById("totalRequests").textContent = totalRequests;
                    document.getElementById("revenue").textContent = money(revenue);
                    document.getElementById("margin").textContent = money(margin);
                    document.getElementById("mrr").textContent = money(report.mrr.active_monthly_recurring_revenue_usd);
                    document.getElementById("activeCustomers").textContent = report.mrr.active_customers;
                    document.getElementById("paymentsMtd").textContent = money(report.payments.month_to_date_volume_usd);
                    document.getElementById("revenueReport").textContent = [
                        `Generated: ${report.generated_at}`,
                        `MRR run rate: ${money(report.mrr.active_monthly_recurring_revenue_usd)}`,
                        `Payment volume MTD: ${money(report.payments.month_to_date_volume_usd)}`,
                        `Payment volume all time: ${money(report.payments.all_time_volume_usd)}`,
                        `Usage revenue MTD: ${money(report.usage.month_to_date.totals.revenue_usd)}`,
                        `Provider cost MTD: ${money(report.usage.month_to_date.totals.provider_cost_usd)}`,
                        `Gross margin MTD: ${money(report.usage.month_to_date.totals.gross_margin_usd)}`,
                        `Gross margin ratio all time: ${report.usage.all_time.totals.gross_margin_ratio ?? "n/a"}`,
                        `Credits outstanding: ${fmt.format(report.risk.credits_outstanding || 0)}`,
                        `Low credit customers: ${report.risk.low_credit_clients.length}`,
                        `High usage customers: ${report.risk.high_usage_clients.length}`,
                        `Negative margin customers: ${report.risk.negative_margin_clients.length}`,
                        `Paused/cancelled customers: ${report.risk.paused_or_cancelled_clients.length}`
                    ].join("\\n");
                    document.getElementById("actionItemSummary").textContent = [
                        `Total: ${actions.counts.total}`,
                        `Critical: ${actions.counts.critical}`,
                        `High: ${actions.counts.high}`,
                        `Medium: ${actions.counts.medium}`,
                        `Low: ${actions.counts.low}`
                    ].join("\\n");
                    document.getElementById("actionItems").innerHTML = actions.items.map(item => `
                        <tr>
                            <td>${escapeHtml(item.severity)}</td>
                            <td>${escapeHtml(item.category)}</td>
                            <td>${escapeHtml(item.client_id || "")}</td>
                            <td><strong>${escapeHtml(item.title)}</strong><br>${escapeHtml(item.detail)}</td>
                            <td>${escapeHtml(item.action || "")}</td>
                        </tr>
                    `).join("");
                    const merchantSummary = merchantHealth.summary || {};
                    document.getElementById("merchantHealthSummary").textContent = [
                        `Total merchants: ${merchantSummary.total || 0}`,
                        `High risk: ${merchantSummary.high || 0}`,
                        `Ready for test: ${merchantSummary.ready_for_test || 0}`,
                        `Live: ${merchantSummary.live || 0}`,
                        `New leads waiting: ${merchantSummary.new_leads || 0}`,
                        `Due follow-ups: ${merchantSummary.due_followups || 0}`
                    ].join("\\n");
                    document.getElementById("merchantHealthRows").innerHTML = (merchantHealth.merchants || []).slice(0, 20).map(merchant => `
                        <tr>
                            <td>${escapeHtml(merchant.risk_level)}<br>${escapeHtml(merchant.health_score)}%</td>
                            <td>${escapeHtml(merchant.business_name)}<br><small>${escapeHtml(merchant.business_slug)}</small></td>
                            <td>${escapeHtml(merchant.onboarding_status)}<br>${escapeHtml(merchant.onboarding_percent)}%</td>
                            <td>${escapeHtml(merchant.total_leads)} total<br>${escapeHtml(merchant.new_leads)} new</td>
                            <td>${escapeHtml(merchant.due_followups)} due</td>
                            <td>${escapeHtml(merchant.next_action || "")}</td>
                        </tr>
                    `).join("");
                    document.getElementById("clients").innerHTML = clients.clients.map(client => `
                        <tr>
                            <td>${escapeHtml(client.client_id || "")}</td>
                            <td>${escapeHtml(client.plan || "")}</td>
                            <td>${escapeHtml(client.credits ?? "")}</td>
                            <td>${escapeHtml(client.status || "")}</td>
                            <td>${escapeHtml(client.billing_email || "")}</td>
                            <td>${escapeHtml(client.api_key_prefix || "")}</td>
                            <td><button class="btn secondary" data-resend-client="${escapeHtml(client.client_id || "")}">Resend Key</button></td>
                        </tr>
                    `).join("");
                    document.querySelectorAll("[data-resend-client]").forEach(button => {
                        button.addEventListener("click", () => resendApiKey(button.dataset.resendClient || ""));
                    });
                    document.getElementById("paymentEvents").innerHTML = events.payment_events.map(event => `
                        <tr>
                            <td>${escapeHtml(event.created_at || "")}</td>
                            <td>${escapeHtml(event.event_type)}</td>
                            <td>${escapeHtml(event.billing_email || "")}</td>
                            <td>${escapeHtml(event.client_id || "")}</td>
                            <td>${escapeHtml(event.plan || "")}</td>
                            <td>${escapeHtml(event.delivery_status || event.response?.delivery?.status || "")}</td>
                        </tr>
                    `).join("");
                    document.getElementById("notifications").innerHTML = notifications.notifications.map(notification => `
                        <tr>
                            <td>${escapeHtml(notification.created_at || "")}</td>
                            <td>${escapeHtml(notification.client_id || "")}</td>
                            <td>${escapeHtml(notification.notification_type || "")}</td>
                            <td>${escapeHtml(notification.delivery_status || "")}</td>
                            <td>${escapeHtml(notification.delivery_provider || "")}</td>
                        </tr>
                    `).join("");
                    renderAuditEvents(audit.events || []);
                    const marginCheck = checks.checks.find(check => check.name === "MARGIN_GUARD");
                    const failed = checks.checks.filter(check => !check.ok).map(check => check.name);
                    document.getElementById("readiness").textContent = [
                        `Ready: ${checks.ready}`,
                        `Failed checks: ${failed.join(", ") || "none"}`,
                        marginCheck ? marginCheck.detail : "Margin guard check unavailable."
                    ].join("\\n\\n");
                    document.getElementById("backups").innerHTML = backups.backups.map(backup => `
                        <tr>
                            <td>${escapeHtml(backup.name)}</td>
                            <td>${escapeHtml(backup.created_at)}</td>
                            <td>${fmt.format(backup.size_bytes || 0)} bytes</td>
                            <td>${escapeHtml(backup.offsite?.status || "")}</td>
                            <td><button class="btn secondary" data-backup-name="${escapeHtml(backup.name)}">Download</button></td>
                        </tr>
                    `).join("");
                    document.querySelectorAll("[data-backup-name]").forEach(button => {
                        button.addEventListener("click", () => downloadBackup(button.dataset.backupName || ""));
                    });
                    document.getElementById("backupScheduler").textContent = [
                        `Enabled: ${backups.scheduler.enabled}`,
                        `Started: ${backups.scheduler.started}`,
                        `Interval: ${backups.scheduler.interval_seconds}s`,
                        `Retention: ${backups.scheduler.retention_count}`,
                        `Last success: ${backups.scheduler.last_success_at || "none"}`,
                        `Last backup: ${backups.scheduler.last_backup?.name || "none"}`,
                        `Last error: ${backups.scheduler.last_error || "none"}`,
                        `Offsite configured: ${backups.scheduler.offsite_configured}`,
                        `Offsite bucket: ${backups.scheduler.offsite_config.bucket || "none"}`,
                        `Offsite prefix: ${backups.scheduler.offsite_config.prefix || "none"}`
                    ].join("\\n");
                    status.textContent = "Loaded.";
                } catch (error) {
                    status.textContent = error.message;
                }
            }
        </script>
        """
    )


@app.get("/plans")
def list_plans():
    return {
        "plans": PLANS,
        "billing": {
            "credit_unit_tokens": CREDIT_UNIT_TOKENS,
            "output_token_weight": OUTPUT_TOKEN_WEIGHT,
            "formula": "credits_spent = ceil((prompt_tokens + completion_tokens * output_token_weight) / credit_unit_tokens)",
        },
    }


@app.get("/models")
def list_models():
    catalog = model_catalog()
    return {
        "providers": {
            provider_key: provider_status(provider_key)
            for provider_key in PROVIDERS
        },
        "models": catalog,
    }


@app.get("/billing/checkout")
def checkout(plan: str, redirect: bool = True):
    ensure_plan(plan)
    link = payment_link_for_plan(plan)

    if not link:
        raise HTTPException(
            status_code=503,
            detail="Payment link is not configured for this plan",
        )

    if redirect:
        return RedirectResponse(link, status_code=303)

    return {
        "plan": plan,
        "payment_link": link,
        "success_url": f"{os.getenv('NEXAFLOW_SITE_URL', 'https://api.nexaflowinfra.com')}/billing/success?plan={plan}",
        "cancel_url": f"{os.getenv('NEXAFLOW_SITE_URL', 'https://api.nexaflowinfra.com')}/billing/cancel?plan={plan}",
        "next_step": "After payment, the gateway creates the customer, emails the API key, and activates plan credits.",
    }


@app.get("/billing/success", response_class=HTMLResponse)
def billing_success(plan: str | None = None):
    plan_name = PLANS.get(plan or "", {}).get("name", "your")
    portal_url = f"{os.getenv('NEXAFLOW_SITE_URL', 'https://api.nexaflowinfra.com')}/portal"
    enquiry_url = f"{os.getenv('NEXAFLOW_SITE_URL', 'https://api.nexaflowinfra.com')}/ai-enquiry"
    return base_html(
        "NexaFlow Payment Complete",
        f"""
        <section class="hero">
            <div>
                <h1>Payment received</h1>
                <p>Your {plan_name} plan is being activated. NexaFlow will email your access details and create your Enquiry merchant setup automatically.</p>
                <div class="actions">
                    <a class="btn" href="{portal_url}">Open Portal</a>
                    <a class="btn secondary" href="{enquiry_url}">View Enquiry Product</a>
                </div>
            </div>
            <div class="product-panel">
                <div class="panel-top"><span>Automatic Fulfillment</span><span>Stripe + Resend + Enquiry</span></div>
                <div class="flow">
                    <div class="flow-row"><span>Payment</span><div class="bar"><span style="width:100%"></span></div><span>done</span></div>
                    <div class="flow-row"><span>Merchant setup</span><div class="bar"><span style="width:100%;background:var(--gold)"></span></div><span>created</span></div>
                    <div class="flow-row"><span>Setup email</span><div class="bar"><span style="width:100%;background:var(--accent)"></span></div><span>sent</span></div>
                </div>
            </div>
        </section>
        <h2>Next Steps</h2>
        <section class="grid">
            <div class="card"><h3>Check Email</h3><p>Search for "NexaFlow Enquiry Inbox is ready" and check spam or promotions if needed.</p></div>
            <div class="card"><h3>Open Inbox</h3><p>Use the private inbox link and business access key in the email to load your leads.</p></div>
            <div class="card"><h3>Share Link</h3><p>Copy your customer enquiry link and place it on WhatsApp, Facebook, Instagram, Google Business Profile, or your website.</p></div>
        </section>
        <p>Use of the service is subject to the <a href="/terms">Terms</a>, <a href="/privacy">Privacy Policy</a>, <a href="/refund-policy">Refund Policy</a>, and <a href="/acceptable-use">Acceptable Use Policy</a>.</p>
        """
    )


@app.get("/billing/cancel", response_class=HTMLResponse)
def billing_cancel(plan: str | None = None):
    plan_name = PLANS.get(plan or "", {}).get("name", "selected")
    return base_html(
        "NexaFlow Checkout Cancelled",
        f"""
        <h1>Checkout cancelled</h1>
        <p>No payment was processed for the {plan_name} plan. You can return to pricing and choose a plan when ready.</p>
        <div class="actions">
            <a class="btn" href="/pricing">Back to Pricing</a>
            <a class="btn secondary" href="/docs">API Docs</a>
        </div>
        """
    )


@app.get("/admin/payment-events")
def list_payment_events(
    limit: int = Query(default=25, ge=1, le=100),
    billing_email: str | None = None,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)

    query = "SELECT * FROM payment_events"
    params = []
    if billing_email:
        query += " WHERE billing_email = ?"
        params.append(billing_email)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with db_connection() as connection:
        rows = connection.execute(query, params).fetchall()

    return {
        "payment_events": [row_to_payment_event(row) for row in rows],
    }


@app.get("/admin/notifications")
def list_client_notifications(
    limit: int = Query(default=25, ge=1, le=100),
    client_id: str | None = None,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)

    query = "SELECT * FROM client_notifications"
    params = []
    if client_id:
        query += " WHERE client_id = ?"
        params.append(client_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with db_connection() as connection:
        rows = connection.execute(query, params).fetchall()

    return {
        "notifications": [row_to_client_notification(row) for row in rows],
    }


@app.post("/admin/backups")
def create_backup(admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    return {
        "backup": create_sqlite_backup(),
        "retention_note": "This backup is stored on the persistent volume. Add off-platform storage before relying on it for disaster recovery.",
    }


@app.get("/admin/backups")
def list_backups(admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    return {
        "backup_path": str(BACKUP_DIR),
        "scheduler": backup_scheduler_status(),
        "backups": list_sqlite_backups(),
    }


@app.post("/admin/backups/offsite-test")
def test_offsite_backup(admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    return {
        "result": test_offsite_backup_upload(),
        "note": "This uploads a tiny probe file only. It does not upload customer data.",
    }


@app.get("/admin/backups/{backup_name}")
def download_backup(
    backup_name: str,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    path = backup_path_for_name(backup_name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")

    response = FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.sqlite3",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/webhooks/payment")
def payment_webhook(
    req: PaymentWebhookRequest,
    x_webhook_secret: str | None = Header(default=None),
):
    if not legacy_payment_webhook_enabled():
        raise HTTPException(status_code=404, detail="Legacy payment webhook is disabled. Use /webhooks/stripe.")

    expected_secret = os.getenv("PAYMENT_WEBHOOK_SECRET")

    if not expected_secret:
        raise HTTPException(status_code=503, detail="Server missing PAYMENT_WEBHOOK_SECRET")

    if not hmac.compare_digest(x_webhook_secret or "", expected_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    return process_payment_event(req)


@app.get("/webhooks/meta")
def meta_webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
):
    expected_token = os.getenv("META_WEBHOOK_VERIFY_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="Server missing META_WEBHOOK_VERIFY_TOKEN")
    if hub_mode == "subscribe" and hmac.compare_digest(hub_verify_token or "", expected_token):
        return Response(hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Invalid Meta webhook verify token")


@app.post("/webhooks/meta")
async def meta_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
):
    app_secret = os.getenv("META_APP_SECRET", "")
    if not app_secret:
        raise HTTPException(status_code=503, detail="Server missing META_APP_SECRET")
    raw_body = await request.body()
    if not verify_meta_signature(raw_body, x_hub_signature_256, app_secret):
        raise HTTPException(status_code=401, detail="Invalid Meta webhook signature")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    return process_meta_webhook_payload(payload)


@app.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None),
):
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if not endpoint_secret:
        raise HTTPException(status_code=503, detail="Server missing STRIPE_WEBHOOK_SECRET")

    raw_body = await request.body()

    if not verify_stripe_signature(raw_body, stripe_signature, endpoint_secret):
        raise HTTPException(status_code=401, detail="Invalid Stripe signature")

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    supported_stripe_events = {
        "checkout.session.completed",
        "invoice.paid",
        "invoice.payment_failed",
        "customer.subscription.deleted",
        "customer.subscription.updated",
        "charge.refunded",
        "charge.dispute.created",
    }

    if event.get("type") not in supported_stripe_events:
        return {
            "processed": False,
            "reason": "ignored_event_type",
            "event_type": event.get("type"),
        }

    payment = stripe_object_to_payment_request(event)
    return process_payment_event(payment)


@app.post("/admin/clients")
def create_client(req: CreateClientRequest, admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    ensure_plan(req.plan)

    credits = req.credits if req.credits is not None else PLANS[req.plan]["included_credits"]
    client, new_api_key = create_client_record(req.client_id, req.plan, credits, req.billing_email)

    return {
        **public_client(req.client_id, client),
        "api_key": new_api_key,
        "warning": "Store this API key now. It will not be shown again.",
    }


@app.get("/admin/clients")
def admin_clients(admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    clients = load_clients()
    normalized_clients = {}

    for client_id, client in clients.items():
        normalized_client = normalize_client(client_id, client)
        normalized_client.pop("api_key", None)
        normalized_clients[client_id] = normalized_client

    save_clients(normalized_clients)

    return {
        "clients": [
            public_client(client_id, client)
            for client_id, client in normalized_clients.items()
        ]
    }


@app.patch("/admin/clients/{client_id}")
def update_client(client_id: str, req: UpdateClientRequest, admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    clients = load_clients()

    if client_id not in clients:
        raise HTTPException(status_code=404, detail="Client not found")

    client = normalize_client(client_id, clients[client_id])

    if req.plan is not None:
        ensure_plan(req.plan)
        client["plan"] = req.plan

    if req.credits is not None:
        client["credits"] = req.credits

    if req.status is not None:
        client["status"] = req.status

    if req.billing_email is not None:
        client["billing_email"] = req.billing_email

    clients[client_id] = client
    save_clients(clients)
    return public_client(client_id, client)


@app.post("/admin/clients/{client_id}/topup")
def topup_client(client_id: str, req: TopupRequest, admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    clients = load_clients()

    if client_id not in clients:
        raise HTTPException(status_code=404, detail="Client not found")

    client = normalize_client(client_id, clients[client_id])
    client["credits"] += req.amount
    clients[client_id] = client
    save_clients(clients)
    if client["credits"] > low_credit_threshold(client["plan"]):
        reset_client_notification(client_id, "low_credit")
    return public_client(client_id, client)


@app.post("/admin/clients/{client_id}/send-api-key")
def send_client_api_key(
    client_id: str,
    req: SendApiKeyRequest,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)

    if not req.rotate:
        raise HTTPException(
            status_code=400,
            detail="Existing API keys are stored hashed only. Use rotate=true to generate and deliver a new key.",
        )

    clients = load_clients()
    if client_id not in clients:
        raise HTTPException(status_code=404, detail="Client not found")

    client = normalize_client(client_id, clients[client_id])
    new_api_key = generate_api_key()
    client["api_key_hash"] = api_key_digest(new_api_key)
    client["api_key_prefix"] = new_api_key[:8]
    clients[client_id] = client
    save_clients(clients)

    delivery = deliver_api_key(client_id, client, new_api_key)

    return {
        **public_client(client_id, client),
        "rotated": True,
        "delivery": delivery,
        "warning": "The API key was rotated. The previous key can no longer be used.",
    }


@app.get("/admin/usage-stats")
def usage_stats(admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    logs = load_logs()
    usage = usage_totals_for_logs(logs)

    return {
        "total_requests": len(logs),
        "client_usage": usage["by_client"],
        "provider_usage": usage["by_provider"],
        "totals": usage["totals"],
    }


@app.get("/admin/revenue-report")
def admin_revenue_report(admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    return admin_revenue_report_data()


@app.get("/admin/action-items")
def admin_action_items(admin_key: str | None = None, x_admin_key: str | None = Header(default=None)):
    admin_guard(admin_key, x_admin_key)
    return admin_action_items_data()


@app.get("/admin/merchant-health")
def admin_merchant_health(
    limit: int = Query(default=100, ge=1, le=500),
    business_slug: str | None = None,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return merchant_health_report(limit=limit, business_slug=business_slug)


@app.post("/apps/enquiry/api/enquiries")
def create_enquiry(req: EnquiryCreateRequest):
    return public_enquiry_response(create_enquiry_record(req))


@app.post("/apps/enquiry/api/trial-requests")
def submit_trial_request(req: TrialRequestCreate):
    trial_request = create_trial_request(req)
    return {
        "id": trial_request["id"],
        "business_name": trial_request["business_name"],
        "status": trial_request["status"],
        "lead_source": trial_request["lead_source"],
        "campaign": trial_request["campaign"],
        "referrer": trial_request["referrer"],
        "created_at": trial_request["created_at"],
        "message": "Trial request received. NexaFlow will follow up for setup.",
    }


@app.get("/apps/enquiry/api/trial-requests")
def get_trial_requests(
    status: str | None = Query(default=None, pattern="^(new|contacted|trial_setup|won|lost|spam)$"),
    limit: int = Query(default=100, ge=1, le=500),
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    trial_requests = list_trial_requests(status=status, limit=limit)
    return {
        "stats": trial_request_stats(trial_requests),
        "trial_requests": trial_requests,
    }


@app.patch("/apps/enquiry/api/trial-requests/{request_id}")
def patch_trial_request(
    request_id: int,
    req: TrialRequestUpdate,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return update_trial_request_status(request_id, req)


@app.post("/apps/enquiry/api/trial-requests/{request_id}/create-profile")
def create_profile_from_trial_request(
    request_id: int,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return trial_request_to_business_profile(request_id)


@app.post("/apps/enquiry/api/business-profiles")
def create_or_update_business_profile(
    req: BusinessProfileRequest,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return upsert_business_profile(req)


@app.post("/apps/enquiry/api/merchant/signup")
def create_merchant_signup(req: MerchantSignupRequest):
    return create_merchant_workspace(req)


@app.get("/apps/enquiry/api/business-profiles")
def get_business_profiles(
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    profiles = list_business_profiles()
    for profile in profiles:
        profile["onboarding"] = business_profile_onboarding_status(profile)
    return {
        "profiles": profiles,
    }


@app.post("/apps/enquiry/api/business-profiles/{business_slug}/send-onboarding")
def send_business_profile_onboarding(
    business_slug: str,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return send_business_onboarding(business_slug)


@app.get("/apps/enquiry/api/business-profiles/{business_slug}")
def get_public_business_profile(business_slug: str):
    profile = get_business_profile(business_slug)
    if profile["status"] != "active":
        raise HTTPException(status_code=404, detail="Business profile not found")
    return public_business_profile_response(profile)


@app.get("/apps/enquiry/api/enquiries")
def list_enquiries(
    status: str | None = Query(default=None, pattern="^(new|contacted|quoted|won|lost|spam)$"),
    business_slug: str | None = None,
    priority: str | None = Query(default=None, pattern="^(hot|warm|normal)$"),
    intent: str | None = Query(default=None, pattern="^(quotation|booking|inventory|general)$"),
    source: str | None = Query(default=None, max_length=80),
    follow_up: str | None = Query(default=None, pattern="^(due|scheduled|none)$"),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return {
        "stats": enquiry_stats(business_slug=business_slug),
        "enquiries": list_enquiry_records(
            status=status,
            business_slug=business_slug,
            priority=priority,
            intent=intent,
            source=source,
            follow_up=follow_up,
            search=search,
            limit=limit,
        ),
    }


@app.get("/apps/enquiry/api/merchant/enquiries")
def list_merchant_enquiries(
    business_slug: str,
    status: str | None = Query(default=None, pattern="^(new|contacted|quoted|won|lost|spam)$"),
    priority: str | None = Query(default=None, pattern="^(hot|warm|normal)$"),
    intent: str | None = Query(default=None, pattern="^(quotation|booking|inventory|general)$"),
    source: str | None = Query(default=None, max_length=80),
    follow_up: str | None = Query(default=None, pattern="^(due|scheduled|none)$"),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    stats = enquiry_stats(business_slug=profile["slug"])
    return {
        "business": profile,
        "onboarding": business_profile_onboarding_status(profile),
        "stats": stats,
        "enquiries": list_enquiry_records(
            status=status,
            business_slug=profile["slug"],
            priority=priority,
            intent=intent,
            source=source,
            follow_up=follow_up,
            search=search,
            limit=limit,
        ),
    }


@app.post("/apps/enquiry/api/merchant/copilot/analyze")
def analyze_merchant_copilot_preview(
    req: MerchantCopilotAnalyzeRequest,
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    if not req.processing_acknowledged:
        raise HTTPException(
            status_code=400,
            detail="Please confirm this buyer message can be processed for AI Copilot preview.",
        )
    if contains_forbidden_channel_secret(req.name, req.phone, req.email, req.message, req.campaign):
        raise HTTPException(
            status_code=400,
            detail="Do not paste passwords, OTPs, cookies, app secrets, or access tokens into AI Copilot.",
        )
    if contains_sensitive_manual_enquiry_content(req.name, req.phone, req.email, req.message, req.campaign):
        raise HTTPException(
            status_code=400,
            detail="Do not paste identity documents, bank statements, payslips, or other sensitive files into AI Copilot.",
        )

    result = merchant_copilot_analysis_response(profile, req)
    write_data_audit_event(
        "enquiry.copilot_previewed",
        "merchant_copilot",
        "enquiry",
        business_slug=profile["slug"],
        metadata={
            "source": result["customer"]["source"],
            "intent": result["intent"],
            "priority": result["priority"],
            "analysis_source": result["analysis_source"],
            "auto_sends": False,
        },
    )
    return result


@app.post("/apps/enquiry/api/merchant/enquiries")
def create_merchant_manual_enquiry(
    req: MerchantManualEnquiryCreate,
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    if not req.processing_acknowledged:
        raise HTTPException(
            status_code=400,
            detail="Please confirm this buyer enquiry can be stored for follow-up before adding it.",
        )
    if contains_forbidden_channel_secret(req.name, req.phone, req.email, req.message, req.campaign):
        raise HTTPException(
            status_code=400,
            detail="Do not paste passwords, OTPs, cookies, app secrets, or access tokens into manual buyer capture.",
        )
    if contains_sensitive_manual_enquiry_content(req.name, req.phone, req.email, req.message, req.campaign):
        raise HTTPException(
            status_code=400,
            detail="Do not paste identity documents, bank statements, payslips, or other sensitive files into manual buyer capture.",
        )
    source = re.sub(r"[^a-z0-9_-]+", "-", (req.source or "manual").strip().lower()).strip("-") or "manual"
    buyer_name = manual_capture_buyer_name(source, req.name)
    buyer_contact = manual_capture_contact_identifier(source, buyer_name, req.phone)
    enquiry_req = EnquiryCreateRequest(
        business_slug=profile["slug"],
        business_type=profile["business_type"],
        name=buyer_name,
        phone=buyer_contact,
        email=req.email,
        message=req.message,
        source=source,
        campaign=req.campaign.strip() or "manual-capture",
        referrer="merchant-manual-capture",
        page_url=site_absolute_url(profile["inbox_url"]),
        pdpa_consent=True,
    )
    consent_notice = (
        "Merchant confirmed this buyer enquiry was received through a business conversation "
        "and may be recorded in NexaFlow for reply, quotation, appointment, loan follow-up, "
        "support, security, and required record keeping. Sensitive documents should not be pasted here."
    )
    return create_enquiry_record(
        enquiry_req,
        actor_type="merchant_manual",
        notify_merchant=False,
        consent_notice_override=consent_notice,
    )


@app.post("/apps/enquiry/api/merchant/demo-enquiries")
def create_merchant_demo_enquiries(
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    return seed_merchant_demo_enquiries(profile)


@app.get("/apps/enquiry/api/merchant/enquiries/export.csv")
def export_merchant_enquiries_csv(
    business_slug: str,
    status: str | None = Query(default=None, pattern="^(new|contacted|quoted|won|lost|spam)$"),
    priority: str | None = Query(default=None, pattern="^(hot|warm|normal)$"),
    intent: str | None = Query(default=None, pattern="^(quotation|booking|inventory|general)$"),
    source: str | None = Query(default=None, max_length=80),
    follow_up: str | None = Query(default=None, pattern="^(due|scheduled|none)$"),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=500, ge=1, le=1000),
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    enquiries = list_enquiry_records(
        status=status,
        business_slug=profile["slug"],
        priority=priority,
        intent=intent,
        source=source,
        follow_up=follow_up,
        search=search,
        limit=limit,
    )
    write_data_audit_event(
        "enquiry.exported",
        "merchant",
        "enquiry",
        business_slug=profile["slug"],
        metadata={
            "count": len(enquiries),
            "status": status or "",
            "priority": priority or "",
            "intent": intent or "",
            "source": source or "",
            "follow_up": follow_up or "",
            "search_used": bool(search),
        },
    )
    filename = f"{profile['slug']}-enquiries.csv"
    return Response(
        enquiries_to_csv(enquiries),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/apps/enquiry/api/followups/digest")
def send_enquiry_followup_digest(
    business_slug: str | None = None,
    dry_run: bool = False,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return send_due_followup_digest(business_slug=business_slug, dry_run=dry_run)


@app.get("/apps/enquiry/api/merchant/profile")
def get_merchant_business_profile(
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    return business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )


@app.get("/apps/enquiry/api/merchant/share-links")
def get_merchant_share_links(
    business_slug: str,
    campaign: str = Query(default="merchant-share", max_length=120),
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    return merchant_share_links(profile, campaign=campaign)


@app.get("/apps/enquiry/api/merchant/channel-connections")
def get_merchant_channel_connections(
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    return channel_connection_response(profile)


@app.get("/apps/enquiry/api/merchant/meta-setup")
def get_merchant_meta_setup(
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    return merchant_meta_setup_response(profile)


@app.patch("/apps/enquiry/api/merchant/channel-connections/{channel}")
def update_merchant_channel_connection(
    channel: str,
    req: ChannelConnectionUpdate,
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    return upsert_channel_connection(profile, channel, req)


@app.post("/apps/enquiry/api/merchant/channel-connections/{channel}/pilot-test")
def create_merchant_meta_pilot_test(
    channel: str,
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    return create_meta_pilot_test_event(profile, channel)


@app.patch("/apps/enquiry/api/merchant/profile")
def update_merchant_business_profile(
    req: BusinessProfileSettingsUpdate,
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    return update_business_profile_settings(profile["slug"], req)


@app.delete("/apps/enquiry/api/merchant/enquiries/{enquiry_id}")
def delete_merchant_enquiry(
    enquiry_id: int,
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    with db_connection() as connection:
        row = connection.execute(
            "SELECT id, business_slug, status FROM enquiries WHERE id = ?",
            (enquiry_id,),
        ).fetchone()
        if not row or row["business_slug"] != profile["slug"]:
            raise HTTPException(status_code=404, detail="Enquiry not found")
        write_data_audit_event(
            "enquiry.deleted",
            "merchant",
            "enquiry",
            enquiry_id,
            business_slug=profile["slug"],
            metadata={"previous_status": row["status"] if "status" in row.keys() else ""},
            connection=connection,
        )
        connection.execute("DELETE FROM enquiries WHERE id = ?", (enquiry_id,))

    return {
        "deleted": True,
        "id": enquiry_id,
        "business_slug": profile["slug"],
    }


@app.patch("/apps/enquiry/api/enquiries/{enquiry_id}")
def update_enquiry_status(
    enquiry_id: int,
    req: EnquiryStatusUpdate,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM enquiries WHERE id = ?",
            (enquiry_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Enquiry not found")

        connection.execute(
            "UPDATE enquiries SET status = ?, updated_at = ? WHERE id = ?",
            (req.status, now_iso(), enquiry_id),
        )
        write_data_audit_event(
            "enquiry.status_updated",
            "admin",
            "enquiry",
            enquiry_id,
            business_slug=row["business_slug"],
            metadata={"from": row["status"], "to": req.status},
            connection=connection,
        )
        updated = connection.execute(
            "SELECT * FROM enquiries WHERE id = ?",
            (enquiry_id,),
        ).fetchone()

    return row_to_enquiry(updated)


@app.patch("/apps/enquiry/api/merchant/enquiries/{enquiry_id}")
def update_merchant_enquiry_status(
    enquiry_id: int,
    req: MerchantEnquiryUpdate,
    business_slug: str,
    business_key: str | None = None,
    x_business_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    if (
        req.status is None
        and req.internal_note is None
        and req.follow_up_at is None
        and req.deal_value is None
    ):
        raise HTTPException(status_code=400, detail="Status, note, follow-up, or deal value is required")

    profile = business_guard(
        business_slug,
        business_key=business_key,
        x_business_key=x_business_key,
        authorization=authorization,
    )
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM enquiries WHERE id = ?",
            (enquiry_id,),
        ).fetchone()
        if not row or row["business_slug"] != profile["slug"]:
            raise HTTPException(status_code=404, detail="Enquiry not found")

        next_status = req.status if req.status is not None else row["status"]
        next_note = req.internal_note.strip() if req.internal_note is not None else (row["internal_note"] or "")
        next_follow_up = req.follow_up_at.strip() if req.follow_up_at is not None else (row["follow_up_at"] or "")
        next_deal_value = req.deal_value if req.deal_value is not None else row["deal_value"]
        connection.execute(
            "UPDATE enquiries SET status = ?, internal_note = ?, follow_up_at = ?, deal_value = ?, updated_at = ? WHERE id = ?",
            (next_status, next_note, next_follow_up, next_deal_value, now_iso(), enquiry_id),
        )
        write_data_audit_event(
            "enquiry.updated",
            "merchant",
            "enquiry",
            enquiry_id,
            business_slug=profile["slug"],
            metadata={
                "status_changed": next_status != row["status"],
                "note_changed": next_note != (row["internal_note"] or ""),
                "follow_up_changed": next_follow_up != (row["follow_up_at"] or ""),
                "deal_value_changed": next_deal_value != row["deal_value"],
            },
            connection=connection,
        )
        updated = connection.execute(
            "SELECT * FROM enquiries WHERE id = ?",
            (enquiry_id,),
        ).fetchone()

    return row_to_enquiry(updated)


@app.get("/client-info")
def client_info(api_key: str):
    client_id, client = find_client_by_api_key(api_key)
    return public_client(client_id, client)


@app.get("/usage-history")
def usage_history(api_key: str):
    client_id, _ = find_client_by_api_key(api_key)
    logs = load_logs()
    client_logs = [log for log in logs if log["client_id"] == client_id]

    return {
        "client_id": client_id,
        "total_requests": len(client_logs),
        "logs": client_logs[-100:],
    }


@app.get("/customer/me")
def customer_me(
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, client = customer_guard(api_key, x_api_key, authorization)
    return public_client(client_id, client)


@app.get("/customer/usage")
def customer_usage(
    limit: int = Query(default=25, ge=1, le=100),
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, _ = customer_guard(api_key, x_api_key, authorization)
    return client_usage_report(client_id, limit)


@app.get("/customer/billing-links")
def customer_billing_links(
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, client = customer_guard(api_key, x_api_key, authorization)
    client = normalize_client(client_id, client)
    billing_portal_available = bool(client.get("stripe_customer_id")) and stripe_billing_portal_configured()
    return {
        "client": public_client(client_id, client),
        "links": plan_billing_links(),
        "billing_portal": {
            "available": billing_portal_available,
            "stripe_customer_id_present": bool(client.get("stripe_customer_id")),
            "configured": stripe_billing_portal_configured(),
            "reason": (
                None
                if billing_portal_available
                else (
                    "This account is not linked to a Stripe customer yet."
                    if not client.get("stripe_customer_id")
                    else "Stripe billing portal is not configured yet."
                )
            ),
        },
    }


@app.get("/customer/enquiry/business-profiles")
def customer_enquiry_business_profiles(
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, _ = customer_guard(api_key, x_api_key, authorization)
    return {
        "profiles": list_business_profiles(client_id=client_id),
    }


@app.post("/customer/enquiry/business-profiles")
def customer_upsert_enquiry_business_profile(
    req: BusinessProfileRequest,
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, client = customer_guard(api_key, x_api_key, authorization)
    if client["status"] != "active":
        raise HTTPException(status_code=403, detail="Client account is not active")
    return upsert_business_profile(req, owner_client_id=client_id)


@app.post("/customer/enquiry/business-profiles/{business_slug}/send-onboarding")
def customer_send_enquiry_business_profile_onboarding(
    business_slug: str,
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, client = customer_guard(api_key, x_api_key, authorization)
    if client["status"] != "active":
        raise HTTPException(status_code=403, detail="Client account is not active")
    return send_business_onboarding(business_slug, owner_client_id=client_id)


@app.post("/customer/billing-portal")
def customer_billing_portal(
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, client = customer_guard(api_key, x_api_key, authorization)
    client = normalize_client(client_id, client)

    if not client.get("stripe_customer_id"):
        raise HTTPException(
            status_code=409,
            detail="This account is not linked to a Stripe customer yet.",
        )

    site_url = os.getenv("NEXAFLOW_SITE_URL", "https://api.nexaflowinfra.com").rstrip("/")
    session = create_stripe_billing_portal_session(
        client["stripe_customer_id"],
        f"{site_url}/portal",
    )
    return {
        "client_id": client_id,
        "url": session["url"],
        "session_id": session.get("id"),
        "livemode": session.get("livemode"),
        "test_mode": session.get("test_mode", False),
    }


@app.post("/customer/rotate-api-key")
def customer_rotate_api_key(
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, client = customer_guard(api_key, x_api_key, authorization)
    clients = load_clients()
    stored_client = normalize_client(client_id, clients[client_id])
    new_api_key = generate_api_key()
    stored_client["api_key_hash"] = api_key_digest(new_api_key)
    stored_client["api_key_prefix"] = new_api_key[:8]
    clients[client_id] = stored_client
    save_clients(clients)

    return {
        **public_client(client_id, stored_client),
        "api_key": new_api_key,
        "rotated": True,
        "warning": "Store this API key now. The previous key can no longer be used.",
    }


@app.post("/v1/chat")
def chat(
    req: ChatRequest,
    api_key: str | None = None,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    client_id, client = customer_guard(api_key, x_api_key, authorization)

    if client["status"] != "active":
        raise HTTPException(status_code=403, detail="Client account is not active")

    if client["credits"] <= 0:
        raise HTTPException(status_code=402, detail="Insufficient credits")

    plan = PLANS[client["plan"]]
    enforce_rate_limit(client_id, plan["rate_limit_per_minute"])
    usage_guard = enforce_usage_guard(client_id, client, plan, req)
    candidates = rank_model_candidates(req, plan)
    fallback_attempts = []
    response = None
    model_key = None
    routed_model = None
    route_score = None

    for candidate_model_key, candidate_model, candidate_score in candidates:
        provider_client = get_provider_client(candidate_model["provider"])

        attempt = {
            "provider": candidate_model["provider"],
            "model": candidate_model["model"],
            "model_key": candidate_model_key,
            "status": "pending",
        }

        if provider_client is None:
            attempt["status"] = "skipped"
            attempt["error"] = "provider_not_configured"
            fallback_attempts.append(attempt)
            continue

        try:
            response = provider_client.chat.completions.create(
                model=candidate_model["model"],
                messages=[
                    {
                        "role": "developer",
                        "content": "You are NexaFlow AI. Provide helpful, lawful, business-safe assistance. Keep responses practical and concise.",
                    },
                    {"role": "user", "content": req.message},
                ],
                extra_headers={
                    "HTTP-Referer": os.getenv("NEXAFLOW_SITE_URL", "http://localhost:8000"),
                    "X-Title": os.getenv("NEXAFLOW_APP_NAME", "NexaFlow AI Gateway"),
                } if candidate_model["provider"] == "openrouter" else None,
            )
            attempt["status"] = "success"
            fallback_attempts.append(attempt)
            model_key = candidate_model_key
            routed_model = candidate_model
            route_score = candidate_score
            break
        except APIStatusError as exc:
            attempt["status"] = "failed"
            attempt["upstream_status"] = exc.status_code
            fallback_attempts.append(attempt)

    if response is None:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "All configured model candidates failed.",
                "attempts": fallback_attempts,
            },
        )

    credits_spent = calculate_credits_spent(response.usage)

    clients = load_clients()
    stored_client = normalize_client(client_id, clients[client_id])
    stored_client["credits"] -= credits_spent
    overage_credits = abs(stored_client["credits"]) if stored_client["credits"] < 0 else 0
    clients[client_id] = stored_client
    save_clients(clients)

    prompt_tokens = getattr(response.usage, "prompt_tokens", 0) if response.usage else 0
    completion_tokens = getattr(response.usage, "completion_tokens", 0) if response.usage else 0
    total_tokens = getattr(response.usage, "total_tokens", 0) if response.usage else 0
    provider_cost_usd = calculate_provider_cost_usd(routed_model, prompt_tokens, completion_tokens)
    revenue_usd = calculate_credit_revenue_usd(plan, credits_spent)
    gross_margin_usd = round(revenue_usd - provider_cost_usd, 8)

    logs = load_logs()
    logs.append(
        {
            "client_id": client_id,
            "provider": routed_model["provider"],
            "model": routed_model["model"],
            "model_key": model_key,
            "task": req.task,
            "routing_strategy": req.routing_strategy,
            "route_score": route_score,
            "fallback_attempts": fallback_attempts,
            "credits_spent": credits_spent,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "provider_cost_usd": provider_cost_usd,
            "revenue_usd": revenue_usd,
            "gross_margin_usd": gross_margin_usd,
            "overage_credits": overage_credits,
            "message_preview": req.message[:120],
            "timestamp": now_iso(),
        }
    )
    save_logs(logs)
    low_credit_notice = maybe_send_low_credit_notice(client_id, stored_client)

    return {
        "reply": response.choices[0].message.content,
        "remaining_credits": stored_client["credits"],
        "credits_spent": credits_spent,
        "overage_credits": overage_credits,
        "provider": routed_model["provider"],
        "model": routed_model["model"],
        "model_key": model_key,
        "routing": {
            "strategy": req.routing_strategy,
            "score": route_score,
            "attempts": fallback_attempts,
        },
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "billing": {
            "provider_cost_usd": provider_cost_usd,
            "revenue_usd": revenue_usd,
            "gross_margin_usd": gross_margin_usd,
        },
        "notifications": {
            "low_credit": low_credit_notice,
        },
        "limits": {
            "estimate": usage_guard["estimate"],
            "daily_usage_before_request": usage_guard["daily_usage"],
            "daily_request_limit": plan["daily_request_limit"],
            "daily_credit_limit": plan["daily_credit_limit"],
            "max_request_credits": plan["max_request_credits"],
        },
    }
