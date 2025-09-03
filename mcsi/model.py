import hashlib
import json
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Union, TypeAlias
from dataclasses import dataclass

import json5
import yaml
from modrinth import VersionType
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


class BaseAction(BaseModel):
    name: str = ""
    if_: Expr = Field("", alias="if")


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


Action = Annotated[
    Union[DummyAction, RenameFile, UnzipFile],
    Field(discriminator="type"),
]

class Asset(ABC, TypedModel):
    model_config = ConfigDict(use_attribute_docstrings=True)
    file_selector: FileSelectorKey | FileSelectorUnion = "all"
    """Selector used to choose files from multiple"""
    asset_id: str | None = None
    """Asset id override"""
    caching: bool = True
    actions: list[Action] = []
    """List of actions to execute after download"""
    folder: Path | None = None
    """Used only in customs group""" # TODO replace this with Group(folder: id|Path)

    _asset_id: str | None = None  # cache
    _file_selector: FileSelector | None = None # cache

    def stable_hash(self) -> str:
        s = self.model_dump_json()
        return hashlib.sha256(s.encode()).hexdigest()

    def resolve_asset_id(self) -> str:
        if self._asset_id:
            return self._asset_id
        if self.asset_id:
            v = self.asset_id
        else:
            v = self.create_asset_id()
        self._asset_id = v
        return v

    @abstractmethod
    def create_asset_id(self) -> str:
        """Returns asset id without versions. Do not invokes any IO"""
        ...
    
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


LatestOrStr: TypeAlias = Literal["latest"] | str


class ModrinthAsset(Asset):
    """Downloads asset from modrinth"""
    version: LatestOrStr
    project_id: str
    channel: VersionType | None = None
    """If not set, then channel is ignored"""
    version_is_id: bool = False
    """If true, than version is consumed as version id"""
    version_name_pattern: re.Pattern | None = None
    """RegEx for version name"""
    ignore_game_version: bool = False
    type: Literal["modrinth"]

    def create_asset_id(self):
        return self.project_id


class GithubReleasesAsset(Asset):
    """Downloads asset from github"""
    version: LatestOrStr
    repository: str
    type: Literal["github"]
    file_selector: FileSelectorKey | FileSelectorUnion = "simple-jar"

    def create_asset_id(self) -> str:
        return self.repository


class GithubActionsAsset(Asset):
    """Downloads artifact from github actions"""
    version: LatestOrStr
    repository: str
    branch: str = "master"
    workflow: str
    name_pattern: re.Pattern | None = None
    """RegEx for artifact name. All artifacts is downloaded if not set"""
    type: Literal["github-actions"]
    file_selector: FileSelectorKey | FileSelectorUnion = "simple-jar"

    def create_asset_id(self) -> str:
        return self.repository+"/"+self.workflow+"@"+self.branch


class DirectUrlAsset(Asset):
    """Downloads asset from specified url"""
    url: HttpUrl
    file_name: str | None = None
    type: Literal["url"]

    def create_asset_id(self) -> str:
        return str(self.url)

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
    "providers"), Field(title="Provider")]

# ProviderUnion: TypeAlias = Annotated[Union[ModrinthProvider, GithubReleasesProvider], Field(title="Provider", discriminator="type")]

class Manifest(BaseModel):
    version: str = __version__
    mc_version: str
    core: Core

    mods: list[AssetUnion] = []
    plugins: list[AssetUnion] = []
    datapacks: list[AssetUnion] = []
    customs: list[AssetUnion] = []

    class Config:
        frozen = True
        use_attribute_docstrings = True

    def get_asset(self, id: str):
        ls = self.mods + self.plugins + self.datapacks + self.customs
        for mf in ls:
            if mf.resolve_asset_id() == id:
                return mf
        return None

    @staticmethod
    def load(file: Path, registries: "Registries", logger: logging.Logger) -> "Manifest":
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
            return Manifest.model_validate(d, context={REGISTRIES_CONTEXT_KEY: registries})
        except ValidationError as e:
            raise ValueError("Failed to load manifest") from e


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


class ModrinthCache(FilesCache):
    type: str = "modrinth"
    version_id: str
    version_number: str

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
        return f"{self.type}-({self.version_hash})"

class PaperCoreCache(FilesCache):
    build_number: int
    type: str = "core/paper"

    def display_name(self) -> str:
        return f"paper-{self.build_number}"


class Cache(BaseModel):
    version: str = __version__
    server_folder: Path
    mc_version: str
    assets: dict[str, AssetCache] = {}
    core: CoreCache | None = None

    @model_validator(mode="after")
    def make_server_folder_absolute(self) -> "Cache":
        self.server_folder = self.server_folder.resolve()
        return self

    @staticmethod
    def create(mf: Manifest, folder: Path):
        return Cache(server_folder=folder, mc_version=mf.mc_version)

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
