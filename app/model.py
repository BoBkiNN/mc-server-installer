import hashlib
import json
from abc import ABC
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Union

import json5
import yaml
from modrinth import VersionType
from papermc_fill import Channel as PaperChannel
from pydantic import (BaseModel, Field, HttpUrl, ValidationError,
                      model_validator, RootModel, ConfigDict)
from pydantic_core import core_schema, SchemaValidator
from registry import *
from regunion import RegistryUnion

# TODO put defaults in registry
class FileSelector(ABC):
    def find_targets(self, ls: list[str]) -> list[str]:
        ...


class AllFilesSelector(FileSelector):
    def find_targets(self, ls: list[str]) -> list[str]:
        return ls


class SimpleJarSelector(FileSelector):
    def find_targets(self, ls: list[str]) -> list[str]:
        return [i for i in ls if i.endswith(".jar") and not i.endswith("-sources.jar") and not i.endswith("-api.jar")]


class AssetProvider(BaseModel):
    # TODO fallback providers

    class Config:
        frozen = True
        use_attribute_docstrings = True

    def create_asset_id(self) -> str:
        """Returns asset id without versions. Do not invokes any IO"""
        ...

    def create_file_selector(self) -> FileSelector:
        return AllFilesSelector()


class ModrinthProvider(AssetProvider):
    """Downloads asset from modrinth"""
    project_id: str
    channel: VersionType | None = None
    """If not set, then channel is ignored"""
    version_is_id: bool = False
    """If true, than version is consumed as version id"""
    version_name_pattern: str | None = None
    """RegEx for version name"""
    ignore_game_version: bool = False
    type: Literal["modrinth"]

    def create_asset_id(self):
        return self.project_id


class GithubReleasesProvider(AssetProvider):
    """Downloads asset from github"""
    repository: str
    type: Literal["github"]

    def create_asset_id(self) -> str:
        return self.repository

    def create_file_selector(self) -> FileSelector:
        return SimpleJarSelector()


class GithubActionsProvider(AssetProvider):
    """Downloads artifact from github actions"""
    repository: str
    branch: str = "master"
    workflow: str
    name_pattern: str | None = None
    """RegEx for artifact name. All artifacts is downloaded if not set"""
    type: Literal["github-actions"]

    def create_asset_id(self) -> str:
        return self.repository+"/"+self.workflow+"@"+self.branch

    def create_file_selector(self) -> FileSelector:
        return SimpleJarSelector()


class DirectUrlProvider(AssetProvider):
    """Downloads asset from specified url"""
    url: HttpUrl
    file_name: str | None = None
    type: Literal["url"]

    def create_asset_id(self) -> str:
        return str(self.url)


class AssetType(Enum):
    MOD = "mod"
    PLUGIN = "plugin"
    DATAPACK = "datapack"
    CUSTOM = "custom"


Provider = Annotated[
    Union[ModrinthProvider, GithubReleasesProvider,
          DirectUrlProvider, GithubActionsProvider],
    Field(discriminator="type"),
]


