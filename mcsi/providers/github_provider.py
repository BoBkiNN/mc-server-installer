import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

import requests
import utils
from core import (AT, CT, DT, AssetInstaller, AssetProvider, AssetsGroup,
                  DownloadData, Environment, UpdateStatus)
from github import Auth, Github, UnknownObjectException
from github.Artifact import Artifact
from github.GitRelease import GitRelease
from github.Repository import Repository
from github.Workflow import Workflow
from github.WorkflowRun import WorkflowRun
from model import (Asset, FilesCache, FileSelectorKey, FileSelectorUnion,
                   LatestOrStr)
from registry import Registries


class GithubReleasesAsset(Asset):
    """Downloads asset from github"""
    version: LatestOrStr
    repository: str
    type: Literal["github"]
    file_selector: FileSelectorKey | FileSelectorUnion = "simple-jar"

    def create_asset_id(self) -> str:
        return self.repository

    def is_latest(self) -> bool:
        return self.version == "latest"


class GithubActionsAsset(Asset):
    """Downloads artifact from github actions"""
    version: int | Literal["latest"]
    repository: str
    branch: str = "master"
    workflow: str
    name_pattern: re.Pattern | None = None
    """RegEx for artifact name. All artifacts is downloaded if not set"""
    type: Literal["github-actions"]
    file_selector: FileSelectorKey | FileSelectorUnion = "simple-jar"

    def create_asset_id(self) -> str:
        return self.repository+"/"+self.workflow+"@"+self.branch

    def is_latest(self) -> bool:
        return self.version == "latest"


class GithubReleaseCache(FilesCache):
    type: str = "github"
    tag: str


class GithubActionsCache(FilesCache):
    type: str = "github-actions"
    run_id: int
    run_number: int


@dataclass
class GithubReleaseData(DownloadData):
    repo: Repository
    release: GitRelease

    @property
    def tag_name(self):
        return self.release.tag_name

    def create_cache(self) -> FilesCache:
        return GithubReleaseCache(files=self.files, tag=self.tag_name)


@dataclass
class GithubActionsData(DownloadData):
    repo: Repository
    workflow: Workflow
    run: WorkflowRun

    def create_cache(self) -> FilesCache:
        return GithubActionsCache(files=self.files, run_id=self.run.id, run_number=self.run.run_number)


class GithubLikeProvider(AssetProvider[AT, CT, DT]):
    github: utils.LateInit[Github] = utils.LateInit()

    def __init__(self) -> None:
        super().__init__()
        self.repo_cache: dict[str, Repository] = {}

    def get_logger_name(self):
        return "Github"

    def setup(self, assets: AssetInstaller):
        super().setup(assets)
        _user_agent: str | bytes = assets.session.headers["User-Agent"]
        if isinstance(_user_agent, bytes):
            user_agent = _user_agent.decode("utf-8")
        elif isinstance(_user_agent, str):
            user_agent = _user_agent
        else:
            raise ValueError("Unknown user-agent")
        self.github = Github(auth=Auth.Token(assets.auth.github)
                             if assets.auth.github else None, user_agent=user_agent)

    def get_repo(self, assets: AssetInstaller, name: str):
        if name in self.repo_cache:
            return self.repo_cache[name]
        try:
            repo = self.github.get_repo(name)
        except UnknownObjectException:
            raise utils.FriendlyException(f"Unknown repository {name}")
        self.repo_cache[name] = repo
        return repo

    def download_github_file(self, assets: AssetInstaller, url: str, out_path: Path, is_binary: bool = False):
        session = requests.Session()
        session.headers.update(assets.session.headers)
        if assets.auth.github:
            session.headers["Authorization"] = f"token {assets.auth.github}"
        if is_binary:
            session.headers["Accept"] = "application/octet-stream"
        self.download_file(session, url, out_path)


