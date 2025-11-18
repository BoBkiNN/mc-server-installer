import hashlib
import json
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Union, TypeAlias
from dataclasses import dataclass

import json5
import yaml
from papermc_fill import Channel as PaperChannel
from pydantic import (BaseModel, Field, HttpUrl, ValidationError,
                      model_validator, RootModel, ConfigDict)
from pydantic_core import core_schema, SchemaValidator
from registry import *
from regunion import RegistryUnion, RegistryKey
import re
import utils
from __version__ import __version__
import logging

class FileSelector(ABC, TypedModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    @abstractmethod
    def find_targets(self, ls: list[str]) -> list[str]:
        ...


class AllFilesSelector(FileSelector):
    type: Literal["all"]

    def find_targets(self, ls: list[str]) -> list[str]:
        return ls


class SimpleJarSelector(FileSelector):
    type: Literal["simple-jar"]

    def find_targets(self, ls: list[str]) -> list[str]:
        return [i for i in ls if i.endswith(".jar") and not i.endswith("-sources.jar") and not i.endswith("-api.jar")]

class RegexFileSelector(FileSelector):
    """Uses RegEx pattern to filter files"""
    type: Literal["pattern"]
    pattern: re.Pattern
    mode: Literal["full"] | Literal["search"] = "search"
    """Regex mode. <br>
    search - part of path must match pattern.<br>
    full - path must fully match pattern"""

    def find_targets(self, ls: list[str]) -> list[str]:
        p = self.pattern
        func = p.fullmatch if self.mode == "full" else p.search
        return [f for f in ls if func(f)]


FileSelectorKey: TypeAlias = Annotated[str, RegistryKey(
    "file_selectors"), Field(title="FileSelectorKey")]
FileSelectorUnion: TypeAlias = Annotated[FileSelector, RegistryUnion(
    "file_selectors"), Field(title="FileSelectorUnion")]


class Expr(str):
    """Expression that returns some result (serialized as str)."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler) -> core_schema.CoreSchema:
        return core_schema.str_schema()

    def __repr__(self) -> str:
        return f"Expr({self})"


class TemplateExpr(RootModel):
    root: str
    _parts: list[str | Expr] = []

    def __str__(self) -> str:
        return self.root

    def parts(self, interpret_escapes: bool = True) -> list[Union[str, Expr]]:
        return utils.parse_template_parts(self.root, Expr, interpret_escapes)

# if __name__ == "__main__":
#     while True:
#         i = input("Template: ")
#         p = TemplateExpr(i).parts()
#         print(p)
#         for t in p:
#          print(", ".join([str(ord(c)) for c in t]))


class BaseAction(ABC, TypedModel):
    name: str = ""
    if_: Expr = Field(Expr(""), alias="if")


class DummyAction(BaseAction):
    type: Literal["dummy"]
    expr: Expr


class RenameFile(BaseAction):
    """Renames primary file"""
    type: Literal["rename"]
    to: TemplateExpr


class UnzipFile(BaseAction):
    """Unzips primary file. Supports .zip"""
    type: Literal["unzip"]
    folder: TemplateExpr = TemplateExpr("")
    """Target folder. If not set, then folder where downloaded file is used"""


ActionUnion: TypeAlias = Annotated[BaseAction, RegistryUnion(
    "actions"), Field(title="ActionUnion")]

class Asset(ABC, TypedModel):
    model_config = ConfigDict(use_attribute_docstrings=True)
    file_selector: FileSelectorKey | FileSelectorUnion = "all"
    """Selector used to choose files from multiple"""
    asset_id: str | None = None
    """Asset id override"""
    caching: bool = True
    actions: list[ActionUnion] = []
    """List of actions to execute after download"""
    folder: Path | None = None
    """Used only in customs group""" # TODO replace this with Group(folder: id|Path)
    if_: Expr = Field(Expr(""), alias="if")
    """Conditions for asset to be processed. If falsy value is returned, asset is completely skipped"""

    _asset_id: str | None = None  # cache
    _file_selector: FileSelector | None = None # cache

    def stable_hash(self) -> str:
        s = self.model_dump_json()
        return hashlib.sha256(s.encode()).hexdigest()

    def resolve_asset_id(self) -> str:
        if self._asset_id:
            return self._asset_id
        t = self.get_type()
        if self.asset_id:
            v = self.asset_id
        else:
            v = self.create_asset_id()
        self._asset_id = f"({v})@{t}"
        return self._asset_id

    @abstractmethod
    def create_asset_id(self) -> str:
        """Returns asset id without versions. Do not invokes any IO"""
        ...
    
    def is_latest(self) -> bool | None:
        return None
    
    def get_file_selector(self, registries: Registries):
        if self._file_selector:
            return self._file_selector
        else:
            s = self.create_file_selector(registries)
            self._file_selector = s
            return s

    def create_file_selector(self, registries: Registries) -> FileSelector:
        reg = registries.get_registry(FileSelector)
        if not reg:
            raise ValueError("No file selector registry")
        if isinstance(self.file_selector, str):
            sel = reg.get(self.file_selector)
            if not sel:
                raise ValueError(f"Unknown file selector type {self.file_selector!r}")
            return sel.model_validate({"type": self.file_selector})
        else:
            return self.file_selector
    
    def dump_short(self):
        d = self.model_dump(exclude_defaults=True, exclude={"asset_id", "type"})
        f = [f"{n}={v!r}" for n, v in d.items()]
        t = ", ".join(f)
        id = self.resolve_asset_id()
        return f"{id}[{t}]"


LatestOrStr: TypeAlias = Literal["latest"] | str

class GithubReleasesAsset(Asset):
    """Downloads asset from github"""
    version: LatestOrStr
    repository: str
    type: Literal["github"]
    file_selector: FileSelectorKey | FileSelectorUnion = "simple-jar"

    def create_asset_id(self) -> str:
        return self.repository
    
    def is_latest(self) -> bool:
        return self.version == "latest"


class GithubActionsAsset(Asset):
    """Downloads artifact from github actions"""
    version: int | Literal["latest"]
    repository: str
    branch: str = "master"
    workflow: str
    name_pattern: re.Pattern | None = None
    """RegEx for artifact name. All artifacts is downloaded if not set"""
    type: Literal["github-actions"]
    file_selector: FileSelectorKey | FileSelectorUnion = "simple-jar"

    def create_asset_id(self) -> str:
        return self.repository+"/"+self.workflow+"@"+self.branch
    
    def is_latest(self) -> bool:
        return self.version == "latest"


class JenkinsAsset(Asset):
    version: Literal["latest"] | int
    url: HttpUrl
    """URL to Jenkins instance"""
    job: str
    """Name of job"""
    file_selector: FileSelectorKey | FileSelectorUnion = "simple-jar"
    type: Literal["jenkins"]

    def create_asset_id(self) -> str:
        host = self.url.host or "Unknown"
        return f"{self.job}@{host}"
    
    def is_latest(self) -> bool:
        return self.version == "latest"

class NoteAsset(Asset):
    """Asset that must manually be installed.<br>
    Logs a message after installation containing note"""
    type: Literal["note"]
    note: str

    def create_asset_id(self) -> str:
        hash = hashlib.sha256(self.note.encode("utf-8")).hexdigest()
        return f"note-{hash[:7]}"

class CoreManifest(BaseModel):
    file_name: str | None = None
    """Renames downloaded jar to this file name"""

    def display_name(self) -> str:
        ...

    def hash_from_ver(self, mc_ver: str) -> str | None:
        ...


class PaperLatestBuild(Enum):
    LATEST = "latest"
    LATEST_STABLE = "latest_stable"

    def __str__(self) -> str:
        return self.value


class PaperCoreManifest(CoreManifest):
    type: Literal["paper"]
    build: PaperLatestBuild | int
    channels: list[PaperChannel] = []
    """Channels to use when finding latest version. Empty means channel will be ignored"""

    def hash_from_ver(self, mc_ver: str) -> str | None:
        if not isinstance(self.build, int):
            return None  # we dont know version if manifest is set to latest
        return hashlib.sha256(f"{mc_ver}/{self.build}".encode()).hexdigest()

    def display_name(self) -> str:
        sf = "@"+str(self.channels) if self.channels else ""
        return f"paper/{self.build}"+sf


Core = Annotated[
    Union[PaperCoreManifest],
    Field(discriminator="type"),
]

AssetUnion: TypeAlias = Annotated[Asset, RegistryUnion(
    "assets"), Field(title="Asset")]

class ManifestMeta(BaseModel):
    """Information about this manifest"""
    model_config = ConfigDict(use_attribute_docstrings=True)

    author: str
    """Server author(s)"""
    version: str = "1.0.0"
    """Server version"""
    description: str = ""
    """Server description"""
    link: str = ""
    """Link to server project page or author page"""

@dataclass
class AssetConflict:
    old: tuple[Asset, str]
    new: tuple[Asset, str]

    def __str__(self) -> str:
        od = self.old[0].dump_short()
        nd = self.new[0].dump_short()
        return f"{self.old[1]} {od} and {self.new[1]} {nd}"

class Manifest(BaseModel):
    version: str = __version__
    meta: ManifestMeta
    mc_version: str
    core: Core

    mods: list[AssetUnion] = []
    plugins: list[AssetUnion] = []
    datapacks: list[AssetUnion] = []
    customs: list[AssetUnion] = []

    class Config:
        frozen = True
        use_attribute_docstrings = True

    _assets: dict[str, tuple[Asset, str]] = {}

    def _resolve_asset(self, asset: Asset, group: str) -> AssetConflict | None:
        id = asset.resolve_asset_id()
        old = self._assets.get(id)
        n = (asset, group)
        self._assets[id] = n
        if old:
            return AssetConflict(old, n)
        return None

    def resolve_assets(self):
        conflicts: list[AssetConflict] = []
        for a in self.mods:
            if (c := self._resolve_asset(a, "mod")) is not None:
                conflicts.append(c)
        for a in self.plugins:
            if (c := self._resolve_asset(a, "plugin")) is not None:
                conflicts.append(c)
        for a in self.datapacks:
            if (c := self._resolve_asset(a, "datapack")) is not None:
                conflicts.append(c)
        for a in self.customs:
            if (c := self._resolve_asset(a, "custom")) is not None:
                conflicts.append(c)
        return conflicts

    def get_asset(self, id: str):
        pair = self._assets.get(id, None)
        if not pair:
            return None
        return pair[0]

    @staticmethod
    def load(file: Path, registries: "Registries", logger: logging.Logger) -> "tuple[Manifest, list[AssetConflict]]":
        ext = file.name.split(".")[-1]
        d: dict[str, Any]
        with open(file, "r", encoding="utf-8") as f:
            if ext in ["json", "yml", "yaml"]:
                d = yaml.load(f, yaml.FullLoader)
            elif ext in ["json5", "jsonc"]:
                d = json5.load(f, encoding="utf-8")
            else:
                raise ValueError(
                    f"Cannot find loader for manifest extension {ext}")
        ver = d.get("version", None)
        if ver is not None and ver != __version__:
            logger.warning(f"Manifest was made for different version ({ver}). You might expect errors")
        try:
            m = Manifest.model_validate(d, context={REGISTRIES_CONTEXT_KEY: registries})
        except ValidationError as e:
            raise ValueError("Failed to load manifest") from e
        cls = m.resolve_assets()
        return m, cls


class FilesCache(BaseModel):
    files: list[Path]
    type: str = "files"
    """List of files after downloading and installation (no temporary files)"""

    def check_files(self, folder: Path):
        for file in self.files:
            path = folder / file
            if not path.is_file():
                return False
        return True


class GithubReleaseCache(FilesCache):
    type: str = "github"
    tag: str


class GithubActionsCache(FilesCache):
    type: str = "github-actions"
    run_id: int
    run_number: int


class JenkinsCache(FilesCache):
    type: str = "jenkins"
    build_number: int

@dataclass
class InvalidReason:
    id: str
    reason: str

class AssetValidState(Enum):
    VALID = InvalidReason("valid", "Asset valid") # not displayed
    REMOVED = InvalidReason("removed", "Asset removed from manifest")
    HASH_MISMATCH = InvalidReason("hash_mismatch", "Asset manifest modified")
    MISSING_FILES = InvalidReason("missing_files", "Some files are missing")

    def is_ok(self):
        return self.value.id == AssetValidState.VALID.value.id


class AssetCache(BaseModel):
    asset_id: str
    asset_hash: str
    update_time: int
    data: Annotated[FilesCache, RegistryUnion("asset_cache")]

    def is_valid(self, folder: Path, hash: str | None):
        if hash is None:  # asset removed from manifest
            return AssetValidState.REMOVED
        if self.asset_hash != hash:
            return AssetValidState.HASH_MISMATCH
        if not self.data.check_files(folder):
            return AssetValidState.MISSING_FILES
        else:
            return AssetValidState.VALID

    @staticmethod
    def create(asset_id: str, hash: str, update_time: int, cache: FilesCache) -> "AssetCache":
        return AssetCache(asset_id=asset_id, asset_hash=hash, update_time=update_time, data=cache)


class CoreCache(BaseModel):
    update_time: int
    data: Annotated[FilesCache, RegistryUnion("asset_cache")]
    version_hash: str  # used for latest checking
    type: str

    def display_name(self) -> str:
        if hasattr(self.data, "display_name"):
            return getattr(self.data, "display_name")()
        return f"{self.type}-({self.version_hash})"

class PaperCoreCache(FilesCache):
    build_number: int
    type: str = "core/paper"

    def display_name(self) -> str:
        return f"paper-{self.build_number}"

DEFAULT_PROFILE = "default"

class Cache(BaseModel):
    version: str = __version__
    server_folder: Path
    mc_version: str
    profile: str = DEFAULT_PROFILE
    assets: dict[str, AssetCache] = {}
    core: CoreCache | None = None

    @model_validator(mode="after")
    def make_server_folder_absolute(self) -> "Cache":
        self.server_folder = self.server_folder.resolve()
        return self

    @staticmethod
    def create(mf: Manifest, folder: Path, profile: str):
        return Cache(server_folder=folder, mc_version=mf.mc_version, profile=profile)

    @staticmethod
    def load(file: Path, registries: Registries):
        with open(file, "r", encoding="utf-8") as f:
            d: dict = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("Loaded json is not a dict")
        assets_schema = core_schema.dict_schema(
            keys_schema=core_schema.str_schema(),
            values_schema=core_schema.dict_schema()  # raw dicts for manual processing
        )
        ctx = {REGISTRIES_CONTEXT_KEY: registries}
        v = SchemaValidator(assets_schema)
        raw_assets: dict[str, dict] = v.validate_python(d.get("assets"),
                                                        context=ctx
                                                        )
        assets: dict[str, AssetCache] = {}
        errors: dict[str, ValidationError] = {}
        for k, v in raw_assets.items():
            try:
                a = AssetCache.model_validate(
                    v, context=ctx)
                assets[k] = a
            except ValidationError as e:
                errors[k] = e
        d["assets"] = assets
        try:
            return Cache.model_validate(d, context=ctx), errors
        except ValidationError as e:
            raise ValueError("Failed to load caches") from e

    def save(self, file: Path, registries: Registries, debug: bool = False):
        t = self.model_dump_json(indent=2 if debug else None,
                                 context={REGISTRIES_CONTEXT_KEY: registries})
        file.write_text(t)
