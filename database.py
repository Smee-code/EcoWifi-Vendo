import sqlite3
import secrets
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

DB_PATH = "ecowifi.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---- time helpers ----
# Sessions are stored as an EXPIRY TIMESTAMP while running, not as a
# counter that something has to decrement. That means the countdown
# stays accurate even if the worker thread is late or the app restarts,
# and it can be displayed down to the second. A paused session has no
# expiry -- it holds its remaining seconds instead.

def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def init_db():
    with get_db() as conn:
        # A session is a device's TIME BALANCE: active, paused, or blocked.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                mac_address TEXT PRIMARY KEY,
                seconds_remaining INTEGER DEFAULT 0,
                expires_at TEXT,
                status TEXT DEFAULT 'blocked',
                claimed_at TEXT,
                bottles_inserted INTEGER DEFAULT 0
            )
        """)

        # A claim is a QUEUE ENTRY: "this device tapped Insert Plastic
        # Bottle and the next validated bottle belongs to it." Kept
        # separate from sessions on purpose -- a customer who is already
        # connected can queue another bottle to top up, which is
        # impossible if 'pending' is a session status.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_address TEXT NOT NULL,
                created_at TEXT NOT NULL,
                granted INTEGER DEFAULT 0,
                granted_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_open ON claims (granted, created_at)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_address TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                minutes_credited INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS vouchers (
                code TEXT PRIMARY KEY,
                minutes INTEGER NOT NULL,
                redeemed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                redeemed_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS rates (
                payment_method TEXT PRIMARY KEY,
                minutes_per_unit INTEGER NOT NULL
            )
        """)

        # Single-row table holding whatever the ESP32 last told us about
        # itself. Persisted rather than kept in memory so the dashboard
        # does not claim "never seen" every time the app restarts.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_seen TEXT,
                bin_full INTEGER DEFAULT 0,
                bin_distance_cm REAL,
                bottles_today INTEGER DEFAULT 0,
                firmware TEXT,
                source_ip TEXT
            )
        """)
        conn.execute("INSERT OR IGNORE INTO device_status (id) VALUES (1)")

        # Permanently banned devices. Distinct from a session being
        # 'blocked', which just means their time ran out -- a banned MAC
        # cannot claim, be granted, redeem, or resume at all.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_macs (
                mac_address TEXT PRIMARY KEY,
                reason TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # Admin credentials and logged-in sessions.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ip TEXT
            )
        """)

        _migrate(conn)

        existing = conn.execute("SELECT COUNT(*) AS c FROM rates").fetchone()
        if existing["c"] == 0:
            conn.execute(
                "INSERT INTO rates (payment_method, minutes_per_unit) VALUES (?, ?)",
                ("bottle", 30),
            )


