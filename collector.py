#!/usr/bin/env python3
"""
System stats collector — production version.
Reads config from environment variables or .env file.

Install:
    pip install psutil psycopg2-binary gputil python-dotenv

Run:
    python collector.py

Systemd service: see sysstats-collector.service
"""

import os
import time
import socket
import logging
import psutil
import psycopg2
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Optional NVIDIA GPU ────────────────────────────────────────────────────────
try:
    import GPUtil
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False
    log.info("GPUtil not found — GPU stats disabled")

# ── Config from environment ────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME",     "sysstats"),
    "user":     os.getenv("DB_USER",     "sysstats"),
    "password": os.getenv("DB_PASSWORD", ""),
    "connect_timeout": 10,
    "sslmode":  os.getenv("DB_SSLMODE",  "prefer"),
}

CPU_INTERVAL  = int(os.getenv("CPU_INTERVAL",  "1"))   # seconds
DISK_INTERVAL = int(os.getenv("DISK_INTERVAL", "60"))  # seconds
HOSTNAME      = os.getenv("COLLECTOR_HOST", socket.gethostname())
MAX_RECONNECT_WAIT = 60   # cap backoff at 60s

# ── DB connection with exponential backoff ─────────────────────────────────────
def get_connection():
    return psycopg2.connect(**DB_CONFIG)

def connect_with_backoff():
    wait = 2
    while True:
        try:
            conn = get_connection()
            conn.autocommit = False
            log.info("Connected to database")
            return conn
        except Exception as e:
            log.error(f"DB connection failed: {e} — retrying in {wait}s")
            time.sleep(wait)
            wait = min(wait * 2, MAX_RECONNECT_WAIT)

# ── Insert helpers ─────────────────────────────────────────────────────────────
def insert_cpu(cur, now, cpu_pct, cpu_freq, cpu_temp):
    cur.execute(
        "INSERT INTO cpu_stats (time, host, cpu_pct, cpu_freq, cpu_temp) VALUES (%s,%s,%s,%s,%s)",
        (now, HOSTNAME, cpu_pct, cpu_freq, cpu_temp),
    )

def insert_gpu(cur, now, gpus):
    for gpu in gpus:
        cur.execute(
            "INSERT INTO gpu_stats (time, host, gpu_index, gpu_pct, gpu_temp, vram_used, vram_total, fan_pct) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                now, HOSTNAME, gpu.id,
                round(gpu.load * 100, 1),
                gpu.temperature,
                int(gpu.memoryUsed  * 1024 * 1024),
                int(gpu.memoryTotal * 1024 * 1024),
                getattr(gpu, "fan", None),
            ),
        )

def insert_ram(cur, now, vm, swap):
    cur.execute(
        "INSERT INTO ram_stats (time, host, ram_used, ram_total, swap_used, swap_total) VALUES (%s,%s,%s,%s,%s,%s)",
        (now, HOSTNAME, vm.used, vm.total, swap.used, swap.total),
    )

def insert_disk(cur, now, partitions, io_before, io_after, elapsed):
    for part in partitions:
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        dev = part.device
        read_bps = write_bps = None
        if io_before and io_after and dev in io_after and dev in io_before:
            read_bps  = max(0, int((io_after[dev].read_bytes  - io_before[dev].read_bytes)  / elapsed))
            write_bps = max(0, int((io_after[dev].write_bytes - io_before[dev].write_bytes) / elapsed))
        cur.execute(
            "INSERT INTO disk_stats (time, host, mount, used, free, total, read_bps, write_bps) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (now, HOSTNAME, part.mountpoint, usage.used, usage.free, usage.total, read_bps, write_bps),
        )

def insert_net(cur, now, net_before, net_after, elapsed):
    for iface, stats in net_after.items():
        if iface == "lo" or iface not in net_before:
            continue
        sent_bps = max(0, int((stats.bytes_sent - net_before[iface].bytes_sent) / elapsed))
        recv_bps = max(0, int((stats.bytes_recv - net_before[iface].bytes_recv) / elapsed))
        cur.execute(
            "INSERT INTO net_stats (time, host, interface, bytes_sent, bytes_recv) VALUES (%s,%s,%s,%s,%s)",
            (now, HOSTNAME, iface, sent_bps, recv_bps),
        )

# ── CPU temp ───────────────────────────────────────────────────────────────────
def get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            if key in temps:
                readings = [t.current for t in temps[key]]
                return round(sum(readings) / len(readings), 1)
        all_readings = [t.current for entries in temps.values() for t in entries]
        return round(sum(all_readings) / len(all_readings), 1) if all_readings else None
    except Exception:
        return None

# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    log.info(f"Collector starting — host={HOSTNAME} cpu_interval={CPU_INTERVAL}s disk_interval={DISK_INTERVAL}s")

    conn           = connect_with_backoff()
    last_disk_time = 0
    net_before     = psutil.net_io_counters(pernic=True)
    disk_io_before = psutil.disk_io_counters(perdisk=True)
    last_tick      = time.monotonic()

    # Warm up cpu_percent (first call always returns 0.0)
    psutil.cpu_percent(interval=None)

    try:
        while True:
            time.sleep(CPU_INTERVAL)
            now     = datetime.now(timezone.utc)
            elapsed = max(time.monotonic() - last_tick, 0.001)  # avoid div/0
            last_tick = time.monotonic()

            net_after = psutil.net_io_counters(pernic=True)
            cpu_pct   = psutil.cpu_percent(interval=None)
            freq      = psutil.cpu_freq()
            cpu_freq  = round(freq.current, 0) if freq else None
            cpu_temp  = get_cpu_temp()
            vm        = psutil.virtual_memory()
            swap      = psutil.swap_memory()
            gpus      = GPUtil.getGPUs() if GPU_AVAILABLE else []

            try:
                with conn.cursor() as cur:
                    insert_cpu(cur, now, cpu_pct, cpu_freq, cpu_temp)
                    insert_ram(cur, now, vm, swap)
                    insert_net(cur, now, net_before, net_after, elapsed)
                    if gpus:
                        insert_gpu(cur, now, gpus)

                    if time.monotonic() - last_disk_time >= DISK_INTERVAL:
                        disk_io_after = psutil.disk_io_counters(perdisk=True)
                        disk_elapsed  = (time.monotonic() - last_disk_time) if last_disk_time else elapsed
                        partitions    = psutil.disk_partitions(all=False)
                        insert_disk(cur, now, partitions, disk_io_before, disk_io_after, disk_elapsed)
                        disk_io_before = disk_io_after
                        last_disk_time = time.monotonic()

                conn.commit()

                # Compact log line — systemd/journalctl friendly
                gpu_str = f" | GPU {gpus[0].load*100:.0f}% {gpus[0].temperature:.0f}°C" if gpus else ""
                log.info(f"CPU {cpu_pct:.1f}% {f'{cpu_temp:.0f}°C' if cpu_temp else 'N/A'} | RAM {vm.percent:.1f}%{gpu_str}")

            except psycopg2.OperationalError as e:
                log.error(f"DB connection lost: {e} — reconnecting")
                conn.rollback()
                try: conn.close()
                except Exception: pass
                conn = connect_with_backoff()

            except Exception as e:
                log.error(f"Insert error: {e}")
                try: conn.rollback()
                except Exception: pass

            net_before = net_after

    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        try: conn.close()
        except Exception: pass

if __name__ == "__main__":
    main()
