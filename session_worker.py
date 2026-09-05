import threading
import time
import logging

import database
import network_service

logger = logging.getLogger(__name__)

# How often to sweep for expired sessions. This is only a revocation
# check -- the countdown itself comes from each session's stored expiry
# timestamp, so the interval affects how promptly access is cut, not how
# accurately time is measured. A late sweep cannot hand out free time.
CHECK_INTERVAL_SECONDS = 10


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
            _check_sessions()
        except Exception as e:
            logger.error(f"Error in session worker loop: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


def start():
    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
