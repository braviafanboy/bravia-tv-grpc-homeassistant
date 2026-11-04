# Development

## Contributing

Issues and pull requests are welcome.

- **Run the checks before opening a pull request.** `ruff check .`,
  `ruff format .` and `pytest tests/` should all pass. CI runs the same
  checks on every push and pull request.
- **State the TV model** you developed and tested against. Capability schemas
  differ between BRAVIA models.
- **Prefer capability-driven code.** Entities are generated from the TV's
  `GetCapabilities` schema. Extend the mapping tables rather than hardcoding
  model-specific lists, so other models keep working.
- **Verify every write against a real TV.** A field that looks settable in
  the schema is not necessarily writable in the current context: the TV often
  returns an empty, success-looking response while ignoring the change.
  Confirm a control with a real change and a read-back, and gate it on its
  `.availability`, `.visibility` and `.unavailable_reason` companions.
- **Leave soundbar control to the
  [bravia-quad-homeassistant](https://github.com/steamEngineer/bravia-quad-homeassistant)
  integration.**

## Project layout

- `custom_components/bravia_tv_grpc/`
  - `grpc/`: the protocol layer. The Sony Seeds auth handshake, protobuf wire
    codecs (`wire.py`, `notify_decode.py`, `get_states_response.py`, …), the
    `GetStates`, `StartNotifyStates` and `GetCapabilities` readers, the
    nonce-gated, AES-GCM app-list and resource readers, and
    `cloud_commands.py`, the Sony cloud command client, and `agent.py`, the
    HA TV Agent client (pairing, pinned TLS, event stream). The wire-decode
    and cloud modules are pure (no Home Assistant, no grpcio). The agent's
    contract is in the agent repository's
    [`docs/contract.md`](https://github.com/braviafanboy/bravia-tv-agent/blob/main/docs/contract.md).
  - `picture.py`: classification of the agent's picture settings into
    entities.
  - `coordinator.py`, `entity.py`, `config_flow.py`, and the platform modules.
- `tests/`: unit tests.
- `tools/`: developer tooling that runs outside Home Assistant.
- `schema/`: captured capability and state dumps for reference.
- `blueprints/`: automation blueprints.

## Setup

```sh
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

Home Assistant, pulled in by `pytest-homeassistant-custom-component`, does not
build on Python 3.14 yet, so use Python 3.13 for the full test suite. The pure
wire-decode tests run on any Python.

## Tools

- `tools/pair_and_enumerate.py` drives the Sony sign-in and gRPC schema
  enumeration outside Home Assistant. It is the starting point for adding a
  new model.
- `tools/ha_stub_harness.py` exercises the real coordinator and entities
  against a live TV with lightweight Home Assistant stand-ins, without a
  running Home Assistant. Set `BRAVIA_HOST` to the TV's address. Its optional
  push test also needs `BRAVIA_REST_PSK`, the pre-shared key of the TV's REST
  IP control. It changes brightness through REST and watches the change
  arrive over gRPC. The harness uses HTTPS on port 443 with the TV's
  self-signed certificate, unverified. Set `BRAVIA_REST_SCHEME=http` for a TV
  in HTTP mode, where the key crosses the network in clear. The test is
  skipped when the key is unset.
- `tools/cloud_command_probe.py` sends one Sony cloud command with the
  credentials saved by `pair_and_enumerate.py`, and prints every HTTP exchange
  plus the TV's result. `caps` lists what a TV advertises.

## Regenerating the protobuf stubs

`grpc/bravia_control_pb2*.py` are generated from `grpc/bravia_control.proto`:

```sh
python -m grpc_tools.protoc -Igrpc --python_out=grpc --grpc_python_out=grpc \
  grpc/bravia_control.proto
```

Then re-apply two manual patches, also noted in `requirements-dev.txt`:

1. Make the `pb2_grpc` import relative: `from . import bravia_control_pb2`.
2. Register the descriptor in a private pool, not the global `Default()` one
   (`_pool = _descriptor_pool.DescriptorPool()` and
   `DESCRIPTOR = _pool.AddSerializedFile(...)`). Without this, the protobuf
   symbol names collide with a co-installed `bravia_quad` integration, which
   uses the same proto package, and whichever loads second fails to import.

## Testing

Tests live in `tests/` in two tiers:

- **Pure wire-decode tests** exercise the protocol codecs with no Home
  Assistant and no network. They run on any Python through a synthetic
  package set up in `conftest.py`.
- **Home Assistant tests** use `pytest-homeassistant-custom-component` and
  cover the config and reauth flows, the coordinator, setup recovery, the
  agent client and entities, and picture settings. They are
  `importorskip`-guarded, so without the plugin they skip and the pure tests
  still run.

```sh
pytest tests/ -q          # run the suite
ruff check .              # lint (matches CI)
ruff format --check .     # formatting (matches CI)
```

CI (`.github/workflows/validate.yml`) runs Ruff, Hassfest (manifest
validation), HACS validation and the tests on every push and pull request.
