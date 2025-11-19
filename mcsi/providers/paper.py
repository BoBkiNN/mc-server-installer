from core import AssetInstaller, UpdateData, UpdateStatus, CoreProvider, Environment
from model import CoreCache, CoreManifest
import providers.api.papermc_fill as papermc
from registry import Registries
from pathlib import Path
from enum import Enum
from typing import Literal
from utils import millis


class PaperLatestBuild(Enum):
    LATEST = "latest"
    LATEST_STABLE = "latest_stable"

    def __str__(self) -> str:
        return self.value


class PaperCoreManifest(CoreManifest):
    type: Literal["paper"]
    build: PaperLatestBuild | int
    channels: list[papermc.Channel] = []
    """Channels to use when finding latest version. Empty means channel will be ignored"""

    def display_name(self) -> str:
        sf = "@"+str(self.channels) if self.channels else ""
        return f"paper/{self.build}"+sf

    def is_latest(self) -> bool:
        return isinstance(self.build, PaperLatestBuild)


class PaperCoreCache(CoreCache):
    build_number: int
    type: Literal["paper"]

    def display_name(self) -> str:
        return f"paper-{self.build_number}"

    def version_name(self) -> str:
        return f"#{self.build_number}"

class PaperCoreProvider(CoreProvider[PaperCoreManifest, PaperCoreCache]):

    def get_paper_build(self, api: papermc.PaperMcFill, core: PaperCoreManifest, mc: str):
        build: papermc.Build | None
        if core.build == PaperLatestBuild.LATEST:
            build = api.get_latest_build("paper", mc)
        elif core.build == PaperLatestBuild.LATEST_STABLE:
            builds = api.get_builds("paper", mc)
            if builds == None:
                build = None
            else:
                build = next((b for b in builds if b.channel ==
                             papermc.Channel.STABLE), None)
        elif core.channels:
            builds = api.get_builds("paper", mc)
            if builds == None:
                build = None
            else:
                build = next(
                    (b for b in builds if b.channel in core.channels), None)
        else:
            build = api.get_build("paper", mc, core.build)
        if build is None:
            raise ValueError(
                f"Failed to find paper build {core.build} for MC {mc}")
        return build

    def download(self, assets: AssetInstaller, core: PaperCoreManifest) -> PaperCoreCache:
        api = papermc.PaperMcFill(assets.session)
        build = self.get_paper_build(api, core, assets.game_version)
        download = build.get_default_download()
        jar_name = core.file_name if core.file_name else download.name
        out = assets.env.folder / jar_name
        assets.download_file(api.session, str(download.url), out)
        hash = core.stable_hash()
        return PaperCoreCache(files=[Path(jar_name)], build_number=build.id,
                              core_hash=hash,
                              update_time=millis(), type="paper")
    
    def has_update(self, assets: AssetInstaller, core: PaperCoreManifest, cached: PaperCoreCache) -> UpdateData:
        api = papermc.PaperMcFill(assets.session)
        build = self.get_paper_build(api, core, assets.game_version)
        cb = cached.build_number
        ab = build.id
        if cb < ab:
            return UpdateStatus.OUTDATED.ver(f"#{ab}")
        elif cb > ab:
            return UpdateStatus.AHEAD.ver(f"#{cb}")
        else:
            return UpdateStatus.UP_TO_DATE.ver(f"#{cb}")


def setup(registries: Registries, env: Environment):
    registries.register_to(CoreProvider, "paper", PaperCoreProvider())
    registries.register_models_to(CoreManifest, PaperCoreManifest)
    registries.register_models_to(CoreCache, PaperCoreCache)