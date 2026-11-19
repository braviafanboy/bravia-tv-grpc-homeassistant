# Development

## Contributing

Issues and pull requests are welcome.

- **Run the checks before opening a pull request.** `ruff check .`,
  `ruff format .`, `mypy` and `pytest tests/` should all pass. CI runs the
  same checks on every push and pull request.
- **Commit with a GitHub no-reply address.** CI rejects a commit whose author
  or committer address is anything else (GitHub: Settings → Emails → *Keep my
  email addresses private*).
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
  - `bravia_tv_client.py`: the gRPC client (handshake, signed calls, the push
    thread). Its callers only ever see its own error types.
  - `grpc_discovery.py`: finding the TV's dynamic port (cached port, mDNS,
    and a rate-limited scan as the last resort).
  - `coordinator.py`: state, the connection's life cycle (connect, go
    offline, reconnect, the stored schema), the Sony cloud and the agent link.
  - `dashboard.py`, `picture_profiles.py`, `ambient.py`, `picture.py`:
    dashboards on the TV, picture profiles, ambient brightness, and the
    classification of the agent's picture settings into entities.
  - `repairs.py`: the repairs a user settles from the Repairs page.
  - `entity.py`, `config_flow.py`, and the platform modules.
- `tests/`: unit tests. `fake_tv.py` is a fake TV for them (the real client
  with the network taken out), serving the schema captured in `schema/`.
- `tools/`: developer tooling that runs outside Home Assistant.
- `schema/`: captured capability and state dumps for reference.
- `blueprints/`: automation blueprints.

## Setup

```sh
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

This tests against the newest Home Assistant that has a test package. CI also
runs the suite against the oldest supported release (`hacs.json`); to do the
same locally, use a second environment and add the pinned test package from
`.github/workflows/validate.yml` to the install. The pure wire-decode tests
run on any Python, with just `pytest` and `cryptography` installed.

Home Assistant installs an integration's requirements under its own package
constraints, so a real install gets the grpcio and protobuf that Home
Assistant version pins, not the newest. CI re-installs them that way before
the tests (the "Use the library versions this Home Assistant installs" step in
the workflow); do the same locally when a result depends on those versions.

A test fails if Home Assistant logs that the integration uses a deprecated or
incorrect API: the "Detected that custom integration 'bravia_tv_grpc' ..."
reports, and the "... was used from bravia_tv_grpc" messages for deprecated
constants, functions and classes. A Python `DeprecationWarning` raised from
the integration's own code fails the test too.

## Tools

- `tools/pair_and_enumerate.py` drives the Sony sign-in and gRPC schema
  enumeration outside Home Assistant. It is the starting point for adding a
  new model.
- `tools/import_smoke.py` imports every module of the integration against
  the installed Home Assistant. CI runs it on the newest Home Assistant
  release, which the test package trails by a few weeks.
- `tools/ha_stub_harness.py` runs the real coordinator and entities against
  a TV without Home Assistant, on stand-ins for the Home Assistant modules the
  integration imports. It covers the local gRPC channel: handshake, schema,
  state seed, push to state, entity mapping, command dispatch.

  ```sh
  python tools/ha_stub_harness.py --host <TV address> --read-only --watch 60
  ```

  - It lists every entity with its value. Unless `--read-only` is given it
    then sets the volume to its current level and selects the current source
    and picture mode again, none of which should change what is on screen.
  - `--watch SECONDS` prints each state change the TV pushes.
  - With `BRAVIA_REST_PSK` set (the pre-shared key of the TV's REST IP
    control) and no `--read-only`, it changes brightness by one step through
    REST, watches the change arrive over gRPC and puts it back. That request
    uses HTTPS on port 443 with the TV's self-signed certificate, unverified.
    Set `BRAVIA_REST_SCHEME=http` for a TV in HTTP mode, where the key crosses
    the network in clear.
  - The gRPC port is found by a scan unless `--port` or `BRAVIA_GRPC_PORT`
    gives it.
  - Credentials come from `schema/.credentials.json`. The TV accepts their
    session keys for a day. `--cloud` lets the harness use Sony's cloud as the
    integration does: it fetches new keys when the TV rejects the stored ones
    and saves them to that file, and the cloud capabilities appear. Without
    it, pair again with `pair_and_enumerate.py`.
  - It never talks to the HA TV Agent on the TV, which is paired with a Home
    Assistant instance.

  Home Assistant names without a written stand-in are made up as inert
  placeholders (`--verbose` lists them), so a new import in the integration
  does not stop the harness. `tests/test_stub_harness.py` runs it against the
  fake TV server in CI.
- `tools/cloud_command_probe.py` sends one Sony cloud command with the
  credentials saved by `pair_and_enumerate.py`, and prints every HTTP exchange
  plus the TV's result. `caps` lists what a TV advertises.

## Regenerating the protobuf stubs

`grpc/bravia_control_pb2*.py` are generated from `grpc/bravia_control.proto`,
in an environment of their own (`pip install -r requirements-proto.txt`: the
pinned generator caps protobuf below what current Home Assistant installs, so
it must not share the test environment):

```sh
python -m grpc_tools.protoc -Igrpc --python_out=grpc --grpc_python_out=grpc \
  grpc/bravia_control.proto
