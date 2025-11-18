from pydantic import HttpUrl
from typing import Literal
from model import Asset, FilesCache
from main import AssetInstaller, AssetProvider, AssetsGroup, DownloadData, Environment, UpdateStatus
import requests
import utils
from registry import Registries

class DirectUrlAsset(Asset):
    """Downloads asset from specified url"""
    url: HttpUrl
    file_name: str | None = None
    type: Literal["url"]

    def create_asset_id(self) -> str:
        return str(self.url)

class DirectUrlProvider(AssetProvider[DirectUrlAsset, FilesCache, DownloadData]):

    def get_logger_name(self):
        return "DirectUrl"

    def download(self, assets: AssetInstaller, asset: DirectUrlAsset, group: AssetsGroup) -> DownloadData:
        if asset.file_name:
            name = asset.file_name
        else:
            path = asset.url.path
            if not path:
                name = asset.resolve_asset_id()
            else:
                name = path.split("/")[-1]
        out = group.get_folder(asset) / name
        try:
            self.download_file(requests.Session(), str(asset.url), out)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise utils.FriendlyException(
                    f"File at {str(asset.url)} not found")
            else:
                raise e
        return DownloadData(files=[out], primary_file=out)

    def has_update(self, assets: AssetInstaller, asset: DirectUrlAsset, group: AssetsGroup, cached: FilesCache) -> UpdateStatus:
        raise NotImplementedError


KEY = "url"

def setup(registries: Registries, env: Environment):
    registries.register_to(AssetProvider, KEY, DirectUrlProvider())
    registries.register_model_to(Asset, DirectUrlAsset)
