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
    def __init__(self, auth: Authorizaition, temp_folder: Path) -> None:
        # TODO token
        self.auth = auth
        self.temp_folder = temp_folder
        self.logger = logging.getLogger("AssetInstaller")
        self.github = Github(auth=Auth.Token(auth.github)) if auth.github else Github()
        self.temp_files: list[Path] = []
        self.repo_cache: dict[str, Repository] = {}
    
    def info(self, msg: object):
        self.logger.info(msg)
    
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
    
    def download_github_file(self, url: str, out_path: Path, is_binary: bool = False):
        session = requests.Session()
        if self.auth.github:
            session.headers["Authorization"] = f"token {self.auth.github}"
        if is_binary:
            session.headers["Accept"] = "application/octet-stream"
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

    def download_github_release(self, provider: GithubReleasesProvider, 
                                options: DownloadOptions):
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
            artifacts = [a for a in ls if pattern.match(a.name)]
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


class Installer:
    def __init__(self, manifest: Manifest, server_folder: Path, auth: Authorizaition) -> None:
        self.manifest = manifest
        self.folder = server_folder
        self.auth = auth
        self.assets = AssetInstaller(auth, self.folder / "tmp")
        self.mods_folder = self.folder / "mods"
        self.plugins_folder = self.folder / "plugins"
    
    def install_mod(self, mod: ModManifest):
        if isinstance(mod.provider, GithubReleasesProvider):
            options = DownloadOptions(mod.version, self.mods_folder,
                                      SimpleJarSelector())
            self.assets.download_github_release(mod.provider, options)
        elif isinstance(mod.provider, GithubActionsProvider):
            options = DownloadOptions(mod.version, self.mods_folder,
                                      SimpleJarSelector())
            self.assets.download_github_actions(mod.provider, options)
        else:
            raise ValueError(f"Unsupported provider {mod.provider.type}")
    
    def install_plugin(self, plugin: PluginManifest):
        if isinstance(plugin.provider, GithubReleasesProvider):
            options = DownloadOptions(plugin.version, self.plugins_folder, 
                                      SimpleJarSelector())
            self.assets.download_github_release(plugin.provider, options)
        elif isinstance(plugin.provider, GithubActionsProvider):
            options = DownloadOptions(plugin.version, self.plugins_folder,
                                      SimpleJarSelector())
            self.assets.download_github_actions(plugin.provider, options)
        else:
            raise ValueError(f"Unsupported provider {plugin.provider.type}")
    
    def install_mods(self):
        self.mods_folder.mkdir(parents=True, exist_ok=True)
        for mod in self.manifest.mods:
            self.install_mod(mod)
    
    def install_plugins(self):
        self.plugins_folder.mkdir(parents=True, exist_ok=True)
        for plugin in self.manifest.plugins:
            self.install_plugin(plugin)


LOG_FORMATTER = logging.Formatter(
    '[%(asctime)s][%(name)s/%(levelname)s]: %(message)s',
    datefmt='%d.%m.%Y %H:%M:%S'
)

def setup_logging():
    logger = logging.getLogger()  # Root logger
    logger.setLevel("INFO")

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)

    console_handler.setFormatter(LOG_FORMATTER)

    # Avoid duplicate handlers if script is reloaded
    if not logger.handlers:
        logger.addHandler(console_handler)
    else:
        logger.handlers.clear()
        logger.addHandler(console_handler)

DEFAULT_MANIFEST_PATHS = ["manifest.json", "manifest.yml", "manifest.yaml"]

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
def main(manifest: Path | None, github_token: str | None):
    setup_logging()
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