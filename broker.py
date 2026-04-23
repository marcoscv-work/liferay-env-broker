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
from urllib.parse import urlparse

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
                headers["Host"] = self.headers.get("Host", f"{proxy.listen_host}:{proxy.listen_port}")
                headers["X-Forwarded-Host"] = headers["Host"]
                headers["X-Forwarded-Proto"] = "http"
                headers["X-Forwarded-Port"] = str(proxy.listen_port)
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
        self.image_cleanup_thread = threading.Thread(target=self._image_cleanup_loop, daemon=True, name="image-cleanup-loop")
        self.image_cleanup_thread.start()
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
            result = self._docker(["inspect", container_name, "--format", "{{.State.Status}} {{.State.Restarting}}"])
            status, restarting = result.stdout.strip().lower().split(maxsplit=1)
            return status == "running" and restarting == "false"
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
        max_user_envs = self._max_environments_for_user(req.user)
        if max_user_envs is not None and len(active_user_envs) >= max_user_envs:
            raise HTTPException(status_code=409, detail="Maximum active environments reached")

        profile = self.config["profiles"][req.profile]
        self._check_capacity_units(registry, req.user, req.profile)

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

    def _max_environments_for_user(self, user: str) -> Optional[int]:
        fallback = self.config.get("max_environments_per_user")
        per_user_limits = self.config.get("max_environments_by_user") or {}
        limit = per_user_limits.get(user, fallback)
        return int(limit) if limit is not None else None

    def _check_capacity_units(self, registry: List[Dict[str, Any]], user: str, requested_profile: str) -> None:
        capacity = self.config.get("capacity") or {}
        if not capacity:
            return
        total_limit = int(capacity.get("total_units", 0) or 0)
        per_user_units = capacity.get("per_user_units") or {}
        user_limit = int(per_user_units.get(user, per_user_units.get("default", total_limit)) or 0)
        requested_units = self._profile_capacity_units(requested_profile)
        active = [x for x in registry if x.get("status") in ["starting", "ready"]]
        total_used = sum(self._profile_capacity_units(x.get("profile", "")) for x in active)
        user_used = sum(self._profile_capacity_units(x.get("profile", "")) for x in active if x.get("user") == user)
        if total_limit and total_used + requested_units > total_limit:
            raise HTTPException(status_code=409, detail="Not enough platform capacity available")
        if user_limit and user_used + requested_units > user_limit:
            raise HTTPException(status_code=409, detail="User capacity limit reached")

    def _profile_capacity_units(self, profile_name: str) -> int:
        profile = self.config.get("profiles", {}).get(profile_name) or {}
        return int(profile.get("capacity_units", 1))

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

    def _liferay_public_url_env(self, public_url: str) -> Dict[str, str]:
        parsed = urlparse(public_url)
        if not parsed.hostname:
            return {}
        env = {
            "LIFERAY_WEB_PERIOD_SERVER_PERIOD_HOST": parsed.hostname,
            "LIFERAY_WEB_PERIOD_SERVER_PERIOD_PROTOCOL": parsed.scheme or "http",
        }
        if parsed.port:
            env["LIFERAY_WEB_PERIOD_SERVER_PERIOD_HTTP_PERIOD_PORT"] = str(parsed.port)
        return env

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

    def _remove_record(self, env_id: str) -> None:
        with self.registry_lock:
            registry = [item for item in self._read_registry() if item["id"] != env_id]
            self._write_registry(registry)

    def _change_status(self, env_id: str, status: str, **extra: Any) -> Dict[str, Any]:
        registry = self._read_registry()
        record = next((x for x in registry if x["id"] == env_id), None)
        if not record:
            raise HTTPException(status_code=404, detail="Environment not found")
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

        ttl_hours = int(self.config["default_ttl_hours"]) if req.ttl_hours is None else int(req.ttl_hours)
        if ttl_hours < 0:
            raise HTTPException(status_code=400, detail="ttl_hours must be 0 or greater")
        ttl_hours = min(ttl_hours, int(self.config["max_ttl_hours"])) if ttl_hours else 0
        now = utcnow()
        expires_at = None if ttl_hours == 0 else now + timedelta(hours=ttl_hours)

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
            "expires_at": expires_at.isoformat() if expires_at else None,
            "last_access_at": None,
            "idle_timeout_minutes": int(self.config["idle_timeout_minutes"]),
            "url": self.config["base_url_template"].format(port=host_port, container_name=container_name, env_id=env_id),
            "properties_file": str(properties_path) if properties_path else None,
            "env": {},
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

        all_env = self._liferay_public_url_env(record["url"])
        all_env.update(req.env)
        if req.db_mode == "external":
            all_env.update(req.db_env)
        record["env"] = all_env
        self._update_record(record)
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
                    if record.get("status") == "starting":
                        threading.Thread(target=self._wait_until_ready, args=(record["id"],), daemon=True, name=f"ready-{record['id']}").start()
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
                host = urlparse(record["url"]).netloc
                resp = requests.get(target_url, headers={"Host": host}, timeout=5, allow_redirects=False)
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
        return record

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
        self._remove_record(env_id)

    def _cleanup_loop(self) -> None:
        interval = int(self.config["cleanup_interval_seconds"])
        while True:
            try:
                self.cleanup_expired_and_idle()
            except Exception:
                pass
            time.sleep(interval)

    def _image_cleanup_loop(self) -> None:
        cleanup_config = self.config.get("image_cleanup") or {}
        if not cleanup_config.get("enabled", True):
            return
        interval = int(cleanup_config.get("interval_hours", 24)) * 3600
        while True:
            try:
                self.cleanup_unused_images()
            except Exception:
                pass
            time.sleep(interval)

    def cleanup_unused_images(self) -> None:
        cleanup_config = self.config.get("image_cleanup") or {}
        if not cleanup_config.get("enabled", True):
            return
        max_age_hours = int(cleanup_config.get("max_unused_age_hours", 168))
        self._docker(["image", "prune", "-a", "--force", "--filter", f"until={max_age_hours}h"], check=False)

    def cleanup_expired_and_idle(self) -> None:
        now = utcnow()
        idle_timeout = timedelta(minutes=int(self.config["idle_timeout_minutes"]))
        for record in self._read_registry():
            if record.get("status") not in ["starting", "ready"]:
                continue
            expires_at = datetime.fromisoformat(record["expires_at"]) if record.get("expires_at") else None
            if expires_at and now >= expires_at:
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
            "capacity": self._capacity_summary(token_user),
        }

    def _capacity_summary(self, token_user: str) -> Dict[str, Any]:
        capacity = self.config.get("capacity") or {}
        profiles = self.config.get("profiles") or {}
        active = [x for x in self._read_registry() if x.get("status") in ["starting", "ready"]]
        total_units = int(capacity.get("total_units", 0) or 0)
        per_user_units = capacity.get("per_user_units") or {}
        user_limit = int(per_user_units.get(token_user, per_user_units.get("default", total_units)) or 0)
        used_units = sum(self._profile_capacity_units(x.get("profile", "")) for x in active)
        user_used_units = sum(self._profile_capacity_units(x.get("profile", "")) for x in active if x.get("user") == token_user)
        user_active = len([x for x in active if x.get("user") == token_user])
        costs = {name: int(profile.get("capacity_units", 1)) for name, profile in profiles.items()}
        return {
            "total_units": total_units,
            "used_units": used_units,
            "available_units": max(total_units - used_units, 0) if total_units else None,
            "active_environments": len(active),
            "user": token_user,
            "user_units": user_used_units,
            "user_unit_limit": user_limit,
            "user_active_environments": user_active,
            "max_user_environments": self._max_environments_for_user(token_user),
            "profile_units": costs,
        }


