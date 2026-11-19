"""Drive the integration's real coordinator and entities against a TV, without
Home Assistant.

    python tools/ha_stub_harness.py --host 192.0.2.10 --read-only

The integration's modules are imported on top of stand-ins for the Home
Assistant modules they use. The ones the run depends on are written out below
(the coordinator and entity base classes, the config entry, storage, timers,
repairs). Any other name the integration imports from Home Assistant is made
up on the spot as an inert placeholder, so a new import in the integration
does not stop the harness from starting. ``tests/test_stub_harness.py`` runs
it against a fake TV, so a change that does break it fails CI.

It covers the integration's own logic on the TV's local gRPC channel: the
handshake, the capability schema, the state seed, push to state, entity
mapping and command dispatch. It does not cover Home Assistant's side (setup,
registries, services); the test suite and a real instance do.

After listing every entity with its value, it:

* sets the volume to its current level and selects the current source and the
  current picture mode again; none of these should change what is on screen.
  ``--read-only`` skips them;
* with ``BRAVIA_REST_PSK`` set (the pre-shared key of the TV's REST IP
  control), changes brightness by one step through REST, watches the change
  arrive over gRPC, and puts it back. ``--read-only`` skips this too;
* with ``--watch SECONDS``, prints every state change the TV pushes, for that
  long or until Ctrl-C.

It never talks to the HA TV Agent app on the TV (that link belongs to the
Home Assistant instance paired with it), and to Sony's cloud only with
``--cloud``.

Credentials are read from ``schema/.credentials.json``, which
``tools/pair_and_enumerate.py`` writes. The TV accepts their session keys for
a day. With ``--cloud`` the harness does what the integration does when the
TV rejects them: it fetches new ones from Sony, and saves them to that file.
Without it, pair again with ``pair_and_enumerate.py``.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Iterable
import contextlib
import copy
from datetime import UTC, datetime, timedelta
import importlib
import importlib.abc
import importlib.machinery
import inspect
import json
import logging
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import types
from typing import Any
from urllib.parse import urlsplit
import urllib.request

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "custom_components" / "bravia_tv_grpc"
CREDENTIALS = REPO / "schema" / ".credentials.json"
# The integration is imported under this name: a package whose path is the
# integration's folder, but whose __init__ (Home Assistant's setup code) is
# never run. Not the folder's own name, and not with ``grpc/`` on sys.path,
# which would shadow grpcio.
PACKAGE = "bravia_tv"

_LOGGER = logging.getLogger("stub_harness")


# ---------------------------------------------------------------------------
# Placeholders: what a Home Assistant name becomes when nothing below says
# ---------------------------------------------------------------------------
# Every name made up during the run (listed with --verbose).
MADE_UP: set[str] = set()


class _Name(str):
    """A constant or enum member, such as ``Platform.SENSOR``: a string holding
    the name in lower case, which is what nearly all of Home Assistant's enums
    are. Members combine with ``|`` like its feature flags."""

    __slots__ = ()

    def __new__(cls, name: str) -> _Name:
        return super().__new__(cls, name.lower())

    @property
    def value(self) -> str:
        return str(self)

    def __or__(self, other: Any) -> _Flags:
        return _Flags((self,)) | other

    __ror__ = __or__

    def __and__(self, other: Any) -> _Flags:
        return _Flags((self,)) & other

    __rand__ = __and__


class _Flags(frozenset):
    """A combination of made-up feature flags."""

    def __or__(self, other: Any) -> _Flags:
        return _Flags(frozenset.__or__(self, _as_flags(other)))

    __ror__ = __or__

    def __and__(self, other: Any) -> _Flags:
        return _Flags(frozenset.__and__(self, _as_flags(other)))

    __rand__ = __and__

    def __repr__(self) -> str:
        return " | ".join(sorted(self)) or "none"


def _as_flags(value: Any) -> _Flags:
    if isinstance(value, _Flags):
        return value
    if isinstance(value, _Name):
        return _Flags((value,))
    return _Flags()  # 0, None, an empty made-up value


class _Nothing:
    """Something of Home Assistant's the run does not depend on (a registry, a
    validator, the auth manager): there is nothing in it. Falsy, empty,
    callable and awaitable, and the same all the way down."""

    def __init__(self, what: str) -> None:
        self._what = what

    def __repr__(self) -> str:
        return f"<stand-in for {self._what}>"

    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 0

    def __iter__(self):
        return iter(())

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) == 1 and not kwargs and inspect.isfunction(args[0]):
            return args[0]  # used as a decorator: the function stays as it is
        return _Nothing(f"{self._what}()")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Nothing(f"{self._what}.{name}")

    def __await__(self):
        async def nothing() -> None:
            return None

        return nothing().__await__()


class _MadeUpMeta(type):
    def __getattr__(cls, name: str) -> Any:
        if name.isupper() and not name.startswith("_"):
            return _Name(name)  # an enum member or a class constant
        raise AttributeError(f"{cls.__name__} has no attribute {name!r}")

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls.__name__.endswith("Feature"):
            return _Flags()  # MediaPlayerEntityFeature(0)
        return super().__call__(*args, **kwargs)


class _MadeUp(metaclass=_MadeUpMeta):
    """Base of every class made up for a Home Assistant name."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def __class_getitem__(cls, item: Any) -> type:
        return cls


