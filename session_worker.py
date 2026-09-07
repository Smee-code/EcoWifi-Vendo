import threading
import time
import logging

import clock
import database
import network_service

logger = logging.getLogger(__name__)

# How often to sweep for expired sessions. This is only a revocation
# check -- the countdown itself comes from each session's stored expiry
# timestamp, so the interval affects how promptly access is cut, not how
# accurately time is measured. A late sweep cannot hand out free time.
CHECK_INTERVAL_SECONDS = 10


def _correct_for_clock_jumps():
    """Keeps expiries honest across NTP steps.

    Runs before the expiry sweep, not after: acting on stale expiries and
    only then noticing the clock moved would cut people off first and
    apologise afterwards."""
    delta = clock.check_drift()
    if not delta:
        return

    shifted = database.shift_all_expiries(delta)
    logger.warning(
        "Clock jumped %.0fs; shifted %d session expiry time(s) to match.",
        delta, shifted,
    )


def _expire_abandoned_turns():
    """Hands the machine to the next customer when the front one leaves.

    Also runs on demand from /status and /grant, but a queue must keep
    moving even when nobody is looking at a portal page."""
    for mac in database.expire_stale_claims():
        logger.info(f"Turn expired for {mac}; the queue moved on")


def _check_sessions():
    for session in database.get_expired_sessions():
        mac = session["mac_address"]
        if network_service.block_mac(mac):
            database.expire_session(mac)
            logger.info(f"Session expired for {mac}, access revoked")
        else:
            # Leave the row active so the next sweep retries -- marking it
            # blocked while the firewall still allows the MAC would let the
            # device keep surfing with the database claiming otherwise.
            logger.error(f"Could not revoke {mac}; will retry next sweep")


def _run_loop():
    while True:
        try:
            _correct_for_clock_jumps()
            _expire_abandoned_turns()
            _check_sessions()
        except Exception as e:
            logger.error(f"Error in session worker loop: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


def start():
    clock.anchor()
    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
