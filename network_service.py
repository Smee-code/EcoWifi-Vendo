import os
import re
import sys
import hashlib
import subprocess
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# These must match the base nftables ruleset you set up on the Orange Pi
# itself (see README) -- a default-DROP policy on client traffic, with
# this named set holding the MAC addresses currently ACCEPTed.
NFT_FAMILY = os.getenv("NFT_FAMILY", "inet")
NFT_TABLE = os.getenv("NFT_TABLE", "fw4")
NFT_SET = os.getenv("NFT_SET", "granted_macs")


def _dev_mode_enabled() -> bool:
    """DEV MODE -- lets the app run on a Windows/macOS laptop for UI work.

    'nft' and 'ip neigh' are Linux-only, so on any other platform the
    real code path can't work at all. Rather than fail, we simulate the
    firewall in memory so the portal and the full claim -> grant flow
    are clickable in a browser.

    Auto-enables off-Linux. Force it either way with ECOWIFI_DEV_MODE=1
    or =0 -- setting it to 0 on Windows makes the app fail loudly
    instead, which is what you want if you're testing error handling.

    This must NEVER be on for the real Orange Pi deployment: it reports
    firewall changes as successful without touching the firewall."""
    override = os.getenv("ECOWIFI_DEV_MODE")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    return not sys.platform.startswith("linux")


DEV_MODE = _dev_mode_enabled()

# Stands in for the nftables 'granted_macs' set while in dev mode.
_dev_granted_macs = set()

if DEV_MODE:
    logger.warning(
        "DEV MODE ACTIVE (platform=%s): the firewall is SIMULATED IN MEMORY. "
        "No nftables rules are applied and no real traffic is gated. "
        "Never run the Orange Pi deployment like this.", sys.platform
    )


def _run(cmd: list):
    """Runs a command and reports whether it actually succeeded.

    Returns (ok, stdout, stderr). Callers must check `ok` -- a failing
    `nft` call is not an exception, it's a non-zero exit code, so
    swallowing it here would make a broken firewall look like a
    working one."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        logger.error(f"Command not found: {cmd[0]} (is nftables/iproute2 installed?)")
        return False, "", f"{cmd[0]}: not found"
    except Exception as e:
        logger.error(f"Command {' '.join(cmd)} raised: {e}")
        return False, "", str(e)

    if result.returncode != 0:
        logger.warning(f"Command {' '.join(cmd)} failed: {result.stderr.strip()}")
        return False, result.stdout, result.stderr

    return True, result.stdout, result.stderr


def allow_mac(mac_address: str) -> bool:
    """Adds a MAC to the granted set. Returns True only if the MAC is
    genuinely in the set afterwards."""
    if DEV_MODE:
        _dev_granted_macs.add(mac_address)
        logger.info(f"[DEV] Granted {mac_address} (simulated, no nftables)")
        return True

    ok, _, stderr = _run(["nft", "add", "element", NFT_FAMILY, NFT_TABLE, NFT_SET,
                          "{ " + mac_address + " }"])

    # Already present is success, not failure -- this happens whenever
    # someone tops up an already-active session.
    if not ok and "File exists" in stderr:
        logger.info(f"{mac_address} was already granted")
        return True

    if not ok:
        logger.error(f"Failed to grant {mac_address}: {stderr.strip()}")
        return False

    logger.info(f"Granted {mac_address}")
    return True


def block_mac(mac_address: str) -> bool:
    """Removes a MAC from the granted set. Returns True only if the MAC
    is genuinely out of the set afterwards."""
    if DEV_MODE:
        _dev_granted_macs.discard(mac_address)
        logger.info(f"[DEV] Revoked {mac_address} (simulated, no nftables)")
        return True

    ok, _, stderr = _run(["nft", "delete", "element", NFT_FAMILY, NFT_TABLE, NFT_SET,
                          "{ " + mac_address + " }"])

    # Not in the set is success -- it's already blocked, which is the
    # state we wanted.
    if not ok and ("No such file or directory" in stderr or "does not exist" in stderr):
        logger.info(f"{mac_address} was already blocked")
        return True

    if not ok:
        logger.error(f"Failed to revoke {mac_address}: {stderr.strip()}")
        return False

    logger.info(f"Revoked {mac_address}")
    return True


def granted_macs() -> list:
    """Current contents of the granted set -- the firewall's own view,
    which is what you want when checking whether the database and the
    firewall actually agree."""
    if DEV_MODE:
        return sorted(_dev_granted_macs)

    ok, stdout, _ = _run(["nft", "list", "set", NFT_FAMILY, NFT_TABLE, NFT_SET])
    if not ok:
        return []
    return sorted(m.upper() for m in re.findall(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", stdout))


def _dev_mac_for_ip(ip_address: str) -> str:
    """A stable fake MAC for a dev-mode client IP.

    Same IP always maps to the same MAC, so a browser behaves like one
    consistent 'device' across claims. Uses the 02: locally-administered
    prefix so these can never collide with a real vendor MAC."""
    digest = hashlib.sha256(ip_address.encode()).hexdigest()
    octets = [digest[i:i+2] for i in range(0, 10, 2)]
    return ("02:" + ":".join(octets)).upper()


def resolve_mac_from_ip(ip_address: str) -> str | None:
    """Looks up the MAC address for a client IP using the kernel's
    neighbor/ARP table. The client must have already sent at least one
    packet (their HTTP request to reach this endpoint counts) for an
    entry to exist. This only works on Linux, and only for devices on
    the same local network segment as this machine -- which is exactly
    the case here, since clients connect directly to this Orange Pi's
    own AP interface."""
    if DEV_MODE:
        mac = _dev_mac_for_ip(ip_address)
        logger.info(f"[DEV] Resolved {ip_address} -> {mac} (simulated, no ARP lookup)")
        return mac

    ok, stdout, stderr = _run(["ip", "neigh", "show", ip_address])
    if not ok:
        logger.error(f"Failed to resolve MAC for {ip_address}: {stderr.strip()}")
        return None

    match = re.search(r"lladdr\s+([0-9a-fA-F:]{17})", stdout)
    if match:
        return match.group(1).upper()

    logger.warning(f"No ARP entry for {ip_address}")
    return None
