"""Repairs the user settles from the Repairs page.

Two, and both end in the same pairing:

* The HA TV Agent was found but is not paired. From agent 0.10 on, pairing
  takes someone at the TV: Home Assistant shows a code, the TV shows a code,
  and the viewer allows the request there if the two are the same. That
  cannot be started behind the user's back, so it waits here until they do.
* The agent's port presents a certificate other than the one this TV is
  paired with. That is what a reinstalled agent looks like, and also what an
  impostor on the network looks like, so the integration does not pair again
  on its own. Confirming here says "that was me", and the pairing that follows
  proves it: the code is tied to the certificate.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from .const import DOMAIN
from .grpc import agent as agentmod

AGENT_IDENTITY_PREFIX = "agent_identity_changed_"
AGENT_PAIRING_PREFIX = "agent_pairing_"

# After "Submit" on the code step, how long to wait for the viewer's answer
# before saying it has not come yet.
_ANSWER_WAIT = 4


class AgentPairingRepairFlow(RepairsFlow):
    """Pair with the agent: show the code, wait for the viewer to allow it.

    ``identity`` makes it the flow for a changed certificate, which first
    asks the user to confirm that the agent was reinstalled.
    """

    def __init__(self, data: dict[str, Any], *, identity: bool) -> None:
        self._entry_id = str(data.get("entry_id") or "")
        self._identity = identity
        self._placeholders = {
            "name": str(data.get("name") or "the TV"),
            "fingerprint": str(data.get("fingerprint") or "unknown"),
            "code": "",
        }

    def _coordinator(self) -> Any:
        return self.hass.data.get(DOMAIN, {}).get(self._entry_id)

    # The result type's name differs between Home Assistant versions.
    async def async_step_init(self, user_input: dict[str, str] | None = None) -> Any:
        if self._identity:
            return await self.async_step_confirm()
        return await self.async_step_start()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> Any:
        """A changed certificate: the user says the agent was reinstalled."""
        if user_input is not None:
            coordinator = self._coordinator()
            if coordinator is None:
                return self.async_create_entry(data={})
            await coordinator.async_agent_accept_new_identity()
            if not await coordinator.async_agent_pairs_by_code():
                # An agent from before 0.10 (or one that is not answering
                # now): the link pairs with it on its own, as it always did.
                return self.async_create_entry(data={})
            return await self._begin()
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders,
        )

    async def async_step_start(self, user_input: dict[str, str] | None = None) -> Any:
        """Not paired yet: explain what is about to happen on the TV."""
        if user_input is not None:
            return await self._begin()
        return self.async_show_form(
            step_id="start",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders,
        )

    async def _begin(self) -> Any:
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="agent_unreachable")
        try:
            code = await coordinator.async_agent_pair_start()
        except agentmod.AgentPairingBusy as err:
            return self.async_abort(
                reason="pairing_busy",
                description_placeholders={
                    **self._placeholders,
                    "seconds": str(err.retry_after),
                },
            )
        except (agentmod.AgentError, HomeAssistantError):
            return self.async_abort(
                reason="agent_unreachable", description_placeholders=self._placeholders
            )
        if not code:
            return self.async_create_entry(data={})  # paired without a code
        self._placeholders["code"] = f"{code[:3]} {code[3:]}"
        return await self.async_step_compare()

    async def async_step_compare(self, user_input: dict[str, str] | None = None) -> Any:
        """The code is on both screens; the viewer answers on the TV."""
        errors: dict[str, str] = {}
        if user_input is not None:
            coordinator = self._coordinator()
            outcome = (
                "failed"
                if coordinator is None
                else await coordinator.async_agent_pair_outcome(_ANSWER_WAIT)
            )
            if outcome == "paired":
                return self.async_create_entry(data={})
            if outcome is not None:
                return self.async_abort(
                    reason=f"pairing_{outcome}",
                    description_placeholders=self._placeholders,
                )
            errors["base"] = "not_allowed_yet"
        return self.async_show_form(
            step_id="compare",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders,
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the flow for a fixable issue of this integration."""
    if issue_id.startswith(AGENT_IDENTITY_PREFIX):
        return AgentPairingRepairFlow(
            data or {"entry_id": issue_id.removeprefix(AGENT_IDENTITY_PREFIX)},
            identity=True,
        )
    if issue_id.startswith(AGENT_PAIRING_PREFIX):
        return AgentPairingRepairFlow(
            data or {"entry_id": issue_id.removeprefix(AGENT_PAIRING_PREFIX)},
            identity=False,
        )
    raise ValueError(f"unknown issue {issue_id}")
