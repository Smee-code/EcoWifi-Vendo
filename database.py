import hashlib
import io
import json
import os
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
                payments_made INTEGER DEFAULT 0
            )
        """)

        # A claim is a QUEUE ENTRY: "this device asked to pay, and the
        # next payment the validator confirms belongs to it." Kept
        # separate from sessions on purpose -- a customer who is already
        # connected can queue another payment to top up, which is
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

        # A rate is a PRICE TIER: "this many of this trigger earns this
        # much time". The quantity-1 tier is the base rate; larger tiers
        # let the operator reward bulk without the time being a straight
        # multiple, e.g. 1 bottle = 30 min but 5 bottles = 4 hours.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rates (
                payment_method TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                minutes INTEGER NOT NULL,
                PRIMARY KEY (payment_method, quantity)
            )
        """)

        # Single-row table holding whatever the payment device last told
        # us about itself. Persisted rather than kept in memory so the
        # dashboard does not claim "never seen" after every restart.
        #
        # `metadata` is deliberately freeform JSON: a bottle validator
        # reports bin levels, a coin acceptor reports a hopper count, a
        # card reader reports a terminal id. The gateway does not need to
        # know which -- it stores what arrives and the dashboard renders
        # whatever keys are present.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_seen TEXT,
                firmware TEXT,
                source_ip TEXT,
                metadata TEXT
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

        # Operator branding. Held in the database rather than on disk so a
        # backup captures the whole machine -- restoring onto a fresh SD
        # card brings the logos back with everything else.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                name TEXT PRIMARY KEY,
                content_type TEXT NOT NULL,
                data BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                is_default INTEGER DEFAULT 0
            )
        """)

        _migrate(conn)

        # No rate is seeded. What one payment trigger is worth is a
        # business decision belonging to whoever runs the machine, and a
        # guessed default would quietly hand out time at a rate nobody
        # chose. The operator defines rates in first-run setup, and
        # /claim and /grant refuse to run until at least one exists.


def _migrate(conn):
    """Brings a pre-existing database up to the current schema.

    The original schema stored `minutes_remaining`, had no pause support,
    tracked pending claims as a session status, and named things after
    bottles. Anything created by an older version is converted in place
    rather than dropped, so a machine already in service keeps its data."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}

    # Decide this up front: the ALTERs below change the table, so testing
    # for the new columns afterwards would give the wrong answer.
    needs_conversion = "minutes_remaining" in cols and "seconds_remaining" not in cols

    if "seconds_remaining" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN seconds_remaining INTEGER DEFAULT 0")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")
    if "payments_made" not in cols:
        if "bottles_inserted" in cols:
            # Rename in place so a machine that has already taken payments
            # keeps its per-device counts.
            conn.execute("ALTER TABLE sessions RENAME COLUMN bottles_inserted TO payments_made")
        else:
            conn.execute("ALTER TABLE sessions ADD COLUMN payments_made INTEGER DEFAULT 0")

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

    _migrate_device_status(conn)
    _migrate_rates(conn)

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


