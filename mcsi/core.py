import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Generic, TypeVar

import requests
import tqdm
from __version__ import __version__
from model import *
from pydantic import BaseModel
from registry import Registries
from utils import LateInit


class UpdateStatus(Enum):
    UP_TO_DATE = False
    AHEAD = False
    OUTDATED = True


class Authorization(BaseModel):
    github: str | None = None


@dataclass
class Environment:
    auth: Authorization
    profile: str
    registries: Registries
    debug: bool


class AssetsGroup(ABC):
    @abstractmethod
    def get_folder(self, asset: Asset) -> Path:
        ...

    @abstractmethod
    def get_manifest_name(self) -> str:
        ...

    @property
    @abstractmethod
    def unit_name(self) -> str:
        ...

# Probably shit class


@dataclass(kw_only=True)
class DownloadData:
    files: list[Path]
    primary_file: Path | None = None

    @property
    def primary(self):
        if self.primary_file:
            return self.primary_file
        elif self.first_file:
            return self.first_file
        else:
            raise ValueError("No files set")

    @primary.setter
    def primary(self, file: Path):
        if self.primary_file:
            self.primary_file = file
        else:
            self.first_file = file

    def unset_primary(self):
        self.primary_file = None

    @property
    def first_file(self):
        if self.files:
            return self.files[0]
        else:
            return None

    @first_file.setter
    def first_file(self, file: Path):
        if self.files:
            self.files[0] = file
        else:
            self.files.append(file)

    def create_cache(self) -> FilesCache:
        return FilesCache(files=self.files)


class AssetInstaller:
    def __init__(self, manifest: Manifest, env: Environment, temp_folder: Path, logger: logging.Logger, session: requests.Session) -> None:
        self.game_version = manifest.mc_version
        self.env = env
        self.registry = self.env.registries
        self.auth = self.env.auth
        self.temp_folder = temp_folder
        self.logger = logger
        self.session = session
        self.temp_files: list[Path] = []

    def info(self, msg: object):
        self.logger.info(msg)

    def debug(self, msg: object):
        self.logger.debug(msg)

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


AT = TypeVar("AT", bound=Asset)
CT = TypeVar("CT", bound=FilesCache)
DT = TypeVar("DT", bound=DownloadData)


class AssetProvider(ABC, Generic[AT, CT, DT]):
    _logger: logging.Logger | None = None
    debug_enabled: LateInit[bool] = LateInit()

    def setup(self, assets: AssetInstaller):
        self.debug_enabled = assets.env.debug

    def get_logger_name(self):
        return type(self).__name__

    @property
    def logger(self):
        if self._logger:
            return self._logger
        l = logging.getLogger(self.get_logger_name())
        l.setLevel(logging.DEBUG if self.debug_enabled else logging.INFO)
        self._logger = l
        return l

    def download_file(self, session: requests.Session, url: str, out_path: Path):
        response = session.get(url, stream=True)
        response.raise_for_status()
        total_size = response.headers["Content-Length"]
        self.logger.debug(
            f"Downloading {total_size} bytes to {out_path.resolve()}")
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

    def info(self, msg: object):
        self.logger.info(msg)

    def debug(self, msg: object):
        self.logger.debug(msg)

    @abstractmethod
    def download(self, assets: AssetInstaller, asset: AT, group: AssetsGroup) -> DT:
        raise NotImplementedError

    def supports_update_checking(self) -> bool:
        return False

    # TODO return new version name for logging
    @abstractmethod
    def has_update(self, assets: AssetInstaller, asset: AT,
                   group: AssetsGroup, cached: CT) -> UpdateStatus:
        raise NotImplementedError


