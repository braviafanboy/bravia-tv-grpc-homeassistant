"""Constants for the Bravia TV (gRPC) integration."""

from __future__ import annotations

DOMAIN = "bravia_tv_grpc"

# Configuration keys
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"
CONF_MODEL_ID = "model_id"
CONF_SW_VERSION = "sw_version"
CONF_GRPC_KEYS = "grpc_keys"
CONF_GRPC_DEVICE_ID = "grpc_device_id"
CONF_GRPC_PORT = "grpc_port"
# The device's stable mDNS unique id (40-hex, survives IP/port changes). Stored
# so a TV that gets a new DHCP lease is matched to its entry and self-heals its
# host, instead of breaking or re-appearing as a new discovery.
CONF_DEVICE_UNIQUE_ID = "device_unique_id"
# Sticky flag: a firmware update has been seen available. Set when
# fw_update.update_exist becomes true; cleared once the device info is
# re-fetched after it clears (i.e. the update was applied).
CONF_FW_PENDING = "fw_update_pending"

# gRPC service identity (verified on the K-65XR8M2). BRAVIA TVs serve this on a
# DYNAMIC port that changes across reboots, so it is discovered at runtime via
# mDNS or a port scan — see grpc_discovery.py.
GRPC_SERVICE = "jp.co.sony.hes.ssh.controldevice.v1.ControlDeviceService"
