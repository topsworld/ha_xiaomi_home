# -*- coding: utf-8 -*-
"""Provide info to system health.
Users can find the aggregated system health by going to Settings > Repairs and 
selecting System information in the three dots menu.
"""
from typing import Any
from homeassistant.components.system_health import SystemHealthRegistration
from homeassistant.core import HomeAssistant, callback

from .miot.const import DOMAIN


@callback
def async_register(
    hass: HomeAssistant, register: SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the info page."""
    # config_entry: ExampleConfigEntry = hass.config_entries.async_entries(DOMAIN)[
    #     0]
    # quota_info = await config_entry.runtime_data.async_get_quota_info()

    # return {
    #     "consumed_requests": quota_info.consumed_requests,
    #     "remaining_requests": quota_info.requests_remaining,
    #     # checking the url can take a while, so set the coroutine in the info dict
    #     "can_reach_server": system_health.async_check_can_reach_url(hass, ENDPOINT),
    # }
    return {
        'domain': DOMAIN,
        'hello': 'world',
    }
