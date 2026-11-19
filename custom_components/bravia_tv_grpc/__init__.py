"""The Bravia TV (gRPC) integration.

Controls Sony BRAVIA TVs over the local ControlDeviceService gRPC API (the
BRAVIA Connect control plane): live push via StartNotifyStates and control via
ExecCommandWithAuth. Entities are driven by the device's GetCapabilities schema.
A few controls that exist only on the TV's Sony-cloud capability set (sleep
timer, picture off, playback transport, deep links, …) go through the Sony
IoT cloud with the same account; see ``grpc/cloud_commands.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .bravia_tv_client import BraviaTvConnectionError, BraviaTvGrpcClient
from .const import (
    CONF_DEVICE_UNIQUE_ID,
    CONF_FW_PENDING,
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_KEYS,
    CONF_GRPC_PORT,
    CONF_MODEL,
    CONF_MODEL_ID,
    CONF_SW_VERSION,
    DATA_ADDRESS_HINTS,
    DOMAIN,
)
from .coordinator import (
    BraviaTvCoordinator,
    BraviaTvReauthRequired,
    rest_options,
    schema_store,
)
from .grpc import credentials as cred
from .grpc.device_id import parse_instance_name
from .grpc_discovery import async_resolve_device_mdns, async_resolve_port

# Module-level so the port lookup has one name to stub in tests.
_resolve_port = async_resolve_port

_LOGGER = logging.getLogger(__name__)

_ISSUE_PORT_UNREACHABLE = "grpc_port_unreachable"
# Repairs raised per TV, by the prefix of their id (the entry id follows).
_ENTRY_ISSUES = (
    f"{_ISSUE_PORT_UNREACHABLE}_",
    "agent_permissions_",
    "agent_outdated_",
    "agent_contract_",
    "agent_identity_changed_",
    "agent_pairing_",
    "rest_key_rejected_",
    "rest_unavailable_",
)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.MEDIA_PLAYER,
    Platform.NOTIFY,
    Platform.NUMBER,
    Platform.REMOTE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.UPDATE,
]


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's device-targeted actions."""
    from .services import async_setup_services

    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bravia TV (gRPC) from a config entry.

    The TV does not have to be on. Once it has been reached from this install
    its capability schema is stored, and a later setup that cannot reach it
    (standby, unplugged) loads anyway: the TV reads as off and the coordinator
    keeps looking for it. Only a TV that has never been reached makes setup
    wait and retry, because there is nothing yet to build its entities from.
    """
    host = entry.data[CONF_HOST]
    creds = cred.parse_credentials_json(entry.data[CONF_GRPC_KEYS])
    device_id = entry.data.get(CONF_GRPC_DEVICE_ID) or creds.get("device_id")

    # Record the device's stable mDNS unique id for entries paired before it was
    # stored, so a later IP change is matched to this entry and self-heals.
    if not entry.data.get(CONF_DEVICE_UNIQUE_ID):
        await _backfill_device_unique_id(hass, entry, host)

    manual_port = entry.options.get(CONF_GRPC_PORT) or None
    client = BraviaTvGrpcClient(
        host,
        entry.data.get(CONF_GRPC_PORT) or 0,
        device_id or "",
        creds.get("hmac_key") or "",
        key_id=creds.get("key_id"),
        session_key=creds.get("session_key"),
    )
    coordinator = BraviaTvCoordinator(hass, entry, client)
    has_schema = await coordinator.async_load_cache()
    # An address mDNS announced for this TV while its setup was failing.
    hint = hass.data.get(DATA_ADDRESS_HINTS, {}).pop(entry.entry_id, None)
    issue_id = f"{_ISSUE_PORT_UNREACHABLE}_{entry.entry_id}"
    try:
        await _async_start(hass, entry, coordinator, manual_port, hint)
    except BraviaTvReauthRequired as err:
        # The refresh token itself is no longer usable — only the user can
        # recover, by re-logging in. Trigger the reauth flow.
        raise ConfigEntryAuthFailed(
            f"Sony credentials rejected, re-authentication required: {err}"
        ) from err
    except BraviaTvConnectionError as err:
        if not has_schema:
            # Never reached from this install: surface an actionable repair
            # (check the TV and network, or set the port in the options) and
            # let Home Assistant retry.
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=_ISSUE_PORT_UNREACHABLE,
                translation_placeholders={"host": host},
            )
            raise ConfigEntryNotReady(str(err)) from err
        _LOGGER.info("%s is unreachable (%s); starting without it", entry.title, err)
        await coordinator.async_start_offline()
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    # The id this repair had before it was raised per TV.
    ir.async_delete_issue(hass, DOMAIN, _ISSUE_PORT_UNREACHABLE)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    coordinator.applied_manual_port = manual_port
    # The keys may just have been renewed: read the credentials back.
    creds = cred.parse_credentials_json(entry.data[CONF_GRPC_KEYS])
    await _ensure_device_info(hass, entry, creds, device_id or "", coordinator)
    _remove_other_picture_entities(hass, entry, coordinator.rest_writable)
    _remove_retired_entities(hass, entry)
    if not coordinator.agent_update_checks:
        _remove_agent_update_entity(hass, entry)
    coordinator.applied_agent_update_checks = coordinator.agent_update_checks
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.agent_entities_created = coordinator.agent_expected
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_start(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BraviaTvCoordinator,
    manual_port: int | None,
    hint: tuple[str, int] | None,
) -> None:
    """Find the TV and start the coordinator connected to it.

    Raises BraviaTvConnectionError when it cannot be reached right now, with
    nothing started."""
    client = coordinator.client
    host = entry.data[CONF_HOST]
    if hint is not None and hint[0] != host:
        # mDNS says the TV moved. Believe it only if that address proves to be
        # the TV; the stored address is not touched otherwise.
        client.host, client.port = hint
        try:
            # Stores the address itself once it has proved to be the TV.
            await coordinator.async_start(verify=True)
        except BraviaTvConnectionError as err:
            _LOGGER.debug("announced address %s:%s not used: %s", *hint, err)
            hint = None
        else:
            return
    port = await _resolve_port(
        hass,
        host,
        entry.data.get(CONF_GRPC_PORT),
        manual_port,
        hint=hint[1] if hint is not None else None,
        scan_key=entry.entry_id,
    )
    if not port:
        raise BraviaTvConnectionError(f"Could not locate gRPC service on {host}")
    client.host, client.port = host, port
    # Stores the resolved port, so the next start is instant.
    await coordinator.async_start()


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
    _, device_unique_id = parse_instance_name(name)
    if device_unique_id:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_UNIQUE_ID: device_unique_id}
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
    updates: dict[str, Any] = {
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
    # Only this TV's own entry: the account may hold other devices (a
    # soundbar), whose model and firmware must not end up on the TV.
    device = next((d for d in devices if d.get("device_id") == device_id), None)
    return cred.device_hardware_info(device) if device else None


def _remove_other_picture_entities(
    hass: HomeAssistant, entry: ConfigEntry, writable: bool
) -> None:
    """Picture settings are sensors without the IP-control key and controls
    with it; drop the other kind from the registry so none are orphaned."""
    registry = er.async_get(hass)
    root = entry.unique_id or entry.entry_id
    prefix = f"{root}_agent_picture_"
    stale_domains = {"sensor"} if writable else {"number", "select", "switch"}
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain in stale_domains and reg.unique_id.startswith(prefix):
            registry.async_remove(reg.entity_id)
    # Power saving: a sensor without the key, a select with it.
    ps_domain = "sensor" if writable else "select"
    ps_id = registry.async_get_entity_id(
        ps_domain, DOMAIN, f"{root}_agent_power_saving"
    )
    if ps_id:
        registry.async_remove(ps_id)


def _remove_agent_update_entity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Agent update checks are off: no update entity, so none is orphaned."""
    registry = er.async_get(hass)
    root = entry.unique_id or entry.entry_id
    entity_id = registry.async_get_entity_id("update", DOMAIN, f"{root}_agent_update")
    if entity_id:
        registry.async_remove(entity_id)