def _migrate(conn):
    """Brings a pre-existing database up to the current schema.

    The original schema stored `minutes_remaining`, had no pause support,
    and tracked pending claims as a session status. Anything created by
    that version is converted in place rather than dropped, so an Orange
    Pi that has already taken real bottles does not lose its sessions."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}

    # Decide this up front: the ALTERs below change the table, so testing
    # for the new columns afterwards would give the wrong answer.
    needs_conversion = "minutes_remaining" in cols and "seconds_remaining" not in cols

    if "seconds_remaining" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN seconds_remaining INTEGER DEFAULT 0")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")
    if "bottles_inserted" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN bottles_inserted INTEGER DEFAULT 0")

    if needs_conversion:
        conn.execute("""
            UPDATE sessions
               SET seconds_remaining = COALESCE(minutes_remaining, 0) * 60
             WHERE seconds_remaining IS NULL OR seconds_remaining = 0
        """)
        # An old 'active' row had no expiry; give it one so it keeps running.
        for row in conn.execute(
                "SELECT mac_address, seconds_remaining FROM sessions WHERE status = 'active'"
        ).fetchall():
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE mac_address = ?",
                (_iso(_now() + timedelta(seconds=row["seconds_remaining"] or 0)),
                 row["mac_address"]),
            )

    # Old 'pending' sessions become real queue entries, so nobody who was
    # mid-claim during an upgrade loses their place.
    stale_pending = conn.execute(
        "SELECT mac_address, claimed_at FROM sessions WHERE status = 'pending'"
    ).fetchall()
    for row in stale_pending:
        conn.execute(
            "INSERT INTO claims (mac_address, created_at, granted) VALUES (?, ?, 0)",
            (row["mac_address"], row["claimed_at"] or _iso(_now())),
        )
        conn.execute(
            "UPDATE sessions SET status = 'blocked' WHERE mac_address = ?",
            (row["mac_address"],),
        )


# ---- sessions ----

def _decorate(row, pending=False):
    """Adds the computed live countdown to a raw session row.

    `remaining_seconds` is derived from the expiry while running, so it
    is correct no matter how long ago the worker last ran."""
    if row is None:
        return None

    session = dict(row)
    status = session.get("status")

    if status == "active" and session.get("expires_at"):
        delta = (_parse(session["expires_at"]) - _now()).total_seconds()
        remaining = max(0, int(delta))
    else:
        remaining = max(0, int(session.get("seconds_remaining") or 0))

    session["remaining_seconds"] = remaining
    session["minutes_remaining"] = remaining // 60
    session["pending_claim"] = pending
    return session


def _ensure_session(conn, mac_address):
    conn.execute(
        """INSERT INTO sessions
           (mac_address, seconds_remaining, expires_at, status, claimed_at, bottles_inserted)
           VALUES (?, 0, NULL, 'blocked', ?, 0)
           ON CONFLICT(mac_address) DO NOTHING""",
        (mac_address, _iso(_now())),
    )


def get_session(mac_address):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE mac_address = ?", (mac_address,)
        ).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM claims WHERE mac_address = ? AND granted = 0 LIMIT 1",
            (mac_address,),
        ).fetchone() is not None
        return _decorate(row, pending)


def get_all_sessions():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY claimed_at DESC"
        ).fetchall()
        pending_macs = {
            r["mac_address"] for r in conn.execute(
                "SELECT DISTINCT mac_address FROM claims WHERE granted = 0"
            ).fetchall()
        }
        return [_decorate(r, r["mac_address"] in pending_macs) for r in rows]


def get_expired_sessions():
    """Active sessions whose expiry has passed -- what the worker revokes."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status = 'active' AND expires_at IS NOT NULL"
        ).fetchall()
        return [s for s in (_decorate(r) for r in rows) if s["remaining_seconds"] <= 0]


DEFAULT_SESSION_CAP_SECONDS = 6 * 3600


def get_session_cap_seconds():
    """Maximum time one device may hold at once. 0 means no cap.

    A cap stops a single customer feeding in bottles all day and holding
    bandwidth other people are queuing for, and it is what the portal's
    progress bar is measured against."""
    stored = get_setting("session_cap_seconds")
    if stored is None:
        return DEFAULT_SESSION_CAP_SECONDS
    try:
        return max(0, int(stored))
    except ValueError:
        return DEFAULT_SESSION_CAP_SECONDS


def set_session_cap_seconds(seconds):
    set_setting("session_cap_seconds", str(max(0, int(seconds))))


