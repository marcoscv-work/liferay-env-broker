#!/usr/bin/env python3
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("BROKER_CONFIG", BASE_DIR / "config.yaml"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


class CreateEnvironmentRequest(BaseModel):
    image: str = Field(..., description="Allowed Docker image")
    profile: str = Field(default="standard", description="Resource profile")
    name: Optional[str] = Field(default=None, description="Optional logical name")
    user: str = Field(..., description="Requesting user")
    portal_properties: Optional[str] = Field(default=None, description="Optional portal-ext.properties content")
    host_port: Optional[int] = Field(default=None, description="Optional host port")
    env: Dict[str, str] = Field(default_factory=dict, description="Additional environment variables")
    ttl_hours: Optional[int] = Field(default=None, description="Optional TTL in hours")
    db_mode: str = Field(default="none", description="none | external")
    db_env: Dict[str, str] = Field(default_factory=dict, description="Environment variables for an external DB")


class ConfigError(RuntimeError):
    pass


class EnvironmentProxy:
    def __init__(self, broker: "Broker", env_id: str, listen_host: str, listen_port: int, target_host: str, target_port: int):
        self.broker = broker
        self.env_id = env_id
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        proxy = self

        class ProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else None
                target_url = f"http://{proxy.target_host}:{proxy.target_port}{self.path}"
                headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection", "content-length"}}
                try:
                    resp = requests.request(
                        self.command,
                        target_url,
                        headers=headers,
                        data=body,
                        stream=True,
                        timeout=180,
                        allow_redirects=False,
                    )
                    proxy.broker.mark_access(proxy.env_id, reason="http_request")
                except requests.RequestException as exc:
                    data = f"Proxy error: {exc}\n".encode("utf-8")
                    self.send_response(502)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                self.send_response(resp.status_code)
                excluded = {"transfer-encoding", "connection", "content-encoding"}
                for key, value in resp.headers.items():
                    if key.lower() in excluded:
                        continue
                    self.send_header(key, value)
                if "Content-Length" not in resp.headers and resp.content is not None:
                    self.send_header("Content-Length", str(len(resp.content)))
                self.end_headers()
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        self.wfile.write(chunk)
                self.wfile.flush()

            def do_GET(self) -> None:
                self._forward()

            def do_POST(self) -> None:
                self._forward()

            def do_PUT(self) -> None:
                self._forward()

            def do_DELETE(self) -> None:
                self._forward()

            def do_PATCH(self) -> None:
                self._forward()

            def do_HEAD(self) -> None:
                self._forward()

            def do_OPTIONS(self) -> None:
                self._forward()

        self.server = ThreadingHTTPServer((self.listen_host, self.listen_port), ProxyHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name=f"proxy-{self.env_id}")
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.server = None
        self.thread = None


class Broker:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = self._load_config()
        self.registry_path = BASE_DIR / self.config["registry_file"]
        self.properties_dir = BASE_DIR / self.config["properties_dir"]
        self.properties_dir.mkdir(parents=True, exist_ok=True)
        self.registry_lock = threading.Lock()
        self.port_lock = threading.Lock()
        self.proxies: Dict[str, EnvironmentProxy] = {}
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True, name="cleanup-loop")
        self.cleanup_thread.start()
        self._restore_proxies()

    def stop_all_proxies(self) -> None:
        for proxy in list(self.proxies.values()):
            proxy.stop()
        self.proxies.clear()

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise ConfigError(f"Configuration file does not exist: {self.config_path}")

        with self.config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        required = [
            "listen_host",
            "listen_port",
            "proxy_bind_host",
            "api_tokens",
            "allowed_image_patterns",
            "profiles",
            "port_range",
            "base_url_template",
            "registry_file",
            "properties_dir",
            "docker_network",
            "default_ttl_hours",
            "max_ttl_hours",
            "cleanup_interval_seconds",
            "ram_buffer_mb",
            "max_environments_per_user",
            "ready_timeout_seconds",
            "ready_check_interval_seconds",
            "idle_timeout_minutes",
        ]
        for key in required:
            if key not in data:
                raise ConfigError(f"Missing required key in config.yaml: {key}")
        return data

    def _read_registry(self) -> List[Dict[str, Any]]:
        if not self.registry_path.exists():
            return []
        with self.registry_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write_registry(self, data: List[Dict[str, Any]]) -> None:
        tmp = self.registry_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(self.registry_path)

    def authenticate(self, authorization_header: Optional[str]) -> str:
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        token = authorization_header.replace("Bearer ", "", 1).strip()
        for user, expected in self.config["api_tokens"].items():
            if secrets.compare_digest(token, expected):
                return user
        raise HTTPException(status_code=401, detail="Invalid token")

    def _docker(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker"] + args
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

    def _docker_inspect_network_ip(self, container_name: str) -> Optional[str]:
        try:
            result = self._docker(["inspect", container_name, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"])
            ip = result.stdout.strip()
            return ip or None
        except subprocess.CalledProcessError:
            return None

    def _docker_container_running(self, container_name: str) -> bool:
        try:
            result = self._docker(["inspect", container_name, "--format", "{{.State.Running}}"])
            return result.stdout.strip().lower() == "true"
        except subprocess.CalledProcessError:
            return False

    def _get_system_memory(self) -> Dict[str, int]:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, value = line.split(":", 1)
                info[key] = int(value.strip().split()[0])
        total_mb = info["MemTotal"] // 1024
        available_mb = info["MemAvailable"] // 1024
        return {"total_mb": total_mb, "available_mb": available_mb}

    def _is_allowed_image(self, image: str) -> bool:
        patterns = self.config.get("allowed_image_patterns", []) or []
        for pattern in patterns:
            try:
                if re.match(pattern, image):
                    return True
            except re.error:
                continue
        return False

    def _ensure_image_available(self, image: str) -> None:
        inspect_result = self._docker(["image", "inspect", image], check=False)
        if inspect_result.returncode == 0:
            return
        pull_result = self._docker(["pull", image], check=False)
        if pull_result.returncode != 0:
            stderr = (pull_result.stderr or "").strip()
            stdout = (pull_result.stdout or "").strip()
            detail = stderr or stdout or "docker pull failed"
            raise RuntimeError(f"Could not pull image {image}: {detail}")

    def _check_quota(self, req: CreateEnvironmentRequest, token_user: str) -> Dict[str, Any]:
        if req.user != token_user:
            raise HTTPException(status_code=403, detail="Payload user does not match the token user")
        if not self._is_allowed_image(req.image):
            raise HTTPException(status_code=400, detail=f"Image is not allowed by pattern: {req.image}")
        if req.profile not in self.config["profiles"]:
            raise HTTPException(status_code=400, detail=f"Profile is not allowed: {req.profile}")
        if req.db_mode not in ["none", "external"]:
            raise HTTPException(status_code=400, detail="db_mode must be 'none' or 'external'")
        if req.db_mode == "external" and not req.db_env:
            raise HTTPException(status_code=400, detail="db_mode=external requires db_env")

        registry = self._read_registry()
        active_statuses = ["starting", "ready"]
        active_user_envs = [x for x in registry if x["user"] == req.user and x["status"] in active_statuses]
        if len(active_user_envs) >= int(self.config["max_environments_per_user"]):
            raise HTTPException(status_code=409, detail="Maximum active environments reached")

        profile = self.config["profiles"][req.profile]
        mem = self._get_system_memory()
        required_mb = int(profile["memory_mb"])
        buffer_mb = int(self.config["ram_buffer_mb"])
        if mem["available_mb"] - required_mb < buffer_mb:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Not enough RAM. Available: {mem['available_mb']} MB, "
                    f"required by profile: {required_mb} MB, minimum buffer: {buffer_mb} MB"
                ),
            )
        return profile

    def _is_port_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.config["proxy_bind_host"], port))
            except OSError:
                return False
        return True

    def _allocate_port(self, requested_port: Optional[int]) -> int:
        start = int(self.config["port_range"]["start"])
        end = int(self.config["port_range"]["end"])
        registry = self._read_registry()
        active_statuses = {"starting", "ready"}
        used_ports = {int(x["host_port"]) for x in registry if x["status"] in active_statuses}

        def candidate_ok(port: int) -> bool:
            return start <= port <= end and port not in used_ports and self._is_port_free(port)

        with self.port_lock:
            if requested_port is not None:
                if not candidate_ok(requested_port):
                    raise HTTPException(status_code=409, detail=f"Requested port is not available: {requested_port}")
                return requested_port
            for port in range(start, end + 1):
                if candidate_ok(port):
                    return port
        raise HTTPException(status_code=409, detail="No free ports available")

    def _make_name(self, req: CreateEnvironmentRequest) -> str:
        raw_name = req.name or f"liferay-{req.user}-{uuid.uuid4().hex[:6]}"
        safe = "".join(ch if ch.isalnum() or ch in ["-", "_"] else "-" for ch in raw_name.lower())
        return safe[:50]

    def _write_properties(self, env_id: str, content: Optional[str]) -> Optional[Path]:
        if not content:
            return None
        path = self.properties_dir / f"{env_id}-portal-ext.properties"
        path.write_text(content, encoding="utf-8")
        return path

    def _update_record(self, updated: Dict[str, Any]) -> None:
        with self.registry_lock:
            registry = self._read_registry()
            for idx, item in enumerate(registry):
                if item["id"] == updated["id"]:
                    registry[idx] = updated
                    break
            else:
                registry.append(updated)
            self._write_registry(registry)

    def _change_status(self, env_id: str, status: str, **extra: Any) -> Dict[str, Any]:
        registry = self._read_registry()
        record = next((x for x in registry if x["id"] == env_id), None)
        if not record:
            raise HTTPException(status_code=404, detail="Entorno no encontrado")
        record["status"] = status
        record["updated_at"] = iso_now()
        for key, value in extra.items():
            record[key] = value
        self._update_record(record)
        return record

    def _registry_record(self, env_id: str) -> Optional[Dict[str, Any]]:
        registry = self._read_registry()
        return next((x for x in registry if x["id"] == env_id), None)

    def create_environment(self, req: CreateEnvironmentRequest, token_user: str) -> Dict[str, Any]:
        profile = self._check_quota(req, token_user)
        host_port = self._allocate_port(req.host_port)
        env_id = uuid.uuid4().hex[:12]
        container_name = self._make_name(req)
        properties_path = self._write_properties(env_id, req.portal_properties)

        ttl_hours = req.ttl_hours or int(self.config["default_ttl_hours"])
        ttl_hours = min(ttl_hours, int(self.config["max_ttl_hours"]))
        now = utcnow()
        expires_at = now + timedelta(hours=ttl_hours)

        record = {
            "id": env_id,
            "user": req.user,
            "image": req.image,
            "profile": req.profile,
            "container_name": container_name,
            "host_port": host_port,
            "status": "starting",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "last_access_at": None,
            "idle_timeout_minutes": int(self.config["idle_timeout_minutes"]),
            "url": self.config["base_url_template"].format(port=host_port, container_name=container_name, env_id=env_id),
            "properties_file": str(properties_path) if properties_path else None,
            "env": req.env,
            "db_mode": req.db_mode,
            "db_env": req.db_env if req.db_mode == "external" else {},
            "error": None,
        }
        self._update_record(record)

        cmd = [
            "run", "-d",
            "--name", container_name,
            "--restart", "unless-stopped",
            "--network", self.config["docker_network"],
            "--memory", f"{profile['memory_mb']}m",
            "--cpus", str(profile['cpus']),
        ]

        all_env = dict(req.env)
        if req.db_mode == "external":
            all_env.update(req.db_env)
        for key, value in all_env.items():
            cmd += ["-e", f"{key}={value}"]

        if properties_path:
            cmd += ["-v", f"{properties_path}:/opt/liferay/portal-ext.properties:ro"]

        extra_args = profile.get("docker_run_args", []) or []
        cmd.extend(extra_args)
        cmd.append(req.image)

        try:
            self._ensure_image_available(req.image)
            result = self._docker(cmd)
            container_id = result.stdout.strip()
            record["container_id"] = container_id
            target_ip = self._docker_inspect_network_ip(container_name)
            if not target_ip:
                raise RuntimeError("Could not resolve container IP")
            record["target_ip"] = target_ip
            self._update_record(record)
            self._ensure_proxy(record)
            threading.Thread(target=self._wait_until_ready, args=(env_id,), daemon=True, name=f"ready-{env_id}").start()
        except Exception as e:
            try:
                self._docker(["rm", "-f", container_name], check=False)
            except Exception:
                pass
            record["status"] = "failed"
            record["error"] = str(e)
            record["updated_at"] = iso_now()
            self._update_record(record)
            raise HTTPException(status_code=500, detail=f"Could not create environment: {e}")

        return self._registry_record(env_id) or record

    def _ensure_proxy(self, record: Dict[str, Any]) -> None:
        env_id = record["id"]
        existing = self.proxies.get(env_id)
        if existing:
            return
        target_ip = record.get("target_ip") or self._docker_inspect_network_ip(record["container_name"])
        if not target_ip:
            raise RuntimeError("Could not resolve container IP to start proxy")
        proxy = EnvironmentProxy(
            broker=self,
            env_id=env_id,
            listen_host=self.config["proxy_bind_host"],
            listen_port=int(record["host_port"]),
            target_host=target_ip,
            target_port=8080,
        )
        proxy.start()
        self.proxies[env_id] = proxy

    def _restore_proxies(self) -> None:
        registry = self._read_registry()
        for record in registry:
            if record.get("status") in ["starting", "ready"] and self._docker_container_running(record["container_name"]):
                try:
                    if not record.get("target_ip"):
                        record["target_ip"] = self._docker_inspect_network_ip(record["container_name"])
                        self._update_record(record)
                    self._ensure_proxy(record)
                except Exception:
                    record["status"] = "failed"
                    record["error"] = "Could not restore proxy after broker restart"
                    record["updated_at"] = iso_now()
                    self._update_record(record)

    def _wait_until_ready(self, env_id: str) -> None:
        timeout_seconds = int(self.config["ready_timeout_seconds"])
        interval = int(self.config["ready_check_interval_seconds"])
        start = time.time()
        while time.time() - start < timeout_seconds:
            record = self._registry_record(env_id)
            if not record:
                return
            if record["status"] not in ["starting", "ready"]:
                return
            if not self._docker_container_running(record["container_name"]):
                self._change_status(env_id, "failed", error="Container stopped during startup")
                return
            target_url = f"http://{record.get('target_ip')}:{8080}/c/portal/login"
            try:
                resp = requests.get(target_url, timeout=5, allow_redirects=False)
                if resp.status_code in [200, 302, 401, 403]:
                    self._change_status(env_id, "ready", ready_at=iso_now(), error=None)
                    return
            except requests.RequestException:
                pass
            time.sleep(interval)
        self._change_status(env_id, "failed", error=f"Readiness timeout after {timeout_seconds}s")

    def mark_access(self, env_id: str, reason: str = "access") -> None:
        record = self._registry_record(env_id)
        if not record:
            return
        record["last_access_at"] = iso_now()
        record["last_access_reason"] = reason
        record["updated_at"] = iso_now()
        self._update_record(record)

    def list_environments(self, token_user: str) -> List[Dict[str, Any]]:
        registry = self._read_registry()
        admin_user = self.config.get("admin_user", "admin")
        return [item for item in registry if token_user in [item["user"], admin_user]]

    def get_environment(self, env_id: str, token_user: str) -> Dict[str, Any]:
        record = self._registry_record(env_id)
        if not record:
            raise HTTPException(status_code=404, detail="Environment not found")
        admin_user = self.config.get("admin_user", "admin")
        if token_user not in [record["user"], admin_user]:
            raise HTTPException(status_code=403, detail="You cannot view this environment")
        return record

    def delete_environment(self, env_id: str, token_user: str, reason: str = "deleted") -> Dict[str, Any]:
        record = self._registry_record(env_id)
        if not record:
            raise HTTPException(status_code=404, detail="Environment not found")
        admin_user = self.config.get("admin_user", "admin")
        if token_user not in [record["user"], admin_user]:
            raise HTTPException(status_code=403, detail="You cannot delete this environment")
        self._destroy_record(record, status=reason)
        return self._registry_record(env_id) or record

    def _destroy_record(self, record: Dict[str, Any], status: str) -> None:
        env_id = record["id"]
        proxy = self.proxies.pop(env_id, None)
        if proxy:
            proxy.stop()
        try:
            self._docker(["rm", "-f", record["container_name"]], check=False)
        except Exception:
            pass
        if record.get("properties_file"):
            try:
                Path(record["properties_file"]).unlink(missing_ok=True)
            except Exception:
                pass
        record["status"] = status
        record["deleted_at"] = iso_now()
        record["updated_at"] = iso_now()
        self._update_record(record)

    def _cleanup_loop(self) -> None:
        interval = int(self.config["cleanup_interval_seconds"])
        while True:
            try:
                self.cleanup_expired_and_idle()
            except Exception:
                pass
            time.sleep(interval)

    def cleanup_expired_and_idle(self) -> None:
        now = utcnow()
        idle_timeout = timedelta(minutes=int(self.config["idle_timeout_minutes"]))
        for record in self._read_registry():
            if record.get("status") not in ["starting", "ready"]:
                continue
            expires_at = datetime.fromisoformat(record["expires_at"])
            if now >= expires_at:
                self._destroy_record(record, status="expired")
                continue
            last_access_at = datetime.fromisoformat(record["last_access_at"]) if record.get("last_access_at") else None
            baseline = last_access_at or datetime.fromisoformat(record["created_at"])
            if now - baseline >= idle_timeout:
                self._destroy_record(record, status="stopped")
                continue
            if not self._docker_container_running(record["container_name"]):
                record["status"] = "failed"
                record["updated_at"] = iso_now()
                record["error"] = record.get("error") or "Container is no longer running"
                self._update_record(record)

    def dashboard_stats(self, token_user: str) -> Dict[str, Any]:
        items = self.list_environments(token_user)
        by_status: Dict[str, int] = defaultdict(int)
        by_user: Dict[str, int] = defaultdict(int)
        for item in items:
            by_status[item["status"]] += 1
            by_user[item["user"]] += 1
        return {
            "total": len(items),
            "by_status": dict(by_status),
            "by_user": dict(by_user),
            "memory": self._get_system_memory(),
        }