def _remove_retired_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop entities of features that were removed, so none are orphaned.

    The Living Decor theme select (removed in 0.4.2): the TV only lists the
    three most recently used themes to other apps, so it could never offer
    the catalogue. The white balance offset numbers (replaced in 0.4.3).
    """
    registry = er.async_get(hass)
    root = entry.unique_id or entry.entry_id
    retired = [("select", f"{root}_agent_living_decor_theme")]
    # The white balance offsets are 20-point lists: they were number (or plain
    # sensor) entities that could never show or set them; since 0.4.3 they are
    # read-only point sensors with their own unique ids.
    for colour in ("red", "green", "blue"):
        for domain in ("number", "sensor"):
            retired.append((domain, f"{root}_agent_picture_{colour}Offset"))
    for domain, unique_id in retired:
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        if entity_id:
            registry.async_remove(entity_id)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when the manual-port option changed; ignore runtime data
    writes (e.g. the firmware-pending flag)."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    new_manual = entry.options.get(CONF_GRPC_PORT) or None
    if coordinator is None or new_manual != coordinator.applied_manual_port:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    # An agent paired for the first time after setup (the token was just
    # stored): set up again so its entities exist.
    if coordinator.agent_expected and not coordinator.agent_entities_created:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    # Agent update checks add or remove the agent's update entity.
    if coordinator.agent_update_checks != coordinator.applied_agent_update_checks:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    # The IP-control key decides between read-only picture sensors and
    # writable controls: a change rebuilds the entities.
    new_rest = rest_options(entry.options)
    if new_rest[0] != coordinator.applied_rest_options[0]:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    # Other options (refresh settings, the agent release manifest URL) take
    # effect in place.
    if new_rest != coordinator.applied_rest_options:
        coordinator.applied_rest_options = new_rest
        await coordinator.async_agent_resync_rest()
    if coordinator.agent is not None:
        await coordinator.async_check_agent_update()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The TV was removed: clear what was kept for it, and drop the dashboard
    display users with the last TV."""
    from .dashboard import async_close_session

    for prefix in _ENTRY_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, f"{prefix}{entry.entry_id}")
    hass.data.get(DATA_ADDRESS_HINTS, {}).pop(entry.entry_id, None)
    store: Store = schema_store(hass, entry.entry_id)
    await store.async_remove()
    await async_close_session(hass, entry.entry_id)
    others = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    if not others:
        from .dashboard import async_remove_display_user

        await async_remove_display_user(hass)