def credit_time(mac_address, minutes, bottles=0):
    """Adds time to a session and starts it running.

    Topping up an already-running session extends its expiry rather than
    resetting it. Topping up a PAUSED session adds to the stored balance
    and leaves it paused -- the customer chose to pause, so we do not
    silently start burning their time again.

    Returns the seconds ACTUALLY added, which is less than requested when
    the session cap is reached. Callers log that number rather than the
    requested one, so the books match what the customer received."""
    requested = int(minutes) * 60
    cap = get_session_cap_seconds()

    if cap:
        current = get_session(mac_address)
        held = current["remaining_seconds"] if current else 0
        # Round the remaining room down to whole minutes. Held time is
        # derived from a timestamp, so a session sitting at the cap
        # reports 3599s rather than 3600s -- without this, the customer
        # gets a meaningless one-second credit and a transaction logged
        # as zero minutes.
        room = (max(0, cap - held) // 60) * 60
        seconds = min(requested, room)
    else:
        seconds = requested

    if seconds <= 0:
        # Still count the bottle -- it went in the bin either way.
        if bottles:
            with get_db() as conn:
                _ensure_session(conn, mac_address)
                conn.execute(
                    """UPDATE sessions SET bottles_inserted = bottles_inserted + ?
                        WHERE mac_address = ?""",
                    (bottles, mac_address),
                )
        return 0

    with get_db() as conn:
        _ensure_session(conn, mac_address)
        row = conn.execute(
            "SELECT * FROM sessions WHERE mac_address = ?", (mac_address,)
        ).fetchone()

        bottles_total = (row["bottles_inserted"] or 0) + bottles

        if row["status"] == "paused":
            conn.execute(
                """UPDATE sessions
                      SET seconds_remaining = seconds_remaining + ?,
                          bottles_inserted = ?
                    WHERE mac_address = ?""",
                (seconds, bottles_total, mac_address),
            )
            return seconds

        # Extend from the current expiry if one is still in the future,
        # otherwise from now.
        current = _parse(row["expires_at"]) if row["expires_at"] else None
        base = current if (current and current > _now()) else _now()
        new_expiry = base + timedelta(seconds=seconds)

        conn.execute(
            """UPDATE sessions
                  SET expires_at = ?,
                      seconds_remaining = ?,
                      status = 'active',
                      bottles_inserted = ?
                WHERE mac_address = ?""",
            (_iso(new_expiry), int((new_expiry - _now()).total_seconds()),
             bottles_total, mac_address),
        )

    return seconds


def pause_session(mac_address):
    """Time Pause: banks the remaining time and stops the clock.

    Returns the paused session, or None if there was nothing to pause."""
    session = get_session(mac_address)
    if not session or session["status"] != "active":
        return None

    remaining = session["remaining_seconds"]
    if remaining <= 0:
        return None

    with get_db() as conn:
        conn.execute(
            """UPDATE sessions
                  SET status = 'paused', seconds_remaining = ?, expires_at = NULL
                WHERE mac_address = ?""",
            (remaining, mac_address),
        )
    return get_session(mac_address)


def resume_session(mac_address):
    """Restarts the clock on a paused session."""
    session = get_session(mac_address)
    if not session or session["status"] != "paused":
        return None

    remaining = int(session["seconds_remaining"] or 0)
    if remaining <= 0:
        return None

    with get_db() as conn:
        conn.execute(
            """UPDATE sessions
                  SET status = 'active', expires_at = ?
                WHERE mac_address = ?""",
            (_iso(_now() + timedelta(seconds=remaining)), mac_address),
        )
    return get_session(mac_address)


def expire_session(mac_address):
    with get_db() as conn:
        conn.execute(
            """UPDATE sessions
                  SET status = 'blocked', seconds_remaining = 0, expires_at = NULL
                WHERE mac_address = ?""",
            (mac_address,),
        )


def set_status(mac_address, status):
    with get_db() as conn:
        conn.execute(
            "UPDATE sessions SET status = ? WHERE mac_address = ?",
            (status, mac_address),
        )


# ---- claims (the bottle queue) ----

def create_claim(mac_address):
    """Queues this device for the next validated bottle.

    Tapping the button repeatedly does NOT stack up claims -- one open
    claim per device, so a customer cannot hoard the queue and take
    bottles that other people fed in."""
    with get_db() as conn:
        _ensure_session(conn, mac_address)
        existing = conn.execute(
            "SELECT id FROM claims WHERE mac_address = ? AND granted = 0 LIMIT 1",
            (mac_address,),
        ).fetchone()
        if existing:
            return existing["id"]

        cur = conn.execute(
            "INSERT INTO claims (mac_address, created_at, granted) VALUES (?, ?, 0)",
            (mac_address, _iso(_now())),
        )
        conn.execute(
            "UPDATE sessions SET claimed_at = ? WHERE mac_address = ?",
            (_iso(_now()), mac_address),
        )
        return cur.lastrowid


def get_oldest_pending():
    """The claim the next bottle belongs to (FIFO)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM claims WHERE granted = 0 ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def mark_claim_granted(claim_id):
    """Closes a claim. Guarded on granted = 0 so two bottles arriving at
    once cannot both consume the same claim -- returns False if this call
    lost the race."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE claims SET granted = 1, granted_at = ? WHERE id = ? AND granted = 0",
            (_iso(_now()), claim_id),
        )
        return cur.rowcount == 1


