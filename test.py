
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
import GPUtil
try:
    import GPUtil
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False
    log.info("GPUtil not found — GPU stats disabled")
    
    
gpus = GPUtil.getGPUs()
print(gpus)
#gpu_str = f" | GPU {gpus.load*100:.0f}% {gpus[0].temperature:.0f}°C"
