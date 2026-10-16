#!/usr/bin/env python3
"""Hardware monitor daemon.

Collects GPU (NVIDIA x4 + AMD), CPU and RAM stats every REFRESH_INTERVAL_SECONDS
and serves the latest snapshot as JSON on GET /stats.
"""

import csv
import io
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5555"))
REFRESH_INTERVAL = float(os.getenv("REFRESH_INTERVAL_SECONDS", "1"))
NVIDIA_SMI_CMD = os.getenv("NVIDIA_SMI_CMD", "nvidia-smi")
AMD_GPU_NAME = os.getenv("AMD_GPU_NAME", "AMD GPU")
AMD_DRM_CARD = os.getenv("AMD_DRM_CARD", "")  # empty = auto-detect first AMD card
CPU_TEMP_HWMON = os.getenv("CPU_TEMP_HWMON", "k10temp")

NVIDIA_QUERY = (
    "index,name,temperature.gpu,utilization.gpu,memory.total,memory.used,"
    "power.draw,power.limit"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hwmon")


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- NVIDIA

def read_nvidia_gpus():
    """Query all NVIDIA GPUs via nvidia-smi CSV output."""
    cmd = [
        NVIDIA_SMI_CMD,
        f"--query-gpu={NVIDIA_QUERY}",
        "--format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("nvidia-smi failed: %s", exc)
        return []
    if out.returncode != 0:
        log.warning("nvidia-smi error rc=%s: %s", out.returncode, out.stderr.strip())
        return []

    gpus = []
    for row in csv.reader(io.StringIO(out.stdout)):
        if len(row) < 8:
            continue
        idx, name, temp, util, mem_total, mem_used, p_draw, p_limit = row[:8]
        gpus.append(
            {
                "vendor": "nvidia",
                "index": _to_int(idx),
                "name": name.strip(),
                "temperature_c": _to_int(temp),
                "utilization_pct": _to_int(util),
                "vram_used_mib": _to_int(mem_used),
                "vram_total_mib": _to_int(mem_total),
                "power_current_w": _to_float(p_draw),
                "power_max_w": _to_float(p_limit),
            }
        )
    return gpus


# ---------------------------------------------------------------- AMD GPU

def find_amd_device():
    """Return the sysfs device dir of the (pinned or first) AMD GPU."""
    if AMD_DRM_CARD:
        dev = Path("/sys/class/drm") / AMD_DRM_CARD / "device"
        return dev if dev.is_dir() else None
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        vendor_file = card / "device" / "vendor"
        try:
            if vendor_file.read_text().strip() == "0x1002":  # AMD
                return card / "device"
        except OSError:
            continue
    return None


def read_amd_gpu(device):
    """Read one AMD GPU from sysfs (busy %, vram, hwmon temp/power)."""
    if device is None:
        return None
    try:
        busy = _to_int((device / "gpu_busy_percent").read_text())
        vram_used = _to_int((device / "mem_info_vram_used").read_text())  # bytes
        vram_total = _to_int((device / "mem_info_vram_total").read_text())  # bytes
    except OSError as exc:
        log.warning("AMD GPU sysfs read failed: %s", exc)
        return None

    temp_c = power_w = power_cap_w = None
    hwmon_link = device / "hwmon"
    if hwmon_link.is_dir():
        for h in sorted(hwmon_link.iterdir()):
            try:
                temp_raw = _to_int((h / "temp1_input").read_text())
            except OSError:
                continue
            temp_c = temp_raw // 1000 if temp_raw is not None else None
            try:
                avg_uw = _to_int((h / "power1_average").read_text())  # microwatts (PPT)
                cap_uw = _to_int((h / "power1_cap").read_text())  # microwatts
            except OSError:
                avg_uw = cap_uw = None
            power_w = round(avg_uw / 1_000_000, 1) if avg_uw is not None else None
            power_cap_w = cap_uw // 1_000_000 if cap_uw is not None else None
            break

    return {
        "vendor": "amd",
        "name": AMD_GPU_NAME,
        "temperature_c": temp_c,
        "utilization_pct": busy,
        "vram_used_mib": vram_used // (1024 * 1024) if vram_used is not None else None,
        "vram_total_mib": vram_total // (1024 * 1024) if vram_total is not None else None,
        "power_current_w": power_w,
        "power_max_w": power_cap_w,
    }


# ---------------------------------------------------------------- CPU

def _cpu_times():
    """Return (total_jiffies, idle_jiffies) from /proc/stat."""
    with open("/proc/stat") as f:
        fields = f.readline().split()
    vals = [int(x) for x in fields[1:]]
    total = sum(vals)
    idle = vals[3] + vals[4]  # idle + iowait
    return total, idle


class CpuUtilization:
    """Computes utilization from successive /proc/stat samples."""

    def __init__(self):
        self._prev = None
        self._lock = threading.Lock()

    def sample(self):
        total, idle = _cpu_times()
        with self._lock:
            if self._prev is None:
                self._prev = (total, idle)
                return None
            prev_total, prev_idle = self._prev
            self._prev = (total, idle)
            d_total = total - prev_total
            d_busy = d_total - (idle - prev_idle)
            if d_total <= 0:
                return None
            return round(100.0 * d_busy / d_total, 1)


def find_cpu_hwmons():
    dirs = []
    for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (h / "name").read_text().strip() == CPU_TEMP_HWMON:
                dirs.append(h)
        except OSError:
            continue
    return dirs


def read_cpu_temperature(hwmons):
    temps = []
    for h in hwmons:
        for f in sorted(h.glob("temp*_input")):
            v = _to_int(f.read_text())
            if v is not None:
                temps.append(v)
    return max(temps) // 1000 if temps else None


# ---------------------------------------------------------------- RAM

def read_ram():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                info[key] = int(parts[0])  # kB
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", max(0, total - info.get("MemFree", 0)))
    return {
        "total_mib": total // 1024,
        "used_mib": (total - available) // 1024,
        "available_mib": available // 1024,
    }


# ---------------------------------------------------------------- Collector

class StatsStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.latest = {
            "timestamp": None,
            "gpus": [],
            "cpu": {},
            "ram": {},
        }

    def set(self, snapshot):
        with self._lock:
            self.latest = snapshot  # reference swap; snapshots are immutable after publish

    def get(self):
        with self._lock:
            return self.latest


def build_snapshot(store_deps):
    nvidia_gpus = read_nvidia_gpus()
    amd_gpu = read_amd_gpu(store_deps["amd_device"])
    gpus = [g for g in nvidia_gpus + [amd_gpu] if g is not None]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "gpus": gpus,
        "cpu": {
            "name": store_deps["cpu_name"],
            "temperature_c": read_cpu_temperature(store_deps["cpu_hwmons"]),
            "utilization_pct": store_deps["cpu_util"].sample(),
        },
        "ram": read_ram(),
    }


def refresh_loop(store, deps):
    while True:
        start = time.monotonic()
        try:
            store.set(build_snapshot(deps))
        except Exception:  # never let the loop die
            log.exception("collector iteration failed")
        elapsed = time.monotonic() - start
        time.sleep(max(0.1, REFRESH_INTERVAL - elapsed))


def read_cpu_name():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


# ---------------------------------------------------------------- HTTP API

DASHBOARD_PATH = BASE_DIR / "web" / "index.html"
ST_DASHBOARD_PATH = BASE_DIR / "web" / "st.html"


def create_app(store):
    app = FastAPI(title="hardware-monitor")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return HTMLResponse(
            content=DASHBOARD_PATH.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/st", include_in_schema=False)
    def st_dashboard():
        return HTMLResponse(
            content=ST_DASHBOARD_PATH.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/stats")
    def stats():
        return store.get()

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "last_update": store.get()["timestamp"],
        }

    return app


def main():
    deps = {
        "amd_device": find_amd_device(),
        "cpu_name": read_cpu_name(),
        "cpu_hwmons": find_cpu_hwmons(),
        "cpu_util": CpuUtilization(),
    }
    if deps["amd_device"] is None:
        log.warning("no AMD GPU found in /sys/class/drm")
    if not deps["cpu_hwmons"]:
        log.warning("CPU hwmon '%s' not found, CPU temperature will be null", CPU_TEMP_HWMON)

    store = StatsStore()
    threading.Thread(target=refresh_loop, args=(store, deps), daemon=True).start()

    app = create_app(store)
    log.info("listening on %s:%s (refresh every %.1fs)", HOST, PORT, REFRESH_INTERVAL)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