class GithubReleasesProvider(GithubLikeProvider[GithubReleasesAsset, GithubReleaseCache, GithubReleaseData]):

    def get_release(self, repo: Repository, version: str):
        release: GitRelease
        if version == "latest":
            release = repo.get_latest_release()
        else:
            release = repo.get_release(version)
        return release

    def download(self, assets: AssetInstaller, asset: GithubReleasesAsset, group: AssetsGroup) -> GithubReleaseData:
        self.debug(f"Getting repository {asset.repository}")
        repo = self.get_repo(assets, asset.repository)
        release: GitRelease = self.get_release(repo, asset.version)
        self.info(f"Found release {release.name!r}")
        ls = release.get_assets()
        m = {a.name: a for a in ls}
        names = asset.get_file_selector(assets.registry).find_targets(list(m))
        folder = group.get_folder(asset)
        files: list[Path] = []
        for k, v in m.items():
            if k not in names:
                continue
            outPath = folder / k
            self.info(f"🌐 Downloading artifact {k} to {outPath}..")
            self.download_github_file(assets, v.url, outPath, True)
            # v.download_asset(str(outPath.resolve())) # type: ignore
            files.append(outPath)
        self.info(f"✅ Downloaded {len(files)} assets from release")
        return GithubReleaseData(repo, release, files=files)

    def supports_update_checking(self) -> bool:
        return True

    def has_update(self, assets: AssetInstaller, asset: GithubReleasesAsset, group: AssetsGroup, cached: GithubReleaseCache) -> UpdateStatus:
        self.debug(f"Getting repository {asset.repository}")
        repo = self.get_repo(assets, asset.repository)
        release: GitRelease = self.get_release(repo, asset.version)
        self.info(f"Found release {release.name!r}")
        if release.tag_name != cached.tag:
            return UpdateStatus.OUTDATED
        else:
            return UpdateStatus.UP_TO_DATE


class GithubActionsProvider(GithubLikeProvider[GithubActionsAsset, GithubActionsCache, GithubActionsData]):

    def get_run(self, workflow: Workflow, asset: GithubActionsAsset):
        runs = workflow.get_runs(branch=asset.branch)  # type: ignore
        run: WorkflowRun | None
        version = asset.version
        if version == "latest":
            run = runs[0]
        else:
            run = next((r for r in runs if r.run_number == version), None)
        if run is None:
            raise ValueError("No run found")
        return run

    def download(self, assets: AssetInstaller, asset: GithubActionsAsset, group: AssetsGroup) -> GithubActionsData:
        repo = self.get_repo(assets, asset.repository)
        workflow = repo.get_workflow(asset.workflow)
        run = self.get_run(workflow, asset)
        self.info(f"Found run '{run.name}#{run.run_number}'")
        ls = run.get_artifacts()
        artifacts: list[Artifact] = []
        if not asset.name_pattern:
            artifacts = [a for a in ls]
        else:
            artifacts = [a for a in ls if asset.name_pattern.search(a.name)]
        if len(artifacts) == 0:
            raise ValueError(f"⚠ No artifacts found in run {run.id}")
        folder = group.get_folder(asset)
        files: list[Path] = []
        for artifact in artifacts:
            tmp = assets.get_temp_file()
            self.info(f"🌐 Downloading artifact {artifact.name} to {tmp}..")
            artifact.archive_download_url
            try:
                self.download_github_file(
                    assets, artifact.archive_download_url, tmp)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 410:
                    raise utils.FriendlyException("Artifact expired")
                else:
                    raise e
            c = 0
            with ZipFile(tmp) as zf:
                targets = asset.get_file_selector(
                    assets.registry).find_targets(zf.namelist())
                for name in targets:
                    self.info(f"Extracting {name}")
                    zf.extract(name, path=folder)
                    files.append(folder / name)
                    c += 1
            self.info(f"✅ Extracted {c} files from artifact {artifact.name}")
            assets.remove_temp_file(tmp)
        return GithubActionsData(repo, workflow, run, files=files)

    def supports_update_checking(self) -> bool:
        return True

    def has_update(self, assets: AssetInstaller, asset: GithubActionsAsset, group: AssetsGroup, cached: GithubActionsCache) -> UpdateStatus:
        repo = self.get_repo(assets, asset.repository)
        workflow = repo.get_workflow(asset.workflow)
        run = self.get_run(workflow, asset)
        self.info(f"Found run '{run.name}#{run.run_number}'")
        if cached.run_number > run.run_number:
            return UpdateStatus.AHEAD
        elif cached.run_number < run.run_number:
            return UpdateStatus.OUTDATED
        else:
            return UpdateStatus.UP_TO_DATE


def setup(registries: Registries, env: Environment):
    registries.register_to(AssetProvider, "github", GithubReleasesProvider())
    registries.register_to(
        AssetProvider, "github-actions", GithubActionsProvider())
    registries.register_models_to(
        Asset, GithubActionsAsset, GithubReleasesAsset)
    registries.register_models_to(
        FilesCache, GithubActionsCache, GithubReleaseCache)
