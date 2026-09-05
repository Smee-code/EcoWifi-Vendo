import os
import shutil
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends, Response
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse

from starlette.concurrency import run_in_threadpool

import auth
import database
import network_service
import session_worker
import system_monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"


def _bootstrap_admin():
    """Makes sure there is a way in, without ever shipping a default
    password (which would be the same on every machine, and public)."""
    if database.has_admin_password():
        # A database created before usernames existed has a working
        # password but no username. Give it the default rather than
        # locking the operator out of their own machine.
        if not database.get_admin_username():
            database.store_admin_username(auth.DEFAULT_USERNAME)
            logger.warning(
                "This database predates admin usernames. The existing password "
                "still works; sign in with username %s and change it under "
                "Settings.", auth.DEFAULT_USERNAME,
            )
        return

    preset = auth.bootstrap_password_from_env()
    if preset:
        salt, digest, iterations = auth.hash_password(preset)
        database.store_admin_password(salt, digest, iterations)
        username = auth.bootstrap_username_from_env() or auth.DEFAULT_USERNAME
        database.store_admin_username(username)
        logger.warning("Admin credentials set from environment (username: %s).", username)
        return

    # No password yet. The setup page needs this token, which only
    # appears here -- in the log -- so somebody on the open WiFi cannot
    # reach /admin/setup first and take the machine.
    logger.warning("=" * 64)
    logger.warning("NO ADMIN PASSWORD IS SET.")
    logger.warning("Open  /admin/setup  and enter this one-time token:")
    logger.warning("    %s", auth.setup_token())
    logger.warning("The token changes every time this app restarts.")
    logger.warning("=" * 64)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    _bootstrap_admin()
    session_worker.start()
    yield


app = FastAPI(lifespan=lifespan)


def _render(name: str) -> str:
    """Reads a page off disk on every request.

    Deliberately not cached: editing the HTML and hitting refresh is the
    whole development loop, and at this traffic level the read costs
    nothing. Once nginx fronts the app (spec section 3.3) it can serve
    these as static assets instead."""
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    return html.replace("__DEV_MODE__", "true" if network_service.DEV_MODE else "false")


def _client_mac(request: Request):
    return network_service.resolve_mac_from_ip(request.client.host)


def _require_rates():
    """Nothing can be granted before the operator has priced a payment.

    Without a rate there is no answer to "how much time is one payment
    worth", and guessing one would hand out time at a rate nobody chose.
    Returns 503 rather than 400: the request is fine, the machine is not
    ready."""
    if not database.has_rates():
        raise HTTPException(
            status_code=503,
            detail="This machine is not configured yet. The operator still needs "
                   "to set what one payment is worth.",
        )


def _reject_if_banned(mac_address):
    """Applied to every path that can hand out time.

    Checking only at /claim would not be enough: a banned device could
    still be sitting in the queue from before the ban, or try to redeem a
    voucher, or resume banked time."""
    if database.is_mac_banned(mac_address):
        raise HTTPException(
            status_code=403,
            detail="This device has been blocked by the operator.",
        )


def is_admin(request: Request) -> bool:
    return database.admin_session_valid(request.cookies.get(auth.COOKIE_NAME))


def require_admin(request: Request):
    """Dependency for every endpoint that reads or changes operator data.

    Returns 401 rather than redirecting: these are called by fetch() from
    the dashboard, and a redirect to an HTML login page would arrive at
    the JSON parser as a syntax error instead of something it can act on."""
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Admin login required")
    return True


def _target_mac(request: Request, payload: dict | None):
    """Which device an action applies to.

    The customer portal passes nothing and gets whichever device is
    making the request, resolved server-side. Naming another device is an
    OPERATOR action and requires an admin session -- otherwise any
    customer could pause, resume or drain a stranger's session just by
    putting their MAC in the request body."""
    if payload and payload.get("mac_address"):
        if not is_admin(request):
            raise HTTPException(
                status_code=403,
                detail="Only the operator can act on another device.",
            )
        return payload["mac_address"]
    return _client_mac(request)


# ---- pages ----

