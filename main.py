from datetime import datetime, timezone
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
    client_id: str = Field(..., min_length=3, max_length=80)
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
    pdpa_consent: bool = False


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


class BusinessProfileSettingsUpdate(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=160)
    business_type: str = Field(default="general", max_length=80)
    whatsapp_phone: str = Field(..., min_length=5, max_length=40)
    contact_email: str | None = Field(default=None, max_length=200)
    offer_summary: str = Field(default="", max_length=600)
    reply_tone: str = Field(default="friendly and professional", max_length=120)
    opening_hours: str = Field(default="", max_length=200)


class EnquiryStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(new|contacted|quoted|won|lost|spam)$")


class MerchantEnquiryUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(new|contacted|quoted|won|lost|spam)$")
    internal_note: str | None = Field(default=None, max_length=1000)
    follow_up_at: str | None = Field(default=None, max_length=80)
    deal_value: float | None = Field(default=None, ge=0, le=1000000000)


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
                intent TEXT,
                priority TEXT,
                estimated_value TEXT,
                reply_draft TEXT,
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(connection, "business_profiles", "client_id", "TEXT")
        ensure_column(connection, "business_profiles", "access_key_hash", "TEXT")
        ensure_column(connection, "business_profiles", "access_key_prefix", "TEXT")


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


def admin_guard(admin_key: str | None = Query(default=None), x_admin_key: str | None = Header(default=None)):
    expected = os.getenv("ADMIN_KEY")
    supplied = x_admin_key or admin_key

    if not expected:
        raise HTTPException(status_code=503, detail="Server missing ADMIN_KEY")

    if supplied != expected:
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

    signed_payload = timestamps[0].encode("utf-8") + b"." + raw_body
    expected = hmac.new(
        endpoint_secret.encode("utf-8"),
        signed_payload,
        "sha256",
    ).hexdigest()

    return any(hmac.compare_digest(expected, signature) for signature in signatures)


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

    supplied = x_api_key or bearer_token or api_key
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
        "form_url": "/enquiry/demo",
        "inbox_url": "/inbox/demo",
        "embed_url": "/embed/enquiry/demo.js",
        "created_at": None,
        "updated_at": None,
    }


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
        if existing and owner_client_id and existing["client_id"] not in {None, owner_client_id}:
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
                access_key_prefix, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                offer_summary = ?, reply_tone = ?, opening_hours = ?, updated_at = ?
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
NexaFlow helps your business collect customer enquiries, identify hot leads, prepare WhatsApp follow-up drafts, and remind you who needs follow-up.

Customer enquiry form:
{form_url}

Private merchant inbox:
{inbox_url}

Business access key:
{access_key}

Website widget code:
{embed_code}

5-minute setup:
1. Open the private merchant inbox.
2. Paste the business access key above and click Load Leads.
3. Open Business settings and confirm your WhatsApp phone, email, service summary, and opening hours.
4. Copy the customer enquiry form link and share it on WhatsApp, Facebook, Instagram, Google Business Profile, or your website.
5. When leads arrive, open the inbox, click WhatsApp, save notes, set follow-up dates, and mark each lead as contacted, quoted, won, or lost.

Data and privacy:
Customer forms include a privacy consent notice. Your private inbox is protected by this business access key. Keep it private.