def cancel_claim(mac_address):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM claims WHERE mac_address = ? AND granted = 0", (mac_address,)
        )


def get_open_claims():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM claims WHERE granted = 0 ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---- transactions ----

def log_transaction(mac_address, payment_method, quantity, minutes_credited):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO transactions
               (mac_address, payment_method, quantity, minutes_credited, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (mac_address, payment_method, quantity, minutes_credited, _iso(_now())),
        )


def get_transactions(limit=100):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_stats():
    """Cumulative metrics for the admin dashboard (spec section 3.3:
    bottle counts and usage history for research)."""
    with get_db() as conn:
        bottles = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS n FROM transactions WHERE payment_method = 'bottle'"
        ).fetchone()["n"]
        minutes = conn.execute(
            "SELECT COALESCE(SUM(minutes_credited), 0) AS n FROM transactions"
        ).fetchone()["n"]
        tx = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
        devices = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM claims WHERE granted = 0"
        ).fetchone()["n"]
        by_status = {
            r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM sessions GROUP BY status"
            ).fetchall()
        }

    return {
        "total_bottles": bottles,
        "total_minutes_granted": minutes,
        "total_transactions": tx,
        "known_devices": devices,
        "active_sessions": by_status.get("active", 0),
        "paused_sessions": by_status.get("paused", 0),
        "pending_claims": pending,
        "blocked_sessions": by_status.get("blocked", 0),
    }


# ---- vouchers ----

def create_voucher(minutes, code=None):
    code = code or secrets.token_hex(4).upper()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO vouchers (code, minutes, redeemed, created_at)
               VALUES (?, ?, 0, ?)""",
            (code, minutes, _iso(_now())),
        )
    return code


def redeem_voucher(code):
    """Marks a voucher used and returns its minutes, or None if the code
    is unknown or already spent. The UPDATE is guarded on redeemed = 0 so
    two simultaneous redemptions of the same code cannot both win."""
    code = (code or "").strip().upper()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM vouchers WHERE code = ? AND redeemed = 0", (code,)
        ).fetchone()
        if not row:
            return None

        cur = conn.execute(
            "UPDATE vouchers SET redeemed = 1, redeemed_at = ? WHERE code = ? AND redeemed = 0",
            (_iso(_now()), code),
        )
        if cur.rowcount == 0:
            return None
        return row["minutes"]


def get_vouchers(limit=100):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM vouchers ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


# ---- device status (the ESP32 detection side) ----

# How long without a heartbeat before the detection side counts as
# offline. Needs to be comfortably longer than the ESP32's heartbeat
# interval, or a single dropped packet shows up as an outage.
ESP32_OFFLINE_AFTER_SECONDS = 90


def record_heartbeat(bin_full=None, bin_distance_cm=None, bottles_today=None,
                     firmware=None, source_ip=None):
    """Stores a heartbeat from the ESP32.

    Every field except the timestamp is optional, so an early firmware
    that only says 'I am alive' still works, and fields it does not
    report keep their previous value instead of being wiped."""
    with get_db() as conn:
        sets = ["last_seen = ?"]
        params = [_iso(_now())]

        for column, value in (
            ("bin_full", None if bin_full is None else int(bool(bin_full))),
            ("bin_distance_cm", bin_distance_cm),
            ("bottles_today", bottles_today),
            ("firmware", firmware),
            ("source_ip", source_ip),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)

        conn.execute(f"UPDATE device_status SET {', '.join(sets)} WHERE id = 1", params)

    return get_device_status()


def get_device_status():
    """Current view of the detection side, with liveness derived from the
    last heartbeat rather than stored -- a stored 'online' flag would stay
    online forever once the ESP32 stopped calling."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM device_status WHERE id = 1").fetchone()

    status = dict(row) if row else {}
    last_seen = _parse(status.get("last_seen"))

    if last_seen is None:
        status["online"] = False
        status["seconds_since_seen"] = None
    else:
        age = (_now() - last_seen).total_seconds()
        status["seconds_since_seen"] = int(age)
        status["online"] = age <= ESP32_OFFLINE_AFTER_SECONDS

    status["bin_full"] = bool(status.get("bin_full"))
    status["offline_after_seconds"] = ESP32_OFFLINE_AFTER_SECONDS
    return status