@app.get("/portal", response_class=HTMLResponse)
async def portal():
    return _render("portal.html")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Captive-portal probes (and anyone typing the bare IP) land here."""
    return _render("portal.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    if not database.has_admin_password():
        return RedirectResponse("/admin/setup", status_code=303)
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    # A machine with no rates cannot do anything useful, so finish setup
    # before showing a dashboard full of zeroes.
    if not database.has_rates():
        return RedirectResponse("/admin/setup-rates", status_code=303)
    return _render("admin.html")


@app.get("/admin/setup-rates", response_class=HTMLResponse)
async def admin_setup_rates_page(request: Request):
    """Second half of first-run setup: what is one payment worth.

    Deliberately a separate gate from the password. The operator has to
    name their own payment trigger, because the gateway ships without one
    -- there is no 'bottle' or 'coin' baked in to fall back on."""
    if not database.has_admin_password():
        return RedirectResponse("/admin/setup", status_code=303)
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    if database.has_rates():
        return RedirectResponse("/admin", status_code=303)
    return _render("setup_rates.html")


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if not database.has_admin_password():
        return RedirectResponse("/admin/setup", status_code=303)
    if is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return _render("login.html")


@app.get("/admin/setup", response_class=HTMLResponse)
async def admin_setup_page():
    if database.has_admin_password():
        return RedirectResponse("/admin/login", status_code=303)
    return _render("setup.html")


@app.post("/admin/setup")
async def admin_setup(request: Request, payload: dict):
    """First-run password creation, gated on the token printed to the log.

    Without the token this endpoint would be a free-for-all: the SSID is
    open, so every customer can reach it, and the first person to POST
    here would own the machine."""
    if database.has_admin_password():
        raise HTTPException(status_code=409, detail="An admin password is already set.")

    ip = request.client.host
    wait = auth.lockout_remaining(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {wait}s.")

    if not auth.check_setup_token((payload.get("setup_token") or "").strip()):
        auth.record_failure(ip)
        logger.warning(f"Rejected setup attempt from {ip} (bad token)")
        raise HTTPException(status_code=403, detail="That setup token is not correct.")

    username = (payload.get("username") or "").strip()
    if not auth.valid_username(username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-32 characters: letters, numbers, dot, dash or underscore.",
        )

    password = payload.get("password") or ""
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Use at least 8 characters.")
    if password != payload.get("confirm"):
        raise HTTPException(status_code=400, detail="The two passwords do not match.")

    salt, digest, iterations = await run_in_threadpool(auth.hash_password, password)
    database.store_admin_password(salt, digest, iterations)
    database.store_admin_username(username)
    auth.clear_failures(ip)
    auth.clear_setup_token()
    logger.warning(f"Admin password created from {ip}")

    return _login_response(request, {"created": True})


def _login_response(request: Request, body: dict):
    token = auth.new_session_token()
    database.create_admin_session(token, auth.SESSION_HOURS, request.client.host)

    response = JSONResponse(body)
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.SESSION_HOURS * 3600,
        httponly=True,      # not readable from JavaScript, so a portal XSS cannot lift it
        samesite="lax",
        path="/",
    )
    return response


@app.post("/admin/login")
async def admin_login(request: Request, payload: dict):
    ip = request.client.host
    wait = auth.lockout_remaining(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {wait}s.")

    salt, digest, iterations = database.get_admin_credentials()

    # Off the event loop: this is deliberately slow, and blocking here
    # would freeze the customer portal for every login attempt.
    #
    # The password is verified even when the username is already wrong.
    # Returning early on a bad username would make it answer far faster
    # than a bad password, which is a free way to discover the real
    # username by timing alone.
    password_ok = await run_in_threadpool(
        auth.verify_password, payload.get("password") or "", salt, digest, iterations
    )
    username_ok = auth.verify_username(payload.get("username") or "",
                                       database.get_admin_username() or "")

    if not (username_ok and password_ok):
        auth.record_failure(ip)
        logger.warning(f"Failed admin login from {ip}")
        # One message for both cases, so this cannot be used to confirm
        # whether a username exists.
        raise HTTPException(status_code=401, detail="Wrong username or password.")

    auth.clear_failures(ip)
    logger.info(f"Admin logged in from {ip}")
    return _login_response(request, {"authenticated": True})


@app.post("/admin/logout")
async def admin_logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        database.delete_admin_session(token)

    response = JSONResponse({"authenticated": False})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.post("/admin/password")
async def change_password(request: Request, payload: dict, _=Depends(require_admin)):
    """Changing the password logs out every other session, which is the
    point of changing it."""
    salt, digest, iterations = database.get_admin_credentials()
    ok = await run_in_threadpool(
        auth.verify_password, payload.get("current_password") or "", salt, digest, iterations
    )
    if not ok:
        raise HTTPException(status_code=403, detail="Current password is not correct.")

    new_username = (payload.get("new_username") or "").strip()
    new_password = payload.get("new_password") or ""

    if not new_username and not new_password:
        raise HTTPException(status_code=400, detail="Enter a new username or a new password.")

    if new_username and not auth.valid_username(new_username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-32 characters: letters, numbers, dot, dash or underscore.",
        )

    if new_password:
        if len(new_password) < 8:
            raise HTTPException(status_code=400, detail="Use at least 8 characters.")
        if new_password != payload.get("confirm"):
            raise HTTPException(status_code=400, detail="The two passwords do not match.")
        new_salt, new_digest, new_iterations = await run_in_threadpool(
            auth.hash_password, new_password
        )
        # Storing a password also drops every existing login, this one included.
        database.store_admin_password(new_salt, new_digest, new_iterations)
    else:
        # A username-only change must still sign other browsers out, so
        # re-store the same hash to clear the session table.
        database.store_admin_password(salt, digest, iterations)

    if new_username:
        database.store_admin_username(new_username)

    logger.warning(f"Admin credentials changed from {request.client.host}")

    # store_admin_password dropped every session including this one.
    return _login_response(request, {
        "changed": True,
        "username": database.get_admin_username(),
    })


@app.get("/admin/session")
async def admin_session(request: Request):
    """Lets the dashboard tell 'logged out' from 'server down'."""
    authenticated = is_admin(request)
    return {
        "authenticated": authenticated,
        "needs_setup": not database.has_admin_password(),
        "username": database.get_admin_username() if authenticated else None,
    }


@app.get("/health")
async def health(_=Depends(require_admin)):
    return {
        "status": "ok",
        "dev_mode": network_service.DEV_MODE,
        "granted_macs": network_service.granted_macs(),
    }


# ---- customer flow ----

@app.post("/claim")
async def claim(request: Request):
    """Called from the captive portal when the customer starts a payment.

    Identifies their MAC from their IP (via the ARP/neighbor table) and
    queues them, so the next payment the external validator confirms
    credits this device."""
    _require_rates()

    client_ip = request.client.host
    mac_address = network_service.resolve_mac_from_ip(client_ip)

    if not mac_address:
        raise HTTPException(
            status_code=400,
            detail="Could not identify your device. Try reconnecting to the WiFi.",
        )

    _reject_if_banned(mac_address)

    claim_id = database.create_claim(mac_address)
    return {"mac_address": mac_address, "claim_id": claim_id, "status": "pending"}


@app.post("/claim/cancel")
async def cancel_claim(request: Request):
    """Withdraws this device from the payment queue.

    Only ever called deliberately (the customer taps Cancel). The portal
    does NOT cancel on a timer: if a claim were dropped while someone was
    already paying, the machine would take the payment and credit
    nobody."""
    mac_address = _client_mac(request)
    if not mac_address:
        raise HTTPException(status_code=400, detail="Could not identify your device.")

    database.cancel_claim(mac_address)
    return {"mac_address": mac_address, "cancelled": True}


@app.get("/status")
async def status(request: Request):
    """Polled by the portal for the live HR/MIN/SEC countdown."""
    client_ip = request.client.host
    mac_address = network_service.resolve_mac_from_ip(client_ip)
    device = database.get_device_status()
    return {
        "ip": client_ip,
        "mac_address": mac_address,
        "session": database.get_session(mac_address) if mac_address else None,
        "session_cap_seconds": database.get_session_cap_seconds(),
        # Wording belongs to the operator: the same portal fronts a bottle
        # validator, a coin slot or a card reader.
        "portal_text": database.get_portal_text(),
        "configured": database.has_rates(),
        # So the portal stops asking for payment the machine cannot take.
        "machine": {
            "accepting": device["accepting"],
            "device_online": device["online"],
            "ever_seen": device["last_seen"] is not None,
            # Whatever the device chose to say for itself, if anything.
            "message": device["message"],
        },
    }


@app.post("/pause")
async def pause(request: Request, payload: dict | None = None):
    """Time Pause -- banks the customer's remaining time and cuts their
    access until they resume. Access is revoked BEFORE the database is
    updated, so a firewall failure leaves the session running rather
    than banking time the customer can still surf on."""
    mac_address = _target_mac(request, payload)
    if not mac_address:
        raise HTTPException(status_code=400, detail="Could not identify your device.")

    session = database.get_session(mac_address)
    if not session or session["status"] != "active":
        raise HTTPException(status_code=400, detail="You have no running time to pause.")

    if not network_service.block_mac(mac_address):
        raise HTTPException(status_code=500, detail="Could not pause your session. Try again.")

    paused = database.pause_session(mac_address)
    if not paused:
        raise HTTPException(status_code=400, detail="You have no running time to pause.")

    logger.info(f"Paused {mac_address} with {paused['remaining_seconds']}s banked")
    return paused


@app.post("/resume")
async def resume(request: Request, payload: dict | None = None):
    """Resume Time -- restarts the clock on banked time."""
    mac_address = _target_mac(request, payload)
    if not mac_address:
        raise HTTPException(status_code=400, detail="Could not identify your device.")

    _reject_if_banned(mac_address)

    resumed = database.resume_session(mac_address)
    if not resumed:
        raise HTTPException(status_code=400, detail="You have no paused time to resume.")

    if not network_service.allow_mac(mac_address):
        # Put it back the way we found it rather than leaving the clock
        # running on a device the firewall is still blocking.
        database.pause_session(mac_address)
        raise HTTPException(status_code=500, detail="Could not resume your session. Try again.")

    logger.info(f"Resumed {mac_address} with {resumed['remaining_seconds']}s")
    return resumed


# ---- grant (the one coupling point to the payment device) ----

def _resolve_payment_method(requested):
    """Picks the rate to charge against.

    A named method must exist. An unnamed one is only unambiguous when a
    single rate is configured -- otherwise the device has to say which
    trigger fired, because the gateway cannot guess what the customer
    actually paid with."""
    methods = database.get_payment_methods()

    if requested:
        requested = str(requested).strip()
        if requested not in methods:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown payment method {requested!r}. Configured: " +
                       ", ".join(methods),
            )
        return requested

    if len(methods) == 1:
        return methods[0]

    raise HTTPException(
        status_code=400,
        detail="Several payment methods are configured, so the request must name "
               "one: " + ", ".join(methods),
    )

def _grant_oldest_pending(payment_method=None, quantity=1):
    """Shared by the real validator endpoint and the dev simulator.

    `payment_method` names which configured rate to apply, so one gateway
    can front several triggers (a coin slot and a bottle chute, say). When
    the caller does not say, the single configured rate is used, which
    keeps simple one-trigger machines simple.

    `quantity` is how many were taken in this one payment. It is priced
    against the operator's tiers, so bulk bonuses apply."""
    _require_rates()

    payment_method = _resolve_payment_method(payment_method)

    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="quantity must be a whole number")
    if quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be at least 1")
    if quantity > database.MAX_QUANTITY:
        raise HTTPException(
            status_code=400,
            detail=f"quantity cannot exceed {database.MAX_QUANTITY}",
        )

    pending = database.get_oldest_pending()
    if not pending:
        raise HTTPException(status_code=400, detail="No pending claim to grant")

    mac_address = pending["mac_address"]

    # A device banned after it queued must not be paid out. Drop the claim
    # so the payment is not silently swallowed by a dead entry.
    if database.is_mac_banned(mac_address):
        database.mark_claim_granted(pending["id"])
        logger.warning(f"Discarded claim from banned device {mac_address}")
        raise HTTPException(status_code=403, detail="That device has been blocked by the operator.")

    earned_minutes = database.calculate_minutes(payment_method, quantity)
    if earned_minutes <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"No rate is configured for payment method {payment_method!r}.",
        )

    # Close the claim BEFORE crediting. If two payments land at once, only
    # one call wins this guard, so a single claim can never be paid twice.
    if not database.mark_claim_granted(pending["id"]):
        raise HTTPException(status_code=409, detail="That claim was just granted")

    added = database.credit_time(mac_address, earned_minutes, payments=quantity)
    credited_minutes = added // 60
    database.log_transaction(mac_address, payment_method, quantity, credited_minutes)
    router_updated = network_service.allow_mac(mac_address)

    session = database.get_session(mac_address)
    return {
        **session,
        "router_updated": router_updated,
        "payment_method": payment_method,
        "quantity": quantity,
        "minutes_earned": earned_minutes,
        "minutes_credited": credited_minutes,
        # True when the session cap swallowed some or all of the payment,
        # so the portal can explain the shortfall instead of appearing
        # to lose time.
        "capped": added < earned_minutes * 60,
    }


