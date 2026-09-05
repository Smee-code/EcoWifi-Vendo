"""Keeps session timing honest on a board with no real-time clock.

The Orange Pi has no battery-backed clock. Its wall clock is whatever
fake-hwclock restored at boot, and it can jump by hours or years the
moment NTP reaches the internet. Sessions are stored as absolute expiry
timestamps -- which is what makes the countdown survive a restart -- so an
uncorrected jump either cuts every paying customer off instantly or hands
them years of free access. Both are silent.

The fix is to measure ELAPSED time with a monotonic clock, which no NTP
step can move, and to treat any disagreement between the two as a jump:
when the wall clock moves without the same amount of real time passing,
every stored expiry is shifted by the same delta. A customer with twenty
minutes left keeps twenty minutes left, whatever the date says.

Monotonic time cannot be persisted (it resets at boot), so the wall clock
still owns storage. This only corrects it while the app is running, which
is exactly when customers are being charged.
"""
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Below this, a difference is ordinary scheduling slop rather than a step.
# NTP slews small corrections gradually; anything this large is a jump.
JUMP_THRESHOLD_SECONDS = 30

_wall_reference = None       # datetime, wall clock at the last anchor
_mono_reference = None       # float, monotonic reading at the same instant
_total_correction = 0.0      # seconds of jump seen since start, for reporting
_jump_count = 0


def anchor():
    """Records where both clocks are now. Called at startup."""
    global _wall_reference, _mono_reference
    _wall_reference = datetime.now(timezone.utc)
    _mono_reference = time.monotonic()


def check_drift():
    """Returns the wall-clock jump since the last check, in seconds.

    Positive means the clock moved forward (the usual case: NTP correcting
    a board that booted behind). Zero means nothing worth acting on.

    Re-anchors on every call, so a jump is reported exactly once."""
    global _wall_reference, _mono_reference, _total_correction, _jump_count

    if _wall_reference is None:
        anchor()
        return 0.0

    wall_now = datetime.now(timezone.utc)
    mono_now = time.monotonic()

    real_elapsed = mono_now - _mono_reference
    wall_elapsed = (wall_now - _wall_reference).total_seconds()
    delta = wall_elapsed - real_elapsed

    _wall_reference = wall_now
    _mono_reference = mono_now

    if abs(delta) < JUMP_THRESHOLD_SECONDS:
        return 0.0

    _total_correction += delta
    _jump_count += 1
    logger.warning(
        "System clock jumped %.0f seconds (%.1f hours). Session expiries are "
        "being shifted by the same amount so nobody gains or loses time.",
        delta, delta / 3600.0,
    )
    return delta


def status():
    """What the dashboard shows about time keeping."""
    wall = datetime.now(timezone.utc)
    return {
        "utc_now": wall.isoformat(),
        # A board that never got NTP and has no saved clock lands in 1970.
        # Anything before this project existed is certainly unsynced.
        "looks_unsynced": wall.year < 2024,
        "jumps_corrected": _jump_count,
        "total_correction_seconds": round(_total_correction),
        "monotonic_uptime_seconds": int(time.monotonic()),
    }
