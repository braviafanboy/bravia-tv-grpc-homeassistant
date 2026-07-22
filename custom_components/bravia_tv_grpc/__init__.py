"""The Bravia TV (gRPC) integration.

Controls Sony BRAVIA TVs over the local ControlDeviceService gRPC API (the
BRAVIA Connect control plane): live push via StartNotifyStates and control via
ExecCommandWithAuth. Entities are driven by the device's GetCapabilities schema.
"""

from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bravia_tv_client import (
    BraviaTvAuthError,
    BraviaTvConnectionError,
    BraviaTvGrpcClient,
)
from .const import (
    CONF_DEVICE_UNIQUE_ID,
    CONF_FW_PENDING,
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_KEYS,
    CONF_GRPC_PORT,
    CONF_MODEL,
    CONF_MODEL_ID,
    CONF_SW_VERSION,
    DOMAIN,
)
from .coordinator import BraviaTvCoordinator
from .grpc import credentials as cred
from .grpc.credentials import GrpcCredentialsError, GrpcCredentialsRefreshError
from .grpc_discovery import (
    async_discover_port_mdns,
    async_resolve_device_mdns,
    discover_grpc_port,
)

# Sony advertises `<friendly name>-<40-hex device_unique_id>._sonysmarthome…`.
_DEVICE_UID_RE = re.compile(r"-([0-9a-f]{40})", re.IGNORECASE)

_LOGGER = logging.getLogger(__name__)