def _migrate_rates(conn):
    """Rebuilds a single-rate table into tiers.

    The old table was one row per method (`minutes_per_unit`). Each of
    those becomes the quantity-1 base tier, so an existing machine keeps
    charging exactly what it charged before."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rates)").fetchall()}
    if "minutes_per_unit" not in cols:
        return

    old_rows = conn.execute(
        "SELECT payment_method, minutes_per_unit FROM rates"
    ).fetchall()

    conn.execute("DROP TABLE rates")
    conn.execute("""
        CREATE TABLE rates (
            payment_method TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            minutes INTEGER NOT NULL,
            PRIMARY KEY (payment_method, quantity)
        )
    """)
    for row in old_rows:
        conn.execute(
            "INSERT INTO rates (payment_method, quantity, minutes) VALUES (?, 1, ?)",
            (row["payment_method"], row["minutes_per_unit"]),
        )


def _migrate_device_status(conn):
    """Folds the old bottle-specific columns into freeform metadata.

    The table used to carry bin_full / bin_distance_cm / bottles_today,
    which only made sense for a bottle validator. Their values are moved
    into the JSON blob rather than dropped, so a machine that has been
    running keeps its last reading."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(device_status)").fetchall()}
    if "metadata" in cols:
        return

    row = conn.execute("SELECT * FROM device_status WHERE id = 1").fetchone()
    carried = {}
    if row:
        for old_key in ("bin_full", "bin_distance_cm", "bottles_today"):
            if old_key in cols and row[old_key] is not None:
                value = row[old_key]
                if old_key == "bin_full":
                    value = bool(value)
                carried[old_key] = value

    last_seen = row["last_seen"] if row and "last_seen" in cols else None
    firmware = row["firmware"] if row and "firmware" in cols else None
    source_ip = row["source_ip"] if row and "source_ip" in cols else None

    conn.execute("DROP TABLE device_status")
    conn.execute("""
        CREATE TABLE device_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_seen TEXT,
            firmware TEXT,
            source_ip TEXT,
            metadata TEXT
        )
    """)
    conn.execute(
        """INSERT INTO device_status (id, last_seen, firmware, source_ip, metadata)
           VALUES (1, ?, ?, ?, ?)""",
        (last_seen, firmware, source_ip, json.dumps(carried) if carried else None),
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
           (mac_address, seconds_remaining, expires_at, status, claimed_at, payments_made)
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


# Customer-facing wording. Every string here names the payment action, so
# it has to be the operator's to change: a bottle validator, a coin slot
# and a card reader need completely different instructions.
DEFAULT_PORTAL_TEXT = {
    "action_label": "Insert payment",
    "action_hint": "Adds time to your session",
    "notice": "Follow the instructions on the machine to add time.",
    "modal_title": "Waiting for payment",
    "modal_body": "Complete the payment on the machine. Your device is credited "
                  "as soon as the validator confirms it.",
    "waiting_text": "Waiting for the machine to confirm",
    "tagline": "Pay on the machine. Get online.",
}

PORTAL_TEXT_PREFIX = "portal_text_"


def get_portal_text():
    """Merged over the defaults, so a key the operator never set still
    renders rather than showing a blank button."""
    text = dict(DEFAULT_PORTAL_TEXT)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (PORTAL_TEXT_PREFIX + "%",),
        ).fetchall()
    for row in rows:
        key = row["key"][len(PORTAL_TEXT_PREFIX):]
        if key in DEFAULT_PORTAL_TEXT and row["value"]:
            text[key] = row["value"]
    return text


def set_portal_text(values):
    """Stores only known keys. An empty value resets that key to default."""
    applied = {}
    for key, value in (values or {}).items():
        if key not in DEFAULT_PORTAL_TEXT:
            continue
        value = (value or "").strip()
        if value:
            set_setting(PORTAL_TEXT_PREFIX + key, value)
        else:
            with get_db() as conn:
                conn.execute("DELETE FROM settings WHERE key = ?",
                             (PORTAL_TEXT_PREFIX + key,))
        applied[key] = value
    return get_portal_text()


DEFAULT_SESSION_CAP_SECONDS = 6 * 3600


def get_session_cap_seconds():
    """Maximum time one device may hold at once. 0 means no cap.

    A cap stops a single customer paying in all day and holding bandwidth
    other people are queuing for, and it is what the portal progress bar
    is measured against."""
    stored = get_setting("session_cap_seconds")
    if stored is None:
        return DEFAULT_SESSION_CAP_SECONDS
    try:
        return max(0, int(stored))
    except ValueError:
        return DEFAULT_SESSION_CAP_SECONDS


def set_session_cap_seconds(seconds):
    set_setting("session_cap_seconds", str(max(0, int(seconds))))


