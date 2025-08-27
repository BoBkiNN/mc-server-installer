from model import *
from github import Github, Auth
from github.Artifact import Artifact
from github.Repository import Repository
from github.WorkflowRun import WorkflowRun
from github.GitRelease import GitRelease
from abc import ABC
import click, logging, sys
import requests
import tqdm
from zipfile import ZipFile
import uuid, os
from dataclasses import dataclass
import re
import modrinth
import time


def millis():
    return int(time.time()*1000) 

class CacheStore:
    def __init__(self, file: Path, debug: bool, folder: Path) -> None:
        self.cache = Cache()
        self.logger = logging.getLogger("Cache")
        self.logger.setLevel(logging.DEBUG if debug else logging.INFO)
        self.file = file
        self.dirty = False
        """Specifies if cache was modified"""
        self.debug = debug
        self.folder = folder
    
    def save(self):
        if not self.dirty:
            return
        self.cache.save(self.file, self.debug)
        self.dirty = False
    
    def reset(self):
        self.cache = Cache()
        self.logger.debug("Cache reset.")
    
    def load(self):
        if not self.file.is_file():
            self.reset()
            return
        try:
            self.cache = Cache.load(self.file)
            self.logger.debug(f"Loaded cache with {len(self.cache.assets)} assets")
        except Exception as e:
            self.logger.error("Exception loading stored cache. Resetting", exc_info=e)
            self.reset()
    
    def invalidate_asset(self, asset: str | AssetManifest):
        id = asset.resolve_asset_id() if isinstance(asset, AssetManifest) else asset
        removed = self.cache.assets.pop(id, None)
        if removed:
            for p in removed.files:
                os.remove(self.folder / p)
            self.logger.info(f"💥 Invalidated asset {id}")
            self.dirty = True
    
    def check_asset(self, asset: str | AssetManifest, hash: str | None):
        """Returns True if cache is valid"""
        id = asset.resolve_asset_id() if isinstance(asset, AssetManifest) else asset
        entry = self.cache.assets.get(id, None)
        if not entry:
            return False
        self.logger.debug(f"Checking cached asset {entry.asset_id}({entry.asset_hash}) with actual {hash}")
        if entry.is_valid(self.folder, hash):
            return entry
        else:
            self.invalidate_asset(id)
            return None
    
    def store_asset(self, asset: AssetInstallation):
        self.cache.assets[asset.asset_id] = asset
        self.dirty = True
    
    def check_all_assets(self, manifest: Manifest):
        c = 0
        for asset in list(self.cache.assets):
            mf = manifest.get_asset(asset)
            if not self.check_asset(asset, mf.stable_hash() if mf else None):
                c += 1
        if c:
            self.logger.info(f"⚠  {c} asset(s) were invalidated")
        


class FileSelector(ABC):
    def find_targets(self, ls: list[str]) -> list[str]:
        ...

class SimpleJarSelector(FileSelector):
    def find_targets(self, ls: list[str]) -> list[str]:
        return [i for i in ls if i.endswith(".jar") and not i.endswith("-sources.jar") and not i.endswith("-api.jar")]

class Authorizaition(BaseModel):
    github: str | None = None

@dataclass
class DownloadOptions:
    asset: AssetManifest
    version: str
    folder: Path
    selector: FileSelector

