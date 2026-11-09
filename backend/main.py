import json
import gzip
import hashlib
import hmac
import os
import secrets
import subprocess
import tarfile
import tempfile
import threading
import contextvars
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "backup-engine" / "backup.py"
load_dotenv(ROOT / "backup-engine" / ".env")
load_dotenv(ROOT / "backend" / ".env", override=False)
CONFIG_PATH = Path(os.getenv("CLOUDRECOVER_CONFIG", ROOT / "backend" / "cloudrecover.json"))
DEFAULT_CONFIG = {
    "source_type": "database",
    "database": {"container": os.getenv("CONTAINER_NAME", "cloudrecover-mysql"), "name": os.getenv("DB_NAME", "student_db"), "user": os.getenv("DB_USER", "backup_user")},
    "website": {"site_id": "my-website", "host": "", "port": 22, "username": "", "remote_path": "/var/www/html", "private_key_path": ""},
    "automation": {"enabled": False, "interval_hours": 24, "last_run": None, "next_run": None, "last_result": "Not scheduled"},
    "history": [],
}
lock = threading.RLock()
stop_event = threading.Event()
worker = None
request_user = contextvars.ContextVar("cloudrecover_user", default=None)
SESSION_COOKIE = "cloudrecover_session"
SESSION_TTL_SECONDS = 12 * 60 * 60


def session_secret():
    secret = os.getenv("CLOUDRECOVER_SESSION_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Set CLOUDRECOVER_SESSION_SECRET before enabling login.")
    return secret.encode()


