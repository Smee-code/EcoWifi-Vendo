"""Gateway-side health checks for the admin System Status panel.

Everything here degrades gracefully rather than raising: this panel is
what you look at when something is already wrong, so a check that cannot
run must report 'unavailable' instead of taking the page down with it.
"""
import os
import shutil
import time
import subprocess
import sys
import logging

logger = logging.getLogger(__name__)

APP_STARTED_AT = time.time()

# The OS-level pieces the spec (section 3.3) expects to be running.
WATCHED_SERVICES = ("hostapd", "dnsmasq", "nftables", "ecowifi-app")

IS_LINUX = sys.platform.startswith("linux")


def uptime_seconds():
    """How long this app process has been up (not the machine)."""
    return int(time.time() - APP_STARTED_AT)


def service_status(name):
    """systemd state for one unit.

    Returns 'active', 'inactive', 'failed', ... or 'unavailable' where
    systemd does not exist (a dev laptop), which is a different thing
    from a service being down and must not be shown as an outage."""
    if not IS_LINUX:
        return "unavailable"

    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return "unavailable"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        logger.warning(f"Could not check service {name}: {e}")
        return "unknown"

    state = (result.stdout or "").strip()
    return state or "unknown"


def services():
    return {name: service_status(name) for name in WATCHED_SERVICES}


def disk(path="."):
    """Free space on the volume holding the database.

    An SD card that fills up silently corrupts an unattended machine, so
    this is worth showing next to everything else."""
    try:
        total, used, free = shutil.disk_usage(os.path.abspath(path))
    except Exception as e:
        logger.warning(f"Could not read disk usage: {e}")
        return {"available": False}

    return {
        "available": True,
        "total_mb": round(total / 1048576),
        "used_mb": round(used / 1048576),
        "free_mb": round(free / 1048576),
        "percent_used": round(used / total * 100, 1) if total else None,
    }


def database_size_kb(db_path):
    try:
        return round(os.path.getsize(db_path) / 1024, 1)
    except OSError:
        return None


def summary(db_path):
    return {
        "platform": sys.platform,
        "app_uptime_seconds": uptime_seconds(),
        "services": services(),
        "disk": disk(os.path.dirname(os.path.abspath(db_path)) or "."),
        "database_kb": database_size_kb(db_path),
        "systemd_available": IS_LINUX,
    }
