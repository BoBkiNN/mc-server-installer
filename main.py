from model import *
from github import Github, Auth
from github.GitRelease import GitRelease
from abc import ABC
import click, logging, sys
import requests
import tqdm


class FileSelector(ABC):
    def find_targets(self, ls: list[str]) -> list[str]:
        ...

class SimpleJarReleaseSelector(FileSelector):
    def find_targets(self, ls: list[str]) -> list[str]:
        return [i for i in ls if i.endswith(".jar") and not i.endswith("-sources.jar") and not i.endswith("-api.jar")]

class Authorizaition(BaseModel):
    github: str | None = None

class AssetInstaller:
    def __init__(self, auth: Authorizaition) -> None:
        # TODO token
        self.auth = auth
        self.logger = logging.getLogger("AssetInstaller")
        self.github = Github(auth=Auth.Token(auth.github)) if auth.github else Github()
    
    def info(self, msg: object):
        self.logger.info(msg)
    
    def download_github_file(self, url: str, out_path: Path):
        session = requests.Session()
        if self.auth.github:
            session.headers["Authorization"] = f"token {self.auth.github}"
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
                                version: str, folder: Path, selector: FileSelector):
        repo = self.github.get_repo(provider.repository)
        release: GitRelease
        if version == "latest":
            release = repo.get_latest_release()
        else:
            release = repo.get_release(version)
        self.info(f"✅ Found release {release.title}")
        assets = release.get_assets()
        m = {a.name: a for a in assets}
        names = selector.find_targets(list(m))
        i = 0
        for k, v in m.items():
            if k not in names: continue
            outPath = folder / k
            if outPath.is_file():
                print(v.size)
                fs = outPath.stat().st_size
                print(fs)
                if fs == v.size:
                    self.info(f"⏩ Skipping artifact {k} due to its presense")
                    continue
            self.info(f"🌐 Downloading artifact {k} to {outPath}..")
            self.download_github_file(v.url, outPath)
            # v.download_asset(str(outPath.resolve())) # type: ignore
            i+=1
        self.info(f"✅ Downloaded {i} assets from release")
        


class Installer:
    def __init__(self, manifest: Manifest, server_folder: Path, auth: Authorizaition) -> None:
        self.manifest = manifest
        self.folder = server_folder
        self.auth = auth
        self.assets = AssetInstaller(auth)
        self.mods_folder = self.folder / "mods"
    
    def install_mod(self, mod: ModManifest):
        if isinstance(mod.provider, GithubReleasesProvider):
            self.assets.download_github_release(mod.provider, mod.version, 
                                                self.mods_folder, 
                                                SimpleJarReleaseSelector())
        else:
            raise ValueError(f"Unsupported provider {mod.provider.type}")
    
    def install_mods(self):
        self.mods_folder.mkdir(parents=True, exist_ok=True)
        for mod in self.manifest.mods:
            self.install_mod(mod)


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

if __name__ == "__main__":
    main()