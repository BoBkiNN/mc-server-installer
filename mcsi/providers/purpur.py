from core import AssetInstaller, UpdateData, UpdateStatus, CoreProvider, Environment
from model import CoreCache, CoreManifest, LatestOrInt
from registry import Registries
from pathlib import Path
from typing import Literal
from utils import millis, FriendlyException, LateInit
from providers.api import papyrus


class PurpurCore(CoreManifest):
    type: Literal["purpur"]
    build: LatestOrInt
    allow_experimental: bool = False
    """If true, builds with experimental type allowed. Ignored if set explicitely"""

    def display_name(self) -> str:
        return f"purpur#{self.build}"

    def is_latest(self) -> bool:
        return self.build == "latest"


class PurpurCoreCache(CoreCache):
    build_number: int
    type: Literal["purpur"]

    def display_name(self) -> str:
        return f"purpur#{self.build_number}"

    def version_name(self) -> str:
        return f"#{self.build_number}"


class PurpurCoreProvider(CoreProvider[PurpurCore, PurpurCoreCache]):
    PURPUR_PROJECT = "purpur"
    api: LateInit[papyrus.Papyrus] = LateInit()

    def setup(self, assets: AssetInstaller):
        super().setup(assets)
        self.api = papyrus.Papyrus(assets.session)

    def get_build(self, core: PurpurCore, mc: str):
        ver = self.api.get_version(self.PURPUR_PROJECT, mc, True)
        if not ver:
            raise FriendlyException(f"Unknown version {mc}")
        builds = ver.builds
        # check again for detailed
        assert isinstance(builds, papyrus.DetailedVersionBuilds)
        if core.is_latest():
            b = builds.latest
            if not b:
                raise ValueError(f"No latest build set for version {mc}")
            if b.is_experimental() and not core.allow_experimental:
                raise FriendlyException(f"Latest build #{b.build} is experimental which is not allowed by manifest")
            return b
        
        build = [build for build in builds.all if build.build == str(core.build)]
        if not build:
            raise FriendlyException(f"Failed to find build #{core.build}")
        return build[0]


    def download(self, assets: AssetInstaller, core: PurpurCore) -> PurpurCoreCache:
        build = self.get_build(core, assets.game_version)
        download = build.get_download_url(self.api)
        def_name = f"{build.get_name()}.jar"
        jar_name = core.file_name if core.file_name else def_name
        out = assets.env.folder / jar_name
        assets.download_file(self.api.session, download, out)
        hash = core.stable_hash()
        return PurpurCoreCache(files=[Path(jar_name)], build_number=int(build.build),
                              core_hash=hash,
                              update_time=millis(), type="purpur")

    def has_update(self, assets: AssetInstaller, core: PurpurCore, cached: PurpurCoreCache) -> UpdateData:
        build = self.get_build(core, assets.game_version)
        cb = cached.build_number
        ab = int(build.build)
        if cb < ab:
            return UpdateStatus.OUTDATED.ver(f"#{ab}")
        elif cb > ab:
            return UpdateStatus.AHEAD.ver(f"#{cb}")
        else:
            return UpdateStatus.UP_TO_DATE.ver(f"#{cb}")


def setup(registries: Registries, env: Environment):
    registries.register_to(CoreProvider, "purpur", PurpurCoreProvider())
    registries.register_models_to(CoreManifest, PurpurCore)
    registries.register_models_to(CoreCache, PurpurCoreCache)