class Expr(str):
    """Expression that returns some result (serialized as str)."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler) -> core_schema.CoreSchema:
        return core_schema.str_schema()

    def __repr__(self) -> str:
        return f"Expr({self})"
    
    


def parse_template_parts(s: str, interpret_escapes: bool = True) -> list[Union[str, Expr]]:
    """
    Split template string `s` into a list of literal strings and Expr parts.
    Expressions are delimited by `${{` ... `}}`.

    Returns: list[str | Expr]
    """
    res: list[Union[str, Expr]] = []
    n = len(s)
    last = 0

    while True:
        pos = s.find("${{", last)
        if pos == -1:
            break
        end = s.find("}}", pos + 3)
        if end == -1:
            # no closing -> stop and treat remainder as literal
            break

        # count backslashes immediately before pos
        bs = 0
        k = pos - 1
        while k >= 0 and s[k] == "\\":
            bs += 1
            k -= 1

        # prefix is everything from `last` up to the first of those backslashes
        prefix = s[last: pos - bs]

        expr_text = s[pos + 3: end]
        token_literal = s[pos: end + 2]  # the whole `${{...}}`

        if not interpret_escapes:
            if prefix:
                res.append(prefix)
            res.append(Expr(expr_text))
            last = end + 2
            continue

        # interpret escapes according to rules
        if bs == 0:
            # normal expression
            if prefix:
                res.append(prefix)
            res.append(Expr(expr_text))
        elif bs == 1:
            # single backslash -> escape token, drop the backslash
            if prefix:
                res.append(prefix)
            res.append(token_literal)
        else:
            # bs >= 2
            kept = bs - 1
            kept_bs = "\\" * kept
            if bs % 2 == 0:
                # even -> keep (bs-1) backslashes, then expression
                if prefix or kept_bs:
                    res.append(prefix + kept_bs)
                res.append(Expr(expr_text))
            else:
                # odd -> keep (bs-1) backslashes, token treated as literal appended together
                res.append(prefix + kept_bs + token_literal)

        last = end + 2

    # append the remainder
    if last < n:
        rest = s[last:]
        if rest:
            res.append(rest)

    return res

class TemplateExpr(RootModel):
    root: str
    _parts: list[str | Expr] = []

    def __str__(self) -> str:
        return self.root

    def parts(self, interpret_escapes: bool = True) -> list[Union[str, Expr]]:
        return parse_template_parts(self.root, interpret_escapes)

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

class AssetManifest(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)
    provider: Provider
    asset_id: str | None = None
    """Asset id override"""
    version: str
    caching: bool = True
    actions: list[Action] = []
    """List of actions to execute after download"""

    _asset_id: str | None = None  # cache

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

    def create_asset_id(self) -> str:
        return f"({self.provider.create_asset_id()})@{self.provider.type}"

    @property
    def type(self) -> AssetType:
        return getattr(self, "_type")

    def get_base_folder(self) -> Path:
        ...
    
    def get_manifest_group(self) -> str:
        ...


class ModManifest(AssetManifest):
    _type = AssetType.MOD

    def get_base_folder(self) -> Path:
        return Path("mods")
    
    def get_manifest_group(self) -> str:
        return "mods"


class PluginManifest(AssetManifest):
    _type = AssetType.PLUGIN

    def get_base_folder(self) -> Path:
        return Path("plugins")
    
    def get_manifest_group(self) -> str:
        return "plugins"


class DatapackManifest(AssetManifest):
    _type = AssetType.DATAPACK

    def get_base_folder(self) -> Path:
        return Path("world") / "datapacks"
    
    def get_manifest_group(self) -> str:
        return "datapacks"


class CustomManifest(AssetManifest):
    _type = AssetType.CUSTOM
    asset_id: str | None = Field(...)  # type: ignore
    folder: Path
    """File name to use. Generated by provider if not set"""
    version: str | None = None  # type: ignore

    def get_base_folder(self) -> Path:
        return self.folder
    
    def get_manifest_group(self) -> str:
        return "customs"


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


class Manifest(BaseModel):
    mc_version: str
    core: Core

    mods: list[ModManifest] = []
    plugins: list[PluginManifest] = []
    datapacks: list[DatapackManifest] = []
    customs: list[CustomManifest] = []

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
    def load(file: Path, registries: "Registries") -> "Manifest":
        ext = file.name.split(".")[-1]
        with open(file, "r", encoding="utf-8") as f:
            if ext in ["json", "yml", "yaml"]:
                d = yaml.load(f, yaml.FullLoader)
            elif ext in ["json5", "jsonc"]:
                d = json5.load(f, encoding="utf-8")
            else:
                raise ValueError(
                    f"Cannot find loader for manifest extension {ext}")
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


class AssetCache(BaseModel):
    asset_id: str
    asset_hash: str
    update_time: int
    data: Annotated[FilesCache, RegistryUnion("asset_cache")]

    def is_valid(self, folder: Path, hash: str | None):
        if hash is None:  # asset removed from manifest
            return False
        if self.asset_hash != hash:
            return False
        return self.data.check_files(folder)

    @staticmethod
    def create(asset_id: str, hash: str, update_time: int, cache: FilesCache) -> "AssetCache":
        return AssetCache(asset_id=asset_id, asset_hash=hash, update_time=update_time, data=cache)


class CoreCache(BaseModel):
    update_time: int
    data: FilesCache
    version_hash: str  # used for latest checking
    type: str

    def display_name(self) -> str:
        return f"{self.type}-({self.version_hash})"


class PaperCoreCache(CoreCache):
    build_number: int
    type: str = "paper"

    def display_name(self) -> str:
        return f"paper-{self.build_number}"


class Cache(BaseModel):
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