@app.post("/grant")
async def grant(payload: dict | None = None):
    """Called by the external payment device once it has validated a
    payment -- a bottle validator, coin acceptor, card reader or anything
    else. Grants whichever device is currently the oldest pending claim.

    Optional body:
        payment_method  which configured trigger fired. Required only when
                        more than one is configured.
        quantity        how many were taken in this one payment (default
                        1). Priced against the operator's tiers, so bulk
                        bonuses apply automatically.

    NOTE: this assumes normally one pending claim at a time. If the
    validator can identify WHICH waiting customer paid, this endpoint
    should be extended to accept and use that identifier rather than
    always taking the oldest claim."""
    payload = payload or {}
    return _grant_oldest_pending(payload.get("payment_method"),
                                 payload.get("quantity", 1))


@app.post("/dev/simulate-payment")
async def dev_simulate_payment(payload: dict | None = None):
    """DEV ONLY -- stands in for the payment device so the claim-to-grant
    flow can be exercised without hardware. Returns 404 outside dev mode
    so it cannot be reached on a deployed machine."""
    if not network_service.DEV_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    payload = payload or {}
    logger.info("[DEV] Simulating a validated payment")
    return _grant_oldest_pending(payload.get("payment_method"),
                                 payload.get("quantity", 1))


# ---- vouchers ----