Keep this access key private. If it is exposed or lost, ask the NexaFlow operator to rotate it.
"""


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
    supplied = x_business_key or business_key or extract_bearer_token(authorization)
    profile = get_business_profile_for_access_key(supplied)
    if normalize_slug(business_slug) != profile["slug"]:
        raise HTTPException(status_code=403, detail="Business key cannot access this inbox")
    return profile


def classify_enquiry(message):
    text = message.lower()
    urgent_keywords = ["urgent", "asap", "today", "tonight", "emergency", "now", "immediately", "紧急", "今天", "马上", "急"]
    quote_keywords = ["price", "quote", "quotation", "cost", "how much", "budget", "package", "多少钱", "价格", "报价", "费用"]
    booking_keywords = ["book", "appointment", "schedule", "reserve", "available", "slot", "预约", "安排", "时间"]
    inventory_keywords = ["stock", "available", "inventory", "in stock", "quantity", "库存", "现货", "数量"]

    if any(keyword in text for keyword in urgent_keywords):
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
    elif intent in {"quotation", "booking"}:
        estimated_value = "medium"
    else:
        estimated_value = "unknown"

    return {
        "intent": intent,
        "priority": priority,
        "estimated_value": estimated_value,
    }


def enquiry_reply_draft(name, business_type, message, classification, profile=None):
    profile = profile or default_enquiry_profile()
    service_label = (profile.get("offer_summary") or business_type.replace("_", " ").strip() or "service").strip()
    business_name = profile.get("business_name") or "us"
    tone = profile.get("reply_tone") or "friendly and professional"
    hours = f" Our opening hours are {profile['opening_hours']}." if profile.get("opening_hours") else ""
    if classification["intent"] == "quotation":
        next_step = "Can you share your preferred date, budget range, and any photos or details so we can prepare a more accurate quote?"
    elif classification["intent"] == "booking":
        next_step = "Can you share your preferred date and time so we can check availability?"
    elif classification["intent"] == "inventory":
        next_step = "Can you confirm the item/model and quantity you need so we can check availability?"
    else:
        next_step = "Can you share a few more details so we can recommend the best next step?"

    urgency = " We can prioritize this because it looks time-sensitive." if classification["priority"] == "hot" else ""
    return (
        f"Hi {name}, thanks for contacting {business_name} about {service_label}.{urgency} "
        f"{next_step}{hours} We will reply in a {tone} way shortly."
    )


def whatsapp_reply_url(phone, reply_draft):
    digits = normalize_phone_for_whatsapp(phone)
    if not digits:
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


def row_to_enquiry(row):
    return {
        "id": row["id"],
        "business_slug": row["business_slug"],
        "name": row["name"],
        "phone": row["phone"],
        "email": row["email"],
        "business_type": row["business_type"],
        "message": row["message"],
        "source": row["source"],
        "intent": row["intent"],
        "priority": row["priority"],
        "estimated_value": row["estimated_value"],
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
Priority: {enquiry['priority']}
Estimated value: {enquiry['estimated_value']}

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
        f"New {enquiry['priority']} enquiry: {enquiry['name']}",
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
        value_line = f"Estimated value: {value}" if value else "Estimated value: not set"
        whatsapp_line = f"WhatsApp: {enquiry['whatsapp_url']}" if enquiry.get("whatsapp_url") else "WhatsApp: not available"
        note_line = f"Note: {enquiry['internal_note']}" if enquiry.get("internal_note") else "Note: none"
        lines.extend(
            [
                f"{index}. {enquiry['name']} ({enquiry['phone']})",
                f"Follow-up date: {enquiry.get('follow_up_at') or 'not set'}",
                f"Intent: {enquiry['intent']} | Priority: {enquiry['priority']} | Status: {enquiry['status']}",
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


def public_enquiry_response(enquiry):
    return {
        "id": enquiry["id"],
        "business_slug": enquiry["business_slug"],
        "intent": enquiry["intent"],
        "priority": enquiry["priority"],
        "status": enquiry["status"],
        "created_at": enquiry["created_at"],
    }


def create_enquiry_record(req):
    profile = get_business_profile(req.business_slug) if req.business_slug else default_enquiry_profile()
    if profile["status"] != "active":
        raise HTTPException(status_code=403, detail="This enquiry form is not accepting new enquiries.")
    if not req.pdpa_consent:
        raise HTTPException(
            status_code=400,
            detail="Consent is required to collect and use your contact details for enquiry follow-up.",
        )
    enforce_enquiry_rate_limit(profile["slug"], req.phone)

    classification = classify_enquiry(req.message)
    business_type = profile.get("business_type") or req.business_type
    reply_draft = enquiry_reply_draft(req.name, business_type, req.message, classification, profile)
    reply_phone = profile.get("whatsapp_phone") or req.phone
    whatsapp_url = whatsapp_reply_url(reply_phone, reply_draft)
    timestamp = now_iso()
    consent_notice = (
        "I agree that my name, contact details, and enquiry message may be collected, used, "
        "and disclosed to the business and NexaFlow service providers for enquiry follow-up, "
        "customer support, security, and record keeping."
    )

    with db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO enquiries (
                business_slug, name, phone, email, business_type, message, source, intent,
                priority, estimated_value, reply_draft, whatsapp_url, merchant_notification_status,
                merchant_notification_error, pdpa_consent, consent_at, consent_notice, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile["slug"],
                req.name.strip(),
                req.phone.strip(),
                normalize_email(req.email),
                business_type.strip() or "general",
                req.message.strip(),
                req.source.strip() or "web",
                classification["intent"],
                classification["priority"],
                classification["estimated_value"],
                reply_draft,
                whatsapp_url,
                "pending",
                None,
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
    delivery = notify_merchant_new_enquiry(profile, enquiry)
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


def list_enquiry_records(
    status=None,
    business_slug=None,
    limit=50,
    priority=None,
    intent=None,
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
        "id",
        "created_at",
        "business_slug",
        "name",
        "phone",
        "email",
        "intent",
        "priority",
        "estimated_value",
        "status",
        "message",
        "reply_draft",
        "whatsapp_url",
        "internal_note",
        "follow_up_at",
        "deal_value",
        "pdpa_consent",
        "consent_at",
        "source",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for enquiry in enquiries:
        writer.writerow({key: enquiry.get(key, "") for key in fieldnames})
    return output.getvalue()


def enquiry_stats(business_slug=None):
    query = "SELECT status, priority, intent, deal_value, follow_up_at FROM enquiries"
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
        "pipeline_value": 0,
        "won_value": 0,
        "scheduled_followups": 0,
        "due_followups": 0,
    }
    for row in rows:
        stats["by_status"][row["status"]] = stats["by_status"].get(row["status"], 0) + 1
        stats["by_priority"][row["priority"]] = stats["by_priority"].get(row["priority"], 0) + 1
        stats["by_intent"][row["intent"]] = stats["by_intent"].get(row["intent"], 0) + 1
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
        "storage": "sqlite",
        "database_path": str(DATABASE_FILE),
        "backup_path": str(BACKUP_DIR),
        "backup_scheduler": backup_scheduler_status(),
        "offsite_backup_configured": offsite_backup_configured(),
        "offsite_backup_config": offsite_backup_config_status(),
        "admin_configured": bool(os.getenv("ADMIN_KEY")),
        "payment_webhook_configured": bool(os.getenv("PAYMENT_WEBHOOK_SECRET")),
        "stripe_webhook_configured": bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
        "stripe_billing_portal_configured": stripe_billing_portal_configured(),
        "email_delivery_configured": email_delivery_configured(),
        "providers": {
            provider_key: provider_status(provider_key)
            for provider_key in PROVIDERS
        },
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


def merchant_html(title, business_name, body, show_sales_contact=False):
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
        if whatsapp_url
        else ""
    )
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{safe_title}</title>
            <style>
                :root {{
                    color-scheme: dark;
                    --bg: #000000;
                    --surface: #0b0b0d;
                    --surface-2: #121214;
                    --ink: #f5f5f5;
                    --muted: #a3a3a3;
                    --line: #2c2822;
                    --soft: #16130f;
                    --brand: #f3c76a;
                    --brand-strong: #ffe3a0;
                    --accent: #f3c76a;
                    --teal: #2dd4bf;
                    --gold: #f3c76a;
                    --danger: #ef4444;
                }}
                * {{ box-sizing: border-box; }}
                body {{
                    margin: 0;
                    font-family: Inter, Segoe UI, Arial, sans-serif;
                    background:
                        radial-gradient(circle at 12% 10%, rgba(45,212,191,.16), transparent 28%),
                        radial-gradient(circle at 88% 18%, rgba(243,199,106,.15), transparent 26%),
                        linear-gradient(180deg, #050505 0%, var(--bg) 42%, #050505 100%);
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
                        linear-gradient(115deg, transparent 0 28%, rgba(45,212,191,.07) 28.2%, transparent 29.4% 48%, rgba(243,199,106,.06) 48.2%, transparent 49.3% 100%),
                        repeating-linear-gradient(90deg, rgba(255,255,255,.024) 0 1px, transparent 1px 88px);
                    mask-image: linear-gradient(180deg, rgba(0,0,0,.86), rgba(0,0,0,.42) 48%, transparent 88%);
                }}
                header, main, footer {{
                    position: relative;
                    z-index: 1;
                }}
                header {{
                    border-bottom: 1px solid var(--line);
                    background: rgba(0, 0, 0, .92);
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
                    width: 28px;
                    height: 28px;
                    border-radius: 6px;
                    background: linear-gradient(135deg, var(--teal), #f2f2f2 48%, var(--gold));
                    display: inline-block;
                }}
                main {{
                    max-width: 1120px;
                    margin: 0 auto;
                    padding: 34px 20px 48px;
                }}
                .hero {{
                    display: grid;
                    grid-template-columns: minmax(0, 1fr) minmax(320px, 430px);
                    gap: 28px;
                    align-items: center;
                    padding: 18px 0 34px;
                }}
                .hero.product-hero {{
                    position: relative;
                    padding: 52px 0 46px;
                    isolation: isolate;
                }}
                .hero.product-hero::before {{
                    content: "";
                    position: absolute;
                    inset: -46px -44px auto auto;
                    width: min(520px, 55vw);
                    height: 260px;
                    border-radius: 999px;
                    background:
                        radial-gradient(circle at 28% 34%, rgba(45,212,191,.18), transparent 28%),
                        radial-gradient(circle at 70% 60%, rgba(243,199,106,.20), transparent 34%),
                        linear-gradient(135deg, rgba(45,212,191,.08), rgba(243,199,106,.07), transparent 72%);
                    filter: blur(20px);
                    opacity: .92;
                    z-index: -1;
                    pointer-events: none;
                }}
                .hero.product-hero::after {{
                    content: "";
                    position: absolute;
                    inset: 20px -70px 0 42%;
                    z-index: -1;
                    pointer-events: none;
                    border-radius: 999px;
                    background:
                        linear-gradient(122deg, transparent 0 16%, rgba(45,212,191,.18) 16.2%, transparent 17.2% 34%, rgba(255,255,255,.08) 34.2%, transparent 35.4% 54%, rgba(243,199,106,.18) 54.2%, transparent 55.4% 100%),
                        repeating-linear-gradient(122deg, transparent 0 26px, rgba(255,255,255,.06) 27px, transparent 29px);
                    mask-image: radial-gradient(ellipse at center, rgba(0,0,0,.86), transparent 72%);
                    transform: skewY(-6deg);
                    opacity: .72;
                }}
                .hero.compact {{ grid-template-columns: minmax(0, 1fr); max-width: 820px; }}
                h1 {{ font-size: 44px; line-height: 1.08; margin: 0 0 12px; letter-spacing: 0; }}
                h2 {{ font-size: 24px; margin: 30px 0 14px; }}
                h3 {{ margin: 0 0 8px; }}
                p {{ color: var(--muted); line-height: 1.6; margin: 0 0 14px; }}
                .lead {{ font-size: 18px; max-width: 720px; }}
                .eyebrow {{
                    color: var(--muted);
                    font-size: 13px;
                    font-weight: 800;
                    margin-bottom: 10px;
                    text-transform: uppercase;
                }}
                .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 20px; }}
                .language-toggle {{
                    display: inline-flex;
                    gap: 6px;
                    border: 1px solid var(--line);
                    border-radius: 999px;
                    padding: 4px;
                    margin-bottom: 18px;
                    background: rgba(255,255,255,.03);
                }}
                .language-toggle button {{
                    border: 0;
                    border-radius: 999px;
                    padding: 7px 11px;
                    background: transparent;
                    color: var(--muted);
                    cursor: pointer;
                    font-weight: 700;
                }}
                .language-toggle button.active {{
                    background: linear-gradient(135deg, var(--gold), #ffffff);
                    color: #000000;
                }}
                .lang-hidden {{ display: none !important; }}
                .btn {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border: 0;
                    border-radius: 6px;
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
                    background: transparent;
                    color: var(--ink);
                    border: 1px solid var(--line);
                }}
                .btn.secondary:hover {{ background: var(--soft); }}
                .product-panel, .form-card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    overflow: hidden;
                    background:
                        linear-gradient(135deg, rgba(45,212,191,.035), rgba(243,199,106,.035)),
                        var(--surface);
                    box-shadow: none;
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
                }}
                .card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 18px;
                    background: var(--surface);
                }}
                .card.accent-card {{
                    background:
                        linear-gradient(135deg, rgba(255,255,255,.08), transparent 56%),
                        var(--surface);
                }}
                .card p:last-child {{ margin-bottom: 0; }}
                .pricing-grid {{
                    display: grid;
                    grid-template-columns: repeat(4, minmax(0, 1fr));
                    gap: 14px;
                    margin-top: 12px;
                }}
                .price-card {{
                    border: 1px solid var(--line);
                    border-radius: 8px;
                    padding: 18px;
                    background: var(--surface);
                    display: grid;
                    gap: 10px;
                    align-content: start;
                }}
                .price-card.highlight {{
                    border-color: #525252;
                    background:
                        linear-gradient(135deg, rgba(255,255,255,.08), transparent 58%),
                        var(--surface);
                }}
                .price-card.trial {{
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
                .form-card {{ padding: 18px; overflow: visible; }}
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
                details.form-card summary {{
                    cursor: pointer;
                    color: var(--ink);
                    font-weight: 800;
                }}
                details.form-card summary + * {{ margin-top: 16px; }}
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
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    color: var(--muted);
                    margin: 8px 0 10px;
                }}
                .admin-split {{
                    display: grid;
                    grid-template-columns: minmax(0, 380px) minmax(0, 1fr);
                    gap: 18px;
                    align-items: start;
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
                    .hero, .grid, .ecosystem-grid, .pricing-grid, .steps, .toolbar, .admin-split, .setup-panel, .share-links {{ grid-template-columns: 1fr; }}
                    h1 {{ font-size: 32px; }}
                    table {{ display: block; overflow-x: auto; }}
                    .signal-row {{ grid-template-columns: 1fr; }}
                    .floating-whatsapp {{ right: 14px; bottom: 14px; min-height: 44px; padding: 10px 14px; }}
                }}
            </style>
        </head>
        <body>
            <header>
                <nav>
                    <a class="brand" href="#"><span class="mark"></span><span>{safe_business_name}</span></a>
                    {sales_contact_nav}
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
                    "Customer enquiry forms may collect names, phone numbers, email addresses, messages, consent records, intent labels, priority labels, reply drafts, internal notes, follow-up dates, and estimated lead values.",
                    "Merchants may use enquiry data only for the stated enquiry follow-up, service, support, security, and record-keeping purposes. Customer data must not be sold as a marketing list or used for unrelated purposes unless the merchant has a lawful basis and required consent.",
                    "NexaFlow provides technical safeguards such as consent capture, business access keys, private merchant inboxes, CSV export controls, and public response minimisation. Merchants remain responsible for their own handling of customer data after access or export.",
                    "Merchants should promptly notify NexaFlow if a business access key, exported file, mailbox, or connected system is exposed or compromised.",
                ],
            ),
            (
                "AI Outputs and Automation",
                [
                    "AI features may classify enquiries, estimate priority, generate reply drafts, route model requests, or assist with business workflows. AI outputs can be inaccurate, incomplete, delayed, biased, or unsuitable for a particular situation.",
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
                        "Enquiry product data such as lead name, phone, email, enquiry message, intent, priority, reply draft, follow-up date, deal value, internal merchant notes, consent timestamp, and consent notice.",
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
    contact_url = sales_whatsapp_url("Hi NexaFlow, I want to know which NexaFlow service fits my business")
    request_link = escape_html(contact_url or "/contact-trial")
    return merchant_html(
        "NexaFlow AI Business Ecosystem",
        "NexaFlow",
        f"""
        <section class="hero product-hero">
            <div>
                <div class="language-toggle" aria-label="Language">
                    <button type="button" class="active" onclick="setProductLang('en')" id="langEn">EN</button>
                    <button type="button" onclick="setProductLang('zh')" id="langZh">中文</button>
                </div>
                <div class="eyebrow">NexaFlow AI Business Ecosystem</div>
                <h1><span data-lang="en">One link to your business tools.</span><span data-lang="zh" class="lang-hidden">一个入口，管理你的生意工具。</span></h1>
                <p class="lead"><span data-lang="en">Choose the service you need first. Start with enquiries today, then add CRM, billing, inventory, or automation when your business is ready.</span><span data-lang="zh" class="lang-hidden">先选择你现在最需要的服务。可以从询盘管理开始，之后再加入 CRM、账单、库存或自动化。</span></p>
                <div class="actions">
                    <a class="btn" href="/ai-enquiry"><span data-lang="en">Start with Enquiry</span><span data-lang="zh" class="lang-hidden">先使用询盘助手</span></a>
                    <a class="btn secondary" href="#services"><span data-lang="en">View Services</span><span data-lang="zh" class="lang-hidden">查看服务</span></a>
                </div>
            </div>
            <div class="hero-side">
                <div class="brand-visual"><img src="/assets/brand/nexaflow-final.png" alt="NexaFlow business ecosystem"></div>
                <div class="product-panel">
                    <div class="panel-top"><span data-lang="en">Business modules</span><span data-lang="zh" class="lang-hidden">商业模块</span><span class="pill good">One core</span></div>
                    <div class="signal-list">
                        <div class="signal-row"><span class="pill hot">Live</span><div><strong><span data-lang="en">Enquiries + Follow-ups</span><span data-lang="zh" class="lang-hidden">询盘 + 跟进</span></strong><span data-lang="en">Capture leads and reply faster on WhatsApp.</span><span data-lang="zh" class="lang-hidden">收集客户询问，并更快用 WhatsApp 跟进。</span></div><a href="/ai-enquiry"><span data-lang="en">Open</span><span data-lang="zh" class="lang-hidden">打开</span></a></div>
                        <div class="signal-row"><span class="pill">Soon</span><div><strong>CRM</strong><span data-lang="en">Customer records and lead stages.</span><span data-lang="zh" class="lang-hidden">客户资料和销售阶段管理。</span></div><a href="{request_link}"><span data-lang="en">Request</span><span data-lang="zh" class="lang-hidden">申请</span></a></div>
                        <div class="signal-row"><span class="pill">Soon</span><div><strong><span data-lang="en">Billing + Inventory</span><span data-lang="zh" class="lang-hidden">账单 + 库存</span></strong><span data-lang="en">Quotations, invoices, stock, and service items.</span><span data-lang="zh" class="lang-hidden">报价、发票、库存和服务项目。</span></div><a href="{request_link}"><span data-lang="en">Request</span><span data-lang="zh" class="lang-hidden">申请</span></a></div>
                    </div>
                </div>
            </div>
        </section>

        <div class="section-head" id="services">
            <div>
                <h2><span data-lang="en">Pick a service</span><span data-lang="zh" class="lang-hidden">选择服务</span></h2>
                <p><span data-lang="en">Start small. Add more workflows later when they become useful.</span><span data-lang="zh" class="lang-hidden">先从一个服务开始。之后有需要时，再加入更多工作流程。</span></p>
            </div>
        </div>
        <section class="grid">
            <div class="card">
                <h3><span data-lang="en">Enquiries</span><span data-lang="zh" class="lang-hidden">询盘管理</span></h3>
                <p><span data-lang="en">One link for customers to submit enquiries with consent.</span><span data-lang="zh" class="lang-hidden">让客户通过一个链接提交询问和同意资料用途。</span></p>
                <a class="btn" href="/ai-enquiry"><span data-lang="en">Use Enquiry</span><span data-lang="zh" class="lang-hidden">使用询盘助手</span></a>
            </div>
            <div class="card">
                <h3><span data-lang="en">Follow-ups</span><span data-lang="zh" class="lang-hidden">客户跟进</span></h3>
                <p><span data-lang="en">Track status, notes, follow-up dates, and WhatsApp reply drafts.</span><span data-lang="zh" class="lang-hidden">记录状态、备注、跟进日期和 WhatsApp 回复草稿。</span></p>
                <a class="btn" href="/ai-enquiry"><span data-lang="en">Included in Enquiry</span><span data-lang="zh" class="lang-hidden">已包含在询盘助手</span></a>
            </div>
            <div class="card">
                <h3>CRM</h3>
                <p><span data-lang="en">Customer profiles, lead stages, and service history.</span><span data-lang="zh" class="lang-hidden">客户资料、销售阶段和服务记录。</span></p>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Request CRM</span><span data-lang="zh" class="lang-hidden">申请 CRM</span></a>
            </div>
            <div class="card">
                <h3><span data-lang="en">Billing</span><span data-lang="zh" class="lang-hidden">账单</span></h3>
                <p><span data-lang="en">Quotations, invoices, payments, and billing records.</span><span data-lang="zh" class="lang-hidden">报价、发票、收款和账单记录。</span></p>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Request Billing</span><span data-lang="zh" class="lang-hidden">申请账单功能</span></a>
            </div>
            <div class="card">
                <h3><span data-lang="en">Inventory</span><span data-lang="zh" class="lang-hidden">库存</span></h3>
                <p><span data-lang="en">Stock, packages, parts, products, and service items.</span><span data-lang="zh" class="lang-hidden">库存、配套、零件、产品和服务项目。</span></p>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Request Inventory</span><span data-lang="zh" class="lang-hidden">申请库存功能</span></a>
            </div>
            <div class="card">
                <h3><span data-lang="en">Automation</span><span data-lang="zh" class="lang-hidden">自动化</span></h3>
                <p><span data-lang="en">Reminders, repeat tasks, and simple workflow automation.</span><span data-lang="zh" class="lang-hidden">提醒、重复任务和简单工作流程自动化。</span></p>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Request Automation</span><span data-lang="zh" class="lang-hidden">申请自动化</span></a>
            </div>
        </section>

        <div class="section-head">
            <div>
                <h2><span data-lang="en">Starter pricing</span><span data-lang="zh" class="lang-hidden">入门价格</span></h2>
                <p><span data-lang="en">Start with NexaFlow Enquiry first. Other modules can be added later as they become available.</span><span data-lang="zh" class="lang-hidden">建议先从 NexaFlow 询盘助手开始。其他模块开发好后可以再加入。</span></p>
            </div>
        </div>
        <section class="pricing-grid">
            <div class="price-card trial">
                <h3><span data-lang="en">30-day trial</span><span data-lang="zh" class="lang-hidden">30 天试用</span></h3>
                <div class="plan-price">Free <span>for trial</span></div>
                <p><span data-lang="en">Test enquiry capture, private inbox, AI sorting, and WhatsApp follow-up drafts.</span><span data-lang="zh" class="lang-hidden">试用询盘收集、私密 inbox、AI 分类和 WhatsApp 回复草稿。</span></p>
                <a class="btn" href="/ai-enquiry"><span data-lang="en">View Enquiry Plan</span><span data-lang="zh" class="lang-hidden">查看询盘方案</span></a>
            </div>
            <div class="price-card">
                <h3><span data-lang="en">Enquiry Starter</span><span data-lang="zh" class="lang-hidden">询盘入门版</span></h3>
                <div class="plan-price">SGD 19 <span>/ month</span></div>
                <p><span data-lang="en">For small service teams that want organized leads and faster WhatsApp follow-up.</span><span data-lang="zh" class="lang-hidden">适合想整理客户询问，并更快 WhatsApp 跟进的小型服务团队。</span></p>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Ask on WhatsApp</span><span data-lang="zh" class="lang-hidden">WhatsApp 咨询</span></a>
            </div>
            <div class="price-card highlight">
                <h3>Enquiry Pro</h3>
                <div class="plan-price">SGD 49 <span>/ month</span></div>
                <p><span data-lang="en">For growing service businesses with daily enquiries and follow-up reminders.</span><span data-lang="zh" class="lang-hidden">适合每天都有客户询问，并需要跟进提醒的成长型商家。</span></p>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Ask on WhatsApp</span><span data-lang="zh" class="lang-hidden">WhatsApp 咨询</span></a>
            </div>
            <div class="price-card">
                <h3>Business</h3>
                <div class="plan-price">SGD 99+ <span>/ month</span></div>
                <p><span data-lang="en">For teams, multiple outlets, higher enquiry volume, or custom workflow setup.</span><span data-lang="zh" class="lang-hidden">适合团队、多分店、更高询盘量或需要客制流程的商家。</span></p>
                <a class="btn secondary" href="{request_link}"><span data-lang="en">Request Business</span><span data-lang="zh" class="lang-hidden">申请 Business</span></a>
            </div>
        </section>

        <div class="section-head">
            <div>
                <h2><span data-lang="en">How it works</span><span data-lang="zh" class="lang-hidden">使用方式</span></h2>
                <p><span data-lang="en">A simple path from service selection to daily operation.</span><span data-lang="zh" class="lang-hidden">从选择服务到日常使用，流程保持简单。</span></p>
            </div>
        </div>
        <section class="steps">
            <div class="step"><div><strong><span data-lang="en">Choose a service</span><span data-lang="zh" class="lang-hidden">选择服务</span></strong><p><span data-lang="en">Start with the workflow your business needs most.</span><span data-lang="zh" class="lang-hidden">先从你生意最需要的流程开始。</span></p></div></div>
            <div class="step"><div><strong><span data-lang="en">Set up your profile</span><span data-lang="zh" class="lang-hidden">设置商家资料</span></strong><p><span data-lang="en">Add your business details, WhatsApp, and service information.</span><span data-lang="zh" class="lang-hidden">填写商家资料、WhatsApp 和服务内容。</span></p></div></div>
            <div class="step"><div><strong><span data-lang="en">Run the workflow</span><span data-lang="zh" class="lang-hidden">开始使用</span></strong><p><span data-lang="en">Share your link and keep customer work organized.</span><span data-lang="zh" class="lang-hidden">分享链接，并把客户事项整理起来。</span></p></div></div>
        </section>

        <div class="section-head">
            <div>
                <h2><span data-lang="en">Trust and data protection</span><span data-lang="zh" class="lang-hidden">信任与资料保护</span></h2>
                <p><span data-lang="en">Customer data should be used only for replies, quotations, appointments, and service follow-up.</span><span data-lang="zh" class="lang-hidden">客户资料应只用于回复、报价、预约和服务跟进。</span></p>
                <p><a href="/privacy">Privacy Policy</a> · <a href="/terms">Terms</a> · <a href="/refund-policy">Refund Policy</a> · <a href="/acceptable-use">Acceptable Use</a></p>
            </div>
        </div>
        <script>
            function setProductLang(lang) {{
                document.querySelectorAll("[data-lang]").forEach(item => {{
                    item.classList.toggle("lang-hidden", item.dataset.lang !== lang);
                }});
                document.getElementById("langEn").classList.toggle("active", lang === "en");
                document.getElementById("langZh").classList.toggle("active", lang === "zh");
                localStorage.setItem("nexaflow_home_lang", lang);
            }}
            setProductLang(localStorage.getItem("nexaflow_home_lang") || "en");
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
                <div class="eyebrow">NexaFlow Enquiry</div>
                <h1><span data-lang="en">AI WhatsApp enquiry assistant for local service businesses</span><span data-lang="zh" class="lang-hidden">给本地服务商家的 AI WhatsApp 询盘助手</span></h1>
                <p class="lead">
                    <span data-lang="en">Collect customer enquiries, understand buyer intent, alert the merchant, and prepare a WhatsApp-ready reply.</span>
                    <span data-lang="zh" class="lang-hidden">自动收集客户询问、判断客户想要什么、通知商家，并准备好可直接用于 WhatsApp 的回复。</span>
                </p>
                <div class="actions">
                    <a class="btn" href="#enquiry-form"><span data-lang="en">Try Demo</span><span data-lang="zh" class="lang-hidden">试用 Demo</span></a>
                    <a class="btn secondary" href="#enquiry-pricing"><span data-lang="en">View Pricing</span><span data-lang="zh" class="lang-hidden">查看价格</span></a>
                </div>
            </div>
            <div class="hero-side">
                <div class="brand-visual"><img src="/assets/brand/nexaflow-final.png" alt="NexaFlow business ecosystem"></div>
                <div class="product-panel">
                    <div class="panel-top"><span data-lang="en">Today</span><span data-lang="zh" class="lang-hidden">今日询盘</span><span class="pill good">WhatsApp-ready</span></div>
                    <div class="signal-list">
                        <div class="signal-row">
                            <span class="pill hot">Hot</span>
                            <div><strong><span data-lang="en">Quotation request</span><span data-lang="zh" class="lang-hidden">报价询问</span></strong><span data-lang="en">Urgent price request for this week.</span><span data-lang="zh" class="lang-hidden">客户想本周获得报价。</span></div>
                            <span data-lang="en">Reply draft</span><span data-lang="zh" class="lang-hidden">回复草稿</span>
                        </div>
                        <div class="signal-row">
                            <span class="pill">Warm</span>
                            <div><strong><span data-lang="en">Booking request</span><span data-lang="zh" class="lang-hidden">预约询问</span></strong><span data-lang="en">Customer asked for available slots.</span><span data-lang="zh" class="lang-hidden">客户想知道可预约时间。</span></div>
                            <span data-lang="en">Follow up</span><span data-lang="zh" class="lang-hidden">跟进</span>
                        </div>
                        <div class="signal-row">
                            <span class="pill">Normal</span>
                            <div><strong><span data-lang="en">General enquiry</span><span data-lang="zh" class="lang-hidden">普通询问</span></strong><span data-lang="en">Customer asked for service details.</span><span data-lang="zh" class="lang-hidden">客户想了解服务详情。</span></div>
                            <span data-lang="en">Check</span><span data-lang="zh" class="lang-hidden">查看</span>
                        </div>
                    </div>
                </div>
            </div>
        </section>
        <section class="grid">
            <div class="card accent-card"><h3><span data-lang="en">One link</span><span data-lang="zh" class="lang-hidden">一个链接</span></h3><p><span data-lang="en">Works as a mini landing page for businesses without a website.</span><span data-lang="zh" class="lang-hidden">没有网站的商家，也可以直接用这个链接接收客户询问。</span></p></div>
            <div class="card"><h3><span data-lang="en">Auto sorting</span><span data-lang="zh" class="lang-hidden">自动分类</span></h3><p><span data-lang="en">Labels quotation, booking, inventory, and general leads.</span><span data-lang="zh" class="lang-hidden">自动判断客户是在问报价、预约、库存，还是普通问题。</span></p></div>
            <div class="card"><h3><span data-lang="en">Fast follow-up</span><span data-lang="zh" class="lang-hidden">快速跟进</span></h3><p><span data-lang="en">Sends merchant alerts and prepares a reply draft for WhatsApp.</span><span data-lang="zh" class="lang-hidden">自动通知商家，并准备好可用于 WhatsApp 的回复草稿。</span></p></div>
        </section>
        <div class="section-head">
            <div>
                <h2><span data-lang="en">Built as a business ecosystem</span><span data-lang="zh" class="lang-hidden">以商业生态系统为基础</span></h2>
                <p><span data-lang="en">Enquiry is the first product. The same NexaFlow core can later support CRM, Billing, Inventory, and Automation.</span><span data-lang="zh" class="lang-hidden">Enquiry 是第一个产品。未来同一个 NexaFlow 核心也可以支持 CRM、Billing、Inventory 和 Automation。</span></p>
            </div>
        </div>
        <section class="ecosystem-grid" aria-label="NexaFlow product ecosystem">
            <div class="ecosystem-pill">Enquiry</div>
            <div class="ecosystem-pill">CRM</div>
            <div class="ecosystem-pill">Billing</div>
            <div class="ecosystem-pill">Inventory</div>
            <div class="ecosystem-pill">Automation</div>
        </section>
        <div class="section-head">
            <div>
                <h2><span data-lang="en">Data safety built in</span><span data-lang="zh" class="lang-hidden">内置资料保护</span></h2>
                <p><span data-lang="en">Designed for merchants who need customer trust before they can win the job.</span><span data-lang="zh" class="lang-hidden">为需要客户信任的本地服务商家而设计。</span></p>
            </div>
        </div>
        <section class="grid">
            <div class="card"><h3><span data-lang="en">Consent before submit</span><span data-lang="zh" class="lang-hidden">提交前同意</span></h3><p><span data-lang="en">Every enquiry records the privacy notice, consent status, and consent time.</span><span data-lang="zh" class="lang-hidden">每个询问都会记录隐私告知、同意状态和同意时间。</span></p></div>
            <div class="card"><h3><span data-lang="en">Private merchant inbox</span><span data-lang="zh" class="lang-hidden">商家私密 inbox</span></h3><p><span data-lang="en">Internal notes, deal value, follow-up dates, and WhatsApp links stay behind a business access key.</span><span data-lang="zh" class="lang-hidden">内部备注、成交金额、跟进日期和 WhatsApp 链接都由商家 access key 保护。</span></p></div>
            <div class="card"><h3><span data-lang="en">Export and records</span><span data-lang="zh" class="lang-hidden">导出与记录</span></h3><p><span data-lang="en">Merchants can export their leads, while Terms and Privacy explain allowed use and responsibilities.</span><span data-lang="zh" class="lang-hidden">商家可以导出 leads，同时条款和隐私政策说明资料用途和责任。</span></p></div>
        </section>
        <div class="section-head" id="enquiry-pricing">
            <div>
                <h2><span data-lang="en">Start with a 30-day trial</span><span data-lang="zh" class="lang-hidden">先免费试用 30 天</span></h2>
                <p><span data-lang="en">Let merchants test the enquiry flow first. After the trial, they can choose a monthly plan based on how many leads they handle.</span><span data-lang="zh" class="lang-hidden">先让商家实际测试询盘流程。试用后，再根据每月客户数量选择配套。</span></p>
            </div>
        </div>
        <section class="pricing-grid">
            <div class="price-card trial">
                <h3><span data-lang="en">Trial</span><span data-lang="zh" class="lang-hidden">试用</span></h3>
                <div class="plan-price"><span data-lang="en">Free</span><span data-lang="zh" class="lang-hidden">免费</span> <span>/ 30 days</span></div>
                <p><span data-lang="en">Best for trying the full workflow with real enquiries.</span><span data-lang="zh" class="lang-hidden">适合先用真实客户询问测试整套流程。</span></p>
                <ul>
                    <li><span data-lang="en">1 business inbox</span><span data-lang="zh" class="lang-hidden">1 个商家 inbox</span></li>
                    <li><span data-lang="en">Enquiry link and widget</span><span data-lang="zh" class="lang-hidden">询问链接与网站 widget</span></li>
                    <li><span data-lang="en">AI reply drafts</span><span data-lang="zh" class="lang-hidden">AI 回复草稿</span></li>
                    <li><span data-lang="en">Manual onboarding support</span><span data-lang="zh" class="lang-hidden">人工协助开通</span></li>
                </ul>
                <a class="btn" target="_blank" rel="noopener" href="/contact-trial"><span data-lang="en">Request Trial</span><span data-lang="zh" class="lang-hidden">申请试用</span></a>
            </div>
            <div class="price-card">
                <h3>Starter</h3>
                <div class="plan-price">SGD 19 <span>/ month</span></div>
                <p><span data-lang="en">For solo owners and small service shops.</span><span data-lang="zh" class="lang-hidden">适合个人老板或小型服务商家。</span></p>
                <ul>
                    <li><span data-lang="en">1 business inbox</span><span data-lang="zh" class="lang-hidden">1 个商家 inbox</span></li>
                    <li><span data-lang="en">Up to 100 enquiries / month</span><span data-lang="zh" class="lang-hidden">每月最多 100 个询问</span></li>
                    <li><span data-lang="en">WhatsApp reply drafts</span><span data-lang="zh" class="lang-hidden">WhatsApp 回复草稿</span></li>
                    <li><span data-lang="en">CSV export</span><span data-lang="zh" class="lang-hidden">CSV 导出</span></li>
                </ul>
            </div>
            <div class="price-card highlight">
                <h3>Pro</h3>
                <div class="plan-price">SGD 49 <span>/ month</span></div>
                <p><span data-lang="en">For growing businesses with daily enquiries.</span><span data-lang="zh" class="lang-hidden">适合每天都有客户询问的成长型商家。</span></p>
                <ul>
                    <li><span data-lang="en">Everything in Starter</span><span data-lang="zh" class="lang-hidden">包含 Starter 全部功能</span></li>
                    <li><span data-lang="en">Up to 500 enquiries / month</span><span data-lang="zh" class="lang-hidden">每月最多 500 个询问</span></li>
                    <li><span data-lang="en">Follow-up digest</span><span data-lang="zh" class="lang-hidden">跟进提醒摘要</span></li>
                    <li><span data-lang="en">Priority setup support</span><span data-lang="zh" class="lang-hidden">优先设置协助</span></li>
                </ul>
            </div>
            <div class="price-card">
                <h3>Business</h3>
                <div class="plan-price">SGD 99+ <span>/ month</span></div>
                <p><span data-lang="en">For teams, multiple outlets, or custom workflows.</span><span data-lang="zh" class="lang-hidden">适合团队、多分店或需要客制流程的商家。</span></p>
                <ul>
                    <li><span data-lang="en">Multiple inboxes</span><span data-lang="zh" class="lang-hidden">多个商家 inbox</span></li>
                    <li><span data-lang="en">Custom lead workflow</span><span data-lang="zh" class="lang-hidden">客制 lead 流程</span></li>
                    <li><span data-lang="en">Higher monthly volume</span><span data-lang="zh" class="lang-hidden">更高每月用量</span></li>
                    <li><span data-lang="en">Managed support</span><span data-lang="zh" class="lang-hidden">管理式支持</span></li>
                </ul>
            </div>
        </section>
        <div class="section-head" id="enquiry-form">
            <div>
                <h2><span data-lang="en">Try the demo</span><span data-lang="zh" class="lang-hidden">试用 Demo</span></h2>
                <p><span data-lang="en">Submit a sample enquiry and see the classification result.</span><span data-lang="zh" class="lang-hidden">提交一个示例询问，看看系统如何自动判断客户意向。</span></p>
            </div>
        </div>
        <section class="form-card">
            <div class="toolbar">
                <label><span data-lang="en">Name</span><span data-lang="zh" class="lang-hidden">姓名</span><input id="leadName" value="Alex Tan"></label>
                <label><span data-lang="en">Phone</span><span data-lang="zh" class="lang-hidden">电话</span><input id="leadPhone" value="6591234567"></label>
            </div>
            <div class="toolbar">
                <label>Email<input id="leadEmail" value="alex@example.com"></label>
                <label><span data-lang="en">Business Type</span><span data-lang="zh" class="lang-hidden">业务类型</span>
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
            <label><input id="pdpaConsent" type="checkbox" checked> <span data-lang="en">I agree that my contact details and enquiry may be used for follow-up, support, security, and record keeping under the Privacy Policy.</span><span data-lang="zh" class="lang-hidden">我同意根据隐私政策，使用我的联系资料和询问内容作跟进、客服、安全和记录用途。</span></label>
            <div class="actions">
                <button class="btn" onclick="submitEnquiry()"><span data-lang="en">Submit Enquiry</span><span data-lang="zh" class="lang-hidden">提交询问</span></button>
            </div>
            <div class="status" id="enquiryStatus">Submit the demo form to create an enquiry and classification result.</div>
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
            setProductLang(localStorage.getItem("nexaflow_enquiry_lang") || "en");
            function escapeHtml(value) {
                return String(value ?? "").replace(/[&<>"']/g, char => ({
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }[char]));
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
                        Intent: ${escapeHtml(result.intent)} | Priority: ${escapeHtml(result.priority)}<br>
                        Internal reply drafts are shown only inside the business inbox.
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
            <label>Email<input id="leadEmail" autocomplete="email" placeholder="you@example.com"></label>
            <label>Message<textarea id="leadMessage">Hi, I would like to enquire about your service.</textarea></label>
            <label><input id="pdpaConsent" type="checkbox"> I agree that my name, contact details, and enquiry message may be used by {business_name} and NexaFlow service providers to respond to this enquiry, provide support, keep records, and protect the service. See the <a href="/privacy" target="_blank">Privacy Policy</a>.</label>
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
            async function submitEnquiry() {{
                const status = document.getElementById("enquiryStatus");
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
                            source: "public-form"
                        }})
                    }});
                    if (!response.ok) {{
                        throw new Error(await response.text());
                    }}
                    const result = await response.json();
                    status.innerHTML = `Enquiry sent. Reference #${{result.id}}. Priority: ${{escapeHtml(result.priority)}}`;
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
                <div class="eyebrow">Merchant inbox</div>
                <h1>{business_name} Leads</h1>
                <p class="lead">View customer enquiries, follow up on WhatsApp, and keep every lead moving.</p>
            </div>
        </section>
        <section class="setup-panel">
            <div class="setup-step"><strong>1. Load inbox</strong><span>Paste your business access key once to unlock your leads.</span></div>
            <div class="setup-step"><strong>2. Share link</strong><span>Send your enquiry link to customers or place it on your website.</span></div>
            <div class="setup-step"><strong>3. Follow up</strong><span>Open hot leads first and use the WhatsApp reply draft.</span></div>
            <div class="setup-step"><strong>4. Track value</strong><span>Set follow-up dates, notes, and estimated deal value.</span></div>
        </section>
        <section class="form-card">
            <div class="toolbar">
                <label>Business access key<input id="businessKey" type="password" placeholder="biz_..."></label>
                <button class="btn" onclick="loadMerchantInbox()">Load Leads</button>
                <button class="btn secondary" onclick="exportMerchantCsv()">Export CSV</button>
            </div>
            <div class="status" id="merchantStatus">Enter your business access key to load this inbox.</div>
            <div id="merchantShareLinks"></div>
        </section>
        <details class="form-card">
            <summary>Business settings</summary>
            <p>Update these only when your WhatsApp, email, service summary, or opening hours change.</p>
            <div class="toolbar">
                <label>Business Name<input id="settingsBusinessName" placeholder="Your business"></label>
                <label>Business Type
                    <select id="settingsBusinessType">
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
                <label>WhatsApp Phone<input id="settingsWhatsapp" placeholder="+65 9123 4567"></label>
                <label>Notification Email<input id="settingsEmail" placeholder="owner@example.com"></label>
            </div>
            <label>Service Summary<textarea id="settingsOffer" placeholder="Tell customers what service you provide."></textarea></label>
            <div class="toolbar">
                <label>Reply Tone<input id="settingsTone" placeholder="friendly and professional"></label>
                <label>Opening Hours<input id="settingsHours" placeholder="Mon-Sat, 9am-6pm"></label>
            </div>
            <button class="btn" onclick="saveMerchantSettings()">Save Settings</button>
            <div class="status" id="settingsStatus">Load leads first, then update your business settings here.</div>
        </details>
        <div class="section-head">
            <div>
                <h2>Pipeline</h2>
                <p>Prioritize hot leads first, then mark each one as contacted, quoted, won, or lost.</p>
            </div>
        </div>
        <section class="grid" id="merchantStats"></section>
        <section class="form-card">
            <div class="toolbar">
                <label>Status
                    <select id="filterStatus">
                        <option value="">All statuses</option>
                        <option value="new">New</option>
                        <option value="contacted">Contacted</option>
                        <option value="quoted">Quoted</option>
                        <option value="won">Won</option>
                        <option value="lost">Lost</option>
                        <option value="spam">Spam</option>
                    </select>
                </label>
                <label>Priority
                    <select id="filterPriority">
                        <option value="">All priorities</option>
                        <option value="hot">Hot</option>
                        <option value="warm">Warm</option>
                        <option value="normal">Normal</option>
                    </select>
                </label>
            </div>
            <div class="toolbar">
                <label>Intent
                    <select id="filterIntent">
                        <option value="">All intents</option>
                        <option value="quotation">Quotation</option>
                        <option value="booking">Booking</option>
                        <option value="inventory">Inventory</option>
                        <option value="general">General</option>
                    </select>
                </label>
                <label>Follow-up
                    <select id="filterFollowUp">
                        <option value="">All follow-ups</option>
                        <option value="due">Due now</option>
                        <option value="scheduled">Scheduled</option>
                        <option value="none">No follow-up</option>
                    </select>
                </label>
            </div>
            <div class="toolbar">
                <label>Search<input id="filterSearch" placeholder="Name, phone, message, note"></label>
            </div>
            <button class="btn" onclick="loadMerchantInbox()">Apply Filters</button>
            <button class="btn secondary" onclick="clearMerchantFilters()">Clear</button>
        </section>
        <table>
            <thead>
                <tr><th>Time</th><th>Lead</th><th>Intent</th><th>Priority</th><th>Message</th><th>Draft</th><th>Follow-up</th><th>Value</th><th>Note</th><th>Status</th><th>Action</th></tr>
            </thead>
            <tbody id="merchantRows"></tbody>
        </table>
        <script>
            const businessSlug = "{slug}";
            function escapeHtml(value) {{
                return String(value ?? "").replace(/[&<>"']/g, char => ({{
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#039;"
                }}[char]));
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
            }}
            function absoluteUrl(path) {{
                return new URL(path, window.location.origin).toString();
            }}
            async function copyMerchantText(value, label) {{
                try {{
                    await navigator.clipboard.writeText(value);
                    document.getElementById("merchantStatus").textContent = `${{label}} copied.`;
                }} catch (error) {{
                    document.getElementById("merchantStatus").textContent = `Copy failed. Select and copy this manually: ${{value}}`;
                }}
            }}
            async function copyMerchantElement(id, label) {{
                const value = document.getElementById(id)?.textContent || "";
                await copyMerchantText(value, label);
            }}
            function renderShareLinks(profile) {{
                const formUrl = absoluteUrl(profile.form_url || `/enquiry/${{businessSlug}}`);
                const inboxUrl = absoluteUrl(profile.inbox_url || `/inbox/${{businessSlug}}`);
                const embedUrl = absoluteUrl(profile.embed_url || `/embed/enquiry/${{businessSlug}}.js`);
                const embedCode = `<script src="${{embedUrl}}"><\\/script>`;
                document.getElementById("merchantShareLinks").innerHTML = `
                    <div class="share-links">
                        <div class="share-link-box">
                            <strong>Customer enquiry link</strong>
                            <code id="merchantFormUrl">${{escapeHtml(formUrl)}}</code>
                            <button class="btn secondary" onclick="copyMerchantElement('merchantFormUrl', 'Customer link')">Copy Link</button>
                        </div>
                        <div class="share-link-box">
                            <strong>Private inbox link</strong>
                            <code id="merchantInboxUrl">${{escapeHtml(inboxUrl)}}</code>
                            <button class="btn secondary" onclick="copyMerchantElement('merchantInboxUrl', 'Inbox link')">Copy Link</button>
                        </div>
                        <div class="share-link-box">
                            <strong>Website widget code</strong>
                            <code id="merchantEmbedCode">${{escapeHtml(embedCode)}}</code>
                            <button class="btn secondary" onclick="copyMerchantElement('merchantEmbedCode', 'Embed code')">Copy Code</button>
                        </div>
                    </div>
                `;
            }}
            async function saveMerchantSettings() {{
                const status = document.getElementById("settingsStatus");
                status.textContent = "Saving business settings...";
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
                            opening_hours: document.getElementById("settingsHours").value
                        }})
                    }});
                    fillMerchantSettings(profile);
                    status.textContent = "Saved. Your public enquiry page and WhatsApp follow-up are updated.";
                    await loadMerchantInbox();
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            async function exportMerchantCsv() {{
                const status = document.getElementById("merchantStatus");
                status.textContent = "Preparing CSV export...";
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
                    status.textContent = "CSV export ready.";
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
            function merchantQuery(limit = 100) {{
                const params = new URLSearchParams({{ business_slug: businessSlug, limit: String(limit) }});
                const status = document.getElementById("filterStatus")?.value;
                const priority = document.getElementById("filterPriority")?.value;
                const intent = document.getElementById("filterIntent")?.value;
                const followUp = document.getElementById("filterFollowUp")?.value;
                const search = document.getElementById("filterSearch")?.value;
                if (status) params.set("status", status);
                if (priority) params.set("priority", priority);
                if (intent) params.set("intent", intent);
                if (followUp) params.set("follow_up", followUp);
                if (search) params.set("search", search);
                return params.toString();
            }}
            function clearMerchantFilters() {{
                document.getElementById("filterStatus").value = "";
                document.getElementById("filterPriority").value = "";
                document.getElementById("filterIntent").value = "";
                document.getElementById("filterFollowUp").value = "";
                document.getElementById("filterSearch").value = "";
                loadMerchantInbox();
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
                    document.getElementById("merchantStatus").textContent = "Lead details saved.";
                    await loadMerchantInbox();
                }} catch (error) {{
                    document.getElementById("merchantStatus").textContent = error.message;
                }}
            }}
            async function deleteMerchantLead(id) {{
                if (!confirm("Delete this enquiry? This removes the lead from this inbox.")) return;
                try {{
                    await merchantApi(`/apps/enquiry/api/merchant/enquiries/${{id}}?business_slug=${{businessSlug}}`, {{
                        method: "DELETE"
                    }});
                    document.getElementById("merchantStatus").textContent = "Lead deleted.";
                    await loadMerchantInbox();
                }} catch (error) {{
                    document.getElementById("merchantStatus").textContent = error.message;
                }}
            }}
            async function loadMerchantInbox() {{
                const status = document.getElementById("merchantStatus");
                status.textContent = "Loading enquiries...";
                try {{
                    const data = await merchantApi(`/apps/enquiry/api/merchant/enquiries?${{merchantQuery(100)}}`);
                    fillMerchantSettings(data.business);
                    renderShareLinks(data.business);
                    const stats = data.stats || {{}};
                    document.getElementById("merchantStats").innerHTML = `
                        <section class="card"><h3>Total</h3><div class="price">${{stats.total || 0}}</div></section>
                        <section class="card"><h3>Hot</h3><div class="price">${{(stats.by_priority || {{}}).hot || 0}}</div></section>
                        <section class="card"><h3>Pipeline Value</h3><div class="price">${{Number(stats.pipeline_value || 0).toLocaleString()}}</div></section>
                        <section class="card"><h3>Due Follow-ups</h3><div class="price">${{stats.due_followups || 0}}</div></section>
                    `;
                    document.getElementById("merchantRows").innerHTML = data.enquiries.map(item => `
                        <tr>
                            <td>${{escapeHtml(item.created_at)}}</td>
                            <td>${{escapeHtml(item.name)}}<br>${{escapeHtml(item.phone)}}<br>${{escapeHtml(item.email || "")}}</td>
                            <td>${{escapeHtml(item.intent)}}</td>
                            <td>${{escapeHtml(item.priority)}}</td>
                            <td>${{escapeHtml(item.message)}}</td>
                            <td>${{escapeHtml(item.reply_draft)}}</td>
                            <td><input id="follow-up-${{item.id}}" type="date" value="${{escapeHtml(item.follow_up_at || "")}}"></td>
                            <td><input id="deal-value-${{item.id}}" type="number" min="0" step="0.01" value="${{item.deal_value ?? ""}}" placeholder="0"></td>
                            <td>
                                <textarea id="note-${{item.id}}" placeholder="Internal follow-up note">${{escapeHtml(item.internal_note || "")}}</textarea>
                                <button class="btn secondary" onclick="saveMerchantNote(${{item.id}})">Save Details</button>
                            </td>
                            <td>${{escapeHtml(item.status)}}</td>
                            <td>
                                ${{item.whatsapp_url ? `<a class="btn secondary" target="_blank" href="${{escapeHtml(item.whatsapp_url)}}">WhatsApp</a>` : ""}}
                                <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'contacted')">Contacted</button>
                                <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'quoted')">Quoted</button>
                                <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'won')">Won</button>
                                <button class="btn secondary" onclick="setMerchantStatus(${{item.id}}, 'lost')">Lost</button>
                                <button class="btn secondary" onclick="deleteMerchantLead(${{item.id}})">Delete</button>
                            </td>
                        </tr>
                    `).join("");
                    status.textContent = "Loaded.";
                }} catch (error) {{
                    status.textContent = error.message;
                }}
            }}
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
  iframe.src = config.formUrl;
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
                    <thead><tr><th>Business</th><th>Slug</th><th>Type</th><th>Status</th><th>Key Prefix</th><th>Links</th></tr></thead>
                    <tbody id="profileRows"></tbody>
                </table>
            </div>
        </section>
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
                <tr><th>Time</th><th>Lead</th><th>Intent</th><th>Priority</th><th>Message</th><th>Draft</th><th>Status</th><th>Action</th></tr>
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
            async function adminApi(path, options = {}) {
                const adminKey = document.getElementById("adminKey").value;
                const headers = { "X-Admin-Key": adminKey, ...(options.headers || {}) };
                const response = await fetch(path, { ...options, headers });
                if (!response.ok) {
                    throw new Error(await response.text());
                }
                return response.json();
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
            async function loadInbox() {
                const status = document.getElementById("inboxStatus");
                status.textContent = "Loading enquiries...";
                try {
                    const data = await adminApi("/apps/enquiry/api/enquiries?limit=50");
                    const stats = data.stats || {};
                    document.getElementById("inboxStats").innerHTML = `
                        <section class="card"><h3>Total</h3><div class="price">${stats.total || 0}</div></section>
                        <section class="card"><h3>Hot</h3><div class="price">${(stats.by_priority || {}).hot || 0}</div></section>
                        <section class="card"><h3>New</h3><div class="price">${(stats.by_status || {}).new || 0}</div></section>
                    `;
                    document.getElementById("enquiryRows").innerHTML = data.enquiries.map(item => `
                        <tr>
                            <td>${escapeHtml(item.created_at)}</td>
                            <td>${escapeHtml(item.name)}<br>${escapeHtml(item.phone)}<br>${escapeHtml(item.email || "")}</td>
                            <td>${escapeHtml(item.intent)}</td>
                            <td>${escapeHtml(item.priority)}</td>
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
            async function loadAdmin() {
                const adminKey = document.getElementById("adminKey").value;
                const status = document.getElementById("status");
                status.textContent = "Loading...";
                try {
                    const [clients, stats, report, actions, events, notifications, checks, backups] = await Promise.all([
                        api("/admin/clients", adminKey),
                        api("/admin/usage-stats", adminKey),
                        api("/admin/revenue-report", adminKey),
                        api("/admin/action-items", adminKey),
                        api("/admin/payment-events?limit=12", adminKey),
                        api("/admin/notifications?limit=12", adminKey),
                        api("/admin/deploy-check", adminKey),
                        api("/admin/backups", adminKey)
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
                    document.getElementById("clients").innerHTML = clients.clients.map(client => `
                        <tr>
                            <td>${client.client_id}</td>
                            <td>${client.plan}</td>
                            <td>${client.credits}</td>
                            <td>${client.status}</td>
                            <td>${client.billing_email || ""}</td>
                            <td>${client.api_key_prefix}</td>
                            <td><button class="btn secondary" onclick="resendApiKey('${client.client_id}')">Resend Key</button></td>
                        </tr>
                    `).join("");
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
                            <td><a class="btn secondary" href="/admin/backups/${encodeURIComponent(backup.name)}?admin_key=${encodeURIComponent(adminKey)}">Download</a></td>
                        </tr>
                    `).join("");
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

    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.sqlite3",
    )


@app.post("/webhooks/payment")
def payment_webhook(
    req: PaymentWebhookRequest,
    x_webhook_secret: str | None = Header(default=None),
):
    expected_secret = os.getenv("PAYMENT_WEBHOOK_SECRET")

    if not expected_secret:
        raise HTTPException(status_code=503, detail="Server missing PAYMENT_WEBHOOK_SECRET")

    if x_webhook_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    return process_payment_event(req)


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


@app.post("/apps/enquiry/api/enquiries")
def create_enquiry(req: EnquiryCreateRequest):
    return public_enquiry_response(create_enquiry_record(req))


@app.post("/apps/enquiry/api/business-profiles")
def create_or_update_business_profile(
    req: BusinessProfileRequest,
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return upsert_business_profile(req)


@app.get("/apps/enquiry/api/business-profiles")
def get_business_profiles(
    admin_key: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    admin_guard(admin_key, x_admin_key)
    return {
        "profiles": list_business_profiles(),
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
    return {
        "slug": profile["slug"],
        "business_name": profile["business_name"],
        "business_type": profile["business_type"],
        "offer_summary": profile["offer_summary"],
        "opening_hours": profile["opening_hours"],
        "form_url": profile["form_url"],
    }


@app.get("/apps/enquiry/api/enquiries")
def list_enquiries(
    status: str | None = Query(default=None, pattern="^(new|contacted|quoted|won|lost|spam)$"),
    business_slug: str | None = None,
    priority: str | None = Query(default=None, pattern="^(hot|warm|normal)$"),
    intent: str | None = Query(default=None, pattern="^(quotation|booking|inventory|general)$"),
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
    return {
        "business": profile,
        "stats": enquiry_stats(business_slug=profile["slug"]),
        "enquiries": list_enquiry_records(
            status=status,
            business_slug=profile["slug"],
            priority=priority,
            intent=intent,
            follow_up=follow_up,
            search=search,
            limit=limit,
        ),
    }


@app.get("/apps/enquiry/api/merchant/enquiries/export.csv")
def export_merchant_enquiries_csv(
    business_slug: str,
    status: str | None = Query(default=None, pattern="^(new|contacted|quoted|won|lost|spam)$"),
    priority: str | None = Query(default=None, pattern="^(hot|warm|normal)$"),
    intent: str | None = Query(default=None, pattern="^(quotation|booking|inventory|general)$"),
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
        follow_up=follow_up,
        search=search,
        limit=limit,
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
            "SELECT id, business_slug FROM enquiries WHERE id = ?",
            (enquiry_id,),
        ).fetchone()
        if not row or row["business_slug"] != profile["slug"]:
            raise HTTPException(status_code=404, detail="Enquiry not found")
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