_ERROR_ENDINGS = ("Error", "Exception", "Failed", "NotReady")


def _make_up(module: str, name: str) -> Any:
    """A placeholder for ``module.name``, by what the name looks like: a
    constant, an exception, an entity base class, some other class, or (lower
    case: a function, an object, a submodule) nothing at all."""
    MADE_UP.add(f"{module}.{name}")
    if name.isupper():
        return _Name(name)
    if name[:1].isupper():
        base: type = _MadeUp
        if name.endswith(_ERROR_ENDINGS):
            base = HomeAssistantError
        elif name.endswith("Entity"):
            base = Entity
        return types.new_class(
            name, (base,), exec_body=lambda ns: ns.update(__module__=module)
        )
    return _Nothing(f"{module}.{name}")


class _Module(types.ModuleType):
    """A stand-in ``homeassistant`` module: what is not set on it is made up."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        value = _make_up(self.__name__, name)
        setattr(self, name, value)
        return value


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves every ``homeassistant`` module, ahead of an installed one."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None):
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            # A package, so that any submodule of it can be imported too.
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        return _Module(spec.name)

    def exec_module(self, module) -> None:
        return None


# ---------------------------------------------------------------------------
# Stand-ins the run depends on
# ---------------------------------------------------------------------------
_strings_cache: dict[str, Any] | None = None


def _translated(section: str, key: str, placeholders: dict[str, Any] | None) -> str:
    """The integration's own text for an exception or a repair."""
    global _strings_cache
    if _strings_cache is None:
        try:
            _strings_cache = json.loads((PKG / "strings.json").read_text())
        except (OSError, ValueError):
            _strings_cache = {}
    entry = _strings_cache.get(section, {}).get(key, {})
    text = entry.get("message") or entry.get("title")
    if not isinstance(text, str):
        return f"{key} {placeholders or ''}".strip()
    for name, value in (placeholders or {}).items():
        text = text.replace("{" + name + "}", str(value))
    return text