@app.get("/vouchers")
async def list_vouchers(_=Depends(require_admin)):
    return database.get_vouchers()


@app.post("/vouchers")
async def generate_voucher(payload: dict, _=Depends(require_admin)):
    minutes = int(payload.get("minutes", 30))
    if minutes <= 0:
        raise HTTPException(status_code=400, detail="minutes must be greater than zero")
    code = database.create_voucher(minutes)
    return {"code": code, "minutes": minutes}


@app.post("/vouchers/redeem")
async def redeem_voucher(request: Request, payload: dict):
    """Redeems a code for time. The portal sends only the code and the
    MAC is resolved from the request, so a customer cannot credit a
    voucher to somebody else's device."""
    code = payload.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="A voucher code is required")

    # Throttled like the admin login. Codes are short, and without this a
    # customer on the open SSID can sit and enumerate them for free time.
    # Namespaced so voucher guesses cannot lock the operator out of the
    # dashboard, and vice versa.
    throttle_key = f"voucher:{request.client.host}"
    wait = auth.lockout_remaining(throttle_key)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Too many incorrect codes. Try again in {wait} seconds.",
        )

    mac_address = _target_mac(request, payload)
    if not mac_address:
        raise HTTPException(status_code=400, detail="Could not identify your device.")

    _reject_if_banned(mac_address)

    minutes = database.redeem_voucher(code)
    if minutes is None:
        auth.record_failure(throttle_key)
        logger.warning(f"Rejected voucher code from {request.client.host}")
        raise HTTPException(status_code=400, detail="Invalid or already-used voucher")

    auth.clear_failures(throttle_key)

    added = database.credit_time(mac_address, minutes)
    credited_minutes = added // 60
    database.log_transaction(mac_address, "voucher", 1, credited_minutes)
    router_updated = network_service.allow_mac(mac_address)

    session = database.get_session(mac_address)
    return {
        **session,
        "router_updated": router_updated,
        "minutes_credited": credited_minutes,
        "capped": added < minutes * 60,
    }


