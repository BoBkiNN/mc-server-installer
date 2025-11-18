from model import Asset, LatestOrStr, FilesCache
import re
from typing import Literal
from dataclasses import dataclass
import api.labrinth as labrinth
from main import AssetProvider, AssetInstaller, AssetsGroup, UpdateStatus, DownloadData, LateInit, Environment
from registry import Registries

class ModrinthAsset(Asset):
    """Downloads asset from modrinth"""
    version: LatestOrStr
    project_id: str
    channel: labrinth.VersionType | None = None
    """If not set, then channel is ignored"""
    version_is_id: bool = False
    """If true, than version is consumed as version id"""
    version_name_pattern: re.Pattern | None = None
    """RegEx for version name"""
    ignore_game_version: bool = False
    type: Literal["modrinth"]

    def create_asset_id(self):
        return self.project_id

    def is_latest(self) -> bool:
        return self.version == "latest"


class ModrinthCache(FilesCache):
    type: str = "modrinth"
    version_id: str
    version_number: str


@dataclass
class ModrinthData(DownloadData):
    version: labrinth.Version
    project: labrinth.Project

    def create_cache(self) -> FilesCache:
        return ModrinthCache(files=self.files, version_id=self.version.id, version_number=self.version.version_number)

class ModrinthProvider(AssetProvider[ModrinthAsset, ModrinthCache, ModrinthData]):
    def get_logger_name(self):
        return "Modrinth"
    
    modrinth: LateInit[labrinth.Modrinth] = LateInit()
    
    def setup(self, assets: AssetInstaller):
        super().setup(assets)
        self.modrinth = labrinth.Modrinth(assets.session)


    def get_version(self, assets: AssetInstaller, project: labrinth.Project, asset: ModrinthAsset):
        self.debug(f"Getting project versions..")
        game_versions = [] if asset.ignore_game_version else [assets.game_version]
        vers = self.modrinth.get_versions(
            asset.project_id, ["spigot", "paper"], game_versions)
        if not vers:
            raise ValueError(
                f"Cannot find versions for project {asset.project_id}")
        name_pattern = asset.version_name_pattern
        filtered: list[labrinth.Version] = []
        self.debug(f"Got {len(vers)} versions from {project.title}")
        for ver in vers:
            # ignoring mc version currently
            if asset.channel and asset.channel != ver.version_type:
                continue
            if asset.version_is_id and asset.version != ver.id:
                continue
            if name_pattern and not name_pattern.search(ver.name):
                continue
            filtered.append(ver)
        if len(filtered) == 0:
            raise ValueError("No valid versions found")
        if asset.version == "latest":
            return filtered[0]
        # at this moment version is not latest and not an version id, so this is version_number
        ver = next(
            (v for v in filtered if v.version_number == asset.version), None)
        if not ver:
            raise ValueError(
                f"Failed to find valid version with number {asset.version} out of {len(filtered)}")
        return ver

    def download(self, assets: AssetInstaller, asset: ModrinthAsset, group: AssetsGroup) -> ModrinthData:
        self.debug(f"Getting project {asset.project_id}")
        project = self.modrinth.get_project(asset.project_id)
        if not project:
            raise ValueError(f"Unknown project {asset.project_id}")
        ver = self.get_version(assets, project, asset)
        self.logger.info(f"Found version {ver.name!r} ({ver.version_number})")

        folder = group.get_folder(asset)

        def download_version(ver: labrinth.Version):
            # TODO use_primary and file_name_pattern properties here to return multiple files
            primary = ver.get_primary()
            if not primary:
                self.logger.warning(
                    f"⚠ No primary file in version '{ver.name}'")
                return None
            out = folder / primary.filename
            self.info(
                f"🌐 Downloading primary file {primary.filename} from version '{ver.name}'")
            self.download_file(self.modrinth.session, str(primary.url), out)
            return [out]

        files = download_version(ver)
        if not files:
            raise ValueError(f"No valid files found in version {ver}")
        self.info(f"✅ Downloaded {len(files)} files from version {ver.name}")
        return ModrinthData(ver, project, files=files)

    def supports_update_checking(self) -> bool:
        return True

    def has_update(self, assets: AssetInstaller, asset: ModrinthAsset, group: AssetsGroup, cached: ModrinthCache) -> UpdateStatus:
        self.debug(f"Getting project {asset.project_id}")
        project = self.modrinth.get_project(asset.project_id)
        if not project:
            raise ValueError(f"Unknown project {asset.project_id}")
        ver = self.get_version(assets, project, asset)
        self.logger.info(f"Found version {ver.name!r} ({ver.version_number})")
        # maybe semver when possible?
        if cached.version_id != ver.id:
            return UpdateStatus.OUTDATED
        return UpdateStatus.UP_TO_DATE

KEY = "modrinth"

def setup(registries: Registries, env: Environment):
    registries.register_to(AssetProvider, KEY, ModrinthProvider())
    registries.register_models_to(Asset, ModrinthAsset)
    registries.register_models_to(FilesCache, ModrinthCache)