broker = Broker(CONFIG_PATH)
app = FastAPI(title="Liferay Local Environment Broker", version="2.0.0")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/profiles")
def profiles(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    broker.authenticate(authorization)
    return broker.config["profiles"]


@app.get("/v1/environments")
def list_environments(authorization: Optional[str] = Header(default=None)) -> List[Dict[str, Any]]:
    token_user = broker.authenticate(authorization)
    return broker.list_environments(token_user)


@app.get("/v1/environments/{environment_id}")
def get_environment(environment_id: str, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    token_user = broker.authenticate(authorization)
    return broker.get_environment(environment_id, token_user)


@app.post("/v1/environments")
def create_environment(req: CreateEnvironmentRequest, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    token_user = broker.authenticate(authorization)
    return broker.create_environment(req, token_user)


@app.delete("/v1/environments/{environment_id}")
def delete_environment(environment_id: str, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    token_user = broker.authenticate(authorization)
    return broker.delete_environment(environment_id, token_user, reason="deleted")


@app.post("/v1/environments/{environment_id}/touch")
def touch_environment(environment_id: str, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    token_user = broker.authenticate(authorization)
    record = broker.get_environment(environment_id, token_user)
    broker.mark_access(environment_id, reason="manual_touch")
    return broker.get_environment(record["id"], token_user)


@app.get("/v1/dashboard")
def dashboard(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    token_user = broker.authenticate(authorization)
    return broker.dashboard_stats(token_user)


@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    return """<!doctype html>
  <html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Liferay Environment Broker</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; background: #f6f8fb; color: #1f2937; }
    .top { display:flex; gap:16px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }
    input, button, select, textarea { padding: 10px; border-radius: 8px; border: 1px solid #cbd5e1; }
    button { cursor:pointer; background:#0f62fe; color:white; border:none; }
    button.secondary { background:#475569; }
    .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin:16px 0; }
    .card { background:white; border-radius:16px; padding:16px; box-shadow: 0 2px 10px rgba(0,0,0,0.06); }
    table { width:100%; border-collapse: collapse; background:white; border-radius: 16px; overflow:hidden; }
    th, td { padding:12px; border-bottom: 1px solid #e5e7eb; text-align:left; vertical-align:top; }
    th { background:#eff6ff; }
    .status { font-weight:bold; text-transform:uppercase; font-size:12px; }
    .ready { color:#0f766e; }
    .starting { color:#b45309; }
    .failed, .stopped, .expired, .deleted { color:#b91c1c; }
    .small { font-size:12px; color:#475569; }
    .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .url { word-break:break-all; }
  </style>
</head>
<body>
  <div class=\"top\">
    <h1 style=\"margin:0\">Liferay Environment Broker</h1>
    <div class=\"toolbar\">
      <input id=\"baseUrl\" placeholder=\"Base URL\" value=\"\" style=\"min-width:240px\" />
      <input id=\"token\" placeholder=\"Bearer token\" type=\"password\" style=\"min-width:240px\" />
      <button onclick=\"loadAll()\">Conectar</button>
    </div>
  </div>

  <div class=\"cards\" id=\"stats\"></div>

  <div class=\"card\" style=\"margin-bottom:16px\">
    <h2 style=\"margin-top:0\">Create Environment</h2>
    <div class=\"toolbar\">
      <input id=\"user\" placeholder=\"user\" />
      <input id=\"image\" placeholder=\"image\" value=\"liferay/dxp:2026.q2.1\" style=\"min-width:280px\" />
      <select id=\"profile\">
        <option value=\"small\">small</option>
        <option value=\"standard\" selected>standard</option>
        <option value=\"large\">large</option>
      </select>
      <select id=\"db_mode\">
        <option value=\"none\" selected>sin DB externa</option>
        <option value=\"external\">DB externa</option>
      </select>
      <input id=\"port\" placeholder=\"puerto opcional\" />
      <input id=\"ttl\" placeholder=\"ttl horas\" />
      <button onclick=\"createEnv()\">Create</button>
    </div>
    <p class=\"small\">Las variables extra se indican en JSON simple.</p>
    <div class=\"toolbar\" style=\"align-items:flex-start\">
      <textarea id=\"env\" placeholder='{"LIFERAY_JVM_OPTS":"-Xms2g -Xmx4g"}' rows=\"4\" style=\"min-width:360px;flex:1\"></textarea>
      <textarea id=\"db_env\" placeholder='{"LIFERAY_JDBC_PERIOD_DEFAULT_PERIOD_URL":"jdbc:postgresql://..."}' rows=\"4\" style=\"min-width:360px;flex:1\"></textarea>
      <textarea id=\"props\" placeholder=\"portal-ext.properties opcional\" rows=\"4\" style=\"min-width:360px;flex:1\"></textarea>
    </div>
  </div>

  <div id=\"message\" class=\"small\" style=\"margin-bottom:12px\"></div>
  <table>
    <thead>
      <tr>
        <th>ID</th><th>Status</th><th>User</th><th>Image</th><th>Port</th><th>URL</th><th>Access</th><th>Created</th><th>Actions</th>
      </tr>
    </thead>
    <tbody id=\"rows\"></tbody>
  </table>

<script>
const $ = (id) => document.getElementById(id);
if (!$("baseUrl").value) {
  $("baseUrl").value = window.location.origin;
}
function authHeaders() {
  return {
    "Authorization": `Bearer ${$("token").value}`,
    "Content-Type": "application/json"
  };
}
function setMessage(text, isError=false) {
  $("message").textContent = text;
  $("message").style.color = isError ? "#b91c1c" : "#475569";
}
async function api(path, options={}) {
  const res = await fetch(`${$("baseUrl").value.replace(/\\/$/, "")}${path}`, {
    ...options,
    headers: {...authHeaders(), ...(options.headers || {})}
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}
function renderStats(data) {
  const cards = [];
  cards.push(`<div class=\"card\"><strong>Total</strong><div>${data.total}</div></div>`);
  cards.push(`<div class=\"card\"><strong>RAM disponible</strong><div>${data.memory.available_mb} MB</div><div class=\"small\">Total ${data.memory.total_mb} MB</div></div>`);
  for (const [k,v] of Object.entries(data.by_status || {})) cards.push(`<div class=\"card\"><strong>${k}</strong><div>${v}</div></div>`);
  $("stats").innerHTML = cards.join("");
}
function renderRows(items) {
  $("rows").innerHTML = items.map(item => `
    <tr>
      <td>${item.id}<div class=\"small\">${item.container_name}</div></td>
      <td><span class=\"status ${item.status}\">${item.status}</span>${item.error ? `<div class=\"small\">${item.error}</div>` : ""}</td>
      <td>${item.user}<div class=\"small\">${item.profile}</div></td>
      <td>${item.image}<div class=\"small\">DB: ${item.db_mode}</div></td>
      <td>${item.host_port}</td>
      <td class=\"url\"><a href=\"${item.url}\" target=\"_blank\">${item.url}</a></td>
      <td>${item.last_access_at || "no access yet"}<div class=\"small\">TTL ${item.expires_at}</div></td>
      <td>${item.created_at}</td>
      <td>
        <button class=\"secondary\" onclick=\"touchEnv('${item.id}')\">Touch</button>
        <button onclick=\"deleteEnv('${item.id}')\">Delete</button>
      </td>
    </tr>
  `).join("");
}
async function loadAll() {
  try {
    const [stats, items] = await Promise.all([api('/v1/dashboard'), api('/v1/environments')]);
    renderStats(stats);
    renderRows(items);
    setMessage(`Loaded ${items.length} environments`);
  } catch (e) {
    setMessage(e.message, true);
  }
}
function parseJsonSafe(text) {
  if (!text || !text.trim()) return {};
  return JSON.parse(text);
}
async function createEnv() {
  try {
    const payload = {
      user: $("user").value,
      image: $("image").value,
      profile: $("profile").value,
      db_mode: $("db_mode").value,
      host_port: $("port").value ? parseInt($("port").value, 10) : null,
      ttl_hours: $("ttl").value ? parseInt($("ttl").value, 10) : null,
      env: parseJsonSafe($("env").value),
      db_env: parseJsonSafe($("db_env").value),
      portal_properties: $("props").value || null,
    };
    const result = await api('/v1/environments', {method:'POST', body: JSON.stringify(payload)});
    setMessage(`Entorno ${result.id} creado con estado ${result.status}`);
    await loadAll();
  } catch (e) {
    setMessage(e.message, true);
  }
}
async function deleteEnv(id) {
  try {
    await api(`/v1/environments/${id}`, {method:'DELETE'});
    await loadAll();
  } catch (e) {
    setMessage(e.message, true);
  }
}
async function touchEnv(id) {
  try {
    await api(`/v1/environments/${id}/touch`, {method:'POST'});
    await loadAll();
  } catch (e) {
    setMessage(e.message, true);
  }
}
setInterval(() => { if ($("token").value) loadAll(); }, 15000);
</script>
</body>
</html>"""