# ---- admin ----

@app.get("/admin/stats")
async def admin_stats(_=Depends(require_admin)):
    return database.get_stats()


@app.get("/admin/system-status")
async def system_status(_=Depends(require_admin)):
    """Everything the operator needs to tell "the machine is fine" from
    "the machine is quietly not taking payments"."""
    device = database.get_device_status()

    # Does the firewall actually match what the database believes? These
    # drift apart if an nft call failed, if the ruleset was reloaded
    # under the app, or right after a restore -- and the symptom is
    # someone surfing for free, or a paying customer with no internet.
    db_active = {
        s["mac_address"].upper() for s in database.get_all_sessions()
        if s["status"] == "active"
    }
    fw_granted = {m.upper() for m in network_service.granted_macs()}

    return {
        "payment_device": device,
        "configured": database.has_rates(),
        "gateway": system_monitor.summary(database.DB_PATH),
        "firewall": {
            "dev_mode": network_service.DEV_MODE,
            "granted_count": len(fw_granted),
            "active_in_db": len(db_active),
            "missing_from_firewall": sorted(db_active - fw_granted),
            "unexpected_in_firewall": sorted(fw_granted - db_active),
            "in_sync": db_active == fw_granted,
        },
    }


@app.post("/device/heartbeat")
async def device_heartbeat(request: Request, payload: dict | None = None):
    """Called periodically by the payment device so the dashboard can show
    whether it is alive and whether it can still take payment.

    This is a SECOND coupling point beyond POST /grant. There is no
    alternative: the sensors are wired to the device, so the gateway
    cannot observe them directly. The boundary stays one-way -- the device
    calls us, we never call it -- so it remains independently testable.

    Body is entirely optional and entirely freeform. Two keys are given
    meaning by the gateway:
        accepting  (bool) -- device cannot currently take payment
        message    (str)  -- what to tell customers while that is true
    Everything else is stored and shown on the dashboard as-is, so any
    hardware can report whatever it has: bin_full, hopper_coins,
    terminal_id, temperature, and so on.

    Keys are merged with what was reported before, so a device that sends
    its firmware once and then only sends a level reading loses nothing."""
    payload = payload or {}
    metadata = payload.get("metadata")

    if not isinstance(metadata, dict):
        # Allow a flat body too: everything that is not a reserved key
        # becomes metadata. Simpler for firmware with a small JSON writer.
        metadata = {k: v for k, v in payload.items()
                    if k not in ("firmware", "metadata")}

    status = database.record_heartbeat(
        firmware=payload.get("firmware"),
        source_ip=request.client.host,
        metadata=metadata,
    )
    return status


