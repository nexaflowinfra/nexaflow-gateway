from datetime import datetime, timezone
from hashlib import sha256
from math import ceil
from pathlib import Path
from time import time
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from openai import APIStatusError, OpenAI
from pydantic import BaseModel, Field

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
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


def generate_client_id(email):
    safe_email = (email or "customer").split("@")[0].lower()
    safe_email = "".join(char if char.isalnum() else "_" for char in safe_email).strip("_")
    safe_email = safe_email[:40] or "customer"
    return f"{safe_email}_{secrets.token_hex(4)}"


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
        return {
            **public_client(client_id, client),
            "api_key": api_key,
            "created": True,
            "delivery": delivery,
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
    return {
        **public_client(client_id, client),
        "created": False,
        "credits_added": credits,
        "credits_applied": should_apply_credits,
        "delivery": {"status": "not_required"},
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
                input, select {{
                    width: 100%;
                    border: 1px solid var(--line);
                    border-radius: 6px;
                    padding: 10px;
                    font: inherit;
                }}
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
        "May 29, 2026",
        [
            (
                "Service",
                [
                    "NexaFlow AI Gateway provides hosted access to AI model routing, usage tracking, customer credits, and related account tools.",
                    "You are responsible for how your account, API keys, prompts, outputs, integrations, and downstream applications are used.",
                ],
            ),
            (
                "Accounts and API Keys",
                [
                    [
                        "Keep API keys secret and rotate them immediately if exposed.",
                        "You may not resell, share, or abuse access outside your authorized business use.",
                        "We may suspend accounts for non-payment, suspicious activity, policy violations, or security risk.",
                    ]
                ],
            ),
            (
                "Billing",
                [
                    "Plans include a monthly credit allowance. Credits are consumed based on estimated and actual token usage, including heavier weighting for model output tokens.",
                    "Payments are processed by third-party payment providers. NexaFlow does not store payment card data.",
                ],
            ),
            (
                "Availability and Changes",
                [
                    "The service depends on third-party infrastructure and AI model providers. We aim for reliable operation, but do not guarantee uninterrupted availability.",
                    "We may change model routes, limits, pricing, or provider availability to protect customers, maintain service quality, or preserve sustainable gross margin.",
                ],
            ),
            (
                "Liability",
                [
                    "AI outputs can be inaccurate or incomplete. Customers must review outputs before relying on them for business, legal, financial, medical, or safety-critical decisions.",
                    "To the maximum extent allowed by law, NexaFlow is not liable for indirect, incidental, special, consequential, or lost-profit damages.",
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
                        "Operational records such as webhook deliveries, customer notifications, backups, and admin actions.",
                    ]
                ],
            ),
            (
                "Why We Process Data",
                [
                    "We process data to provide the service, bill customers, prevent abuse, monitor reliability, send account notifications, diagnose incidents, and maintain required business records.",
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
    cards = "".join(plan_card(plan_key, plan) for plan_key, plan in PLANS.items())
    return base_html(
        "NexaFlow AI Gateway",
        f"""
        <section class="hero">
            <div>
                <h1>Multi-model AI API gateway for profitable token routing</h1>
                <p>NexaFlow routes each request across OpenAI and OpenRouter, tracks token cost, charges credits, and records gross margin for every customer.</p>
                <div class="actions">
                    <a class="btn" href="/pricing">View Plans</a>
                    <a class="btn secondary" href="/docs">Open API Docs</a>
                </div>
            </div>
            <div class="product-panel" aria-label="NexaFlow gateway metrics">
                <div class="panel-top"><span>Live Gateway</span><span>SQLite + Webhooks</span></div>
                <div class="metrics">
                    <div class="metric"><span>Providers</span><strong>2</strong><span>OpenAI, OpenRouter</span></div>
                    <div class="metric"><span>Routing</span><strong>Profit</strong><span>Fallback ready</span></div>
                    <div class="metric"><span>Billing</span><strong>Token</strong><span>Cost ledger</span></div>
                </div>
                <div class="flow">
                    <div class="flow-row"><span>OpenAI</span><div class="bar"><span style="width:82%"></span></div><span>economy</span></div>
                    <div class="flow-row"><span>OpenRouter</span><div class="bar"><span style="width:64%;background:var(--gold)"></span></div><span>fallback</span></div>
                    <div class="flow-row"><span>Margin</span><div class="bar"><span style="width:91%;background:var(--accent)"></span></div><span>tracked</span></div>
                </div>
            </div>
        </section>
        <h2>Plans</h2>
        <section class="grid">{cards}</section>
        """
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
        <section class="grid" id="billingLinks"></section>
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
                    status.textContent = `Loaded ${fmt.format(usage.totals.requests)} requests.`;
                } catch (error) {
                    status.textContent = error.message;
                    document.getElementById("summary").innerHTML = "";
                    document.getElementById("billingLinks").innerHTML = "";
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
                    const [clients, stats, report, events, notifications, checks, backups] = await Promise.all([
                        api("/admin/clients", adminKey),
                        api("/admin/usage-stats", adminKey),
                        api("/admin/revenue-report", adminKey),
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
    docs_url = f"{os.getenv('NEXAFLOW_SITE_URL', 'https://api.nexaflowinfra.com')}/docs"
    portal_url = f"{os.getenv('NEXAFLOW_SITE_URL', 'https://api.nexaflowinfra.com')}/portal"
    return base_html(
        "NexaFlow Payment Complete",
        f"""
        <section class="hero">
            <div>
                <h1>Payment received</h1>
                <p>Your {plan_name} plan is being activated. NexaFlow sends the API key to the email address used at checkout, then you can test everything from the customer portal.</p>
                <div class="actions">
                    <a class="btn" href="{portal_url}">Open Portal</a>
                    <a class="btn secondary" href="{docs_url}">API Docs</a>
                </div>
            </div>
            <div class="product-panel">
                <div class="panel-top"><span>Automatic Fulfillment</span><span>Stripe + Resend</span></div>
                <div class="flow">
                    <div class="flow-row"><span>Payment</span><div class="bar"><span style="width:100%"></span></div><span>done</span></div>
                    <div class="flow-row"><span>Account</span><div class="bar"><span style="width:100%;background:var(--gold)"></span></div><span>active</span></div>
                    <div class="flow-row"><span>Email</span><div class="bar"><span style="width:100%;background:var(--accent)"></span></div><span>sent</span></div>
                </div>
            </div>
        </section>
        <h2>Next Steps</h2>
        <section class="grid">
            <div class="card"><h3>Check Email</h3><p>Search for "NexaFlow AI Gateway API access" and check spam or promotions if needed.</p></div>
            <div class="card"><h3>Open Portal</h3><p>Paste your API key into the portal to view credits, usage, and billing links.</p></div>
            <div class="card"><h3>Send Test</h3><p>Use the portal test request to confirm model routing and credit tracking.</p></div>
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
    return {
        "client": public_client(client_id, client),
        "links": plan_billing_links(),
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