class CacheStore:
    def __init__(self, file: Path, mf: Manifest, env: "Environment", folder: Path) -> None:
        self.mf = mf
        self.env = env
        self.cache = Cache.create(mf, folder, env.profile)
        self.logger = logging.getLogger("Cache")
        self.logger.setLevel(logging.DEBUG if env.debug else logging.INFO)
        self.file = file
        self.dirty = False
        """Specifies if cache was modified"""
        self.folder = folder

    def save(self):
        if not self.dirty:
            return
        self.cache.version = __version__
        self.cache.profile = self.env.profile
        self.cache.save(self.file, self.env.registries, self.env.debug)
        self.dirty = False

    def reset(self):
        self.cache = Cache.create(self.mf, self.folder, self.env.profile)
        self.logger.debug("Cache reset.")

    def load(self):
        if not self.file.is_file():
            self.reset()
            return
        try:
            self.cache, asset_errors = Cache.load(
                self.file, self.env.registries)
            for k, error in asset_errors.items():
                lines = []
                for e in error.errors():
                    loc = '.'.join(str(x) for x in e['loc'])
                    msg = e['msg']
                    inp = e["input"]
                    type_ = e.get('type', 'unknown')
                    lines.append(
                        f"{k}.{loc} {msg} [type={type_}, input={inp}]")
                t = "\n".join(lines)
                self.logger.warning(
                    f"Failed to load asset cache entry {k}: \n{t}")
            self.logger.debug(
                f"Loaded cache with {len(self.cache.assets)} assets")
        except Exception as e:
            self.logger.error(
                "Exception loading stored cache. Resetting", exc_info=e)
            self.reset()
            return
        if self.cache.version != __version__:
            self.logger.warning(
                f"Cache is saved with different version ({self.cache.version}). You might expect loading errors")
        if self.cache.mc_version and self.cache.mc_version != self.mf.mc_version:
            self.logger.info(
                f"Resetting cache due to changed minecraft version {self.cache.mc_version} -> {self.mf.mc_version}")
            self.reset()
        abs_folder = self.folder.resolve()
        if self.cache.server_folder != abs_folder:
            self.logger.warning(
                f"Server folder differs from cache: {self.cache.server_folder} -> {abs_folder}")
            self.logger.warning(
                "This might mean that all cached data is invalid in current new location, so resetting")
            self.reset()

    def invalidate_asset(self, asset: str | Asset, reason: InvalidReason | None = None):
        id = asset.resolve_asset_id() if isinstance(asset, Asset) else asset
        removed = self.cache.assets.pop(id, None)
        if removed:
            for p in removed.data.files:
                stored_file = self.cache.server_folder / p
                if stored_file.is_file():
                    os.remove(stored_file)
            msg = f"💥 Invalidated asset {id}"
            if reason:
                msg += f" due to: {reason.reason}"
            self.logger.info(msg)
            self.dirty = True

    def check_asset(self, asset: str | Asset, hash: str | None):
        """Returns None if cache is invalid"""
        id = asset.resolve_asset_id() if isinstance(asset, Asset) else asset
        entry = self.cache.assets.get(id, None)
        if not entry:
            return None
        actual_hash_str = None if hash is None else hash[:7]+".."
        self.logger.debug(
            f"Checking cached asset {entry.asset_id} {entry.asset_hash[:7]}.. with actual {actual_hash_str}")
        state = entry.is_valid(self.folder, hash)
        if state.is_ok():
            return entry
        else:
            self.invalidate_asset(id, state.value)
            return None

    def store_asset(self, asset: AssetCache):
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

    def invalidate_core(self):
        p = self.cache.core
        if p:
            self.cache.core = None
            self.dirty = True
            self.logger.info("⚠  Core invalidated")

    def store_core(self, core: CoreCache):
        self.cache.core = core
        self.dirty = True

    def check_core(self, core: Core, mc_ver: str):
        cached = self.cache.core
        if not cached:
            return None
        if not cached.data.check_files(self.folder):
            self.logger.debug("Invalidating core due to invalid files")
            self.invalidate_core()
            return None
        if cached.type != core.type:
            self.logger.debug(
                f"Invalidating core due to changed type {cached.type} -> {core.type}")
            self.invalidate_core()
            return None
        vhash = core.hash_from_ver(mc_ver)
        if vhash is not None and cached.version_hash != vhash:
            # hash is provided and not matching
            self.logger.debug(
                f"Invalidating core due to changed version hash {cached.version_hash} -> {vhash}")
            self.invalidate_core()
            return None
        return cached


class UpdateResult(Enum):
    SKIPPED = 0
    FAILED = 1
    UPDATED = 2
    UP_TO_DATE = 3
    FOUND = 4
