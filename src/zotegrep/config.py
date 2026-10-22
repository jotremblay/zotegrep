"""Where ZoteGrep keeps its files, and the configuration file itself.

Everything lives in two places: a JSON configuration file (Zotero libraries,
API keys, sync interval) and a data directory (SQLite index and downloaded
PDFs). Both default to the platform's usual folders; setting ZOTEGREP_HOME,
or passing --home on the command line, puts both under a single directory.
"""
import os
import re
import json
import tempfile
from pathlib import Path
from dataclasses import dataclass, field, asdict

import platformdirs

APP_NAME = "zotegrep"
DEFAULT_SYNC_INTERVAL = 10
MIN_SYNC_INTERVAL = 10
MAX_SYNC_INTERVAL = 7 * 24 * 60

# The ID and the key end up in the URL and headers of Zotero API calls: only
# their legitimate formats are accepted.
LIBRARY_ID_RE = re.compile(r"^[0-9]{1,20}$")
API_KEY_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
LIBRARY_TYPES = ("user", "group")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Paths:
    config_file: Path
    data_dir: Path

    @property
    def db_file(self) -> Path:
        return self.data_dir / "search.db"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    @classmethod
    def from_home(cls, home: str | os.PathLike | None = None) -> "Paths":
        home = home or os.environ.get("ZOTEGREP_HOME")
        if home:
            root = Path(home).expanduser().resolve()
            return cls(root / "config.json", root / "data")
        return cls(
            platformdirs.user_config_path(APP_NAME, appauthor=False) / "config.json",
            platformdirs.user_data_path(APP_NAME, appauthor=False),
        )


paths = Paths.from_home()


def set_home(home: str | os.PathLike | None):
    """Point every module at another home (command line --home, tests)."""
    global paths
    paths = Paths.from_home(home)


@dataclass
class Library:
    id: int                 # local identifier, stable for the index
    type: str               # "user" or "group"
    zotero_id: str          # Zotero user or group ID
    api_key: str
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or f"{self.type} {self.zotero_id}"

    def public(self) -> dict:
        """What can be shown in a browser or printed: everything but the key."""
        return {"id": self.id, "name": self.name, "label": self.label,
                "zotero_library_id": self.zotero_id, "zotero_library_type": self.type}


@dataclass
class Config:
    libraries: list[Library] = field(default_factory=list)
    sync_interval: int = DEFAULT_SYNC_INTERVAL   # minutes; 0 = only on demand

    def library(self, library_id: int) -> Library | None:
        return next((lib for lib in self.libraries if lib.id == library_id), None)

    def add_library(self, type: str, zotero_id: str, api_key: str, name: str = "") -> Library:
        type, zotero_id, api_key = type.strip().lower(), str(zotero_id).strip(), api_key.strip()
        validate_library(type, zotero_id, api_key)
        if any(lib.type == type and lib.zotero_id == zotero_id for lib in self.libraries):
            raise ConfigError("This library is already configured")
        lib = Library(
            id=max((lib.id for lib in self.libraries), default=0) + 1,
            type=type, zotero_id=zotero_id, api_key=api_key, name=name.strip()[:60],
        )
        self.libraries.append(lib)
        return lib

    def update_library(self, library_id: int, name: str | None = None, api_key: str | None = None) -> Library:
        lib = self.library(library_id)
        if not lib:
            raise ConfigError("Library not found")
        if api_key:
            if not API_KEY_RE.match(api_key.strip()):
                raise ConfigError("Invalid API key format")
            lib.api_key = api_key.strip()
        if name is not None:
            lib.name = name.strip()[:60]
        return lib

    def remove_library(self, library_id: int) -> Library:
        lib = self.library(library_id)
        if not lib:
            raise ConfigError("Library not found")
        self.libraries.remove(lib)
        return lib

    def set_sync_interval(self, minutes: int):
        minutes = int(minutes)
        self.sync_interval = 0 if minutes <= 0 else min(max(MIN_SYNC_INTERVAL, minutes), MAX_SYNC_INTERVAL)


def validate_library(type: str, zotero_id: str, api_key: str):
    if type not in LIBRARY_TYPES:
        raise ConfigError("Library type must be 'user' or 'group'")
    if not LIBRARY_ID_RE.match(zotero_id):
        raise ConfigError("Library ID must be a number")
    if not API_KEY_RE.match(api_key):
        raise ConfigError("Invalid API key format")


def load() -> Config:
    try:
        raw = json.loads(paths.config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, ValueError) as e:
        raise ConfigError(f"Cannot read {paths.config_file}: {e}")
    cfg = Config(sync_interval=int(raw.get("sync_interval", DEFAULT_SYNC_INTERVAL)))
    for item in raw.get("libraries", []):
        cfg.libraries.append(Library(
            id=int(item["id"]), type=item["type"], zotero_id=str(item["zotero_id"]),
            api_key=item["api_key"], name=item.get("name", ""),
        ))
    return cfg


def save(cfg: Config):
    """Atomic write, readable by the current user only (it holds API keys)."""
    target = paths.config_file
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"sync_interval": cfg.sync_interval, "libraries": [asdict(lib) for lib in cfg.libraries]}
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
