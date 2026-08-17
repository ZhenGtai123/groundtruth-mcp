"""`groundtruth.toml` — one file the MCP server and the CI gate both read.

The single most useful thing in here is that the threshold band has exactly
one home. In the codebase this pattern was extracted from, the CI script owned
the band and the agent-facing tool kept its own copy; they drifted, and for a
while the tool cheerfully reported PASS on numbers CI would reject. Config
that two consumers read cannot drift from itself.

TOML because it is in the standard library from 3.11 (`tomllib`), so the
config format costs no dependency, and because `[[lint.rule]]` arrays-of-
tables are the natural shape for a rule list.
"""

from __future__ import annotations

import importlib
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .checks.rules import RuleSet
from .contracts import ToolkitError
from .data.guard import SqlGuard
from .data.postgres_source import PostgresSource
from .data.sqlite_source import SqliteSource
from .stats import Threshold
from .toolkit import Toolkit

CONFIG_FILENAME = "groundtruth.toml"


class ConfigError(ToolkitError):
    """A config problem, phrased as the edit that fixes it."""


def find_config(start: Path | str | None = None) -> Path | None:
    """`groundtruth.toml` here or in any parent directory, or `$GROUNDTRUTH_CONFIG`."""
    override = os.environ.get("GROUNDTRUTH_CONFIG")
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for directory in [current, *current.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


@dataclass
class ProjectConfig:
    path: Path
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.path.parent

    @classmethod
    def load(cls, path: Path | str) -> "ProjectConfig":
        config_path = Path(path).resolve()
        if not config_path.is_file():
            raise ConfigError(f"no config file at {config_path}")
        try:
            with config_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc
        return cls(path=config_path, raw=raw)

    @classmethod
    def discover(cls, start: Path | str | None = None) -> "ProjectConfig":
        found = find_config(start)
        if found is None:
            raise ConfigError(
                f"no {CONFIG_FILENAME} found in this directory or any parent. Create one (see "
                "examples/checkout-flow/groundtruth.toml) or pass --config explicitly."
            )
        return cls.load(found)

    # -- sections -----------------------------------------------------------

    def section(self, name: str) -> Mapping[str, Any]:
        value = self.raw.get(name, {})
        if not isinstance(value, Mapping):
            raise ConfigError(f"[{name}] must be a table in {self.path}")
        return value

    @property
    def project_name(self) -> str:
        return str(self.section("project").get("name") or self.root.name)

    def resolve(self, relative: str) -> Path:
        """Paths in the config are relative to the config file, never to cwd.

        An agent runs tools from wherever it happens to be; a gate that only
        works when invoked from the repo root is a gate that fails at 3am for
        reasons unrelated to the code.
        """
        candidate = Path(relative).expanduser()
        return candidate if candidate.is_absolute() else (self.root / candidate).resolve()

    # -- assembly -----------------------------------------------------------

    def build(self) -> Toolkit:
        """Import the project's toolkit and layer the config on top of it."""
        kit = self._import_toolkit()

        simulate = self.section("simulate")
        if "runs" in simulate:
            kit.default_runs = int(simulate["runs"])
        if "max_runs" in simulate:
            kit.max_runs = int(simulate["max_runs"])
        if "max_steps" in simulate:
            kit.max_steps = int(simulate["max_steps"])
        if "seed_policy" in simulate:
            policy = str(simulate["seed_policy"])
            if policy not in ("offset", "hash"):
                raise ConfigError(
                    f"[simulate].seed_policy must be 'offset' or 'hash', got {policy!r}"
                )
            kit.seed_policy = policy  # type: ignore[assignment]

        output = self.section("output")
        if "max_chars" in output:
            kit.max_output_chars = int(output["max_chars"])

        settings = self.section("settings")
        if settings:
            kit.settings.update(dict(settings))

        rules = self._build_rules()
        if rules is not None:
            kit.use_rules(rules)

        thresholds = self._build_thresholds()
        if thresholds:
            kit.use_thresholds(thresholds)

        source = self._build_data_source()
        if source is not None:
            kit.use_data_source(source)

        return kit

    def _import_toolkit(self) -> Toolkit:
        target = str(self.section("project").get("toolkit", "")).strip()
        if not target:
            raise ConfigError(
                f"[project].toolkit is missing from {self.path}. Set it to `module:attribute` — "
                'e.g. toolkit = "groundtruth_app:kit" for a groundtruth_app.py next to this file.'
            )
        module_name, _, attribute = target.partition(":")
        if not module_name or not attribute:
            raise ConfigError(
                f"[project].toolkit must look like `module:attribute`, got {target!r}"
            )

        # The toolkit module lives in the user's project, not on the installed
        # path; make its directory importable without requiring them to package it.
        root = str(self.root)
        if root not in sys.path:
            sys.path.insert(0, root)

        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ConfigError(
                f"could not import {module_name!r} (from [project].toolkit = {target!r}); "
                f"looked in {root}. {exc}"
            ) from exc

        kit = getattr(module, attribute, None)
        if kit is None:
            raise ConfigError(f"module {module_name!r} has no attribute {attribute!r}")
        if not isinstance(kit, Toolkit):
            raise ConfigError(
                f"{target} is a {type(kit).__name__}, not a Toolkit — "
                "did you point at the function instead of the toolkit object?"
            )
        return kit

    def _build_rules(self) -> RuleSet | None:
        lint = self.section("lint")
        specs: list[Mapping[str, Any]] = list(lint.get("rule", []))

        rules_path = lint.get("rules")
        if rules_path:
            path = self.resolve(str(rules_path))
            if not path.is_file():
                raise ConfigError(f"[lint].rules points at {path}, which does not exist")
            try:
                with path.open("rb") as handle:
                    external = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
            specs = list(external.get("rule", [])) + specs

        if not specs:
            return None
        return RuleSet.from_dicts(specs)

    def _build_thresholds(self) -> list[Threshold]:
        specs = self.raw.get("thresholds", [])
        if not specs:
            return []
        if not isinstance(specs, list):
            raise ConfigError("[[thresholds]] must be an array of tables")
        try:
            return Threshold.from_dicts(specs)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    def _build_data_source(self):
        data = self.section("data")
        if not data:
            return None

        driver = str(data.get("driver", "")).strip().lower()
        guard = SqlGuard.from_config(data)

        url = str(data.get("url", "")).strip()
        env_name = str(data.get("url_env", "")).strip()
        if env_name:
            # The env var wins when set: local dev points at a scratch copy,
            # CI points at a fixture, and neither edits the committed config.
            url = os.environ.get(env_name, url)

        if driver == "sqlite":
            if not url:
                raise ConfigError("[data].driver = 'sqlite' needs a `url` (a path to the .db file)")
            return SqliteSource(self.resolve(url), guard)
        if driver in ("postgres", "postgresql"):
            return PostgresSource(url, guard)
        raise ConfigError(
            f"[data].driver = {driver!r} is not supported. Use 'sqlite' or 'postgres', or "
            "register a custom source with kit.use_data_source() in your toolkit module."
        )