```

Then re-apply two manual patches, also noted in `requirements-proto.txt`:

1. Make the `pb2_grpc` import relative: `from . import bravia_control_pb2`.
2. Register the descriptor in a private pool, not the global `Default()` one
   (`_pool = _descriptor_pool.DescriptorPool()` and
   `DESCRIPTOR = _pool.AddSerializedFile(...)`). Without this, the protobuf
   symbol names collide with a co-installed `bravia_quad` integration, which
   uses the same proto package, and whichever loads second fails to import.

## Releases and the paired agent version

Each integration release is paired with one HA TV Agent release
(`AGENT_VERSION` in `const.py`): the agent's update entity offers exactly that
version, from that release's `agent.json`, never "latest". `AGENT_MIN_VERSION`
is the oldest agent the integration works with; an older agent raises the
`agent_outdated` repair, whose text names what doesn't work.

Pairing changed with agent 0.10.0: someone at the TV allows it, after
comparing a code (the agent's `docs/contract.md` has the protocol). It can no
longer happen behind the user's back, so for such an agent the link raises
the `agent_pairing` repair, and its fix flow (`repairs.py`) asks the agent,
shows the code, opens the agent on the TV and collects the token once the
viewer has allowed the request. Agents before that open a window on launch
and are paired with as before, without the user; the integration tells the
two apart by `pairing.method` in `/v1/info`. An integration from before this
change cannot
pair with an agent from 0.10.0 on. That only reaches someone who installs a
newer agent by hand: the update entity offers the agent version that belongs
to the installed integration, and an existing pairing survives the update.

When an agent release is part of a change:

1. Release the agent first (tag `vX.Y.Z` in bravia-tv-agent; its workflow
   publishes the APK and `agent.json`).
2. Set `AGENT_VERSION` to it. Raise `AGENT_MIN_VERSION` only if the
   integration needs that agent, and update the repair text to say what fails
   without it.
3. Release the integration.

## Testing

Tests live in `tests/` in two tiers:

- **Pure tests** exercise the protocol codecs, the request signing, the
  capability schema reader and the agent and cloud clients' pure parts, with
  no Home Assistant and no network. They run on any Python through a
  synthetic package set up in `conftest.py`.
- **Home Assistant tests** use `pytest-homeassistant-custom-component` and
  cover setup, unload and removal against a fake TV, the offline and
  reconnect paths, the config, options and reauth flows, the coordinator, the
  agent link and its trust rules, diagnostics, the actions, and the entities.
  Three files open sockets on loopback: `test_client_grpc.py` runs the real
  gRPC client against a fake TV server (`tests/fake_tv_server.py`),
  `test_stub_harness.py` runs `tools/ha_stub_harness.py` against the same
  server as a separate process, and `test_agent_tls.py` runs the agent client
  against a TLS server. They are `importorskip`-guarded, so without the
  plugin they skip and the pure tests still run.

```sh
pytest tests/ -q          # run the suite
ruff check .              # lint (matches CI)
ruff format --check .     # formatting (matches CI)
mypy                      # types (matches CI; configured in pyproject.toml)
```

CI (`.github/workflows/validate.yml`) runs, on every push and pull request:

- Ruff, Hassfest (manifest, strings and services validation) and HACS
  validation;
- the tests on the oldest supported and the newest testable Home Assistant,
  each with the grpcio and protobuf that Home Assistant version installs,
  with a coverage floor, and the type check;
- an import of every module on the newest Home Assistant release;
- a check that every commit uses a GitHub no-reply address.

Hassfest is not part of `pytest`: run it locally before a release with
`docker run --rm --platform linux/amd64 -v "$PWD/custom_components:/github/workspace/custom_components:ro" ghcr.io/home-assistant/hassfest:latest`.