def credit_time(mac_address, minutes, payments=0):
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
        # Still count the payment -- the machine took it either way.
        if payments:
            with get_db() as conn:
                _ensure_session(conn, mac_address)
                conn.execute(
                    """UPDATE sessions SET payments_made = payments_made + ?
                        WHERE mac_address = ?""",
                    (payments, mac_address),
                )
        return 0

    with get_db() as conn:
        _ensure_session(conn, mac_address)
        row = conn.execute(
            "SELECT * FROM sessions WHERE mac_address = ?", (mac_address,)
        ).fetchone()

        payments_total = (row["payments_made"] or 0) + payments

        if row["status"] == "paused":
            conn.execute(
                """UPDATE sessions
                      SET seconds_remaining = seconds_remaining + ?,
                          payments_made = ?
                    WHERE mac_address = ?""",
                (seconds, payments_total, mac_address),
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
                      payments_made = ?
                WHERE mac_address = ?""",
            (_iso(new_expiry), int((new_expiry - _now()).total_seconds()),
             payments_total, mac_address),
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


# ---- claims (the payment queue) ----

def create_claim(mac_address):
    """Queues this device for the next confirmed payment.

    Tapping the button repeatedly does NOT stack up claims -- one open
    claim per device, so a customer cannot hoard the queue and take
    payments that other people made."""
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
    """The claim the next confirmed payment belongs to (FIFO)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM claims WHERE granted = 0 ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def mark_claim_granted(claim_id):
    """Closes a claim. Guarded on granted = 0 so two payments arriving at
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
    """Cumulative metrics for the admin dashboard.

    Payments are counted across every configured method rather than one
    hardcoded name: filtering on 'bottle' would report zero forever on a
    machine fitted with a coin acceptor or a card reader."""
    with get_db() as conn:
        payments = conn.execute(
            """SELECT COALESCE(SUM(quantity), 0) AS n FROM transactions
                WHERE payment_method <> 'voucher'"""
        ).fetchone()["n"]
        by_method = {
            r["payment_method"]: r["n"] for r in conn.execute(
                """SELECT payment_method, COALESCE(SUM(quantity), 0) AS n
                     FROM transactions GROUP BY payment_method"""
            ).fetchall()
        }
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
        "total_payments": payments,
        "payments_by_method": by_method,
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


# ---- device status (the external payment device) ----

# How long without a heartbeat before the payment device counts as
# offline. Needs to be comfortably longer than the device's heartbeat
# interval, or a single dropped packet shows up as an outage.
DEVICE_OFFLINE_AFTER_SECONDS = 90


# Metadata keys the gateway itself understands. Everything else is stored
# and displayed verbatim without the gateway caring what it means.
ACCEPTING_KEY = "accepting"     # bool: is the device able to take payment
MESSAGE_KEY = "message"         # str: shown to customers when not accepting


def record_heartbeat(firmware=None, source_ip=None, metadata=None):
    """Stores a heartbeat from the payment device.

    Everything except the timestamp is optional, so firmware that only
    says 'I am alive' still works. Metadata keys are MERGED into whatever
    was reported before, so a device that sends its firmware once and
    then only sends a level reading does not wipe the rest."""
    with get_db() as conn:
        row = conn.execute("SELECT metadata FROM device_status WHERE id = 1").fetchone()
        merged = {}
        if row and row["metadata"]:
            try:
                merged = json.loads(row["metadata"])
            except ValueError:
                merged = {}
        if metadata:
            merged.update(metadata)

        sets = ["last_seen = ?", "metadata = ?"]
        params = [_iso(_now()), json.dumps(merged)]

        for column, value in (("firmware", firmware), ("source_ip", source_ip)):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)

        conn.execute(f"UPDATE device_status SET {', '.join(sets)} WHERE id = 1", params)

    return get_device_status()


def get_device_status():
    """Current view of the payment device.

    Liveness is derived from the last heartbeat rather than stored: a
    stored 'online' flag would stay online forever once the device
    stopped calling, which is exactly when it matters."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM device_status WHERE id = 1").fetchone()

    status = dict(row) if row else {}

    metadata = {}
    if status.get("metadata"):
        try:
            metadata = json.loads(status["metadata"])
        except ValueError:
            metadata = {}
    status["metadata"] = metadata

    last_seen = _parse(status.get("last_seen"))
    if last_seen is None:
        status["online"] = False
        status["seconds_since_seen"] = None
    else:
        age = (_now() - last_seen).total_seconds()
        status["seconds_since_seen"] = int(age)
        status["online"] = age <= DEVICE_OFFLINE_AFTER_SECONDS

    # A device may declare itself unable to take payment (bin full, hopper
    # jammed, terminal offline). Absent that key, being online is enough.
    declared = metadata.get(ACCEPTING_KEY)
    status["accepting"] = bool(status["online"]) and (True if declared is None else bool(declared))
    status["message"] = metadata.get(MESSAGE_KEY)
    status["offline_after_seconds"] = DEVICE_OFFLINE_AFTER_SECONDS
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

