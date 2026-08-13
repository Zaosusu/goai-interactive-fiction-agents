from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.agents.visual_asset_generation.background_removal import PROJECT_ROOT, REMBG_MODEL_MINIMUM_BYTES, _rembg_model_root

MODULE_NAME = "app.agents.visual_asset_generation.model_manager"
MODEL_MINIMUM_BYTES = REMBG_MODEL_MINIMUM_BYTES


@dataclass(frozen=True)
class RembgModelStatus:
    model: str
    path: str
    state: str
    size_bytes: int
    updated_at: str
    error: str = ""
    pid: int = 0
    log_path: str = ""


def _job_path(model: str) -> Path:
    return _rembg_model_root() / ".jobs" / f"{model}.json"


def _launch_lock_path(model: str) -> Path:
    return _rembg_model_root() / ".jobs" / f"{model}.launch.lock"


def _read_job(model: str) -> dict:
    path = _job_path(model)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_job(status: RembgModelStatus) -> None:
    path = _job_path(status.model)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(asdict(status), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def _job_is_recent(job: dict, *, seconds: int = 120) -> bool:
    try:
        updated = datetime.fromisoformat(str(job.get("updated_at") or ""))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated).total_seconds() <= seconds
    except ValueError:
        return False


def _model_minimum_bytes(model: str) -> int:
    return MODEL_MINIMUM_BYTES.get(model, 1_000_000)


def rembg_model_status(model: str) -> RembgModelStatus:
    path = _rembg_model_root() / f"{model}.onnx"
    size = path.stat().st_size if path.is_file() else 0
    job = _read_job(model)
    job_pid = int(job.get("pid") or 0)
    job_state = str(job.get("state") or "")
    if size >= _model_minimum_bytes(model):
        state = "ready"
    elif job_state == "downloading" and _process_is_alive(job_pid):
        state = "downloading"
    elif job_state == "starting" and _job_is_recent(job):
        state = "downloading"
    elif job_state in {"starting", "downloading"}:
        state = "failed"
    elif job_state == "failed":
        state = "failed"
    else:
        state = "missing"
    return RembgModelStatus(
        model=model,
        path=str(path),
        state=state,
        size_bytes=size,
        updated_at=str(job.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        error=str(
            job.get("error")
            or ("model download process exited before the verified ONNX file was ready" if state == "failed" else "")
        ),
        pid=job_pid if state == "downloading" else 0,
        log_path=str(job.get("log_path") or ""),
    )


def download_rembg_model(model: str) -> RembgModelStatus:
    root = _rembg_model_root()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["U2NET_HOME"] = str(root)
    existing = rembg_model_status(model)
    if existing.state == "ready":
        return existing
    log_path = str((_read_job(model).get("log_path") or ""))
    _write_job(
        RembgModelStatus(
            model=model,
            path=str(root / f"{model}.onnx"),
            state="downloading",
            size_bytes=0,
            updated_at=datetime.now(timezone.utc).isoformat(),
            pid=os.getpid(),
            log_path=log_path,
        )
    )
    try:
        from rembg import new_session

        # rembg/pooch verifies the official checksum before returning. The
        # session is created in this helper process and released on exit; the
        # API process will load the verified file only when it is needed.
        new_session(model)
        ready = rembg_model_status(model)
        ready = RembgModelStatus(
            **{
                **asdict(ready),
                "state": "ready",
                "pid": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_job(ready)
        return ready
    except Exception as exc:
        current = rembg_model_status(model)
        failed = RembgModelStatus(
            **{
                **asdict(current),
                "state": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "pid": 0,
                "log_path": log_path,
            }
        )
        _write_job(failed)
        return failed


def start_rembg_model_download(model: str) -> dict[str, str | int]:
    """Start a detached project-local downloader without blocking the API."""
    current = rembg_model_status(model)
    if current.state in {"ready", "downloading"}:
        return {
            "model": model,
            "state": current.state,
            "pid": current.pid,
            "model_dir": str(_rembg_model_root()),
            "log_path": current.log_path,
        }
    root = _rembg_model_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = _launch_lock_path(model)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd: int | None = None
    for attempt in range(2):
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(lock_fd, f"{os.getpid()}\n".encode("ascii"))
            break
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 120
            except OSError:
                stale = False
            if stale and attempt == 0:
                lock_path.unlink(missing_ok=True)
                continue
            return {
                "model": model,
                "state": "downloading",
                "pid": 0,
                "model_dir": str(root),
                "log_path": str((_read_job(model).get("log_path") or "")),
            }
    log_dir = PROJECT_ROOT / "output" / "model_downloads"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"rembg-{model}.log"
    command = [sys.executable, "-m", MODULE_NAME, "download", model]
    _write_job(
        RembgModelStatus(
            model=model,
            path=str(root / f"{model}.onnx"),
            state="starting",
            size_bytes=0,
            updated_at=datetime.now(timezone.utc).isoformat(),
            log_path=str(log_path),
        )
    )
    try:
        popen_options: dict = {"close_fds": True}
        if os.name == "nt":
            popen_options["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            popen_options["start_new_session"] = True
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                **popen_options,
            )
        observed = _read_job(model)
        if str(observed.get("state") or "") == "starting":
            _write_job(
                RembgModelStatus(
                    model=model,
                    path=str(root / f"{model}.onnx"),
                    state="downloading",
                    size_bytes=0,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    pid=process.pid,
                    log_path=str(log_path),
                )
            )
        return {
            "model": model,
            "state": "downloading",
            "pid": process.pid,
            "model_dir": str(root),
            "log_path": str(log_path),
        }
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def ensure_rembg_models_async(models: tuple[str, ...] | None = None) -> list[dict[str, str | int]]:
    """Ensure configured models in background; safe to call on every API startup."""
    if models is None:
        configured = os.getenv("REMBG_AUTO_MODELS", "u2netp")
        models = tuple(dict.fromkeys(item.strip() for item in configured.split(",") if item.strip()))
    return [start_rembg_model_download(model) for model in models]


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage project-local rembg models")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "download", "start"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("model")
    args = parser.parse_args()
    if args.command == "status":
        payload = asdict(rembg_model_status(args.model))
    elif args.command == "download":
        payload = asdict(download_rembg_model(args.model))
    else:
        payload = start_rembg_model_download(args.model)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("state") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