def make_session(role, client_id=None):
    payload = json.dumps({"role": role, "client_id": client_id, "expires": int(datetime.now(timezone.utc).timestamp()) + SESSION_TTL_SECONDS}, separators=(",", ":")).encode()
    encoded = secrets.token_urlsafe(32) + "." + base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(session_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(session_secret(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = encoded.split(".", 1)[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data["expires"] < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("expired")
        if data["role"] == "client" and not any(c["id"] == data.get("client_id") for c in load_config()["clients"]):
            raise ValueError("client")
        return data
    except Exception:
        raise HTTPException(status_code=401, detail="Please sign in.")


def require_admin(request: Request):
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Central administration access required.")
    return user


def current_user_from_context():
    user = request_user.get()
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return user


def require_admin_from_context():
    user = current_user_from_context()
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Central administration access required.")
    return user


def scoped_client(config, user):
    return get_client(config, user["client_id"] if user["role"] == "client" else config["selected_client_id"])


def load_config():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if "clients" not in data:
        legacy_id = "local-database"
        data["clients"] = [{
            "id": legacy_id,
            "name": "Local database",
            "website_url": "",
            "source_type": data.get("source_type", DEFAULT_CONFIG["source_type"]),
            "database": data.get("database", DEFAULT_CONFIG["database"]),
            "website": data.get("website", DEFAULT_CONFIG["website"]),
            "automation": data.get("automation", DEFAULT_CONFIG["automation"]),
            "history": data.get("history", []),
            "mongo": {"database": "", "uri_encrypted": ""},
        }]
        data["selected_client_id"] = legacy_id
    data.setdefault("clients", [])
    if not data["clients"]:
        data["clients"] = [{"id": "local-database", "name": "Local database", "website_url": "", "source_type": "database", "database": DEFAULT_CONFIG["database"], "website": DEFAULT_CONFIG["website"], "automation": DEFAULT_CONFIG["automation"], "history": [], "mongo": {"database": "", "uri_encrypted": ""}}]
    data.setdefault("selected_client_id", data["clients"][0]["id"])
    merged = {**DEFAULT_CONFIG, **data}
    merged["database"] = {**DEFAULT_CONFIG["database"], **data.get("database", {})}
    merged["website"] = {**DEFAULT_CONFIG["website"], **data.get("website", {})}
    merged["source_type"] = data.get("source_type", DEFAULT_CONFIG["source_type"])
    merged["automation"] = {**DEFAULT_CONFIG["automation"], **data.get("automation", {})}
    merged["history"] = data.get("history", [])
    merged["restores"] = data.get("restores", [])
    merged["clients"] = data["clients"]
    if not any(client["id"] == merged["selected_client_id"] for client in merged["clients"]):
        merged["selected_client_id"] = merged["clients"][0]["id"]
    return merged


def get_client(config, client_id=None):
    client_id = client_id or config["selected_client_id"]
    client = next((item for item in config["clients"] if item["id"] == client_id), None)
    if not client:
        raise HTTPException(status_code=404, detail="Client website not found.")
    return client


def encryption_fernet():
    key = os.getenv("CLOUDRECOVER_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("Set CLOUDRECOVER_ENCRYPTION_KEY before saving MongoDB credentials.")
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError("CLOUDRECOVER_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temp_path.replace(CONFIG_PATH)


def perform_backup(source="manual", client_id=None):
    with lock:
        config = load_config()
        client = get_client(config, client_id)
        if client["source_type"] == "website":
            entries = []
            if client.get("website", {}).get("host"):
                entries.append(perform_website_backup(client, source))
            if client.get("mongo", {}).get("uri_encrypted"):
                entries.append(perform_mongodb_backup(client, source))
            if not entries:
                raise RuntimeError("Configure website SFTP files or a MongoDB database for this client first.")
            return entries
        if client["database"].get("type") == "mongodb":
            return perform_mongodb_backup(client, source)
        database = client["database"]
        env = os.environ.copy()
        env["CONTAINER_NAME"] = database.get("container", DEFAULT_CONFIG["database"]["container"])
        env["DB_NAME"] = database["name"]
        env["DB_USER"] = database.get("user", DEFAULT_CONFIG["database"]["user"])
        if client["id"] != "local-database":
            prefix = os.getenv("S3_PREFIX", "cloudrecover/backups").strip("/")
            env["S3_PREFIX"] = f"{prefix + '/' if prefix else ''}clients/{client['id']}"
        command = [os.getenv("CLOUDRECOVER_PYTHON", "python"), str(ENGINE)]
        started = datetime.now(timezone.utc)
        result = subprocess.run(command, cwd=ENGINE.parent, env=env, capture_output=True, text=True, timeout=3600)
        finished = datetime.now(timezone.utc)
        output = result.stdout if result.returncode == 0 else result.stderr or result.stdout
        entry = {"id": finished.strftime("%Y%m%dT%H%M%S%fZ"), "client_id": client["id"], "source_type": "mysql", "source": source, "started_at": started.isoformat(), "finished_at": finished.isoformat(), "status": "success" if result.returncode == 0 else "failed", "detail": output[-3000:]}
        if result.returncode == 0:
            for line in output.splitlines():
                if line.startswith("S3 location: s3://"):
                    entry["s3_key"] = line.removeprefix(f"S3 location: s3://{os.getenv('S3_BUCKET', '')}/")
                elif line.startswith("Compressed size: "):
                    entry["size"] = line.removeprefix("Compressed size: ")
                elif line.startswith("SHA-256: "):
                    entry["checksum"] = line.removeprefix("SHA-256: ")
        entry["client_id"] = client["id"]
        client["history"].insert(0, entry)
        client["history"] = client["history"][:50]
        if source == "automated":
            automation = client["automation"]
            automation["last_run"] = finished.isoformat()
            automation["last_result"] = entry["status"].title()
            automation["next_run"] = (finished.timestamp() + automation["interval_hours"] * 3600)
            automation["next_run"] = datetime.fromtimestamp(automation["next_run"], timezone.utc).isoformat()
        save_config(config)
        return entry


def perform_website_backup(client, source):
    site = client["website"]
    if not site.get("host") or not site.get("username") or not site.get("remote_path") or not site.get("private_key_path"):
        raise RuntimeError("Configure the website SFTP host, username, remote path, and private key file first.")
    try:
        import paramiko
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install backend requirements to use website SFTP backups (paramiko and boto3).") from exc
    key_path = Path(site["private_key_path"]).expanduser()
    if not key_path.is_file():
        raise RuntimeError("The configured SFTP private key file was not found on the CloudRecover host.")
    started = datetime.now(timezone.utc)
    timestamp = started.strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix="cloudrecover-site-") as folder:
        work = Path(folder)
        site_files = work / "site"
        site_files.mkdir()
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            ssh.connect(hostname=site["host"], port=int(site["port"]), username=site["username"], key_filename=str(key_path), look_for_keys=False, allow_agent=False, timeout=15)
            sftp = ssh.open_sftp()
            def fetch(remote_dir, local_dir):
                for entry in sftp.listdir_attr(remote_dir):
                    if entry.filename in {".", ".."}:
                        continue
                    remote_item = f"{remote_dir.rstrip('/')}/{entry.filename}"
                    local_item = local_dir / entry.filename
                    mode = entry.st_mode
                    if mode is not None and mode & 0o170000 == 0o040000:
                        local_item.mkdir(exist_ok=True)
                        fetch(remote_item, local_item)
                    elif mode is None or mode & 0o170000 == 0o100000:
                        local_item.parent.mkdir(parents=True, exist_ok=True)
                        sftp.get(remote_item, str(local_item))
            fetch(site["remote_path"], site_files)
            sftp.close()
        finally:
            ssh.close()
        archive = work / "website.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(site_files, arcname="site")
        digest_state = hashlib.sha256()
        with archive.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest_state.update(chunk)
        digest = digest_state.hexdigest()
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            raise RuntimeError("S3_BUCKET is not configured on the backup service.")
        region = os.getenv("AWS_REGION", "ap-southeast-2")
        prefix = os.getenv("S3_PREFIX", "cloudrecover/backups").strip("/")
        object_key = f"{prefix + '/' if prefix else ''}clients/{client['id']}/website_{timestamp}.tar.gz"
        s3client = boto3.client("s3", region_name=region)
        s3client.upload_file(str(archive), bucket, object_key, ExtraArgs={"ServerSideEncryption": "AES256"})
        s3client.put_object(Bucket=bucket, Key=object_key + ".sha256", Body=f"{digest}  {object_key}\n".encode(), ServerSideEncryption="AES256")
        finished = datetime.now(timezone.utc)
        entry = {"id": finished.strftime("%Y%m%dT%H%M%S%fZ"), "client_id": client["id"], "source": source, "source_type": "website-files", "site_id": client["id"], "started_at": started.isoformat(), "finished_at": finished.isoformat(), "status": "success", "s3_key": object_key, "size": str(archive.stat().st_size), "checksum": digest, "detail": f"Website files backed up from {site['host']}:{site['remote_path']}"}
    config = load_config()
    stored_client = get_client(config, client["id"])
    stored_client["history"].insert(0, entry)
    stored_client["history"] = stored_client["history"][:50]
    if source == "automated":
        automation = stored_client["automation"]
        automation["last_run"] = finished.isoformat()
        automation["last_result"] = "Success"
        automation["next_run"] = datetime.fromtimestamp(finished.timestamp() + automation["interval_hours"] * 3600, timezone.utc).isoformat()
    save_config(config)
    return entry


def perform_mongodb_backup(client, source):
    mongo = client.get("mongo", {})
    encrypted_uri = mongo.get("uri_encrypted", "")
    if not encrypted_uri:
        raise RuntimeError("Configure the MongoDB connection URI for this client first.")
    try:
        import boto3
        uri = encryption_fernet().decrypt(encrypted_uri.encode()).decode()
    except Exception as exc:
        raise RuntimeError(f"Could not load the encrypted MongoDB connection. {str(exc)[:250]}") from exc
    started = datetime.now(timezone.utc)
    timestamp = started.strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix="cloudrecover-mongo-") as folder:
        work = Path(folder)
        archive = work / "mongodb.archive.gz"
        config_file = work / "mongodump.yaml"
        # mongodump supports a config file for sensitive URI values; keep it only in a temporary directory.
        config_file.write_text("uri: " + json.dumps(uri) + "\n", encoding="utf-8")
        try:
            result = subprocess.run([os.getenv("MONGODUMP_BIN", "mongodump"), "--config", str(config_file), "--db", mongo["database"], "--archive", str(archive), "--gzip"], capture_output=True, text=True, timeout=3600)
        except FileNotFoundError as exc:
            raise RuntimeError("mongodump is not installed or is not available on the CloudRecover host PATH.") from exc
        finally:
            config_file.unlink(missing_ok=True)
        if result.returncode:
            safe_error = (result.stderr or result.stdout).replace(uri, "[MongoDB URI redacted]")
            raise RuntimeError(f"MongoDB dump failed: {safe_error[-1200:]}")
        digest_state = hashlib.sha256()
        with archive.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest_state.update(chunk)
        digest = digest_state.hexdigest()
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            raise RuntimeError("S3_BUCKET is not configured on the backup service.")
        prefix = os.getenv("S3_PREFIX", "cloudrecover/backups").strip("/")
        object_key = f"{prefix + '/' if prefix else ''}clients/{client['id']}/mongodb_{timestamp}.archive.gz"
        s3client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-southeast-2"))
        s3client.upload_file(str(archive), bucket, object_key, ExtraArgs={"ServerSideEncryption": "AES256"})
        s3client.put_object(Bucket=bucket, Key=object_key + ".sha256", Body=f"{digest}  {object_key}\n".encode(), ServerSideEncryption="AES256")
        finished = datetime.now(timezone.utc)
        entry = {"id": finished.strftime("%Y%m%dT%H%M%S%fZ"), "client_id": client["id"], "source": source, "source_type": "mongodb", "started_at": started.isoformat(), "finished_at": finished.isoformat(), "status": "success", "s3_key": object_key, "size": str(archive.stat().st_size), "checksum": digest, "detail": f"MongoDB database {mongo['database']} backed up"}
    config = load_config()
    stored_client = get_client(config, client["id"])
    stored_client["history"].insert(0, entry)
    stored_client["history"] = stored_client["history"][:50]
    if source == "automated":
        automation = stored_client["automation"]
        automation["last_run"] = finished.isoformat()
        automation["last_result"] = "Success"
        automation["next_run"] = datetime.fromtimestamp(finished.timestamp() + automation["interval_hours"] * 3600, timezone.utc).isoformat()
    save_config(config)
    return entry


def scheduler_loop():
    while not stop_event.wait(10):
        with lock:
            config = load_config()
            due_ids = [client["id"] for client in config["clients"] if client.get("automation", {}).get("enabled") and (not client["automation"].get("next_run") or datetime.fromisoformat(client["automation"]["next_run"]) <= datetime.now(timezone.utc))]
        for client_id in due_ids:
            try:
                perform_backup("automated", client_id)
            except Exception as exc:
                with lock:
                    config = load_config()
                    client = get_client(config, client_id)
                    failed_at = datetime.now(timezone.utc)
                    automation = client["automation"]
                    automation["last_run"] = failed_at.isoformat()
                    automation["last_result"] = f"Failed: {str(exc)[:180]}"
                    automation["next_run"] = datetime.fromtimestamp(failed_at.timestamp() + automation["interval_hours"] * 3600, timezone.utc).isoformat()
                    client["history"].insert(0, {"id": failed_at.strftime("%Y%m%dT%H%M%S%fZ"), "client_id": client_id, "source": "automated", "finished_at": failed_at.isoformat(), "status": "failed", "detail": str(exc)[:1000]})
                    client["history"] = client["history"][:50]
                    save_config(config)


@asynccontextmanager
async def lifespan(app):
    global worker
    stop_event.clear()
    worker = threading.Thread(target=scheduler_loop, daemon=True, name="cloudrecover-scheduler")
    worker.start()
    yield
    stop_event.set()
    if worker:
        worker.join(timeout=2)


app = FastAPI(title="CloudRecover API", description="Database backup and recovery platform", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def authenticate_api(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in {"/api/login", "/api/logout", "/api/session"}:
        try:
            user = current_user(request)
        except HTTPException as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        token = request_user.set(user)
        try:
            return await call_next(request)
        finally:
            request_user.reset(token)
    return await call_next(request)


class AutomationUpdate(BaseModel):
    enabled: bool
    interval_hours: int = Field(default=24, ge=1, le=720)


class DatabaseUpdate(BaseModel):
    container: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_$-]+$")
    user: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.@-]+$")


class WebsiteUpdate(BaseModel):
    site_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.@-]+$")
    remote_path: str = Field(min_length=1, max_length=512)
    private_key_path: str = Field(default="", max_length=1024)


class SourceUpdate(BaseModel):
    source_type: str = Field(pattern=r"^(database|website)$")


class ClientCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    website_url: str = Field(default="", max_length=2048)
    database_type: str = Field(default="mongodb", pattern=r"^(mongodb|mysql-container|none)$")
    database_name: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_$-]*$")
    mongodb_uri: str = Field(default="", max_length=4096)
    sftp_host: str = Field(default="", max_length=253, pattern=r"^[A-Za-z0-9.-]*$")
    sftp_port: int = Field(default=22, ge=1, le=65535)
    sftp_username: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_.@-]*$")
    sftp_path: str = Field(default="", max_length=512)
    sftp_key_path: str = Field(default="", max_length=1024)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


def public_client(client):
    return {
        "id": client["id"], "name": client["name"], "website_url": client.get("website_url", ""),
        "source_type": client.get("source_type", "website"),
        "database_type": client.get("database", {}).get("type", "mongodb" if client.get("mongo", {}).get("uri_encrypted") else "mysql-container" if client.get("source_type") == "database" else "none"),
        "database_name": client.get("mongo", {}).get("database", client.get("database", {}).get("name", "")),
        "mongodb_configured": bool(client.get("mongo", {}).get("uri_encrypted")),
        "sftp_configured": bool(client.get("website", {}).get("host")),
        "sftp_host": client.get("website", {}).get("host", ""),
        "automation": client.get("automation", DEFAULT_CONFIG["automation"]),
        "history": client.get("history", [])[:10],
    }


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse((Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"))


@app.post("/api/login")
def login(credentials: LoginInput, response: Response):
    username = credentials.username.strip()
    password = credentials.password
    if username.casefold() in {"central administration", "central-admin", "admin", "ca"} and hmac.compare_digest(password, os.getenv("CLOUDRECOVER_ADMIN_PASSWORD", "ca$123")):
        role, client_id, display_name = "admin", None, "Central administration"
    else:
        config = load_config()
        client = next((item for item in config["clients"] if item["name"].casefold() == username.casefold()), None)
        if not client or not hmac.compare_digest(password, client["name"] + "$123"):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        role, client_id, display_name = "client", client["id"], client["name"]
    response.set_cookie(SESSION_COOKIE, make_session(role, client_id), httponly=True, secure=os.getenv("CLOUDRECOVER_COOKIE_SECURE", "false").lower() == "true", samesite="strict", max_age=SESSION_TTL_SECONDS, path="/")
    return {"role": role, "name": display_name}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed out"}


@app.get("/api/session")
def session(request: Request):
    user = current_user(request)
    client = get_client(load_config(), user["client_id"]) if user["role"] == "client" else None
    return {"role": user["role"], "name": client["name"] if client else "Central administration"}


@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
def status():
    config = load_config()
    user = current_user_from_context()
    client = scoped_client(config, user)
    last_success = next((item for item in client.get("history", []) if item["status"] == "success"), None)
    is_admin = user["role"] == "admin"
    visible_history = ([{**entry, "client_name": item["name"]} for item in config["clients"] for entry in item.get("history", [])] if is_admin else client.get("history", []))
    visible_history.sort(key=lambda item: item.get("finished_at", item.get("started_at", "")), reverse=True)
    return {"role": user["role"], "client": public_client(client), "clients": [public_client(item) for item in config["clients"]] if is_admin else [public_client(client)], "selected_client_id": client["id"], "source_type": client["source_type"], "database": client.get("database", {}), "website": {key: value for key, value in client.get("website", {}).items() if key != "private_key_path"}, "website_private_key_configured": bool(client.get("website", {}).get("private_key_path")), "mongo": {"database": client.get("mongo", {}).get("database", ""), "configured": bool(client.get("mongo", {}).get("uri_encrypted"))}, "automation": client["automation"], "history": visible_history[:50], "last_success": last_success, "bucket": os.getenv("S3_BUCKET", "Configured in backup service environment"), "region": os.getenv("AWS_REGION", "ap-southeast-2")}


@app.get("/api/clients")
def list_clients():
    config = load_config()
    user = current_user_from_context()
    if user["role"] == "admin":
        return {"clients": [public_client(client) for client in config["clients"]], "selected_client_id": config["selected_client_id"]}
    client = get_client(config, user["client_id"])
    return {"clients": [public_client(client)], "selected_client_id": client["id"]}


@app.post("/api/clients", status_code=201)
def create_client(client_input: ClientCreate):
    require_admin_from_context()
    if client_input.database_type == "none" and not client_input.sftp_host:
        raise HTTPException(status_code=422, detail="Add an SFTP website source or choose a database to protect.")
    if client_input.database_type == "mysql-container" and client_input.sftp_host:
        raise HTTPException(status_code=422, detail="This local MySQL profile cannot be combined with remote SFTP files yet. Use MongoDB for a website database profile.")
    if client_input.database_type == "mongodb" and not client_input.mongodb_uri:
        raise HTTPException(status_code=422, detail="MongoDB connection URI is required when MongoDB is selected.")
    if client_input.mongodb_uri and not client_input.mongodb_uri.startswith(("mongodb://", "mongodb+srv://")):
        raise HTTPException(status_code=422, detail="MongoDB URI must start with mongodb:// or mongodb+srv://.")
    if client_input.database_type == "mongodb" and not client_input.database_name:
        raise HTTPException(status_code=422, detail="MongoDB database name is required.")
    if client_input.sftp_host and not (client_input.sftp_username and client_input.sftp_path and client_input.sftp_key_path):
        raise HTTPException(status_code=422, detail="SFTP host, username, remote path, and private key path are all required to back up website files.")
    if ".." in Path(client_input.sftp_path).parts or "\x00" in client_input.sftp_path:
        raise HTTPException(status_code=422, detail="SFTP path must not contain parent-directory segments.")
    try:
        encrypted_uri = encryption_fernet().encrypt(client_input.mongodb_uri.encode()).decode() if client_input.mongodb_uri else ""
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    with lock:
        config = load_config()
        base = "".join(ch.lower() if ch.isalnum() else "-" for ch in client_input.name).strip("-")[:48] or "client"
        client_id = base
        suffix = 2
        existing = {item["id"] for item in config["clients"]}
        while client_id in existing:
            client_id = f"{base}-{suffix}"
            suffix += 1
        profile = {
            "id": client_id, "name": client_input.name, "website_url": client_input.website_url,
            "source_type": "website" if client_input.database_type in {"mongodb", "none"} else "database",
            "database": {**DEFAULT_CONFIG["database"], "type": client_input.database_type, "name": client_input.database_name or DEFAULT_CONFIG["database"]["name"]},
            "website": {"site_id": client_id, "host": client_input.sftp_host, "port": client_input.sftp_port, "username": client_input.sftp_username, "remote_path": client_input.sftp_path or "/var/www/html", "private_key_path": client_input.sftp_key_path},
            "mongo": {"database": client_input.database_name, "uri_encrypted": encrypted_uri},
            "automation": {**DEFAULT_CONFIG["automation"]}, "history": [],
        }
        config["clients"].append(profile)
        config["selected_client_id"] = client_id
        save_config(config)
    return {"client": public_client(profile)}


@app.put("/api/clients/{client_id}/select")
def select_client(client_id: str):
    require_admin_from_context()
    with lock:
        config = load_config()
        get_client(config, client_id)
        config["selected_client_id"] = client_id
        save_config(config)
    return {"selected_client_id": client_id}


@app.put("/api/automation")
def update_automation(update: AutomationUpdate):
    user = current_user_from_context()
    with lock:
        config = load_config()
        automation = scoped_client(config, user)["automation"]
        automation["enabled"] = update.enabled
        automation["interval_hours"] = update.interval_hours
        automation["next_run"] = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + update.interval_hours * 3600, timezone.utc).isoformat() if update.enabled else None
        automation["last_result"] = "Scheduled" if update.enabled else "Paused"
        save_config(config)
    return {"automation": automation}


@app.put("/api/database")
def update_database(update: DatabaseUpdate):
    user = current_user_from_context()
    with lock:
        config = load_config()
        scoped_client(config, user)["database"] = update.model_dump()
        save_config(config)
    return {"database": scoped_client(config, user)["database"]}


@app.put("/api/website")
def update_website(update: WebsiteUpdate):
    user = current_user_from_context()
    path_parts = Path(update.remote_path).parts
    if ".." in path_parts or "\x00" in update.remote_path:
        raise HTTPException(status_code=422, detail="Remote website path must not contain parent-directory segments.")
    with lock:
        config = load_config()
        new_website = update.model_dump()
        website = scoped_client(config, user)["website"]
        if not new_website["private_key_path"]:
            new_website["private_key_path"] = website.get("private_key_path", "")
        scoped_client(config, user)["website"] = new_website
        save_config(config)
    return {"website": {key: value for key, value in scoped_client(config, user)["website"].items() if key != "private_key_path"}, "private_key_configured": True}


@app.put("/api/source")
def update_source(update: SourceUpdate):
    user = current_user_from_context()
    with lock:
        config = load_config()
        scoped_client(config, user)["source_type"] = update.source_type
        save_config(config)
    return {"source_type": scoped_client(config, user)["source_type"]}


@app.post("/api/backups", status_code=202)
def trigger_backup():
    user = current_user_from_context()
    config = load_config()
    return start_backup(scoped_client(config, user)["id"])


@app.post("/api/clients/{client_id}/backups", status_code=202)
def trigger_client_backup(client_id: str):
    user = current_user_from_context()
    if user["role"] != "admin" and user["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="You can only back up your own client data.")
    get_client(load_config(), client_id)
    return start_backup(client_id)


def start_backup(client_id):
    def run():
        try:
            perform_backup("manual", client_id)
        except Exception as exc:
            with lock:
                config = load_config()
                client = get_client(config, client_id)
                client["history"].insert(0, {"id": datetime.now(timezone.utc).isoformat(), "client_id": client_id, "source": "manual", "finished_at": datetime.now(timezone.utc).isoformat(), "status": "failed", "detail": str(exc)[:1000]})
                client["history"] = client["history"][:50]
                save_config(config)
    threading.Thread(target=run, daemon=True).start()
    return {"status": "started", "client_id": client_id}


@app.get("/api/history")
def history():
    config = load_config()
    user = current_user_from_context()
    if user["role"] == "admin":
        return {"backups": [{**entry, "client_name": client["name"]} for client in config["clients"] for entry in client.get("history", [])]}
    return {"backups": get_client(config, user["client_id"]).get("history", [])}


def latest_s3_backup(database_name):
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise HTTPException(status_code=503, detail="S3_BUCKET is not configured on the backup service.")
    prefix = os.getenv("S3_PREFIX", "cloudrecover/backups").strip("/")
    object_prefix = f"{prefix}/{database_name}/" if prefix else f"{database_name}/"
    try:
        import boto3
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="boto3 is required for S3 recovery. Install the backend requirements.") from exc
    client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-southeast-2"))
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=object_prefix)
    objects = [item for page in pages for item in page.get("Contents", []) if item["Key"].endswith(".sql.gz")]
    if not objects:
        raise HTTPException(status_code=404, detail="No SQL backup was found for this database.")
    return bucket, client, max(objects, key=lambda item: item["LastModified"])["Key"]


def restore_latest_to_new_container(restore_id):
    config = load_config()
    client = get_client(config)
    if client["id"] != "local-database" or client.get("source_type") != "database" or client.get("database", {}).get("type") == "mongodb":
        raise HTTPException(status_code=400, detail="The local Docker recovery flow currently supports database sources only. Website restoration needs a configured hosting destination.")
    db_name = client["database"]["name"]
    bucket, client, key = latest_s3_backup(db_name)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    container = f"cloudrecover-restore-{timestamp}"
    volume = f"{container}-data"
    # Give each isolated restore its own credential and persist it with the job
    # record so the operator can connect to the restored database later.
    password = secrets.token_urlsafe(24)
    with lock:
        config = load_config()
        record = next((item for item in config.get("restores", []) if item.get("id") == restore_id), None)
        if record:
            record.update({"container": container, "volume": volume, "database": db_name, "root_password": password})
            save_config(config)
    with tempfile.TemporaryDirectory(prefix="cloudrecover-restore-") as folder:
        compressed = Path(folder) / "backup.sql.gz"
        sql = Path(folder) / "backup.sql"
        checksum_key = key + ".sha256"
        try:
            sidecar = client.get_object(Bucket=bucket, Key=checksum_key)["Body"].read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise HTTPException(status_code=422, detail="The latest backup has no readable checksum sidecar; restore was stopped.") from exc
        expected = sidecar.split()[0] if sidecar.split() else ""
        client.download_file(bucket, key, str(compressed))
        digest = hashlib.sha256(compressed.read_bytes()).hexdigest()
        if not expected or not hmac.compare_digest(digest, expected):
            raise HTTPException(status_code=422, detail="Backup checksum verification failed; restore was stopped.")
        try:
            with gzip.open(compressed, "rb") as source, sql.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
        except (OSError, EOFError) as exc:
            raise HTTPException(status_code=422, detail="Backup could not be decompressed; restore was stopped.") from exc
        commands = [
            ["docker", "volume", "create", volume],
            ["docker", "run", "-d", "--name", container, "-e", f"MYSQL_ROOT_PASSWORD={password}", "-e", f"MYSQL_DATABASE={db_name}", "-v", f"{volume}:/var/lib/mysql", "mysql:8.4"],
        ]
        try:
            for command in commands:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
            ready = False
            for _ in range(60):
                ping = subprocess.run(["docker", "exec", container, "mysqladmin", "ping", "-h", "127.0.0.1", "-uroot", f"-p{password}", "--silent"], capture_output=True, text=True, timeout=10)
                if ping.returncode == 0:
                    ready = True
                    break
                threading.Event().wait(2)
            if not ready:
                raise RuntimeError("New MySQL container did not become ready within 120 seconds.")
            with sql.open("rb") as source:
                imported = subprocess.run(["docker", "exec", "-i", "-e", f"MYSQL_PWD={password}", container, "mysql", "-uroot", db_name], stdin=source, capture_output=True, text=True, timeout=1800)
            if imported.returncode:
                raise RuntimeError((imported.stderr or "SQL import failed")[-1200:])
            check = subprocess.run(["docker", "exec", container, "mysql", "-uroot", f"-p{password}", db_name, "-Nse", "SHOW TABLES"], capture_output=True, text=True, timeout=30)
            if check.returncode:
                raise RuntimeError((check.stderr or "Post-restore validation failed")[-1200:])
        except Exception as exc:
            # Preserve the new container and its volume so an operator can inspect a failed restore.
            raise HTTPException(status_code=500, detail=f"Restore failed for {container}; inspect it before cleanup. {str(exc)[:1200]}") from exc
    restored_at = datetime.now(timezone.utc).isoformat()
    return {"status": "success", "container": container, "volume": volume, "database": db_name, "root_password": password, "source_key": key, "restored_at": restored_at, "tables": [table for table in check.stdout.splitlines() if table]}


@app.post("/api/recovery/latest")
def recover_latest():
    """Restore the newest checksum-verified SQL backup into a fresh local Docker container."""
    require_admin_from_context()
    config = load_config()
    client = get_client(config)
    if client["id"] != "local-database" or client.get("source_type") != "database" or client.get("database", {}).get("type") == "mongodb":
        raise HTTPException(status_code=400, detail="One-click recovery is currently available only for the local MySQL source.")
    if not client["database"]["name"].replace("_", "").replace("$", "").isalnum():
        raise HTTPException(status_code=400, detail="Database name is not supported for this restore flow.")
    restore_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    with lock:
        config.setdefault("restores", []).insert(0, {"id": restore_id, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "database": client["database"]["name"], "client_id": client["id"]})
        save_config(config)

    def run_restore():
        try:
            result = restore_latest_to_new_container(restore_id)
        except Exception as exc:
            result = {"status": "failed", "detail": str(exc.detail if isinstance(exc, HTTPException) else exc)[:1500]}
        with lock:
            current = load_config()
            record = next((item for item in current.get("restores", []) if item.get("id") == restore_id), None)
            if record:
                record.update(result)
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                save_config(current)

    threading.Thread(target=run_restore, daemon=True, name=f"cloudrecover-restore-{restore_id}").start()
    return {"status": "started", "restore_id": restore_id}


@app.get("/api/recovery/history")
def recovery_history():
    require_admin_from_context()
    return {"restores": load_config().get("restores", [])}


@app.get("/api/recovery/status/{restore_id}")
def recovery_status(restore_id: str):
    require_admin_from_context()
    record = next((item for item in load_config().get("restores", []) if item.get("id") == restore_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="Restore job not found.")
    return record