# Guards the tier solver below. Nobody is inserting a thousand bottles in
# one go, and an unbounded value would let a bad request allocate a huge
# table inside the request.
MAX_QUANTITY = 500


def get_rate(payment_method, quantity=1):
    """Minutes earned by exactly this quantity, or None if no such tier."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT minutes FROM rates WHERE payment_method = ? AND quantity = ?",
            (payment_method, quantity),
        ).fetchone()
        return row["minutes"] if row else None


def get_all_rates():
    """Every tier, cheapest quantity first within each method."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM rates ORDER BY payment_method, quantity"
        ).fetchall()
        return [dict(row) for row in rows]


def get_payment_methods():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT payment_method FROM rates ORDER BY payment_method"
        ).fetchall()
        return [r["payment_method"] for r in rows]


def get_rate_tiers(payment_method):
    """{quantity: minutes} for one method."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT quantity, minutes FROM rates WHERE payment_method = ? ORDER BY quantity",
            (payment_method,),
        ).fetchall()
        return {r["quantity"]: r["minutes"] for r in rows}


def calculate_minutes(payment_method, quantity=1):
    """Best time earned by `quantity` of this trigger.

    Tiers can be combined, and the combination chosen is the one worth the
    MOST minutes -- the customer always gets the best price their quantity
    qualifies for. With tiers 1=30 and 3=120, four items are priced 3+1 =
    150 minutes, not 4x30 = 120.

    Solved exactly with a small dynamic program rather than greedily
    taking the largest tier first: greedy is wrong whenever two smaller
    bonus tiers beat one larger one, and quietly underpaying a customer is
    the kind of bug nobody reports and everybody notices."""
    if quantity < 1:
        return 0
    if quantity > MAX_QUANTITY:
        quantity = MAX_QUANTITY

    tiers = get_rate_tiers(payment_method)
    if not tiers:
        return 0

    # best[q] = most minutes obtainable from exactly q items
    NOT_REACHABLE = -1
    best = [NOT_REACHABLE] * (quantity + 1)
    best[0] = 0

    for q in range(1, quantity + 1):
        for tier_qty, tier_minutes in tiers.items():
            if tier_qty <= q and best[q - tier_qty] != NOT_REACHABLE:
                candidate = best[q - tier_qty] + tier_minutes
                if candidate > best[q]:
                    best[q] = candidate

    if best[quantity] != NOT_REACHABLE:
        return best[quantity]

    # No exact combination (which needs a quantity-1 tier to be missing).
    # Fall back to the best price for FEWER items rather than refusing to
    # credit anything -- the customer has already paid.
    for q in range(quantity - 1, 0, -1):
        if best[q] != NOT_REACHABLE:
            return best[q]
    return 0


def has_rates():
    """True once the operator has defined at least one payment trigger.

    Nothing can be granted before this: without a rate there is no answer
    to how much time a payment is worth."""
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM rates").fetchone()["c"] > 0


def delete_rate(payment_method, quantity=None):
    """Removes one tier, or the whole method when quantity is None.

    Removing the quantity-1 tier removes the method entirely: without a
    base rate the remaining tiers could not price an ordinary single
    payment, which is the case that actually happens."""
    with get_db() as conn:
        base_exists = conn.execute(
            'SELECT 1 FROM rates WHERE payment_method = ? AND quantity = 1',
            (payment_method,),
        ).fetchone() is not None

        # Only cascade when the base tier is really there. Asking to remove
        # a tier that does not exist must not take the whole method with it.
        if quantity is None or (quantity == 1 and base_exists):
            cur = conn.execute(
                'DELETE FROM rates WHERE payment_method = ?', (payment_method,)
            )
        else:
            cur = conn.execute(
                "DELETE FROM rates WHERE payment_method = ? AND quantity = ?",
                (payment_method, quantity),
            )
        return cur.rowcount > 0


def set_rate(payment_method, minutes, quantity=1):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO rates (payment_method, quantity, minutes) VALUES (?, ?, ?)
               ON CONFLICT(payment_method, quantity)
               DO UPDATE SET minutes = excluded.minutes""",
            (payment_method, quantity, minutes),
        )