class HomeAssistantError(Exception):
    def __init__(
        self,
        *args: Any,
        translation_domain: str | None = None,
        translation_key: str | None = None,
        translation_placeholders: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders

    def __str__(self) -> str:
        if self.args or not self.translation_key:
            return super().__str__()
        return _translated(
            "exceptions", self.translation_key, self.translation_placeholders
        )


class ServiceValidationError(HomeAssistantError):
    pass


class UpdateFailed(HomeAssistantError):
    pass


def callback(func: Callable) -> Callable:
    return func


class Entity(_MadeUp):
    """What the integration's entities inherit from Home Assistant's entity
    classes: each property reads the matching ``_attr_`` value unless the
    entity defines it, and the calls an entity makes on itself do nothing."""

    hass: Any = None
    platform_name: str = ""

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("_"):
            try:
                return object.__getattribute__(self, f"_attr_{name}")
            except AttributeError:
                pass
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")

    @property
    def available(self) -> bool:
        return True

    def async_write_ha_state(self) -> None:
        return None

    def async_on_remove(self, func: Callable) -> None:
        return None


class CoordinatorEntity(Entity):
    def __init__(self, coordinator: Any, context: Any = None) -> None:
        self.coordinator = coordinator
        self.hass = coordinator.hass

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


class DataUpdateCoordinator:
    def __class_getitem__(cls, item: Any) -> type:
        return cls

    def __init__(
        self,
        hass: Any,
        logger: logging.Logger,
        *,
        name: str | None = None,
        config_entry: Any = None,
        **kwargs: Any,
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.config_entry = config_entry
        self.data: Any = None
        self.last_update_success = True
        self._listeners: list[Callable[[], None]] = []

    def async_add_listener(
        self, update_callback: Callable[[], None], context: Any = None
    ) -> Callable[[], None]:
        self._listeners.append(update_callback)

        def remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove

    def async_update_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    def async_set_updated_data(self, data: Any) -> None:
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()

    def async_set_update_error(self, err: Exception) -> None:
        self.last_update_success = False
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        self._listeners.clear()


class Store:
    """Home Assistant's storage helper, in memory for the length of the run."""

    def __class_getitem__(cls, item: Any) -> type:
        return cls

    def __init__(self, hass: Any, version: int, key: str, *args: Any, **kw: Any):
        self._hass = hass
        self.key = key

    async def async_load(self) -> Any:
        return copy.deepcopy(self._hass.storage.get(self.key))

    async def async_save(self, data: Any) -> None:
        self._hass.storage[self.key] = copy.deepcopy(data)

    def async_delay_save(self, data_func: Callable[[], Any], delay: float = 0) -> None:
        self._hass.storage[self.key] = copy.deepcopy(data_func())

    async def async_remove(self) -> None:
        self._hass.storage.pop(self.key, None)


def _fire(hass: Any, action: Callable) -> None:
    result = action(datetime.now(UTC))
    if inspect.isawaitable(result):
        hass.async_create_task(result, name="stub_harness_timer")


def async_call_later(
    hass: Any, delay: float | timedelta, action: Callable
) -> Callable[[], None]:
    seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
    return hass.loop.call_later(seconds, _fire, hass, action).cancel


def async_track_time_interval(
    hass: Any, action: Callable, interval: timedelta, **kwargs: Any
) -> Callable[[], None]:
    seconds = interval.total_seconds()
    handle: list[asyncio.TimerHandle] = []

    def fire() -> None:
        handle[0] = hass.loop.call_later(seconds, fire)
        _fire(hass, action)

    handle.append(hass.loop.call_later(seconds, fire))
    return lambda: handle[0].cancel()


def async_get_clientsession(hass: Any, *args: Any, **kwargs: Any) -> Any:
    return hass.session


def async_create_issue(hass: Any, domain: str, issue_id: str, **details: Any) -> None:
    hass.issues[issue_id] = details


def async_delete_issue(hass: Any, domain: str, issue_id: str) -> None:
    hass.issues.pop(issue_id, None)


def slugify(text: Any, *, separator: str = "_") -> str:
    return re.sub(r"[^a-z0-9]+", separator, str(text or "").lower()).strip(separator)


def utcnow() -> datetime:
    return datetime.now(UTC)


class GuardedSession:
    """What the integration gets where it asks for Home Assistant's shared web
    session. It reaches Sony's cloud, and only when the run was started with
    ``--cloud``. Everything else is refused, above all the HA TV Agent on the
    TV: the integration would pair with it."""

    def __init__(self, hosts: Iterable[str], session: Any = None) -> None:
        self._hosts = frozenset(hosts)
        self._session = session
        # The host of every request that was refused.
        self.refused: list[str] = []

    @property
    def cloud_refused(self) -> bool:
        """The integration wanted Sony's cloud and the run was not given it."""
        return any(host in self._hosts for host in self.refused)

    def _allow(self, url: Any) -> Any:
        import aiohttp

        host = urlsplit(str(url)).hostname or str(url)
        if host not in self._hosts:
            self.refused.append(host)
            raise aiohttp.ClientConnectionError(
                f"the stub harness does not connect to {host}"
            )
        if self._session is None:
            self.refused.append(host)
            raise aiohttp.ClientConnectionError(
                f"the stub harness uses Sony's cloud ({host}) only with --cloud"
            )
        return self._session

    def get(self, url: Any, **kwargs: Any) -> Any:
        return self._allow(url).get(url, **kwargs)

    def post(self, url: Any, **kwargs: Any) -> Any:
        return self._allow(url).post(url, **kwargs)

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        return self._allow(url).request(method, url, **kwargs)


class ConfigEntry:
    def __init__(
        self,
        *,
        data: dict[str, Any],
        title: str,
        unique_id: str | None,
        entry_id: str = "stub-harness",
        options: dict[str, Any] | None = None,
    ) -> None:
        self.data = dict(data)
        self.options = dict(options or {})
        self.title = title
        self.unique_id = unique_id
        self.entry_id = entry_id
        self.state = _Name("LOADED")
        # Called after the integration changed the entry.
        self.on_update: Callable[[ConfigEntry], None] | None = None

    def async_on_unload(self, func: Callable) -> None:
        return None

    def async_create_background_task(
        self, hass: Any, target: Any, name: str | None = None, **kwargs: Any
    ) -> asyncio.Task:
        return hass.async_create_task(target, name)

    async_create_task = async_create_background_task

    def add_update_listener(self, listener: Callable) -> Callable[[], None]:
        return lambda: None

    def async_start_reauth(self, hass: Any, *args: Any, **kwargs: Any) -> None:
        hass.note("the integration asked for a new Sony sign-in")


class _ConfigEntries:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self.entries: list[ConfigEntry] = []

    def async_entries(self, *args: Any, **kwargs: Any) -> list[ConfigEntry]:
        return list(self.entries)

    def async_get_entry(self, entry_id: str) -> ConfigEntry | None:
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    def async_update_entry(self, entry: ConfigEntry, **changes: Any) -> bool:
        changed = False
        for field in ("data", "options", "title", "unique_id"):
            if field in changes and changes[field] != getattr(entry, field):
                value = changes[field]
                setattr(entry, field, dict(value) if isinstance(value, dict) else value)
                changed = True
        if changed and entry.on_update is not None:
            entry.on_update(entry)
        return changed

    def async_schedule_reload(self, entry_id: str) -> None:
        self._hass.note("the integration asked Home Assistant to reload the entry")

    async def async_reload(self, entry_id: str) -> bool:
        self.async_schedule_reload(entry_id)
        return True


class HomeAssistant:
    """As much of Home Assistant's core object as the integration calls."""

    def __init__(self, config_dir: str, session: GuardedSession) -> None:
        self.loop = asyncio.get_running_loop()
        self.data: dict[str, Any] = {}
        self.config = types.SimpleNamespace(
            config_dir=config_dir,
            path=lambda *parts: os.path.join(config_dir, *parts),
        )
        self.config_entries = _ConfigEntries(self)
        self.session = session
        self.storage: dict[str, Any] = {}
        # Repairs the integration raised, by issue id.
        self.issues: dict[str, dict[str, Any]] = {}
        # Things it asked of Home Assistant that a harness cannot do.
        self.notes: list[str] = []
        self._tasks: set[asyncio.Task] = set()

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    async def async_add_executor_job(self, target: Callable, *args: Any) -> Any:
        return await self.loop.run_in_executor(None, target, *args)

    def async_create_task(
        self, target: Any, name: str | None = None, **kwargs: Any
    ) -> asyncio.Task:
        task = self.loop.create_task(target, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    async_create_background_task = async_create_task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("task %s failed", task.get_name(), exc_info=task.exception())

    async def async_stop(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def __getattr__(self, name: str) -> Any:
        # auth, bus, services, states…: nothing there.
        if name.startswith("_"):
            raise AttributeError(name)
        return _Nothing(f"hass.{name}")


def _stand_ins() -> dict[str, dict[str, Any]]:
    return {
        "homeassistant.core": {"HomeAssistant": HomeAssistant, "callback": callback},
        "homeassistant.const": {
            "CONF_HOST": "host",
            "PERCENTAGE": "%",
            "LIGHT_LUX": "lx",
            "SIGNAL_STRENGTH_DECIBELS_MILLIWATT": "dBm",
        },
        "homeassistant.config_entries": {"ConfigEntry": ConfigEntry},
        "homeassistant.exceptions": {
            "HomeAssistantError": HomeAssistantError,
            "ServiceValidationError": ServiceValidationError,
        },
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": DataUpdateCoordinator,
            "CoordinatorEntity": CoordinatorEntity,
            "UpdateFailed": UpdateFailed,
        },
        "homeassistant.helpers.storage": {"Store": Store},
        "homeassistant.helpers.event": {
            "async_call_later": async_call_later,
            "async_track_time_interval": async_track_time_interval,
        },
        "homeassistant.helpers.aiohttp_client": {
            "async_get_clientsession": async_get_clientsession,
        },
        "homeassistant.helpers.issue_registry": {
            "async_create_issue": async_create_issue,
            "async_delete_issue": async_delete_issue,
        },
        "homeassistant.helpers.device_registry": {"DeviceInfo": dict},
        "homeassistant.helpers.entity": {"Entity": Entity},
        "homeassistant.util": {"slugify": slugify},
        "homeassistant.util.dt": {"utcnow": utcnow, "now": utcnow},
    }


def install_stand_ins() -> None:
    """Put the stand-ins where ``import homeassistant…`` finds them, and make
    the integration importable as ``bravia_tv``. Once per process, and never
    in one that has imported the real Home Assistant."""
    if PACKAGE in sys.modules:
        return
    if "homeassistant" in sys.modules:
        raise RuntimeError("Home Assistant is already imported in this process")
    sys.meta_path.insert(0, _Finder())
    for name, members in _stand_ins().items():
        module = importlib.import_module(name)
        for attribute, value in members.items():
            setattr(module, attribute, value)
    MADE_UP.clear()
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PKG)]
    sys.modules[PACKAGE] = package


def platform_modules() -> list[str]:
    """The integration's entity platforms: its modules that set entities up."""
    return sorted(
        path.stem
        for path in PKG.glob("*.py")
        if path.stem != "__init__"
        and "async def async_setup_entry(" in path.read_text()
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def load_credentials(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not data.get("device_id"):
        raise ValueError(f"{path} does not hold Sony credentials")
    return data


def save_credentials(path: Path, keys_json: str) -> None:
    """Write the credentials the integration just refreshed back to the file
    the tools share: all at once, and readable by its owner only."""
    data = json.loads(keys_json)
    if not isinstance(data, dict) or not data.get("hmac_key"):
        raise ValueError("refusing to save credentials without session keys")
    temporary = path.with_name(path.name + ".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as out:
        out.write(json.dumps(data, indent=2))
    os.replace(temporary, path)


def credentials_saver(
    path: Path, creds: dict[str, Any], keys_field: str
) -> Callable[[ConfigEntry], None]:
    """What to do when the integration changes the entry: it stores what Sony
    hands it there (new session keys, a new refresh token), and the file the
    tools share must follow, or it is left holding keys that no longer work."""
    saved = dict(creds)

    def entry_updated(entry: ConfigEntry) -> None:
        nonlocal saved
        keys_json = entry.data.get(keys_field)
        new = json.loads(keys_json) if keys_json else None
        if isinstance(new, dict) and new != saved:
            save_credentials(path, keys_json)
            saved = new
            print(f"credentials from Sony saved to {path}")

    return entry_updated


def _value_of(entity: Any) -> str:
    if entity.platform_name == "media_player":
        level = entity.volume_level
        return (
            f"{entity.state}, volume "
            f"{'unknown' if level is None else round(level * 100)}%, "
            f"muted {entity.is_volume_muted}, source {entity.source!r}"
        )
    if entity.platform_name == "update":
        return f"{entity.installed_version!r} -> {entity.latest_version!r}"
    for name in ("native_value", "current_option", "is_on"):
        try:
            return repr(getattr(entity, name))
        except AttributeError:
            continue
    return "-"


def _key_of(entity: Any, root: str) -> str:
    unique_id = str(getattr(entity, "unique_id", None) or type(entity).__name__)
    return unique_id.removeprefix(f"{root}_")


def list_entities(entities: list[Any], root: str) -> int:
    """Print every entity with its value; the number that could not be read."""
    failed = 0
    width = max((len(_key_of(e, root)) for e in entities), default=0)
    for entity in sorted(entities, key=lambda e: (e.platform_name, _key_of(e, root))):
        try:
            value = _value_of(entity)
            if not entity.available:
                value += "  (unavailable)"
        except Exception as err:  # noqa: BLE001 - one bad entity is a finding
            failed += 1
            value = f"!! {type(err).__name__}: {err}"
        print(f"  {entity.platform_name:13} {_key_of(entity, root):{width}}  {value}")
    return failed


async def write_checks(entities: list[Any]) -> int:
    """Send writes that change nothing on screen; the number that failed."""
    failed = 0

    async def check(what: str, call: Any) -> None:
        nonlocal failed
        try:
            await call
        except Exception as err:  # noqa: BLE001 - reported, not fatal
            failed += 1
            print(f"  {what}: FAILED ({type(err).__name__}: {err})")
        else:
            print(f"  {what}: accepted")

    player = next((e for e in entities if e.platform_name == "media_player"), None)
    if player is not None:
        print(f"media player features: {player.supported_features!r}")
        level = player.volume_level
        if level is not None:
            await check(
                f"volume set to its current level ({round(level * 100)}%)",
                player.async_set_volume_level(level),
            )
        source, sources = player.source, player.source_list or []
        shown = ", ".join(sources[:8]) + (" …" if len(sources) > 8 else "")
        print(f"  sources ({len(sources)}): {shown or 'none'}")
        if source in sources:
            await check(
                f"current source {source!r} selected again",
                player.async_select_source(source),
            )
    mode = next(
        (
            e
            for e in entities
            if e.platform_name == "select"
            and getattr(e, "_grpc_path", None) == "display_setting.picture_mode"
        ),
        None,
    )
    if mode is not None and mode.current_option:
        await check(
            f"current picture mode {mode.current_option!r} "
            f"(of {len(mode.options)}) selected again",
            mode.async_select_option(mode.current_option),
        )
    return failed


async def push_check(hass: HomeAssistant, entities: list[Any], host: str) -> int:
    """Change brightness through the TV's REST API and watch it arrive over
    gRPC, then put it back. 1 if the change did not arrive."""
    psk = os.environ.get("BRAVIA_REST_PSK")
    if not psk:
        print("push check skipped: set BRAVIA_REST_PSK to the TV's IP-control key")
        return 0
    brightness = next(
        (
            e
            for e in entities
            if e.platform_name == "number"
            and getattr(e, "_grpc_path", None) == "display_setting.brightness"
        ),
        None,
    )
    if brightness is None or brightness.native_value is None:
        print("push check skipped: no brightness value")
        return 0
    # HTTPS on port 443 with the TV's own certificate (issued by its "Sony TV
    # Root CA", so not verified here) when IP control is set to it, which
    # closes plain HTTP; BRAVIA_REST_SCHEME=http for a TV in HTTP mode.
    scheme = os.environ.get("BRAVIA_REST_SCHEME", "https")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    def set_brightness(value: int) -> None:
        body = json.dumps(
            {
                "method": "setPictureQualitySettings",
                "params": [
                    {"settings": [{"target": "brightness", "value": str(value)}]}
                ],
                "id": 1,
                "version": "1.0",
            }
        ).encode()
        request = urllib.request.Request(
            f"{scheme}://{host}/sony/video",
            body,
            {"Content-Type": "application/json", "X-Auth-PSK": psk},
        )
        with urllib.request.urlopen(request, timeout=8, context=context) as reply:
            reply.read()

    start = int(brightness.native_value)
    highest = getattr(brightness, "native_max_value", 100.0)
    target = start + 1 if start < highest else start - 1
    print(f"push check: brightness {start} -> {target} through REST")
    await hass.async_add_executor_job(set_brightness, target)
    arrived = False
    for _ in range(20):
        await asyncio.sleep(0.25)
        if brightness.native_value == float(target):
            arrived = True
            break
    print(
        f"  over gRPC: {brightness.native_value} ({'arrived' if arrived else 'DID NOT ARRIVE'})"
    )
    await hass.async_add_executor_job(set_brightness, start)
    await asyncio.sleep(1.0)
    print(f"  put back: {brightness.native_value}")
    return 0 if arrived else 1


async def watch(coordinator: Any, seconds: float) -> None:
    """Print every change of the coordinator's state for a while."""
    before = dict(coordinator.data or {})
    offline = coordinator.offline

    def changed() -> None:
        nonlocal before, offline
        now = dict(coordinator.data or {})
        stamp = datetime.now().strftime("%H:%M:%S")
        if coordinator.offline != offline:
            offline = coordinator.offline
            print(f"  {stamp} the TV {'cannot be reached' if offline else 'is back'}")
        for path in sorted(set(before) | set(now)):
            if before.get(path) != now.get(path):
                print(f"  {stamp} {path}: {before.get(path)!r} -> {now.get(path)!r}")
        sys.stdout.flush()
        before = now

    remove = coordinator.async_add_listener(changed)
    print(f"watching for changes for {seconds:g} s (Ctrl-C ends it)", flush=True)
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        # Ctrl-C: asyncio cancels the run. Stop watching and finish normally.
        print("stopped watching")
    finally:
        remove()


async def run(args: argparse.Namespace) -> int:
    install_stand_ins()
    const = importlib.import_module(f"{PACKAGE}.const")
    client_module = importlib.import_module(f"{PACKAGE}.bravia_tv_client")
    coordinator_module = importlib.import_module(f"{PACKAGE}.coordinator")
    credentials_module = importlib.import_module(f"{PACKAGE}.grpc.credentials")
    discovery = importlib.import_module(f"{PACKAGE}.grpc_discovery")

    try:
        creds = load_credentials(args.credentials)
    except (OSError, ValueError) as err:
        print(f"cannot read the credentials: {err}", file=sys.stderr)
        print("create them with tools/pair_and_enumerate.py", file=sys.stderr)
        return 2

    sony_hosts = {
        urlsplit(value).hostname
        for value in vars(credentials_module).values()
        if isinstance(value, str) and value.startswith("https://")
    }
    web = None
    if args.cloud:
        import aiohttp

        web = aiohttp.ClientSession()

    with tempfile.TemporaryDirectory(prefix="bravia-stub-harness-") as config_dir:
        hass = HomeAssistant(config_dir, GuardedSession(sony_hosts, web))
        try:
            return await _run(
                args, hass, creds, const, client_module, coordinator_module, discovery
            )
        finally:
            await hass.async_stop()
            if web is not None:
                await web.close()


async def _run(
    args: argparse.Namespace,
    hass: HomeAssistant,
    creds: dict[str, Any],
    const: Any,
    client_module: Any,
    coordinator_module: Any,
    discovery: Any,
) -> int:
    port = args.port
    if not port:
        print(
            f"looking for the gRPC port on {args.host} (it changes when the TV restarts)"
        )
        port = await hass.async_add_executor_job(
            discovery.scan_for_grpc_port, args.host
        )
        if not port:
            print(f"no BRAVIA control service found on {args.host}", file=sys.stderr)
            return 1
    print(f"TV at {args.host}:{port}")

    entry = ConfigEntry(
        data={
            "host": args.host,
            const.CONF_GRPC_PORT: port,
            const.CONF_GRPC_DEVICE_ID: creds["device_id"],
            const.CONF_GRPC_KEYS: json.dumps(creds),
        },
        title=f"Bravia TV ({args.host})",
        unique_id=creds["device_id"],
    )
    hass.config_entries.entries.append(entry)
    if args.cloud:
        entry.on_update = credentials_saver(
            args.credentials, creds, const.CONF_GRPC_KEYS
        )

    client = client_module.BraviaTvGrpcClient(
        args.host,
        port,
        creds["device_id"],
        creds.get("hmac_key") or "",
        key_id=creds.get("key_id"),
        session_key=creds.get("session_key"),
    )
    coordinator = coordinator_module.BraviaTvCoordinator(hass, entry, client)
    hass.data.setdefault(const.DOMAIN, {})[entry.entry_id] = coordinator
    try:
        await coordinator.async_start()
    except coordinator_module.BraviaTvReauthRequired as err:
        print(
            f"could not start: Sony refused the stored sign-in ({err})", file=sys.stderr
        )
        print("pair again with tools/pair_and_enumerate.py", file=sys.stderr)
        await coordinator.async_shutdown()
        return 1
    except client_module.BraviaTvError as err:
        print(f"could not start: {err}", file=sys.stderr)
        if hass.session.cloud_refused:
            print(
                "the TV rejected the stored session keys (they last a day): run "
                "again with --cloud to fetch new ones from Sony, or pair again "
                "with tools/pair_and_enumerate.py",
                file=sys.stderr,
            )
        await coordinator.async_shutdown()
        return 1

    failures = 0
    try:
        state = coordinator.data or {}
        print(
            f"started: {len(client.capabilities)} capability paths, "
            f"{len(state)} state values, "
            f"{len(coordinator.cloud_capabilities)} cloud capabilities, "
            f"{len(coordinator.app_list)} apps"
        )

        entities: list[Any] = []
        for name in platform_modules():
            module = importlib.import_module(f"{PACKAGE}.{name}")

            def add(
                new: Iterable[Any], *_: Any, platform: str = name, **__: Any
            ) -> None:
                for entity in new:
                    entity.hass = hass
                    entity.platform_name = platform
                    entities.append(entity)

            await module.async_setup_entry(hass, entry, add)
        print(f"entities: {len(entities)}")
        unreadable = list_entities(entities, entry.unique_id or entry.entry_id)
        if unreadable:
            print(f"{unreadable} entities could not be read")
            failures += unreadable

        if args.read_only:
            print("read-only: nothing written to the TV")
        else:
            print("writes that change nothing on screen:")
            failures += await write_checks(entities)
            failures += await push_check(hass, entities, args.host)

        if args.watch > 0:
            await watch(coordinator, args.watch)

        for issue_id, details in sorted(hass.issues.items()):
            text = _translated(
                "issues",
                str(details.get("translation_key") or issue_id),
                details.get("translation_placeholders"),
            )
            print(f"repair raised: {issue_id}: {text}")
        for text in hass.notes:
            print(f"note: {text}")
        if args.verbose:
            for host in sorted(set(hass.session.refused)):
                print(f"web requests refused: {host}")
            print(f"Home Assistant names made up ({len(MADE_UP)}):")
            for name in sorted(MADE_UP):
                print(f"  {name}")
    finally:
        await coordinator.async_shutdown()
    print("shut down cleanly" if not failures else f"shut down; {failures} failure(s)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the integration's coordinator and entities against a TV, "
        "without Home Assistant.",
        epilog="BRAVIA_REST_PSK (and BRAVIA_REST_SCHEME, default https) enable "
        "the push check through the TV's REST IP control.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("BRAVIA_HOST"),
        help="the TV's address (default: $BRAVIA_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BRAVIA_GRPC_PORT") or 0),
        help="the TV's gRPC port (default: $BRAVIA_GRPC_PORT, else a port scan)",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=CREDENTIALS,
        help="Sony credentials file (default: schema/.credentials.json)",
    )
    parser.add_argument(
        "--read-only", action="store_true", help="write nothing to the TV"
    )
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="use Sony's cloud as the integration does: cloud capabilities, and "
        "new session keys (saved to the credentials file) when the TV rejects "
        "the stored ones",
    )
    parser.add_argument(
        "--watch",
        type=float,
        default=0,
        metavar="SECONDS",
        help="afterwards, print state changes as the TV pushes them",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="the integration's debug log"
    )
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("give the TV's address with --host or $BRAVIA_HOST")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(run(args))
    return 130


if __name__ == "__main__":
    raise SystemExit(main())
