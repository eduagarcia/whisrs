"""Reading and writing whisrs's config.toml, and the reference of every key.

The file is edited with tomlkit, so comments, order and formatting survive a
save. It is often a symlink into a dotfiles repository: the new text is
written next to the file it points to and renamed over it, so the link stays
a link. Every save keeps the previous text in a backup folder first.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
import re
import tomllib
from dataclasses import dataclass, field

import tomlkit
from tomlkit.items import Table

HOME = pathlib.Path.home()


def config_dir() -> pathlib.Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(HOME / ".config")
    return pathlib.Path(base) / "whisrs"


def config_path() -> pathlib.Path:
    return config_dir() / "config.toml"


def backup_dir() -> pathlib.Path:
    base = os.environ.get("XDG_STATE_HOME") or str(HOME / ".local" / "state")
    return pathlib.Path(base) / "whisrs-ui" / "backups"


# Keys whose values are secrets: shown masked by the UI.
SECRET_KEY = re.compile(r"(^|_)(api_key|key|token|secret|password)$")


def is_secret(key: str) -> bool:
    return bool(SECRET_KEY.search(key))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_text(path: pathlib.Path | None = None) -> str:
    path = path or config_path()
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def load(path: pathlib.Path | None = None) -> dict:
    """The config as plain data (what whisrs reads)."""
    return tomllib.loads(read_text(path))


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """An edit that would leave the file unreadable or meaningless."""


def _toml_value(value):
    """`value` as a tomlkit item that reads well in the file: multi-line
    strings as literal blocks, lists one item per line."""
    if isinstance(value, str) and "\n" in value:
        if "'''" not in value:
            return tomlkit.string(value, literal=True, multiline=True)
        return tomlkit.string(value, multiline=True)
    if isinstance(value, list):
        array = tomlkit.array()
        for item in value:
            array.append(item)
        if len(value) > 3 or any(isinstance(v, str) and len(v) > 20 for v in value):
            array.multiline(True)
        return array
    return value


def _section(doc, names: list[str], create: bool):
    """The table at `names` in `doc` (`["general"]`, `["overlay", "colors"]`),
    created when `create`; `None` when it is missing and not created."""
    table = doc
    for name in names:
        if name not in table:
            if not create:
                return None
            table[name] = tomlkit.table()
        table = table[name]
        if not isinstance(table, (Table, tomlkit.TOMLDocument, dict)):
            raise ConfigError(f"[{'.'.join(names)}] is not a table in config.toml")
    return table


def apply_ops(text: str, ops: list[dict]) -> str:
    """Apply `ops` to the TOML `text` and return the new text.

    Each op is `{"op": "set", "path": [section..., key], "value": v}` or
    `{"op": "unset", "path": [...]}` (the key goes, so its default applies).
    A section left empty by an unset is removed too.
    """
    doc = tomlkit.parse(text)
    for op in ops:
        path = list(op.get("path") or [])
        if len(path) < 1:
            raise ConfigError(f"an edit has no key: {op!r}")
        *sections, key = path
        kind = op.get("op")
        if kind == "set":
            table = _section(doc, sections, create=True)
            table[key] = _toml_value(op.get("value"))
        elif kind == "unset":
            table = _section(doc, sections, create=False)
            if table is not None and key in table:
                del table[key]
                if sections and len(table) == 0:
                    parent = _section(doc, sections[:-1], create=False)
                    if parent is not None:
                        del parent[sections[-1]]
        else:
            raise ConfigError(f"unknown edit {kind!r}")
    new_text = tomlkit.dumps(doc)
    validate_text(new_text)
    return new_text


def validate_text(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"the TOML does not parse: {e}") from e


@dataclass
class Saved:
    path: pathlib.Path
    backup: pathlib.Path | None
    changed: bool


def write_text(text: str, path: pathlib.Path | None = None) -> Saved:
    """Write `text` as the config: a backup of the old text first, then the
    new one next to the real file and renamed over it (a symlink stays one)."""
    validate_text(text)
    path = path or config_path()
    target = path.resolve() if path.exists() else path
    old = read_text(path)
    if old == text:
        return Saved(path=target, backup=None, changed=False)
    backup = None
    if old:
        bdir = backup_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = bdir / f"config-{stamp}.toml"
        backup.write_text(old, encoding="utf-8")
        _keep_newest(bdir, "config-*.toml", 30)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.whisrs-ui.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, os.stat(target).st_mode & 0o777)
    except FileNotFoundError:
        os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return Saved(path=target, backup=backup, changed=True)


def _keep_newest(folder: pathlib.Path, pattern: str, keep: int) -> None:
    files = sorted(folder.glob(pattern))
    for old in files[:-keep]:
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The reference of every key (docs/config-reference.md of the fork)
# ---------------------------------------------------------------------------


@dataclass
class Key:
    name: str
    type: str
    default: str
    values: str
    description: str
    # What the UI makes of `type` and `values`.
    kind: str = "text"  # bool, int, float, choice, list, text, table
    choices: list[str] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    secret: bool = False


@dataclass
class Section:
    name: str  # "general", "overlay.colors", "[agents]" for an array
    array: bool
    guide: str
    keys: list[Key]


def reference_path() -> pathlib.Path:
    """`docs/config-reference.md` of the whisrs checkout this file is in."""
    here = pathlib.Path(__file__).resolve()
    for folder in here.parents:
        candidate = folder / "docs" / "config-reference.md"
        if candidate.is_file():
            return candidate
    return here.parents[3] / "docs" / "config-reference.md"


_CODE = re.compile(r"`([^`]+)`")
_RANGE = re.compile(r"(-?[\d.]+) to (-?[\d.]+)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _cells(line: str) -> list[str]:
    # Split a markdown table row on pipes that are not inside backticks.
    cells, cur, in_code = [], [], False
    for ch in line.strip().strip("|"):
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    cells.append("".join(cur).strip())
    return cells


def _classify(key: Key) -> None:
    t = key.type.lower()
    key.secret = is_secret(key.name)
    if t.startswith("bool"):
        key.kind = "bool"
    elif t.startswith("integer"):
        key.kind = "int"
    elif t.startswith("number") or t.startswith("float"):
        key.kind = "float"
    elif t.startswith("list"):
        key.kind = "list"
    elif t.startswith("table") or t.startswith("map"):
        key.kind = "table"
    else:
        key.kind = "text"
    m = _RANGE.search(key.values)
    if m and key.kind in ("int", "float"):
        key.minimum, key.maximum = float(m.group(1)), float(m.group(2))
    if key.kind == "text":
        # A closed set: every value is in backticks and nothing else is said.
        stripped = _CODE.sub("", key.values)
        stripped = re.sub(r"\((coming soon|[^)]*)\)", "", stripped)
        if _CODE.search(key.values) and not re.search(r"[A-Za-z]", stripped.replace("or", "")):
            key.kind = "choice"
            key.choices = _CODE.findall(key.values)


def load_reference(path: pathlib.Path | None = None) -> list[Section]:
    path = path or reference_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    sections: list[Section] = []
    current: Section | None = None
    for line in lines:
        head = re.match(r"^## \[(\[)?([^\]]+)\]?\]$", line.strip())
        if head:
            current = Section(name=head.group(2), array=bool(head.group(1)), guide="", keys=[])
            sections.append(current)
            continue
        if current is None:
            continue
        if line.startswith("Guide:") or line.startswith("Guides:"):
            current.guide = _LINK.sub(r"\1", line.split(":", 1)[1]).strip()
            continue
        if line.startswith("| `"):
            cells = _cells(line)
            if len(cells) < 5:
                continue
            name = _CODE.sub(r"\1", cells[0])
            key = Key(
                name=name,
                type=cells[1],
                default=_CODE.sub(r"\1", cells[2]),
                values=cells[3],
                description=_LINK.sub(r"\1", cells[4]),
            )
            _classify(key)
            current.keys.append(key)
    return sections


def reference_as_data(path: pathlib.Path | None = None) -> list[dict]:
    out = []
    for s in load_reference(path):
        out.append(
            {
                "name": s.name,
                "array": s.array,
                "guide": s.guide,
                "keys": [k.__dict__ for k in s.keys],
            }
        )
    return out