_ISSUE_PORT_UNREACHABLE = "grpc_port_unreachable"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.REMOTE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bravia TV (gRPC) from a config entry."""
    host = entry.data[CONF_HOST]
    creds = cred.parse_credentials_json(entry.data[CONF_GRPC_KEYS])
    device_id = entry.data.get(CONF_GRPC_DEVICE_ID) or creds.get("device_id")

    # Record the device's stable mDNS unique id for entries paired before it was
    # stored, so a later IP change is matched to this entry and self-heals.
    if not entry.data.get(CONF_DEVICE_UNIQUE_ID):
        await _backfill_device_unique_id(hass, entry, host)

    manual_port = entry.options.get(CONF_GRPC_PORT) or None
    port = await _resolve_port(hass, host, entry.data.get(CONF_GRPC_PORT), manual_port)
    if not port:
        # Surface an actionable repair; the user can check the TV/network or set
        # a manual port in the integration's options.
        ir.async_create_issue(
            hass,
            DOMAIN,
            _ISSUE_PORT_UNREACHABLE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_ISSUE_PORT_UNREACHABLE,
            translation_placeholders={"host": host},
        )
        raise ConfigEntryNotReady(f"Could not locate gRPC service on {host}")
    ir.async_delete_issue(hass, DOMAIN, _ISSUE_PORT_UNREACHABLE)
    # Persist the resolved port so the next start is instant.
    if port != entry.data.get(CONF_GRPC_PORT):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_GRPC_PORT: port}
        )

    try:
        coordinator = await _connect(hass, entry, host, port, creds, device_id)
    except BraviaTvAuthError as err:
        # A TV reboot invalidates the Sony session keys (and changes the port,
        # already handled above). Refresh the keys via the cloud and retry once.
        _LOGGER.info("gRPC auth failed (%s); refreshing Sony session keys", err)
        try:
            creds = await _refresh_credentials(hass, entry, creds, device_id)
        except Exception as err2:  # noqa: BLE001
            if _needs_reauth(err2):
                # The refresh token itself is no longer usable — only the user
                # can recover, by re-logging in. Trigger the reauth flow.
                raise ConfigEntryAuthFailed(
                    f"Sony credentials rejected, re-authentication required: {err2}"
                ) from err2
            # A transient cloud/network error (e.g. a reset connection while the
            # TV is still booting). Let HA retry with backoff — do NOT force the
            # user through a re-login for a temporary blip.
            raise ConfigEntryNotReady(
                f"Sony key refresh temporarily failed: {err2}"
            ) from err2
        try:
            coordinator = await _connect(hass, entry, host, port, creds, device_id)
        except (BraviaTvAuthError, BraviaTvConnectionError) as err2:
            # Fresh, valid cloud keys were still rejected by the TV — it is
            # almost certainly still settling after a reboot. Re-authentication
            # would only yield equivalent keys, so retry rather than prompt.
            raise ConfigEntryNotReady(
                f"TV rejected refreshed credentials, will retry: {err2}"
            ) from err2
    except BraviaTvConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    coordinator.applied_manual_port = manual_port
    await _ensure_device_info(hass, entry, creds, device_id, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _backfill_device_unique_id(
    hass: HomeAssistant, entry: ConfigEntry, host: str
) -> None:
    """Resolve the device's mDNS unique id at the current host and persist it.

    Best-effort — a blocked mDNS just leaves the entry as-is (it keeps working;
    only the automatic IP-change recovery is unavailable until next time).
    """
    try:
        resolved = await async_resolve_device_mdns(hass, host)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("unique-id backfill mDNS lookup failed", exc_info=True)
        return
    if not resolved:
        return
    _, name, _ = resolved
    match = _DEVICE_UID_RE.search(name or "")
    if match:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICE_UNIQUE_ID: match.group(1).lower()},
        )


async def _ensure_device_info(
    hass: HomeAssistant,
    entry: ConfigEntry,
    creds: dict,
    device_id: str,
    coordinator: BraviaTvCoordinator,
) -> None:
    """Fetch the device model/model_id/sw_version from the Sony IoT device list.

    Fetched once for the initial backfill, and again once a firmware update has
    been applied (so the installed version stays current). Best-effort — never
    blocks setup.
    """
    current_update = bool((coordinator.data or {}).get("fw_update.update_exist"))
    pending = bool(entry.data.get(CONF_FW_PENDING))
    firmware_settled = pending and not current_update  # was available, now applied
    if entry.data.get(CONF_MODEL) and not firmware_settled:
        # Not the initial backfill and no update just applied — only track a
        # newly-available update so we can re-fetch once it's installed.
        if current_update and not pending:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_FW_PENDING: True}
            )
        return
    hardware = await _fetch_device_hardware(hass, creds, device_id)
    if hardware is None:
        return
    updates = {
        key: hardware[field]
        for key, field in (
            (CONF_MODEL, "model"),
            (CONF_MODEL_ID, "model_id"),
            (CONF_SW_VERSION, "sw_version"),
        )
        if hardware.get(field)
    }
    # Sync the flag to reality: cleared once we've re-read after an applied
    # update, set if an update is (still) available.
    updates[CONF_FW_PENDING] = current_update
    hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})


async def _fetch_device_hardware(
    hass: HomeAssistant, creds: dict, device_id: str
) -> dict[str, str | None] | None:
    """Best-effort Sony IoT device-info fetch; None on failure."""
    try:
        session = async_get_clientsession(hass)
        token = (
            await cred.async_refresh_access_token(session, creds["refresh_token"])
        )["access_token"]
        devices = (await cred.async_get_devices(session, token)).get("devices", [])
    except Exception:  # noqa: BLE001
        _LOGGER.debug("device-info fetch failed", exc_info=True)
        return None
    device = next(
        (d for d in devices if d.get("device_id") == device_id),
        devices[0] if devices else None,
    )
    return cred.device_hardware_info(device) if device else None


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when the manual-port option changed; ignore runtime data
    writes (e.g. the firmware-pending flag)."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    new_manual = entry.options.get(CONF_GRPC_PORT) or None
    if coordinator is None or new_manual != coordinator.applied_manual_port:
        await hass.config_entries.async_reload(entry.entry_id)


async def _connect(
    hass: HomeAssistant,
    entry: ConfigEntry,
    host: str,
    port: int,
    creds: dict,
    device_id: str,
) -> BraviaTvCoordinator:
    """Build a client + coordinator and start it (connect, seed, push)."""
    client = BraviaTvGrpcClient(
        host,
        port,
        device_id,
        creds["hmac_key"],
        key_id=creds.get("key_id"),
        session_key=creds.get("session_key"),
    )
    coordinator = BraviaTvCoordinator(hass, entry, client)
    await coordinator.async_start()
    return coordinator


def _needs_reauth(err: Exception) -> bool:
    """Whether a key-refresh failure means the user must re-authenticate.

    Only a Sony cloud rejection of the refresh token (or a missing token) is
    unrecoverable without the user; network resets, 5xx and TV-side handshake
    errors are transient and should be retried instead.
    """
    if isinstance(err, GrpcCredentialsError):
        return True
    if isinstance(err, GrpcCredentialsRefreshError):
        return err.status in (400, 401, 403)
    return False


async def _refresh_credentials(
    hass: HomeAssistant, entry: ConfigEntry, creds: dict, device_id: str
) -> dict:
    """Fetch fresh Sony session keys and persist them to the config entry."""
    session = async_get_clientsession(hass)
    new_creds = await cred.async_refresh_credentials(session, creds, device_id)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_GRPC_KEYS: cred.credentials_to_json(new_creds)},
    )
    return new_creds


async def _resolve_port(
    hass: HomeAssistant, host: str, cached: int | None, manual: int | None = None
) -> int | None:
    """Locate the dynamic gRPC port: manual -> cached -> mDNS -> full scan."""
    # 0. Manual override from options — try it first (escape hatch for networks
    #    where discovery is blocked). If it no longer serves (TV rebooted onto a
    #    new port), fall through to normal discovery rather than failing.
    if manual and await hass.async_add_executor_job(_port_serves_grpc, host, manual):
        return manual
    # 1. Cached port from a previous run — instant when the TV hasn't rebooted.
    if cached and await hass.async_add_executor_job(_port_serves_grpc, host, cached):
        return cached
    # 2. mDNS (_sonysmarthome._tcp) — fast, handles a changed port after reboot.
    #    Best-effort: any zeroconf hiccup must fall through to the scan, never
    #    fail setup.
    try:
        port = await async_discover_port_mdns(hass, host)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("mDNS port discovery failed; falling back to scan", exc_info=True)
        port = None
    if port and await hass.async_add_executor_job(_port_serves_grpc, host, port):
        return port
    # 3. TCP scan — last resort. The TV's dynamic port has only ever been seen
    #    in the Linux ephemeral range, so scan that first (about half the ports,
    #    so half the time); fall back to the full range if it isn't found.
    candidates: tuple[int, ...] = (cached,) if cached else ()
    port = await hass.async_add_executor_job(
        discover_grpc_port, host, candidates, range(32768, 61000)
    )
    if port:
        return port
    return await hass.async_add_executor_job(
        discover_grpc_port, host, candidates, range(1024, 65536)
    )


def _port_serves_grpc(host: str, port: int) -> bool:
    from .grpc_discovery import is_control_device_service

    return is_control_device_service(host, port)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: BraviaTvCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unloaded
