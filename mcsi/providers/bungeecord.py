from core import AssetInstaller, UpdateData
from providers.jenkins_provider import JenkinsProvider, JenkinsCache, JenkinsAsset, LatestOrInt
from core import *
from utils import millis

class BungeeCordCore(CoreManifest):
    type: Literal["bungeecord"]
    build: LatestOrInt

    def is_latest(self) -> bool:
        return self.build == "latest"
    
    def display_name(self) -> str:
        return f"BungeeCord#{self.build}"

class BungeeCordCache(CoreCache):
    type: Literal["bungeecord"]
    build_number: int

    def display_name(self) -> str:
        return f"BungeeCord-{self.build_number}"

    def version_name(self) -> str:
        return f"#{self.build_number}"


BUNGEECORD_JENKINS_URL = "https://hub.spigotmc.org/jenkins/"
BUNGEECORD_JOB = "BungeeCord"

class BungeeCordGroup(AssetsGroup):
    def get_folder(self, asset: Asset) -> Path:
        return Path()

    def get_manifest_name(self) -> str:
        raise NotImplementedError("BungeeCordGroup is intended for internal use")

    @property
    def unit_name(self) -> str:
        return "BungeeCord"

class BungeeCordFileSelector(FileSelector):
    def find_targets(self, ls: list[str]) -> list[str]:
        return [f for f in ls if f == "BungeeCord.jar"]

class BungeeCordProvider(CoreProvider[BungeeCordCore, BungeeCordCache]):
    jkp: LateInit[JenkinsProvider] = LateInit()
    group = BungeeCordGroup()

    def setup(self, assets: AssetInstaller):
        super().setup(assets)
        self.jkp = JenkinsProvider()
        self.jkp.setup(assets)

    def create_asset(self, build: LatestOrInt):
        return JenkinsAsset.model_validate({
            "url": BUNGEECORD_JENKINS_URL,
            "job": BUNGEECORD_JOB,
            "type": "jenkins",
            "version": build,
            "asset_id": "(BungeeCord)@bungeecord",
            "file_selector": BungeeCordFileSelector()
        })

    def download(self, assets: AssetInstaller, core: BungeeCordCore) -> BungeeCordCache:
        asset = self.create_asset(core.build)
        jd = self.jkp.download(assets, asset, self.group)
        
        if core.file_name:
            old_name = jd.primary
            renamed = jd.primary.with_name(core.file_name)
            if renamed.exists():
                self.logger.warning(f"Found existing file {renamed}, it will be deleted.")
                os.remove(renamed)
            jd.primary.rename(renamed)
            jd.primary = renamed
            self.logger.info(f"Renamed {old_name} to {renamed}")
        hash = core.stable_hash()
        return BungeeCordCache(files=jd.files, 
                               build_number=jd.build.number,
                               core_hash=hash,
                               update_time=millis(),
                               type="bungeecord")
    
    def has_update(self, assets: AssetInstaller, core: BungeeCordCore, cached: BungeeCordCache) -> UpdateData:
        asset = self.create_asset(core.build)
        jc = JenkinsCache(files=cached.files, build_number=cached.build_number)
        return self.jkp.has_update(assets, asset, self.group, jc)

KEY = "bungeecord"

def setup(registries: Registries, env: Environment):
    registries.register_to(CoreProvider, KEY, BungeeCordProvider())
    registries.register_models_to(CoreManifest, BungeeCordCore)
    registries.register_models_to(CoreCache, BungeeCordCache)
