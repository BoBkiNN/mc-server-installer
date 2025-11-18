import api.jenkins_models as jm
import jenkins
from model import FileSelectorKey, FileSelectorUnion, FilesCache, Asset
from main import AssetProvider, AssetInstaller
from core import UpdateStatus, Environment, AssetsGroup, DownloadData
from dataclasses import dataclass
from typing import Literal
from pydantic import HttpUrl
from pathlib import Path
from registry import Registries


class JenkinsCache(FilesCache):
    type: str = "jenkins"
    build_number: int


@dataclass
class JenkinsData(DownloadData):
    job: jm.Job
    build: jm.Build
    artifacts: list[jm.Artifact]

    def create_cache(self) -> FilesCache:
        return JenkinsCache(files=self.files, build_number=self.build.number)

class JenkinsAsset(Asset):
    version: Literal["latest"] | int
    url: HttpUrl
    """URL to Jenkins instance"""
    job: str
    """Name of job"""
    file_selector: FileSelectorKey | FileSelectorUnion = "simple-jar"
    type: Literal["jenkins"]

    def create_asset_id(self) -> str:
        host = self.url.host or "Unknown"
        return f"{self.job}@{host}"

    def is_latest(self) -> bool:
        return self.version == "latest"

class JenkinsProvider(AssetProvider[JenkinsAsset, JenkinsCache, JenkinsData]):

    def get_logger_name(self):
        return "Jenkins"

    def get_build(self, j: jenkins.Jenkins, job: jm.Job, asset: JenkinsAsset):
        build: jm.Build | None
        if asset.version == "latest":
            lsb = job.lastSuccessfulBuild
            if not lsb:
                raise ValueError(
                    f"No latest successful build found for {job.name}")
            build = jm.Build.get_build(j, asset.job, lsb.number)
        else:
            build = jm.Build.get_build(j, asset.job, asset.version)
        if not build:
            raise ValueError(
                f"Build {asset.job}#{asset.version} not found")
        if not build.result.is_complete_build():
            raise ValueError(
                f"Build {build.fullDisplayName} is not completed: {build.result}")
        return build

    def download(self, assets: AssetInstaller, asset: JenkinsAsset, group: AssetsGroup) -> JenkinsData:
        j = jenkins.Jenkins(str(asset.url))
        job = jm.Job.get_job(j, asset.job)
        if not job:
            raise ValueError(f"Unknown job {asset.job}")
        build: jm.Build = self.get_build(j, job, asset)
        self.info(f"Found build {build.fullDisplayName}")
        fn = asset.get_file_selector(assets.registry).find_targets(
            [a.fileName for a in build.artifacts])
        filtered = [a for a in build.artifacts if a.fileName in fn]
        if not filtered:
            raise ValueError("No artifacts passed filter")

        folder = group.get_folder(asset)

        def download_artifact(a: jm.Artifact):
            url = f"{build.url}artifact/"+a.relativePath
            to = folder / a.fileName
            self.info(
                f"🌐 Downloading artifact {a.fileName} from build '{build.fullDisplayName}'")
            self.download_file(assets.session, url, to)
            return to
        files: dict[Path, jm.Artifact] = {}
        for a in filtered:
            p = download_artifact(a)
            if p:
                self.info(f"✅ Downloaded artifact to {p}")
                files[p] = a
            else:
                self.logger.warning(
                    f"⚠  Failed to download artifact {a.fileName}")
        return JenkinsData(job, build, artifacts=list(files.values()), files=list(files.keys()))

    def supports_update_checking(self):
        return True

    def has_update(self, assets: AssetInstaller, asset: JenkinsAsset, group: AssetsGroup, cached: JenkinsCache) -> UpdateStatus:
        j = jenkins.Jenkins(str(asset.url))
        job = jm.Job.get_job(j, asset.job)
        if not job:
            raise ValueError(f"Unknown job {asset.job}")
        build: jm.Build = self.get_build(j, job, asset)
        self.info(f"Found build {build.fullDisplayName}")
        if cached.build_number > build.number:
            return UpdateStatus.AHEAD
        elif cached.build_number < build.number:
            return UpdateStatus.OUTDATED
        else:
            return UpdateStatus.UP_TO_DATE


KEY = "jenkins"


def setup(registries: Registries, env: Environment):
    registries.register_to(AssetProvider, KEY, JenkinsProvider())
    registries.register_models_to(Asset, JenkinsAsset)
    registries.register_models_to(FilesCache, JenkinsCache)
