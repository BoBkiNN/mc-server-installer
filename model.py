from pydantic import BaseModel, Field, HttpUrl, ValidationError
from typing import Annotated, Union, Literal
import yaml, json5, json
from pathlib import Path
from modrinth import VersionType
from enum import Enum
import hashlib
from papermc_fill import Channel as PaperChannel


class AssetProvider(BaseModel):
     # TODO fallback providers

    class Config:
        frozen = True
    
    def create_asset_id(self) -> str:
        """Returns asset id without versions. Do not invokes any IO"""
        ...

class ModrinthProvider(AssetProvider):
    """Downloads asset from modrinth"""
    project_id: str
    channel: VersionType | None = None
    """If not set, then channel is ignored"""
    version_is_id: bool = False
    """If true, than version is consumed as version id"""
    version_name_pattern: str | None = None
    """RegEx for version name"""
    ignore_game_version: bool = True
    type: Literal["modrinth"]

    def create_asset_id(self):
        return self.project_id

class GithubReleasesProvider(AssetProvider):
    """Downloads asset from github"""
    repository: str
    type: Literal["github"]

    def create_asset_id(self) -> str:
        return self.repository

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

class DirectUrlProvider(AssetProvider):
    """Downloads asset from specified url"""
    url: HttpUrl
    type: Literal["url"]

    def create_asset_id(self) -> str:
        return str(self.url)

class AssetType(Enum):
    MOD = "mod"
    PLUGIN = "plugin"
    DATAPACK = "datapack"

Provider = Annotated[
    Union[ModrinthProvider, GithubReleasesProvider,
          DirectUrlProvider, GithubActionsProvider],
    Field(discriminator="type"),
]

class AssetManifest(BaseModel):
    provider: Provider
    asset_id: str | None = None
    """Asset id override"""

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
        return v

    def create_asset_id(self) -> str:
        return f"({self.provider.create_asset_id()})@{self.provider.type}"
    
    @property
    def type(self) -> AssetType:
        return getattr(self, "_type")


class ModManifest(AssetManifest):
    version: str
    _type = AssetType.MOD


class PluginManifest(AssetManifest):
    version: str
    _type = AssetType.PLUGIN


class DatapackManifest(AssetManifest):
    _type = AssetType.DATAPACK

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
            return None # we dont know version if manifest is set to latest
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

    class Config:
        frozen = True
    
    def get_asset(self, id: str):
        ls = self.mods + self.plugins + self.datapacks
        for mf in ls:
            if mf.resolve_asset_id() == id:
                return mf
        return None
    
    @staticmethod
    def load(file: Path) -> "Manifest":
        ext = file.name.split(".")[-1]
        with open(file, "r", encoding="utf-8") as f:
            if ext in ["json", "yml", "yaml"]:
                d = yaml.load(f, yaml.FullLoader)
            elif ext in ["json5", "jsonc"]:
                d = json5.load(f, encoding="utf-8")
            else:
                raise ValueError(f"Cannot find loader for manifest extension {ext}")
        try:
            return Manifest.model_validate(d)
        except ValidationError as e:
            raise ValueError("Failed to load manifest") from e

class FilesInstallation(BaseModel):
    update_time: int
    """UNIX epoch in millis"""
    files: list[Path]
    """List of files after downloading and installation (no temporary files)"""

    def check_files(self, folder: Path):
        for file in self.files:
            path = folder / file
            if not path.is_file():
                return False
        return True

class AssetInstallation(FilesInstallation):
    asset_id: str
    asset_hash: str

    def is_valid(self, folder: Path, hash: str | None):
        if hash is None: # asset removed from manifest
            return False
        if self.asset_hash != hash:
            return False
        return self.check_files(folder)
    
    @staticmethod
    def create(asset_id: str, hash: str, update_time: int, files: list[Path]) -> "AssetInstallation":
        return AssetInstallation(asset_id=asset_id, asset_hash=hash, update_time=update_time, files=files)

class CoreInstallation(FilesInstallation):
    version_hash: str # used for latest checking
    type: str

    def display_name(self) -> str:
        return f"{self.type}-({self.version_hash})"

class PaperCoreInstallation(CoreInstallation):
    build_number: int
    type: str = "paper"

    def display_name(self) -> str:
        return f"paper-{self.build_number}"

class Cache(BaseModel):
    assets: dict[str, AssetInstallation] = {}
    core: CoreInstallation | None = None
    mc_version: str = ""

    @staticmethod
    def create(mf: Manifest):
        return Cache(mc_version=mf.mc_version)

    @staticmethod
    def load(file: Path):
        with open(file, "r", encoding="utf-8") as f:
            d = json.load(f)
        try:
            return Cache.model_validate(d)
        except ValidationError as e:
            raise ValueError("Failed to load caches") from e
    
    def save(self, file: Path, debug: bool = False):
        file.write_text(self.model_dump_json(indent=2 if debug else None))
