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
# Bearer token of the HA TV Agent app paired to this entry (the companion
# app on the TV that reports lux, now-playing, remote battery, …). Absent until
# an agent has been discovered and paired.
CONF_AGENT_TOKEN = "agent_token"
# SHA-256 of the agent's self-signed TLS certificate (DER), learned from the
# handshake during pairing and pinned on every later connection.
CONF_AGENT_FINGERPRINT = "agent_fingerprint"
# The agent's self-signed certificate (DER, base64): the TLS trust anchor the
# client verifies against, so a different certificate fails the handshake.
CONF_AGENT_CERT = "agent_cert"
# Name the agent shows for the paired client.
AGENT_CLIENT_NAME = "Home Assistant"

# Picture settings over the TV's REST IP control, relayed by the agent.
# Write access follows the key: empty = read-only sensors, set = controls.
CONF_REST_PSK = "rest_psk"
CONF_PICTURE_POLL_S = "picture_poll_s"
DEFAULT_PICTURE_POLL_S = 60
MIN_PICTURE_POLL_S = 10
MAX_PICTURE_POLL_S = 3600
CONF_PICTURE_EVENT_REFRESH = "picture_event_refresh"
DEFAULT_PICTURE_EVENT_REFRESH = True

# gRPC service identity (verified on the K-65XR8M2). BRAVIA TVs serve this on a
# DYNAMIC port that changes across reboots, so it is discovered at runtime via
# mDNS or a port scan — see grpc_discovery.py.
GRPC_SERVICE = "jp.co.sony.hes.ssh.controldevice.v1.ControlDeviceService"

# HA TV Agent versions this integration release is paired with. The update
# entity offers exactly AGENT_VERSION (never "latest"), so agent updates arrive
# with integration updates and never run ahead of them. Below AGENT_MIN_VERSION
# a repair explains what does not work. Bump both when releasing a new agent
# that this integration needs (docs/development.md, "Releases").
AGENT_VERSION = "0.5.0"
AGENT_MIN_VERSION = "0.4.2"

# Where the integration reads the paired agent release: the JSON manifest
# {"version", "url", "sha256", "notes"} published with that release. Options
# may point it elsewhere (a private repo, or Home Assistant serving the file).
CONF_AGENT_MANIFEST_URL = "agent_manifest_url"
DEFAULT_AGENT_MANIFEST_URL = (
    "https://github.com/braviafanboy/bravia-tv-agent/releases/download/"
    f"v{AGENT_VERSION}/agent.json"
)
# Whether to check for agent updates at all (no update entity, no requests
# to GitHub when off).
CONF_AGENT_UPDATE_CHECKS = "agent_update_checks"
DEFAULT_AGENT_UPDATE_CHECKS = True
