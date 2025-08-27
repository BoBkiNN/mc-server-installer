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
    version: str
    folder: Path
    selector: FileSelector

class AssetInstaller:
    def __init__(self, auth: Authorizaition, temp_folder: Path, logger: logging.Logger) -> None:
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
        i = 0
        for k, v in m.items():
            if k not in names: continue
            outPath = options.folder / k
            if outPath.is_file():
                fs = outPath.stat().st_size
                if fs == v.size:
                    self.info(f"⏩ Skipping artifact {k} due to its presense and matching sizes")
                    continue
            self.info(f"🌐 Downloading artifact {k} to {outPath}..")
            self.download_github_file(v.url, outPath, True)
            # v.download_asset(str(outPath.resolve())) # type: ignore
            i+=1
        self.info(f"✅ Downloaded {i} assets from release")
    
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
                    c += 1
            self.info(f"✅ Extracted {c} files from artifact {artifact.name}")
            self.remove_temp_file(tmp)
    
    def download_modrinth(self, provider: ModrinthProvider, options: DownloadOptions):
        self.debug(f"Getting project {provider.project_id}")
        project = self.modrinth.get_project(provider.project_id)
        if not project:
            raise ValueError(f"Unknown project {provider.project_id}")
        self.debug(f"Getting project versions..")
        vers = self.modrinth.get_versions(provider.project_id, ["spigot", "paper"])
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
                return False
            out = options.folder / primary.filename
            self.info(
                f"🌐 Downloading primary file {primary.filename} from version '{ver.name}'")
            self.download_file(self.modrinth.session, str(primary.url), out)
            return True
        if options.version == "latest":
            ver = filtered[0]
            if download_version(ver):
                self.info(f"✅ Downloaded latest version {ver.name}")
                return
            else:
                raise ValueError(f"Failed to download version {ver.name}. See errors above for details")
        c = 0
        for ver in filtered:
            if download_version(ver):
                c += 1
        if c == 0:
            raise ValueError(f"No valid versions found out of {len(filtered)}")
        self.info(f"✅ Downloaded {c} files")
            


class Installer:
    def __init__(self, manifest: Manifest, server_folder: Path, auth: Authorizaition) -> None:
        self.manifest = manifest
        self.folder = server_folder
        self.auth = auth
        self.logger = logging.getLogger("Installer")
        self.assets = AssetInstaller(auth, self.folder / "tmp", self.logger)
        self.mods_folder = self.folder / "mods"
        self.plugins_folder = self.folder / "plugins"
    
    def install(self, provider: AssetProvider, options: DownloadOptions):
        if isinstance(provider, GithubReleasesProvider):
            self.assets.download_github_release(provider, options)
        elif isinstance(provider, GithubActionsProvider):
            self.assets.download_github_actions(provider, options)
        elif isinstance(provider, ModrinthProvider):
            self.assets.download_modrinth(provider, options)
        else:
            raise ValueError(f"Unsupported provider {type(provider)}")

    def install_mod(self, mod: ModManifest):
        self.logger.info(f"🔄 Downloading mod {mod.get_asset_id()}")
        options = DownloadOptions(mod.version, self.mods_folder,
                                  SimpleJarSelector())
        self.install(mod.provider, options)
    
    def install_plugin(self, plugin: PluginManifest):
        self.logger.info(f"🔄 Downloading plugin {plugin.get_asset_id()}")
        options = DownloadOptions(plugin.version, self.plugins_folder,
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
    installer.install_mods()
    installer.install_plugins()

if __name__ == "__main__":
    main()