@app.get("/sessions")
async def sessions(_=Depends(require_admin)):
    return database.get_all_sessions()


@app.post("/block")
async def block(request: Request, payload: dict | None = None, _=Depends(require_admin)):
    mac_address = _target_mac(request, payload)
    if not mac_address:
        raise HTTPException(status_code=400, detail="mac_address is required")

    blocked = network_service.block_mac(mac_address)
    if not blocked:
        raise HTTPException(status_code=500, detail="Could not revoke access; firewall rejected it")

    database.expire_session(mac_address)
    return {"mac_address": mac_address, "blocked": blocked}


@app.get("/transactions")
async def transactions(_=Depends(require_admin)):
    return database.get_transactions()


# ---- backup / restore ----

@app.get("/admin/backup")
async def download_backup(_=Depends(require_admin)):
    """Streams a consistent snapshot of the database.

    Worth doing before every rate change or software update -- an SD card
    in an unattended machine is the single most likely thing to fail."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tmp_dir = tempfile.mkdtemp(prefix="ecowifi-backup-")
    dest = Path(tmp_dir) / f"ecowifi-backup-{stamp}.db"

    try:
        database.backup_to(dest)
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Could not create backup: {e}")

    return FileResponse(
        path=dest,
        media_type="application/octet-stream",
        filename=dest.name,
    )


@app.post("/admin/restore")
async def restore_backup(request: Request, _=Depends(require_admin)):
    """Replaces the live database with an uploaded backup.

    Sent as a raw body rather than multipart on purpose -- multipart would
    pull in python-multipart, and this is the only upload in the app.

    The uploaded file is validated BEFORE anything touches the live
    database, and the current database is saved alongside first, so a bad
    restore is recoverable rather than terminal."""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="No file was uploaded")

    # Read these before the swap; the restored file will not contain them.
    kept_salt, kept_hash = database.get_admin_credentials()
    kept_token = request.cookies.get(auth.COOKIE_NAME)

    if len(body) > 64 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Backup file is unreasonably large")

    tmp_dir = tempfile.mkdtemp(prefix="ecowifi-restore-")
    incoming = Path(tmp_dir) / "incoming.db"
    incoming.write_bytes(body)

    ok, message = database.inspect_db_file(incoming)
    if not ok:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=message)

    # Keep the database we are about to overwrite.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safety = f"{database.DB_PATH}.pre-restore-{stamp}"
    try:
        database.backup_to(safety)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.error(f"Refusing to restore, could not save current database: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Could not back up the current database, so nothing was changed: {e}",
        )

    try:
        os.replace(incoming, database.DB_PATH)
    except OSError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.error(f"Restore failed during swap: {e}")
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")

    shutil.rmtree(tmp_dir, ignore_errors=True)

    database.init_db()          # migrate the restored file if it is older

    # Carry the CURRENT credentials across rather than importing whatever
    # the backup held. A backup taken before authentication existed, or
    # with an old password, would otherwise lock the operator out of their
    # own machine -- and the token in this request already proved they are
    # the operator, so re-issue their login too.
    if kept_salt and kept_hash:
        database.store_admin_password(kept_salt, kept_hash)
        if kept_token:
            database.create_admin_session(kept_token, auth.SESSION_HOURS, request.client.host)

    resync = _resync_firewall()

    logger.warning(f"Database restored from upload ({message}); previous saved to {safety}")
    return {
        "restored": True,
        "detail": message,
        "previous_database": safety,
        "firewall_resync": resync,
    }


def _resync_firewall():
    """Makes the firewall match the database.

    Needed after a restore, because the restored sessions have nothing to
    do with whichever MACs the running firewall happens to allow."""
    granted = {m.upper() for m in network_service.granted_macs()}
    active = {
        s["mac_address"].upper() for s in database.get_all_sessions()
        if s["status"] == "active"
    }

    added, removed = [], []
    for mac in active - granted:
        if network_service.allow_mac(mac):
            added.append(mac)
    for mac in granted - active:
        if network_service.block_mac(mac):
            removed.append(mac)

    return {"granted": added, "revoked": removed}


@app.post("/admin/resync-firewall")
async def resync_firewall(_=Depends(require_admin)):
    """Manual trigger for the same reconciliation, for when System Status
    reports the firewall and database disagree."""
    return _resync_firewall()


# ---- MAC control ----

@app.get("/admin/blocklist")
async def list_banned(_=Depends(require_admin)):
    return database.get_banned_macs()


@app.post("/admin/blocklist")
async def ban_device(payload: dict, _=Depends(require_admin)):
    """Permanently bans a device: no claims, grants, vouchers or resumes.

    Different from /block, which just ends the current session. Use this
    for someone gaming the sensor or reconnecting to farm free time."""
    mac_address = (payload.get("mac_address") or "").strip().upper()
    if not mac_address:
        raise HTTPException(status_code=400, detail="mac_address is required")

    database.ban_mac(mac_address, payload.get("reason"))

    # Kick them off now rather than at expiry.
    database.cancel_claim(mac_address)
    network_service.block_mac(mac_address)
    database.expire_session(mac_address)

    logger.warning(f"Banned {mac_address}: {payload.get('reason') or 'no reason given'}")
    return {"mac_address": mac_address, "banned": True}


@app.post("/admin/blocklist/remove")
async def unban_device(payload: dict, _=Depends(require_admin)):
    mac_address = (payload.get("mac_address") or "").strip().upper()
    if not mac_address:
        raise HTTPException(status_code=400, detail="mac_address is required")

    removed = database.unban_mac(mac_address)
    if not removed:
        raise HTTPException(status_code=404, detail="That MAC is not banned")

    logger.info(f"Unbanned {mac_address}")
    return {"mac_address": mac_address, "banned": False}


@app.get("/rates")
async def list_rates():
    """Every tier, plus what each quantity would ACTUALLY earn.

    A tier priced worse than the base rate is never chosen by the solver,
    so publishing its own figure would quote customers a price the machine
    will not charge. effective_minutes is what they would really get."""
    rates = database.get_all_rates()
    for row in rates:
        row['effective_minutes'] = database.calculate_minutes(
            row['payment_method'], row['quantity'])
    return rates


@app.post("/rates")
async def update_rate(payload: dict, _=Depends(require_admin)):
    """Creates or updates one price tier.

    quantity defaults to 1, which is the base rate for that trigger.
    Higher quantities are bulk tiers: 3 bottles for 2 hours, and so on."""
    payment_method = (payload.get("payment_method") or "").strip()
    # minutes_per_unit is accepted as an alias so older callers keep working.
    minutes = payload.get("minutes", payload.get("minutes_per_unit"))

    if not payment_method or minutes is None:
        raise HTTPException(
            status_code=400, detail="payment_method and minutes are required")

    try:
        minutes = int(minutes)
        quantity = int(payload.get("quantity", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="minutes and quantity must be whole numbers")

    if minutes <= 0:
        raise HTTPException(status_code=400, detail="minutes must be greater than zero")
    if quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be at least 1")
    if quantity > database.MAX_QUANTITY:
        raise HTTPException(
            status_code=400, detail=f"quantity cannot exceed {database.MAX_QUANTITY}")

    tiers = database.get_rate_tiers(payment_method)
    if quantity > 1 and 1 not in tiers:
        raise HTTPException(
            status_code=400,
            detail="Set the base rate first: add a quantity of 1 for "
                   f"{payment_method!r} before adding bulk tiers.",
        )

    database.set_rate(payment_method, minutes, quantity)
    return {"payment_method": payment_method, "quantity": quantity, "minutes": minutes}


@app.post("/rates/delete")
async def delete_rate(payload: dict, _=Depends(require_admin)):
    """Removes one bulk tier, or a whole payment method.

    Omit quantity (or pass 1) to remove the method entirely. The last
    remaining method cannot be removed, because a machine with no rates
    cannot grant anything."""
    payment_method = (payload.get("payment_method") or "").strip()
    if not payment_method:
        raise HTTPException(status_code=400, detail="payment_method is required")

    quantity = payload.get("quantity")
    if quantity is not None:
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="quantity must be a whole number")

    removing_method = quantity is None or quantity == 1
    if removing_method and len(database.get_payment_methods()) <= 1:
        raise HTTPException(
            status_code=400,
            detail="This is the only payment method. Add another before removing it, "
                   "or the machine cannot grant any time.",
        )

    if not database.delete_rate(payment_method, quantity):
        raise HTTPException(status_code=404, detail="No such rate")

    logger.warning(f"Rate removed: {payment_method} (quantity {quantity or 'all'})")
    return {"payment_method": payment_method, "quantity": quantity, "deleted": True}


@app.get("/admin/portal-text")
async def get_portal_text(_=Depends(require_admin)):
    """Current customer-facing wording, plus the defaults so the dashboard
    can show what each field falls back to."""
    return {
        "text": database.get_portal_text(),
        "defaults": database.DEFAULT_PORTAL_TEXT,
    }


@app.post("/admin/portal-text")
async def set_portal_text(payload: dict, _=Depends(require_admin)):
    """Updates the wording shown to customers. Send only the keys you want
    to change; an empty value resets that key to its default."""
    values = payload.get("text") if isinstance(payload.get("text"), dict) else payload
    unknown = [k for k in values if k not in database.DEFAULT_PORTAL_TEXT]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail="Unknown portal text keys: " + ", ".join(sorted(unknown)),
        )

    text = database.set_portal_text(values)
    logger.info("Portal wording updated")
    return {"text": text, "defaults": database.DEFAULT_PORTAL_TEXT}


@app.get("/admin/session-cap")
async def get_session_cap(_=Depends(require_admin)):
    return {"session_cap_seconds": database.get_session_cap_seconds()}


@app.post("/admin/session-cap")
async def set_session_cap(payload: dict, _=Depends(require_admin)):
    """Maximum time a single device may hold. 0 removes the cap."""
    hours = payload.get("hours")
    if hours is None:
        raise HTTPException(status_code=400, detail="hours is required")
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="hours must be a number")
    if hours < 0:
        raise HTTPException(status_code=400, detail="hours cannot be negative")

    database.set_session_cap_seconds(int(hours * 3600))
    return {"session_cap_seconds": database.get_session_cap_seconds()}