# ---- MAC control (permanent bans) ----

def ban_mac(mac_address, reason=None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO blocked_macs (mac_address, reason, created_at) VALUES (?, ?, ?)
               ON CONFLICT(mac_address) DO UPDATE SET reason = excluded.reason""",
            (mac_address.upper(), reason, _iso(_now())),
        )


def unban_mac(mac_address):
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM blocked_macs WHERE mac_address = ?", (mac_address.upper(),)
        )
        return cur.rowcount > 0


def is_mac_banned(mac_address):
    if not mac_address:
        return False
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM blocked_macs WHERE mac_address = ?", (mac_address.upper(),)
        ).fetchone() is not None


def get_banned_macs():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM blocked_macs ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---- rates ----

def get_rate(payment_method):
    with get_db() as conn:
        row = conn.execute(
            "SELECT minutes_per_unit FROM rates WHERE payment_method = ?",
            (payment_method,),
        ).fetchone()
        return row["minutes_per_unit"] if row else None


def get_all_rates():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM rates ORDER BY payment_method").fetchall()
        return [dict(row) for row in rows]


def set_rate(payment_method, minutes_per_unit):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO rates (payment_method, minutes_per_unit) VALUES (?, ?)
               ON CONFLICT(payment_method) DO UPDATE SET minutes_per_unit = excluded.minutes_per_unit""",
            (payment_method, minutes_per_unit),
        )


# ---- admin credentials and sessions ----

def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


def has_admin_password():
    return get_setting("admin_password_hash") is not None


def store_admin_password(salt_b64, hash_b64, iterations):
    """Replaces the admin password and invalidates every existing login,
    so changing the password actually kicks out anyone already in."""
    set_setting("admin_password_salt", salt_b64)
    set_setting("admin_password_hash", hash_b64)
    set_setting("admin_password_iterations", str(iterations))
    with get_db() as conn:
        conn.execute("DELETE FROM admin_sessions")


def get_admin_credentials():
    """Returns (salt, hash, iterations). Iterations is None for a password
    written before the count was recorded; auth treats that as the old
    default rather than failing to verify it."""
    stored = get_setting("admin_password_iterations")
    return (get_setting("admin_password_salt"),
            get_setting("admin_password_hash"),
            int(stored) if stored else None)


def create_admin_session(token, hours, ip=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token, created_at, expires_at, ip) VALUES (?, ?, ?, ?)",
            (token, _iso(_now()), _iso(_now() + timedelta(hours=hours)), ip),
        )


def admin_session_valid(token):
    """Checks a login cookie. Expired rows are deleted as they are found,
    which keeps the table from growing without needing a sweeper."""
    if not token:
        return False

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM admin_sessions WHERE token = ?", (token,)
        ).fetchone()
        if not row:
            return False

        if _parse(row["expires_at"]) <= _now():
            conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
            return False

    return True


def delete_admin_session(token):
    with get_db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))


def count_admin_sessions():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM admin_sessions").fetchone()["n"]


# ---- backup / restore ----

REQUIRED_TABLES = {"sessions", "claims", "transactions", "vouchers", "rates"}


def backup_to(dest_path):
    """Writes a consistent snapshot to dest_path.

    Uses SQLite's own backup API rather than copying the file. A plain
    file copy taken while a write is in flight can capture a torn page
    and produce a backup that will not open -- which you would only
    discover on the day you needed it."""
    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(str(dest_path))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return dest_path


def inspect_db_file(path):
    """Checks an uploaded file really is one of our databases.

    Returns (ok, message). Restoring is destructive, so this runs before
    anything touches the live file."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return False, f"Not a readable SQLite database: {e}"

    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            return False, f"Database failed its integrity check: {integrity}"

        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            return False, f"Not an EcoWifi backup -- missing tables: {', '.join(sorted(missing))}"

        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return True, f"Valid backup with {sessions} session row(s)"
    except sqlite3.Error as e:
        return False, f"Could not read the database: {e}"
    finally:
        conn.close()
