import atexit
import logging
import re
import shutil
import subprocess
import threading

import database

logger = logging.getLogger(__name__)

_TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
_stop_event = threading.Event()
_process = None
_process_lock = threading.Lock()


def _set_process(proc):
    global _process
    with _process_lock:
        _process = proc


def _terminate_process():
    global _process
    _stop_event.set()
    with _process_lock:
        proc = _process
        _process = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


atexit.register(_terminate_process)


def _run_process(cmd: list[str], label: str, detect_quick_url: bool = False) -> bool:
    logger.info("Starting %s", label)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    _set_process(proc)

    found_url = False
    try:
        if proc.stdout is not None:
            for raw_line in proc.stdout:
                if _stop_event.is_set():
                    break
                line = raw_line.strip()
                if line:
                    logger.info("cloudflared: %s", line)
                if detect_quick_url:
                    match = _TUNNEL_URL_RE.search(line)
                    if match:
                        url = match.group(0).rstrip("/")
                        database.set_runtime_public_api_url(url)
                        found_url = True
                        logger.info("Public API quick tunnel ready: %s", url)
        return_code = proc.wait()
        if not _stop_event.is_set():
            logger.warning("cloudflared exited with code %s", return_code)
    except Exception:
        logger.exception("Cloudflare tunnel process failed")
    finally:
        _set_process(None)

    return found_url


def _quick_supervisor(local_url: str):
    try:
        database.set_runtime_public_api_url("")
    except Exception:
        logger.exception("Could not clear old runtime API URL")

    while not _stop_event.is_set():
        binary = shutil.which("cloudflared")
        if not binary:
            logger.error("cloudflared not found; automatic tunnel cannot start")
            return
        cmd = [binary, "tunnel", "--url", local_url, "--no-autoupdate"]
        _run_process(cmd, f"Cloudflare Quick Tunnel -> {local_url}", detect_quick_url=True)
        if _stop_event.wait(5):
            break
        logger.info("Restarting Cloudflare quick tunnel...")


def _named_supervisor(token: str, public_url: str):
    # Fixed custom-domain URL always wins over old quick-tunnel URLs.
    try:
        database.set_runtime_public_api_url(public_url.rstrip("/"))
    except Exception:
        logger.exception("Could not save custom public API URL")

    while not _stop_event.is_set():
        binary = shutil.which("cloudflared")
        if not binary:
            logger.error("cloudflared not found; named tunnel cannot start")
            return
        cmd = [binary, "tunnel", "--no-autoupdate", "run", "--token", token]
        _run_process(cmd, f"Cloudflare named tunnel for {public_url}")
        if _stop_event.wait(5):
            break
        logger.info("Restarting Cloudflare named tunnel...")


def start_auto_tunnel(port: int) -> threading.Thread:
    local_url = f"http://127.0.0.1:{int(port)}"
    thread = threading.Thread(
        target=_quick_supervisor,
        args=(local_url,),
        daemon=True,
        name="cloudflare-quick-tunnel",
    )
    thread.start()
    return thread


def start_named_tunnel(token: str, public_url: str) -> threading.Thread:
    thread = threading.Thread(
        target=_named_supervisor,
        args=(str(token).strip(), str(public_url).strip().rstrip("/")),
        daemon=True,
        name="cloudflare-named-tunnel",
    )
    thread.start()
    return thread