class AssetInstaller:
    def __init__(self, manifest: Manifest, auth: Authorizaition, temp_folder: Path, logger: logging.Logger) -> None:
        self.game_version = manifest.mc_version
        self.auth = auth
        self.temp_folder = temp_folder
        self.logger = logger
        self.github = Github(auth=Auth.Token(auth.github)) if auth.github else Github()
        self.modrinth = modrinth.Modrinth()
        self.temp_files: list[Path] = []
        self.repo_cache: dict[str, Repository] = {}
    
    def info(self, msg: object):
        self.logger.info(msg)

    def debug(self, msg: object):
        self.logger.debug(msg)
    
    def get_repo(self, name: str):
        if name in self.repo_cache:
            return self.repo_cache[name]
        repo = self.github.get_repo(name)
        self.repo_cache[name] = repo
        return repo
    
    def get_temp_file(self):
        self.temp_folder.mkdir(parents=True, exist_ok=True)
        id = uuid.uuid4()
        ret = self.temp_folder / str(id)
        self.temp_files.append(ret)
        return ret
    
    def remove_temp_file(self, path: Path):
        self.temp_files.remove(path)
        if path.exists():
            os.remove(path)
        if len(self.temp_files) == 0:
            if len(os.listdir(self.temp_folder)) == 0:
                os.rmdir(self.temp_folder)
    
    def clear_temp(self):
        for path in list(self.temp_files):
            if path.exists():
                os.remove(path)
            self.temp_files.remove(path)
        if self.temp_folder.exists() and not os.listdir(self.temp_folder):
            os.rmdir(self.temp_folder)
    
    def download_file(self, session: requests.Session, url: str, out_path: Path):
        response = session.get(url, stream=True)
        response.raise_for_status()
        total_size = response.headers["Content-Length"]
        with open(out_path, "wb") as f, tqdm.tqdm(
            total=int(total_size),
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=out_path.name,
        ) as bar:
            for chunk in response.iter_content(chunk_size=4096):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))
        bar.close()
    
    def download_github_file(self, url: str, out_path: Path, is_binary: bool = False):
        session = requests.Session()
        if self.auth.github:
            session.headers["Authorization"] = f"token {self.auth.github}"
        if is_binary:
            session.headers["Accept"] = "application/octet-stream"
        self.download_file(session, url, out_path)

    def download_github_release(self, provider: GithubReleasesProvider, 
                                options: DownloadOptions):
        self.debug(f"Getting repository {provider.repository}")
        repo = self.get_repo(provider.repository)
        release: GitRelease
        version = options.version
        if version == "latest":
            release = repo.get_latest_release()
        else:
            release = repo.get_release(version)
        self.info(f"✅ Found release {release.title}")
        assets = release.get_assets()
        m = {a.name: a for a in assets}
        names = options.selector.find_targets(list(m))
        ret: list[Path] = []
        for k, v in m.items():
            if k not in names: continue
            outPath = options.folder / k
            self.info(f"🌐 Downloading artifact {k} to {outPath}..")
            self.download_github_file(v.url, outPath, True)
            # v.download_asset(str(outPath.resolve())) # type: ignore
            ret.append(outPath)
        self.info(f"✅ Downloaded {len(ret)} assets from release")
        return ret
    
    def download_github_actions(self, provider: GithubActionsProvider, 
                                         options: DownloadOptions):
        repo = self.get_repo(provider.repository)
        workflow = repo.get_workflow(provider.workflow)
        runs = workflow.get_runs(branch=provider.branch)  # type: ignore
        run: WorkflowRun | None
        if options.version == "latest":
            run = runs[0]
        else:
            number = int(options.version)
            run = next((r for r in runs if r.run_number == number), None)
        if run is None:
            raise ValueError("No run found")
        ls = run.get_artifacts()
        artifacts: list[Artifact] = []
        if not provider.name_pattern:
            artifacts = [a for a in ls]
        else:
            pattern = re.compile(provider.name_pattern)
            artifacts = [a for a in ls if pattern.search(a.name)]
        if len(artifacts) == 0:
            self.logger.warning(f"⚠ No artifacts found in run {run.id}")
        ret: list[Path] = []
        for artifact in artifacts:
            tmp = self.get_temp_file()
            self.info(f"🌐 Downloading artifact {artifact.name} to {tmp}..")
            artifact.archive_download_url
            self.download_github_file(artifact.archive_download_url, tmp)
            c = 0
            with ZipFile(tmp) as zf:
                targets = options.selector.find_targets(zf.namelist())
                for name in targets:
                    self.info(f"Extracting {name}")
                    zf.extract(name, path=options.folder)
                    ret.append(options.folder / name)
                    c += 1
            self.info(f"✅ Extracted {c} files from artifact {artifact.name}")
            self.remove_temp_file(tmp)
        return ret
    
    def download_modrinth(self, provider: ModrinthProvider, options: DownloadOptions):
        self.debug(f"Getting project {provider.project_id}")
        project = self.modrinth.get_project(provider.project_id)
        if not project:
            raise ValueError(f"Unknown project {provider.project_id}")
        self.debug(f"Getting project versions..")
        game_versions = [] if provider.ignore_game_version else [self.game_version]
        vers = self.modrinth.get_versions(provider.project_id, ["spigot", "paper"], game_versions)
        if not vers:
            raise ValueError(f"Cannot find versions for project {provider.project_id}")
        name_pattern = re.compile(provider.version_name_pattern) if provider.version_name_pattern else None
        filtered: list[modrinth.Version] = []
        self.debug(f"Got {len(vers)} versions from {project.title}")
        for ver in vers:
            # ignoring mc version currently
            if provider.channel and provider.channel != ver.version_type:
                continue
            if provider.version_is_id and options.version != ver.version_number:
                continue
            if name_pattern and not name_pattern.search(ver.name):
                continue
            filtered.append(ver)
        if len(filtered) == 0:
            raise ValueError("No valid versions found")
        def download_version(ver: modrinth.Version):
            primary = ver.get_primary()
            if not primary:
                self.logger.warning(
                    f"⚠ No primary file in version '{ver.name}'")
                return None
            out = options.folder / primary.filename
            self.info(
                f"🌐 Downloading primary file {primary.filename} from version '{ver.name}'")
            self.download_file(self.modrinth.session, str(primary.url), out)
            return out
        if options.version == "latest":
            ver = filtered[0]
            file = download_version(ver)
            if file:
                self.info(f"✅ Downloaded latest version {ver.name}")
                return [file]
            else:
                raise ValueError(f"Failed to download version {ver.name}. See errors above for details")
        ret: list[Path] = []
        for ver in filtered:
            file = download_version(ver)
            if file:
                ret.append(file)
        if not ret:
            raise ValueError(f"No valid versions found out of {len(filtered)}")
        self.info(f"✅ Downloaded {len(ret)} files")
        return ret
            