# ---- assets (logos) ----

LOGO_VARIANTS = ("large", "small")


def _asset_name(variant):
    return f"logo_{variant}"


def get_asset(name):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def set_asset(name, data, content_type="image/png", is_default=False):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO assets (name, content_type, data, updated_at, is_default)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   content_type = excluded.content_type,
                   data = excluded.data,
                   updated_at = excluded.updated_at,
                   is_default = excluded.is_default""",
            (name, content_type, data, _iso(_now()), 1 if is_default else 0),
        )


def delete_asset(name):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM assets WHERE name = ?", (name,))
        return cur.rowcount > 0


def get_logo(variant):
    return get_asset(_asset_name(variant))


def set_logo(variant, data, is_default=False):
    set_asset(_asset_name(variant), data, "image/png", is_default)


def delete_logo(variant):
    return delete_asset(_asset_name(variant))


def _version_of(timestamp):
    return hashlib.sha1((timestamp or "").encode()).hexdigest()[:8]


def logo_summary():
    """What the dashboard and the portal need to know about branding.

    `version` changes whenever a logo does, and the portal puts it in the
    image URL. Without it a customer whose browser cached the old logo
    would keep seeing it after the operator uploaded a new one."""
    summary = {}
    for variant in LOGO_VARIANTS:
        row = get_logo(variant)
        summary[variant] = {
            "present": row is not None,
            "is_default": bool(row["is_default"]) if row else False,
            "updated_at": row["updated_at"] if row else None,
            "bytes": len(row["data"]) if row else 0,
            # Short digest of the timestamp. It only has to CHANGE when the
            # logo does; slicing the timestamp produced strings like
            # "00+0000", which are unreadable and can repeat.
            "version": _version_of(row["updated_at"]) if row else "0",
        }
    return summary


def seed_default_logos(assets_dir):
    """Installs the shipped logos on a machine that has none.

    Only ever fills a gap: an operator's own upload is never replaced,
    and a logo they deliberately removed stays removed, because removal
    leaves a row-less variant that this would otherwise resurrect. That
    is why the deleted state is recorded in settings."""
    for variant in LOGO_VARIANTS:
        if get_logo(variant) is not None:
            continue
        if get_setting(f"logo_{variant}_cleared") == "1":
            continue

        path = os.path.join(assets_dir, f"default-logo-{variant}.png")
        try:
            with open(path, "rb") as handle:
                set_logo(variant, handle.read(), is_default=True)
        except OSError:
            # Shipping without the file is not fatal; the portal falls
            # back to its text wordmark.
            pass


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


def get_admin_username():
    return get_setting("admin_username")


def store_admin_username(username):
    set_setting("admin_username", username.strip().lower())


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