broker = Broker(CONFIG_PATH)
app = FastAPI(title="Liferay Local Environment Broker", version="2.0.0")
docker_tag_cache: Dict[str, Any] = {"expires_at": 0, "data": None}


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/profiles")
def profiles(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    broker.authenticate(authorization)
    return broker.config["profiles"]


@app.get("/v1/me")
def me(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    token_user = broker.authenticate(authorization)
    return {"user": token_user, "is_admin": token_user == broker.config.get("admin_user", "admin")}


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


@app.get("/v1/images/liferay-dxp-tags")
def liferay_dxp_tags(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    broker.authenticate(authorization)
    now = time.time()
    if docker_tag_cache["data"] and docker_tag_cache["expires_at"] > now:
        return docker_tag_cache["data"]

    source_url = "https://hub.docker.com/r/liferay/dxp/tags"
    api_url = "https://hub.docker.com/v2/repositories/liferay/dxp/tags?page_size=12&ordering=last_updated"
    try:
        resp = requests.get(api_url, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        tags = [
            {
                "name": item["name"],
                "image": f"liferay/dxp:{item['name']}",
                "last_updated": item.get("last_updated"),
            }
            for item in payload.get("results", [])
            if item.get("name")
        ]
        data = {"repository": "liferay/dxp", "source_url": source_url, "tags": tags}
        docker_tag_cache["data"] = data
        docker_tag_cache["expires_at"] = now + 300
        return data
    except requests.RequestException as exc:
        if docker_tag_cache["data"]:
            stale_data = dict(docker_tag_cache["data"])
            stale_data["stale"] = True
            return stale_data
        raise HTTPException(status_code=502, detail=f"Could not load Docker Hub tags: {exc}")


@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    return """<!doctype html>
  <html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Liferay Environment Broker</title>
  <style>
    :root {
      --bg: #0b1020;
      --surface: #121a2f;
      --surface-muted: #17213a;
      --text: #e5edf7;
      --text-muted: #94a3b8;
      --border: #26344f;
      --accent: #4f8cff;
      --accent-strong: #6ea2ff;
      --secondary: #334155;
      --danger: #f87171;
      --ready: #2dd4bf;
      --starting: #fbbf24;
      --ttl: #c084fc;
      --shadow: rgba(0,0,0,0.35);
      --button-glow: rgba(79,140,255,0.35);
      --input-bg: #0f172a;
      --table-head: #18233d;
      --success-bg: rgba(45,212,191,0.12);
    }
    [data-theme="light"] {
      --bg: #f6f8fb;
      --surface: #ffffff;
      --surface-muted: #f8fafc;
      --text: #1f2937;
      --text-muted: #475569;
      --border: #cbd5e1;
      --accent: #0f62fe;
      --accent-strong: #2563eb;
      --secondary: #475569;
      --danger: #b91c1c;
      --ready: #0f766e;
      --starting: #b45309;
      --ttl: #7c3aed;
      --shadow: rgba(0,0,0,0.08);
      --button-glow: rgba(15,98,254,0.25);
      --input-bg: #ffffff;
      --table-head: #eff6ff;
      --success-bg: rgba(15,118,110,0.10);
    }
    * { box-sizing: border-box; }
    body {
      font-family: Arial, sans-serif;
      margin: 24px;
      background: var(--bg);
      color: var(--text);
      transition: background 180ms ease, color 180ms ease;
    }
    .top { display:flex; flex-direction:column; gap:12px; align-items:flex-start; margin-bottom:16px; }
    input, button, select, textarea {
      padding: 10px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--input-bg);
      color: var(--text);
    }
    input::placeholder, textarea::placeholder { color: var(--text-muted); }
    button {
      position: relative;
      overflow: hidden;
      cursor:pointer;
      background: linear-gradient(180deg, var(--accent-strong), var(--accent));
      color:white;
      border:none;
      box-shadow: 0 8px 18px var(--button-glow);
      transform: translateY(0);
      transition: transform 120ms ease, box-shadow 120ms ease, filter 120ms ease;
    }
    button:hover { filter: brightness(1.05); box-shadow: 0 10px 24px var(--button-glow); }
    button:active { transform: translateY(2px) scale(0.98); box-shadow: 0 3px 8px var(--button-glow); }
    button:disabled { cursor: progress; filter: saturate(0.7); opacity: 0.78; transform:none; }
    button::after {
      content: "";
      position: absolute;
      inset: 0;
      background: radial-gradient(circle at center, rgba(255,255,255,0.34), transparent 42%);
      opacity: 0;
      transform: scale(0.45);
      transition: opacity 160ms ease, transform 160ms ease;
      pointer-events: none;
    }
    button:active::after { opacity: 1; transform: scale(1.4); }
    button.secondary { background: var(--secondary); box-shadow: none; }
    button.link-button {
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--accent-strong);
      box-shadow: none;
      font: inherit;
      text-align: left;
    }
    button.link-button:hover { filter:none; text-decoration:underline; box-shadow:none; }
    .theme-toggle {
      position: fixed;
      top: 18px;
      right: 18px;
      width: 42px;
      height: 42px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
      border-radius: 50%;
      font-size: 20px;
      line-height: 1;
      z-index: 10;
    }
    .login-card { max-width:540px; margin:16px 0; }
    .login-card.hidden { display:none; }
    .session-card {
      display:none;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      margin:16px 0;
      background:var(--success-bg);
    }
    .session-card.visible { display:flex; }
    .authenticated-area { display:none; }
    .authenticated-area.visible { display:block; }
    .cards {
      display:grid;
      grid-template-columns:repeat(4, minmax(0, 1fr));
      grid-template-areas:"capacity capacity ram total";
      gap:12px;
      margin:16px 0;
      align-items:stretch;
    }
    .card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; box-shadow: 0 2px 10px var(--shadow); }
    .metric-card { min-height:92px; }
    .metric-line { display:flex; gap:12px; align-items:baseline; justify-content:space-between; font-size:18px; white-space:nowrap; }
    .metric-line span { font-weight:700; }
    .capacity-card { grid-area:capacity; min-width:0; }
    .ram-card { grid-area:ram; }
    .total-card { grid-area:total; }
    .capacity-bar { height:10px; border-radius:999px; background:var(--surface-muted); overflow:hidden; margin:12px 0 8px; border:1px solid var(--border); }
    .capacity-fill { height:100%; background:linear-gradient(90deg, var(--accent), var(--ready)); width:0%; }
    .profile-costs { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
    .profile-cost { padding:4px 8px; border:1px solid var(--border); border-radius:999px; color:var(--text-muted); font-size:12px; }
    .field-user { display:flex; align-items:center; min-height:42px; padding:0 12px 0 0; color:var(--text); font-weight:700; white-space:nowrap; }
    .field-user span { color:var(--text-muted); font-weight:400; margin-right:6px; }
    .port-input { width:100px; flex:0 0 100px; }
    .ttl-field { position:relative; display:flex; align-items:center; gap:6px; flex:0 0 240px; }
    .ttl-field input { width:100%; min-width:0; }
    .info-icon {
      width:24px;
      height:24px;
      display:inline-flex;
      align-items:center;
      justify-content:center;
      border-radius:50%;
      border:1px solid var(--border);
      color:var(--text-muted);
      background:var(--surface-muted);
      font-size:13px;
      font-weight:700;
      flex:0 0 auto;
      cursor:help;
    }
    .ttl-remaining { color:var(--ttl) !important; font-weight:700; }
    .advanced-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; align-items:start; margin-top:12px; }
    .field-block { display:flex; flex-direction:column; gap:6px; min-width:0; }
    .field-block label, details summary { font-weight:700; color:var(--text); }
    .field-block textarea { width:100%; min-width:0; resize:vertical; }
    .db-env-field.hidden { display:none; }
    details.advanced-properties { margin-top:12px; }
    details.advanced-properties summary { cursor:pointer; }
    details.advanced-properties textarea { width:100%; margin-top:8px; min-width:0; resize:vertical; }
    .connect-card { display:grid; grid-template-columns: minmax(0, 1fr) auto; gap:10px; align-items:end; }
    .connect-card input { width: 100%; }
    .connect-fields { display:grid; gap:10px; min-width:0; }
    .image-picker { display:grid; gap:6px; min-width:280px; position:relative; }
    .image-picker input { width:100%; }
    .image-links { position:absolute; top:calc(100% + 6px); left:0; display:flex; gap:12px; align-items:center; flex-wrap:wrap; line-height:1; }
    .create-toolbar { align-items:center; margin-bottom:28px; }
    .history-toggle { display:flex; align-items:center; gap:8px; margin-top:20px; }
    table { width:100%; border-collapse: collapse; background:var(--surface); border-radius: 8px; overflow:hidden; }
    th, td { padding:12px; border-bottom: 1px solid var(--border); text-align:left; vertical-align:top; }
    th { background:var(--table-head); }
    a { color: var(--accent-strong); }
    .status { font-weight:bold; text-transform:uppercase; font-size:12px; }
    .ready { color:var(--ready); }
    .starting { color:var(--starting); }
    .failed, .stopped, .expired, .deleted { color:var(--danger); }
    .small { font-size:12px; color:var(--text-muted); }
    .message { display:none; margin-bottom:12px; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--surface); }
    .message.visible { display:block; }
    .message.error { border-color:var(--danger); color:var(--danger); }
    .timestamp { font-size:11px; color:var(--text-muted); white-space:nowrap; }
    .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .url { word-break:break-all; }
    .table-wrap { width: 100%; }
    .actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .has-tooltip { overflow: visible; }
    .has-tooltip::before {
      content: attr(data-tooltip);
      position: absolute;
      top: 50%;
      right: calc(100% + 10px);
      width: max-content;
      max-width: 240px;
      padding: 8px 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      box-shadow: 0 8px 24px var(--shadow);
      font-size: 12px;
      line-height: 1.35;
      text-align: left;
      white-space: normal;
      opacity: 0;
      transform: translate(4px, -50%);
      transition: opacity 140ms ease 2s, transform 140ms ease 2s;
      pointer-events: none;
      z-index: 20;
    }
    .has-tooltip::after {
      content: "";
      position: absolute;
      top: 50%;
      right: calc(100% + 3px);
      width: 0;
      height: 0;
      border-top: 7px solid transparent;
      border-bottom: 7px solid transparent;
      border-left: 7px solid var(--surface);
      opacity: 0;
      transform: translate(4px, -50%);
      transition: opacity 140ms ease 2s, transform 140ms ease 2s;
      pointer-events: none;
      z-index: 21;
    }
    .has-tooltip:hover::before,
    .has-tooltip:focus-visible::before {
      opacity: 1;
      transform: translate(0, -50%);
    }
    .has-tooltip:hover::after,
    .has-tooltip:focus-visible::after {
      opacity: 1;
      transform: translate(0, -50%);
    }
    .ttl-field .has-tooltip::before {
      top: calc(100% + 10px);
      right: 0;
      max-width: 280px;
      transform: translateY(-4px);
    }
    .ttl-field .has-tooltip::after {
      top: calc(100% + 3px);
      right: 8px;
      border-left: 7px solid transparent;
      border-right: 7px solid transparent;
      border-bottom: 7px solid var(--surface);
      border-top: 0;
      transform: translateY(-4px);
    }
    .ttl-field .has-tooltip:hover::before,
    .ttl-field .has-tooltip:focus-visible::before,
    .ttl-field .has-tooltip:hover::after,
    .ttl-field .has-tooltip:focus-visible::after {
      transform: translateY(0);
    }
    @media (min-width: 1800px) {
      .cards {
        grid-template-columns:repeat(6, minmax(0, 1fr));
        grid-template-areas:"capacity capacity capacity ram total total";
      }
    }
    @media (max-width: 980px) {
      .cards {
        grid-template-columns:repeat(2, minmax(0, 1fr));
        grid-template-areas:
          "capacity capacity"
          "ram total";
      }
      .connect-card { grid-template-columns: 1fr; align-items:stretch; }
      .connect-card button { width: 100%; }
    }
    @media (max-width: 820px) {
      .cards {
        grid-template-columns:1fr;
        grid-template-areas:
          "capacity"
          "ram"
          "total";
      }
      .connect-card { grid-template-columns: 1fr; align-items:stretch; }
      .connect-card button { width: 100%; }
    }
    @media (max-width: 760px) {
      body { margin: 16px; }
      .theme-toggle { top: 12px; right: 12px; }
      .top { padding-right: 48px; }
      .session-card.visible { align-items:stretch; flex-direction:column; }
      .metric-line { white-space:normal; }
      .toolbar { align-items: stretch; }
      .toolbar input, .toolbar select, .toolbar textarea, .toolbar button { width: 100%; min-width: 0 !important; }
      .create-toolbar { align-items:stretch; margin-bottom:0; }
      .field-user { min-height:auto; padding:0; }
      .port-input, .ttl-field { width:100%; flex:1 1 auto; }
      .advanced-grid { grid-template-columns:1fr; }
      .image-picker { width:100%; min-width:0; }
      .image-links { position:static; }
      .image-links button, .image-links a { width:auto !important; }
      .connect-card { grid-template-columns: 1fr; align-items:stretch; }
      .connect-card button { width: 100%; }
      .table-wrap {
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        border: 1px solid var(--border);
        border-radius: 8px;
      }
      .table-wrap table { min-width: 980px; border-radius: 0; }
    }
  </style>
</head>
<body>
  <button class=\"theme-toggle secondary\" id=\"themeToggle\" type=\"button\" onclick=\"toggleTheme()\" aria-label=\"Switch to light mode\" title=\"Switch theme\">&#9728;</button>
  <div class=\"top\">
    <h1 style=\"margin:0\">Liferay Environment Broker</h1>
  </div>

  <div id=\"loginCard\" class=\"card login-card\">
    <div class=\"connect-card\">
      <input id=\"token\" placeholder=\"Bearer token\" type=\"password\" />
      <button id=\"connectButton\" onclick=\"loadAll()\">Connect</button>
    </div>
  </div>

  <div id=\"sessionCard\" class=\"card session-card\">
    <div>
      <strong id=\"sessionUser\">Connected</strong>
      <div id=\"sessionEndpoint\" class=\"small\"></div>
    </div>
    <button class=\"secondary\" onclick=\"disconnect()\">Change token</button>
  </div>

  <div id=\"message\" class=\"message small\" role=\"status\" aria-live=\"polite\"></div>

  <div id=\"authenticatedArea\" class=\"authenticated-area\">
  <div id=\"stats\" class=\"cards\"></div>

  <div class=\"card\" style=\"margin-bottom:16px\">
    <h2 style=\"margin-top:0\">Create Environment</h2>
    <div class=\"toolbar create-toolbar\">
      <div class=\"field-user\"><span>User</span><strong id=\"userLabel\">-</strong></div>
      <input id=\"user\" type=\"hidden\" />
      <div class=\"image-picker\">
        <input id=\"image\" placeholder=\"image\" value=\"liferay/dxp:7.4.13.nightly\" list=\"imageSuggestions\" onfocus=\"loadImageSuggestions()\" />
        <datalist id=\"imageSuggestions\"></datalist>
        <div class=\"small image-links\">
          <button class=\"link-button\" type=\"button\" onclick=\"loadImageSuggestions(true)\">Refresh image suggestions</button>
          <a href=\"https://hub.docker.com/r/liferay/dxp/tags\" target=\"_blank\" rel=\"noreferrer\">Liferay Docker</a>
        </div>
      </div>
      <select id=\"profile\">
        <option value=\"small\">small</option>
        <option value=\"standard\" selected>standard</option>
        <option value=\"large\">large</option>
      </select>
      <select id=\"db_mode\" onchange=\"syncDbFields()\">
        <option value=\"none\" selected>No external DB</option>
        <option value=\"external\">External DB</option>
      </select>
      <input id=\"port\" class=\"port-input\" placeholder=\"port\" maxlength=\"5\" inputmode=\"numeric\" />
      <div class=\"ttl-field\">
        <input id=\"ttl\" placeholder=\"ttl hours, max 120, 0 = no TTL\" inputmode=\"numeric\" />
        <span class=\"info-icon has-tooltip\" tabindex=\"0\" data-tooltip=\"Time to live in hours. Empty uses the default. Maximum is 120 hours. Use 0 to disable maximum lifetime expiration. Environments are still stopped after 60 minutes without access.\">i</span>
      </div>
      <button id=\"createButton\" onclick=\"createEnv()\">Create</button>
    </div>
    <p class=\"small\">Environment and database fields use JSON. Portal properties use .properties syntax.</p>
    <div class=\"advanced-grid\">
      <div class=\"field-block\">
        <label for=\"env\">Environment variables JSON</label>
        <textarea id=\"env\" placeholder='{\"LIFERAY_JVM_OPTS\":\"-Xms2g -Xmx4g\"}' rows=\"4\"></textarea>
      </div>
      <div id=\"dbEnvField\" class=\"field-block db-env-field hidden\">
        <label for=\"db_env\">External DB variables JSON</label>
        <textarea id=\"db_env\" placeholder='{\"LIFERAY_JDBC_PERIOD_DEFAULT_PERIOD_URL\":\"jdbc:postgresql://...\"}' rows=\"4\"></textarea>
      </div>
    </div>
    <details class=\"advanced-properties\">
      <summary>Advanced portal-ext.properties</summary>
      <textarea id=\"props\" placeholder=\"feature.flag.LPD-12345=true&#10;feature.flag.LPD-12345.system=true\" rows=\"5\"></textarea>
    </details>
  </div>

  <div class=\"table-wrap\">
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Status</th><th>User</th><th>Image</th><th>Port</th><th style=\"min-width:210px\">URL</th><th>Access</th><th>Created / TTL</th><th>Actions</th>
        </tr>
      </thead>
      <tbody id=\"rows\"></tbody>
    </table>
  </div>
  </div>

<script>
const $ = (id) => document.getElementById(id);
const storageKeys = {
  token: "liferayBroker.token",
  user: "liferayBroker.user",
  showHistory: "liferayBroker.showHistory",
  theme: "liferayBroker.theme"
};
let imageSuggestionsLoaded = false;
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const toggle = $("themeToggle");
  const switchingToLight = theme === "dark";
  toggle.innerHTML = switchingToLight ? "&#9728;" : "&#9790;";
  toggle.setAttribute("aria-label", switchingToLight ? "Switch to light mode" : "Switch to dark mode");
  toggle.title = switchingToLight ? "Switch to light mode" : "Switch to dark mode";
}
function restoreSavedInputs() {
  if ($("token")) $("token").value = localStorage.getItem(storageKeys.token) || "";
  if ($("user")) $("user").value = localStorage.getItem(storageKeys.user) || "";
  if ($("showHistory")) $("showHistory").checked = localStorage.getItem(storageKeys.showHistory) === "true";
  applyTheme(localStorage.getItem(storageKeys.theme) || "dark");
}
function saveInputs() {
  if ($("token")) localStorage.setItem(storageKeys.token, $("token").value);
  if ($("user")) localStorage.setItem(storageKeys.user, $("user").value);
  if ($("showHistory")) localStorage.setItem(storageKeys.showHistory, $("showHistory").checked ? "true" : "false");
}
function setTokenUser(user) {
  if (!$("user") || !user) return;
  $("user").value = user;
  if ($("userLabel")) $("userLabel").textContent = user;
  saveInputs();
}
function brokerBaseUrl() {
  return window.location.origin;
}
function setConnectedSession(user) {
  $("loginCard").classList.add("hidden");
  $("sessionCard").classList.add("visible");
  $("authenticatedArea").classList.add("visible");
  $("sessionUser").textContent = `Connected as ${user}`;
  $("sessionEndpoint").textContent = brokerBaseUrl();
}
function disconnect() {
  localStorage.removeItem(storageKeys.token);
  localStorage.removeItem(storageKeys.user);
  if ($("token")) $("token").value = "";
  if ($("user")) {
    $("user").value = "";
  }
  if ($("userLabel")) $("userLabel").textContent = "-";
  $("stats").innerHTML = "";
  $("rows").innerHTML = "";
  $("sessionCard").classList.remove("visible");
  $("loginCard").classList.remove("hidden");
  $("authenticatedArea").classList.remove("visible");
  setMessage("");
}
function toggleTheme() {
  const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem(storageKeys.theme, nextTheme);
  applyTheme(nextTheme);
}
restoreSavedInputs();
function syncDbFields() {
  const showDbEnv = $("db_mode") && $("db_mode").value === "external";
  if ($("dbEnvField")) $("dbEnvField").classList.toggle("hidden", !showDbEnv);
  if (!showDbEnv && $("db_env")) $("db_env").value = "";
}
syncDbFields();
function bindSavedInput(id) {
  if (!$(id)) return;
  if ($(id).dataset.bound === "true") return;
  $(id).dataset.bound = "true";
  $(id).addEventListener("input", saveInputs);
  $(id).addEventListener("change", () => {
    saveInputs();
    if ($("token").value) loadAll();
  });
}
["token", "user"].forEach(bindSavedInput);
function authHeaders() {
  saveInputs();
  return {
    "Authorization": `Bearer ${$("token").value}`,
    "Content-Type": "application/json"
  };
}
function setMessage(text, isError=false) {
  $("message").textContent = text;
  $("message").classList.toggle("visible", Boolean(text));
  $("message").classList.toggle("error", isError);
  $("message").style.color = isError ? "var(--danger)" : "var(--text-muted)";
}
function setButtonBusy(id, busy, label) {
  const button = $(id);
  if (!button) return;
  if (!button.dataset.defaultLabel) button.dataset.defaultLabel = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.defaultLabel;
}
function renderImageSuggestions(tags) {
  const datalist = $("imageSuggestions");
  if (!datalist) return;
  datalist.innerHTML = tags.map((tag) => `<option value=\"${tag.image}\">${tag.name}</option>`).join("");
}
async function loadImageSuggestions(force=false) {
  if (imageSuggestionsLoaded && !force) return;
  if (!$("token").value) return;
  try {
    const data = await api("/v1/images/liferay-dxp-tags");
    renderImageSuggestions(data.tags || []);
    imageSuggestionsLoaded = true;
  } catch (e) {
    if (force) setMessage(e.message, true);
  }
}
async function api(path, options={}) {
  const res = await fetch(`${brokerBaseUrl()}${path}`, {
    ...options,
    headers: {...authHeaders(), ...(options.headers || {})}
  });
  if (!res.ok) throw new Error(formatApiError(await res.text(), res.status));
  return await res.json();
}
function formatApiError(rawText, status) {
  let detail = rawText;
  try {
    const parsed = JSON.parse(rawText);
    detail = parsed.detail || rawText;
  } catch (_) {}
  const friendly = {
    "Maximum active environments reached": "You already have the maximum number of active environments. Delete your current environment before creating a new one.",
    "Not enough platform capacity available": "There is not enough shared platform capacity for this profile right now. Try a smaller profile or delete another environment.",
    "User capacity limit reached": "This profile would exceed your capacity limit. Delete your current environment or choose a smaller profile.",
    "Missing Bearer token": "Enter your Bearer token before connecting.",
    "Invalid token": "The Bearer token is not valid for this broker.",
    "Payload user does not match the token user": "The selected user does not match the Bearer token. Use the user assigned to that token.",
    "Image is not allowed": "This Docker image is not allowed by the broker rules.",
    "Insufficient memory available": "There is not enough free RAM for this profile right now.",
    "Requested port is already in use": "That port is already in use. Leave the port empty or choose another one."
  };
  return friendly[detail] || detail || `Request failed with status ${status}`;
}
function renderStats(data) {
  const cards = [];
  if (data.capacity) cards.push(renderCapacityCard(data.capacity));
  cards.push(`<div class=\"card metric-card ram-card\"><div class=\"metric-line\"><strong>RAM</strong><span>${data.memory.available_mb} MB</span></div><div class=\"small\">Available of ${data.memory.total_mb} MB</div></div>`);
  cards.push(`<div class=\"card metric-card total-card\"><div class=\"metric-line\"><strong>Visible Environments</strong><span>${data.total}</span></div><label class=\"small history-toggle\"><input id=\"showHistory\" type=\"checkbox\" /> Show history</label></div>`);
  $("stats").innerHTML = cards.join("");
  if ($("showHistory")) {
    $("showHistory").checked = localStorage.getItem(storageKeys.showHistory) === "true";
    bindSavedInput("showHistory");
  }
}
function renderCapacityCard(capacity) {
  const total = capacity.total_units || 0;
  const used = capacity.used_units || 0;
  const percent = total ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const costs = Object.entries(capacity.profile_units || {})
    .map(([name, units]) => `<span class=\"profile-cost\">${name}: ${units}u</span>`)
    .join("");
  return `
    <div class=\"card metric-card capacity-card\">
      <div class=\"metric-line\"><strong>Machine Capacity</strong><span>${used}/${total || "-"}</span></div>
      <div class=\"capacity-bar\"><div class=\"capacity-fill\" style=\"width:${percent}%\"></div></div>
      <div class=\"small\">Platform: ${capacity.active_environments} active environments</div>
      <div class=\"small\">Token ${capacity.user}: ${capacity.user_units}/${capacity.user_unit_limit || "-"} units, ${capacity.user_active_environments}/${capacity.max_user_environments || "-"} environments</div>
      <div class=\"profile-costs\">${costs}</div>
    </div>
  `;
}
function formatTtl(expiresAt) {
  if (!expiresAt) return "TTL none";
  const remainingMs = new Date(expiresAt) - new Date();
  if (remainingMs <= 0) return "TTL expired";
  const totalMinutes = Math.ceil(remainingMs / 60000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `TTL ${days}d ${hours}h left`;
  if (hours > 0) return `TTL ${hours}h ${minutes}m left`;
  return `TTL ${minutes}m left`;
}
function renderRows(items) {
  const visibleItems = $("showHistory") && $("showHistory").checked
    ? items
    : items.filter((item) => !["deleted", "failed", "stopped", "expired"].includes(item.status));
  visibleItems.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
  $("rows").innerHTML = visibleItems.map(item => `
    <tr>
      <td>${item.id}<div class=\"small\">${item.container_name}</div></td>
      <td><span class=\"status ${item.status}\">${item.status}</span>${item.error ? `<div class=\"small\">${item.error}</div>` : ""}</td>
      <td>${item.user}<div class=\"small\">${item.profile}</div></td>
      <td>${item.image}<div class=\"small\">DB: ${item.db_mode}</div></td>
      <td>${item.host_port}</td>
      <td class=\"url\"><a href=\"${item.url}\" target=\"_blank\">${item.url}</a></td>
      <td>${item.last_access_at || "no access yet"}</td>
      <td class=\"timestamp\">${item.created_at}<div class=\"small ttl-remaining\">${formatTtl(item.expires_at)}</div></td>
      <td>
        <div class=\"actions\">
          <button class=\"secondary has-tooltip\" data-tooltip=\"Updates last access time so this environment is not stopped by the inactivity timeout.\" onclick=\"touchEnv('${item.id}')\">Touch</button>
          <button class=\"has-tooltip\" data-tooltip=\"Stops and removes the Docker container, shuts down its proxy, and removes it from this list.\" onclick=\"deleteEnv('${item.id}')\">Delete</button>
        </div>
      </td>
    </tr>
  `).join("");
}
async function loadAll() {
  try {
    setButtonBusy("connectButton", true, "Connecting...");
    saveInputs();
    const [me, stats, items] = await Promise.all([api('/v1/me'), api('/v1/dashboard'), api('/v1/environments')]);
    setTokenUser(me.user);
    setConnectedSession(me.user);
    renderStats(stats);
    renderRows(items);
    loadImageSuggestions().catch(() => {});
    setMessage(`Loaded ${visibleCount(items)} of ${items.length} environments`);
  } catch (e) {
    setMessage(e.message, true);
  } finally {
    setButtonBusy("connectButton", false);
  }
}
function visibleCount(items) {
  if ($("showHistory") && $("showHistory").checked) return items.length;
  return items.filter((item) => !["deleted", "failed", "stopped", "expired"].includes(item.status)).length;
}
function parseJsonSafe(text) {
  if (!text || !text.trim()) return {};
  return JSON.parse(text);
}
async function createEnv() {
  try {
    setButtonBusy("createButton", true, "Creating...");
    setMessage("Creating environment. Pulling the Docker image may take a little while on first use.");
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
    setMessage(`Environment ${result.id} created with status ${result.status}`);
    await loadAll();
  } catch (e) {
    setMessage(e.message, true);
  } finally {
    setButtonBusy("createButton", false);
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
if ($("token").value) loadAll();
</script>
</body>
</html>"""
