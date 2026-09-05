"""Admin authentication.

Deliberately small: password hashing and opaque session tokens out of the
standard library, so this adds no dependency to a machine that has to run
unattended on an SD card.

Threat model worth stating, because it shapes the choices below. The
vendo broadcasts an OPEN WiFi network. Every customer in range can reach
this app. So the admin surface is exposed to the public by design, and
"nobody can reach it anyway" is never an available assumption.
"""
import os
import hmac
import base64
import hashlib
import logging
import secrets
import time

logger = logging.getLogger(__name__)

# PBKDF2 rather than scrypt: it is available everywhere without relying on
# the platform OpenSSL build, which matters on an Armbian image we do not
# control.
#
# OWASP suggests 600k iterations for PBKDF2-SHA256, which measured at ~1.9s
# on a desktop and would be several seconds on an SBC. That is not just a
# slow login: it is an unauthenticated denial of service, since anyone can
# post to /admin/login and make the machine chew CPU. 200k keeps a
# brute-force expensive while staying usable on an Orange Pi, and the
# per-IP lockout below is what actually limits guessing.
#
# The count used for each password is stored WITH that password, so this
# can be raised later without locking anyone out of an existing hash.
DEFAULT_ITERATIONS = int(os.getenv("ECOWIFI_PBKDF2_ITERATIONS", "200000"))

# Hashes written before the iteration count was recorded used this value.
LEGACY_ITERATIONS = 600_000
SESSION_HOURS = 12
COOKIE_NAME = "ecowifi_admin"

# Brute-force throttling. The lockout is per client IP and held in memory:
# losing it on restart is acceptable, and it keeps the database out of the
# path of an attack that is trying to hammer the database.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
_failed_attempts = {}


def hash_password(password: str, salt: bytes = None, iterations: int = None):
    """Returns (salt_b64, hash_b64, iterations) for storage.

    CPU-bound and slow by design -- call it through
    starlette.concurrency.run_in_threadpool so it does not stall the event
    loop and take the customer portal down with it."""
    salt = salt or secrets.token_bytes(16)
    iterations = iterations or DEFAULT_ITERATIONS
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return (base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
            iterations)


def verify_password(password: str, salt_b64: str, hash_b64: str,
                    iterations: int = None) -> bool:
    """Constant-time comparison, so a wrong password cannot be narrowed
    down by timing how long the rejection took.

    `iterations` comes from storage rather than the current default, so
    changing the default does not invalidate existing passwords."""
    if not salt_b64 or not hash_b64:
        return False
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except Exception:
        return False

    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations or LEGACY_ITERATIONS
    )
    return hmac.compare_digest(candidate, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


# ---- first-run setup ----
# Generated once per process when no admin password exists yet. It is
# printed to the log, which on the Orange Pi means `journalctl -u
# ecowifi-app`. Requiring it stops a customer on the open SSID from
# reaching /admin/setup first and claiming the machine -- without it,
# first-run setup is a race that the operator can lose.

_setup_token = None


def setup_token() -> str:
    global _setup_token
    if _setup_token is None:
        _setup_token = secrets.token_urlsafe(9)
    return _setup_token


def clear_setup_token():
    global _setup_token
    _setup_token = None


def check_setup_token(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, setup_token())


def bootstrap_password_from_env():
    """Lets install.sh (or systemd) preset the password without anyone
    touching a browser. Returns the password, or None if unset."""
    value = os.getenv("ECOWIFI_ADMIN_PASSWORD")
    return value.strip() if value and value.strip() else None


# ---- throttling ----

def record_failure(ip: str):
    count, _ = _failed_attempts.get(ip, (0, 0))
    _failed_attempts[ip] = (count + 1, time.time())


def clear_failures(ip: str):
    _failed_attempts.pop(ip, None)


def lockout_remaining(ip: str) -> int:
    """Seconds this IP must wait, or 0 if it may try now."""
    count, last = _failed_attempts.get(ip, (0, 0))
    if count < MAX_ATTEMPTS:
        return 0

    elapsed = time.time() - last
    if elapsed >= LOCKOUT_SECONDS:
        _failed_attempts.pop(ip, None)
        return 0
    return int(LOCKOUT_SECONDS - elapsed)
