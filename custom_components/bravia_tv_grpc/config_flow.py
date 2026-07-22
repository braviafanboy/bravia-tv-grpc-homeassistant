"""Config flow for the Bravia TV (gRPC) integration.

Pairing paths:
  * user     -- collect the TV host/IP and discover the dynamic gRPC port.
  * zeroconf -- auto-discovery via ``_sonysmarthome._tcp``; host + port arrive
                pre-resolved, so we go straight to the Sony login.
  * oauth    -- present the Sony Seeds login URL; the user logs in with the
                Sony account paired to the TV and pastes the ssh-app://signin
                redirect back. We exchange it for credentials and fetch the
                device's gRPC session keys.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import voluptuous as vol

from .const import (
    CONF_DEVICE_UNIQUE_ID,
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_KEYS,
    CONF_GRPC_PORT,
    DOMAIN,
)
from .grpc.credentials import (
    GrpcNotATvError,
    GrpcOAuthError,
    async_complete_oauth_flow,
    credentials_to_json,
    start_oauth_login,
)
from .grpc_discovery import async_resolve_device_mdns, discover_grpc_port

_LOGGER = logging.getLogger(__name__)

# Sony advertises `<friendly name>-<40-hex device_unique_id>._sonysmarthome…`.
_DEVICE_UID_RE = re.compile(r"-([0-9a-f]{40})", re.IGNORECASE)


def _parse_mdns_name(name: str) -> tuple[str | None, str | None]:
    """Return (friendly_name, device_unique_id) from an mDNS instance name."""
    label = (name or "").split("._sonysmarthome")[0]
    match = _DEVICE_UID_RE.search(label)
    if match:
        return label[: match.start()].strip(" -") or None, match.group(1).lower()
    return (label or None), None


def _is_soundbar_model(properties: dict[str, Any] | None) -> bool:
    """Whether an mDNS advertisement is a Sony soundbar (BRAVIA Theatre).

    Soundbars share the ``_sonysmarthome._tcp`` service and Sony account with the
    TV but are handled by the bravia_quad integration. Their model (mDNS
    ``imName``) is ``HT-*`` — a prefix no TV uses — so this never suppresses a
    real TV; anything it misses is still caught after sign-in by the cloud
    ``device_type`` check.
    """
    props = {str(k).lower(): v for k, v in (properties or {}).items()}
    return str(props.get("imname") or "").upper().startswith("HT-")


class BraviaTvGrpcConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Bravia TV gRPC config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return BraviaTvGrpcOptionsFlow()

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int | None = None
        self._code_verifier: str | None = None
        self._state: str | None = None
        self._auth_url: str | None = None
        self._reauth_entry: ConfigEntry | None = None
        self._device_unique_id: str | None = None

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Re-authenticate an existing entry after the Sony keys stop working.

        Triggered when the automatic key refresh fails (e.g. the refresh token
        was revoked), so the user re-logs in without deleting the integration.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is not None:
            self._host = self._reauth_entry.data.get(CONF_HOST)
            self._port = self._reauth_entry.data.get(CONF_GRPC_PORT)
        return await self.async_step_oauth()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a TV auto-discovered via mDNS (_sonysmarthome._tcp)."""
        host = discovery_info.host
        # Don't even offer a soundbar (handled by bravia_quad) — it shares the
        # service + account. The cloud device_type check after sign-in is the
        # authoritative backstop for anything this model check can't see.
        if _is_soundbar_model(discovery_info.properties):
            return self.async_abort(reason="not_a_tv")
        # The soundbar advertises the same service on the same account; capture
        # the device's unique id so pairing binds to the discovered device (and
        # rejects a non-TV) instead of guessing.
        friendly, self._device_unique_id = _parse_mdns_name(discovery_info.name)
        # An already-configured TV that reappears at a new address (new DHCP
        # lease) is matched by its stable device_unique_id; refresh the stored
        # host/port in place so a moved TV self-heals instead of breaking or
        # showing up as a brand-new discovery.
        if self._device_unique_id is not None:
            for entry in self._async_current_entries():
                if entry.data.get(CONF_DEVICE_UNIQUE_ID) == self._device_unique_id:
                    self._async_refresh_entry_address(entry, host, discovery_info.port)
                    return self.async_abort(reason="already_configured")
        # Fallback for entries paired before the unique id was recorded.
        for entry in self._async_current_entries():
            if entry.data.get(CONF_HOST) == host:
                return self.async_abort(reason="already_configured")
        self._host = host
        self._port = discovery_info.port
        await self.async_set_unique_id(discovery_info.name)
        self._abort_if_unique_id_configured()
        # Show the real device name so a TV and a soundbar are distinguishable.
        self.context["title_placeholders"] = {"name": friendly or f"Bravia TV ({host})"}
        return await self.async_step_oauth()

    @callback
    def _async_refresh_entry_address(
        self, entry: ConfigEntry, host: str, port: int | None
    ) -> None:
        """Update a moved TV's stored host/port in place and reload it."""
        updates: dict[str, Any] = {}
        if host and entry.data.get(CONF_HOST) != host:
            updates[CONF_HOST] = host
        if port and entry.data.get(CONF_GRPC_PORT) != port:
            updates[CONF_GRPC_PORT] = port
        if updates:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, **updates}
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect host, resolve the device, and discover the gRPC control port."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            # Resolve the entered IP to its exact Sony device via mDNS first: this
            # yields the port AND the device's unique id, so an account with more
            # than one TV pairs the right one and a soundbar is rejected up front.
            # Best-effort: an mDNS/zeroconf failure must degrade to the port scan,
            # never crash the flow.
            try:
                resolved = await async_resolve_device_mdns(self.hass, self._host)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("mDNS device resolution failed", exc_info=True)
                resolved = None
            if resolved is not None:
                port, name, props = resolved
                if _is_soundbar_model(props):
                    return self.async_abort(reason="not_a_tv")
                self._port = port
                _, self._device_unique_id = _parse_mdns_name(name)
            else:
                # mDNS blocked: scan for the port. Without a unique id, device
                # selection falls back to the single TV on the account.
                # The dynamic port has only been seen in the ephemeral range, so
                # scan that first (about half the ports); fall back to full.
                self._port = await self.hass.async_add_executor_job(
                    discover_grpc_port, self._host, (), range(32768, 61000)
                ) or await self.hass.async_add_executor_job(
                    discover_grpc_port, self._host, (), range(1024, 65536)
                )
            if not self._port:
                errors["base"] = "no_grpc_service"
            else:
                return await self.async_step_oauth()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Present the Sony login URL and exchange the redirect."""
        errors: dict[str, str] = {}
        if self._auth_url is None:
            self._auth_url, self._code_verifier, self._state = start_oauth_login()

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            reauth_device_id = (
                self._reauth_entry.data.get(CONF_GRPC_DEVICE_ID)
                if self._reauth_entry is not None
                else None
            )
            try:
                credentials = await async_complete_oauth_flow(
                    session,
                    user_input["redirect_url"],
                    self._code_verifier,
                    expected_state=self._state,
                    device_id=reauth_device_id,
                    device_unique_id=self._device_unique_id,
                )
            except GrpcNotATvError:
                # The discovered device is a soundbar (or other non-TV) — that is
                # the bravia_quad integration's job, not this one.
                return self.async_abort(reason="not_a_tv")
            except GrpcOAuthError:
                errors["base"] = "oauth_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Sony Seeds pairing failed")
                errors["base"] = "pairing_failed"
            else:
                device_id = credentials.get("device_id")
                if self._reauth_entry is not None:
                    # New keys must belong to the same TV we're re-authing.
                    if (
                        self._reauth_entry.unique_id
                        and device_id != self._reauth_entry.unique_id
                    ):
                        return self.async_abort(reason="wrong_account")
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data={
                            **self._reauth_entry.data,
                            CONF_GRPC_DEVICE_ID: device_id,
                            CONF_GRPC_KEYS: credentials_to_json(credentials),
                        },
                    )
                    await self.hass.config_entries.async_reload(
                        self._reauth_entry.entry_id
                    )
                    return self.async_abort(reason="reauth_successful")
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Bravia TV ({self._host})",
                    data={
                        CONF_HOST: self._host,
                        CONF_GRPC_PORT: self._port,
                        CONF_GRPC_DEVICE_ID: device_id,
                        CONF_DEVICE_UNIQUE_ID: self._device_unique_id,
                        CONF_GRPC_KEYS: credentials_to_json(credentials),
                    },
                )

        return self.async_show_form(
            step_id="oauth",
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            description_placeholders={"auth_url": self._auth_url},
            errors=errors,
        )


class BraviaTvGrpcOptionsFlow(OptionsFlow):
    """Options: override the auto-discovered gRPC port.

    The TV's gRPC port is dynamic and normally discovered automatically. This is
    an escape hatch for networks where mDNS and the port scan are blocked and the
    user knows the current port; 0 (the default) restores auto-discovery.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the manual-port option."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_GRPC_PORT: user_input.get(CONF_GRPC_PORT) or 0}
            )
        current = self.config_entry.options.get(CONF_GRPC_PORT, 0)
        schema = vol.Schema(
            {
                vol.Optional(CONF_GRPC_PORT, default=current): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=65535)
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