class Installer:
    def __init__(self, manifest: Manifest, server_folder: Path, auth: Authorizaition) -> None:
        self.manifest = manifest
        self.folder = server_folder
        self.auth = auth
        self.logger = logging.getLogger("Installer")
        self.cache = CacheStore(self.folder / ".install_cache.json", True, self.folder)
        self.assets = AssetInstaller(self.manifest, auth, self.folder / "tmp", self.logger)
        self.mods_folder = self.folder / "mods"
        self.plugins_folder = self.folder / "plugins"
    
    # TODO get rid of DownloadOptions in favor of AssetManifest
    def install(self, provider: AssetProvider, options: DownloadOptions) -> AssetInstallation:
        asset = options.asset
        asset_id = asset.resolve_asset_id()
        asset_hash = asset.stable_hash()
        cached = self.cache.check_asset(asset_id, asset_hash)
        if cached:
            self.logger.info(f"⏩ Skipping {asset.type.value} {asset_id} as it already installed")
            return cached
        
        self.logger.info(f"🔄 Downloading {asset.type.value} {asset_id}")
        ls: list[Path]
        if isinstance(provider, GithubReleasesProvider):
            ls = self.assets.download_github_release(provider, options)
        elif isinstance(provider, GithubActionsProvider):
            ls = self.assets.download_github_actions(provider, options)
        elif isinstance(provider, ModrinthProvider):
            ls = self.assets.download_modrinth(provider, options)
        else:
            raise ValueError(f"Unsupported provider {type(provider)}")
        
        # TODO post-download steps here
        result = AssetInstallation.create(asset_id, asset_hash, millis(), ls)
        self.cache.store_asset(result)
        return result

    def install_mod(self, mod: ModManifest):
        
        options = DownloadOptions(mod, mod.version, self.mods_folder,
                                  SimpleJarSelector())
        self.install(mod.provider, options)
    
    def install_plugin(self, plugin: PluginManifest):
        options = DownloadOptions(plugin, plugin.version, self.plugins_folder,
                                  SimpleJarSelector())
        self.install(plugin.provider, options)
    
    def install_mods(self):
        mods = self.manifest.mods
        if not mods: return
        self.logger.info(f"🔄 Installing {len(mods)} mod(s)")
        self.mods_folder.mkdir(parents=True, exist_ok=True)
        for mod in mods:
            self.install_mod(mod)
    
    def install_plugins(self):
        plugins = self.manifest.plugins
        if not plugins: return
        self.logger.info(f"🔄 Installing {len(plugins)} plugin(s)")
        self.plugins_folder.mkdir(parents=True, exist_ok=True)
        for plugin in self.manifest.plugins:
            self.install_plugin(plugin)


LOG_FORMATTER = logging.Formatter(
    '[%(asctime)s][%(name)s/%(levelname)s]: %(message)s',
    datefmt='%d.%m.%Y %H:%M:%S'
)

def setup_logging(debug: bool):
    logger = logging.getLogger()  # Root logger
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)

    console_handler.setFormatter(LOG_FORMATTER)

    # Avoid duplicate handlers if script is reloaded
    if not logger.handlers:
        logger.addHandler(console_handler)
    else:
        logger.handlers.clear()
        logger.addHandler(console_handler)

DEFAULT_MANIFEST_PATHS = ["manifest.json", "manifest.yml", "manifest.yaml", "manifest.json5", "manifest.jsonc"]

@click.command()
@click.option(
    "--manifest",
    type=click.Path(path_type=Path),  # ensures Path object
    default=None,
    help="Path to the manifest file",
)
@click.option(
    "--github-token",
    type=str,
    default=None,
    help="GitHub token for github providers",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Debug logging switch",
)
def main(manifest: Path | None, github_token: str | None, debug: bool):
    setup_logging(debug)
    mfp: Path
    if manifest is not None:
        mfp = manifest
    else:
        for n in DEFAULT_MANIFEST_PATHS:
            p = Path(n)
            if p.is_file():
                mfp = p
                break
        else:
            print("No manifest.json found or passed")
            return
    
    auth = Authorizaition(github=github_token)
    mf = Manifest.load(mfp)
    installer = Installer(mf, Path(""), auth)
    installer.cache.load()
    installer.cache.check_all_assets(installer.manifest)
    installer.install_mods()
    installer.install_plugins()
    installer.cache.save()
    installer.assets.clear_temp()

if __name__ == "__main__":
    main()