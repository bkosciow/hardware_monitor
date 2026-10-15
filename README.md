# hardware_monitor

Small REST API that exposes live hardware stats for PC:
NVIDIA RTX, AMD Radeon AI PRO R9700, AMD CPU, RAM.

- `GET /` — mobile-first dashboard for a phone (see [Dashboard](#dashboard))
- `GET /stats` — latest snapshot as JSON (refreshed in the background every `REFRESH_INTERVAL_SECONDS`)
- `GET /health` — liveness + last update time

Listens on all interfaces, port 5555 by default. Configuration lives in `.env`.

## Dashboard

`GET /` serves a self-contained page (no CDN) designed for a phone held vertically, e.g. a 24/7 server status display:

- Horizontal 0–100 bars: temp + utilization per GPU, CPU utilization, RAM usage (extra: CPU temp, VRAM and power current/max per GPU as text)
- Color steps — temp: green ≤40 °C, yellow ≤60, orange ≤80, red 81+; utilization: green ≤30 %, yellow ≤60, orange ≤85, red 86+
- Auto-refresh every 2 s by default; change it with the ⚙ button — persisted in browser `localStorage`
- Dark (near-black, night friendly) theme by default, light theme available — choice also persisted
- Green/red dot next to the clock shows live/offline connection state; bars dim while offline

## Run

```bash
# first time only:
python3 -m venv venv && venv/bin/pip install -r requirements.txt

venv/bin/python app.py
```

Example client:

```bash
curl http://<host>:5555/stats | python3 -m json.tool
```

Note: `cpu.utilization_pct` is `null` on the very first sample (needs two /proc/stat reads).

## JSON shape

```json
{
  "timestamp": "2026-10-08T12:00:00.000+00:00",
  "gpus": [
    {
      "vendor": "nvidia",
      "index": 0,
      "name": "NVIDIA GeForce RTX 5060 Ti",
      "temperature_c": 53,
      "utilization_pct": 100,
      "vram_used_mib": 13994,
      "vram_total_mib": 16311,
      "power_current_w": 31.5,
      "power_max_w": 150.0
    },
    {
      "vendor": "amd",
      "name": "Radeon AI PRO R9700",
      "temperature_c": 64,
      "utilization_pct": 12,
      "vram_used_mib": 8000,
      "vram_total_mib": 16384,
      "power_current_w": 45.2,
      "power_max_w": 210
    }
  ],
  "cpu": {"name": "AMD Ryzen 7 3700X 8-Core Processor", "temperature_c": 38, "utilization_pct": 12.5},
  "ram": {"total_mib": 65536, "used_mib": 24000, "available_mib": 41536}
}
```

## Data sources

| Metric | Source |
|---|---|
| NVIDIA temp/util/VRAM | `nvidia-smi --query-gpu=...` (CSV) |
| AMD temp/util/VRAM | `/sys/class/drm/cardN/device/` (gpu_busy_percent, mem_info_vram_*, hwmon temp1_input) |
| CPU temperature | k10temp hwmon (`/sys/class/hwmon/*/temp*_input`) |
| CPU utilization | `/proc/stat` delta between refresh cycles |
| RAM | `/proc/meminfo` (used = total - available) |

If a source fails on a given cycle the collector logs a warning and keeps serving; failed fields are `null`.

## Install as a system service (optional, needs sudo)

```bash
sudo cp hardware-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hardware-monitor
journalctl -u hardware-monitor -f
```
