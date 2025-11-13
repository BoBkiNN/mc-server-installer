import os
import sys

# Support vendored dependencies for zipapp builds
VENDOR_PATH = os.path.join(os.path.dirname(__file__), "_vendor")
if os.path.isdir(VENDOR_PATH) and VENDOR_PATH not in sys.path:
    sys.path.insert(0, VENDOR_PATH)


import logging
import time
import uuid
from dataclasses import dataclass
from typing import Sequence
from zipfile import ZipFile

import click
import colorlog
import jenkins
import jenkins_models as jm
import modrinth
import papermc_fill as papermc
import requests
import tqdm
from __version__ import __version__
from asteval import Interpreter
from asteval.astutils import ExceptionHolder
from github import Auth, Github, UnknownObjectException
from github.Artifact import Artifact
from github.GitRelease import GitRelease
from github.Repository import Repository
from github.Workflow import Workflow
from github.WorkflowRun import WorkflowRun
from model import *
from regunion import make_registry_schema_generator


def millis():
    return int(time.time()*1000)


class CacheStore:
    def __init__(self, file: Path, mf: Manifest, registries: Registries, debug: bool, folder: Path) -> None:
        self.mf = mf
        self.registries = registries
        self.cache = Cache.create(mf, folder)
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
        self.cache.version = __version__
        self.cache.save(self.file, self.registries, self.debug)
        self.dirty = False

    def reset(self):
        self.cache = Cache.create(self.mf, self.folder)
        self.logger.debug("Cache reset.")

    def load(self, registries: Registries):
        if not self.file.is_file():
            self.reset()
            return
        try:
            self.cache, asset_errors = Cache.load(self.file, registries)
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
            self.logger.warning("Cache is saved with different version. You might expect loading erros")
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


class Authorization(BaseModel):
    github: str | None = None


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


@dataclass
class ModrinthData(DownloadData):
    version: modrinth.Version
    project: modrinth.Project

    def create_cache(self) -> FilesCache:
        return ModrinthCache(files=self.files, version_id=self.version.id, version_number=self.version.version_number)


@dataclass
class JenkinsData(DownloadData):
    job: jm.Job
    build: jm.Build
    artifacts: list[jm.Artifact]

    def create_cache(self) -> FilesCache:
        return JenkinsCache(files=self.files, build_number=self.build.number)


class ExpressionProcessor:
    def __init__(self, logger: logging.Logger, folder: Path) -> None:
        self.logger = logger
        self.folder = folder
        self.intpr = Interpreter(minimal=True)

    def log_error(self, error: ExceptionHolder, expr: Expr, source_key: str, source_text: str):
        if not error.exc:
            exc_name = "UnknownError"
        else:
            try:
                exc_name = error.exc.__name__
            except AttributeError:
                exc_name = str(error.exc)
            if exc_name in (None, 'None'):
                exc_name = "UnknownError"
        exc_msg = str(error.msg)

        lineno = getattr(error.node, "lineno", 1) or 1
        col = getattr(error.node, "col_offset", 0) or 0

        # Extract offending line from source_text
        src_lines = source_text.splitlines()
        line_text = src_lines[lineno - 1] if 0 <= lineno - \
            1 < len(src_lines) else ""

        # Build caret marker
        marker = " " * col + "^"

        # Final pretty message
        msg = (
            f"💥 Failed to evaluate expression in {source_key}\n"
            f"  Expression: {str(expr)!r}\n"
            f"  {exc_name}: {exc_msg}\n"
            f"  {line_text}\n"
            f"  {marker} (line {lineno}, column {col})"
        )
        self.logger.error(msg)

    def eval(self, expr: Expr, source_key: str, source_text: str):
        res = self.intpr.eval(expr)
        errors: list[ExceptionHolder] = self.intpr.error
        error = errors[0] if errors else None
        if error is None:
            return res
        self.log_error(error, expr, source_key, source_text)
        return error

    def eval_template(self, expr: TemplateExpr, source_key: str, source_text: str):
        parts = expr.parts()
        bs = ""
        ei = 0
        for part in parts:
            if isinstance(part, Expr):
                v = self.eval(part, source_key+f"${ei}", source_text)
                if isinstance(v, ExceptionHolder):
                    return v
                bs += str(v)
                ei += 1
            else:
                bs += part
        return bs
    
    def eval_if(self, key: str, if_code: Expr):
        """
        Executes a code string and returns True or False.

        Integer return values are converted to boolean using `bool(v)`.

        :param key: Key to description
        :type key: str
        :param if_code: Code to evaluate
        :type if_code: Expr

        :return: True if code returned True or any truthy value, False if code returned False, None if evaluation error occurred
        :rtype: bool or None
        """
        v = self.eval(if_code, key, str(if_code))
        if isinstance(v, ExceptionHolder):
            self.logger.error(
                "Failed to process if statement, see above errors for details")
            return
        if isinstance(v, bool):
            b = v
        elif isinstance(v, int):
            b = bool(v)
        elif isinstance(v, str):
            b = True if v.lower() == "true" else False
        else:
            self.logger.warning(
                f"If statement in {key} returned non-bool. Expected True of False")
            b = True
        return b

    def handle(self, key: str, action: BaseAction, data: DownloadData):
        # TODO return bool or enum stating error or ok
        if_code = action.if_
        if if_code:
            b = self.eval_if(key+".if", if_code)
            if not b: # False or None
                return
        if isinstance(action, DummyAction):
            v = self.eval(action.expr, key+".expr", str(action.expr))
            if isinstance(v, ExceptionHolder):
                self.logger.error(
                    "Failed to process expression, see above errors for details")
                return
            self.logger.info(f"Dummy expression at {key} returned {v}")
        elif isinstance(action, RenameFile):
            frp = data.primary
            if not frp:
                self.logger.error("No files to rename")
                return
            to = self.eval_template(action.to, key+".to", str(action.to))
            if isinstance(to, ExceptionHolder):
                return
            top = frp.with_name(to)
            if top.is_file():
                os.remove((self.folder / top).resolve())
            frp.rename(top)
            data.primary = top
            self.logger.info(f"✅ Renamed file from {frp} to {top}")
        elif isinstance(action, UnzipFile):
            if action.folder.root:
                folder = self.eval_template(
                    action.folder, key+".folder", action.folder.root)
                if isinstance(folder, ExceptionHolder):
                    return
            else:
                pf = data.primary.parent
                if pf.is_absolute():
                    folder = pf
                else:
                    folder = self.folder / data.primary.parent
            with ZipFile(self.folder / data.primary, "r") as zip_ref:
                zip_ref.extractall(folder)
            self.logger.info(f"✅ Unzipped {data.primary} into {folder}")
        else:
            raise ValueError(f"Unknown action {type(action)}")

    def process(self, asset: Asset, group: "AssetsGroup", data: DownloadData):
        ls = asset.actions
        if not ls:
            return data
        self.intpr.symtable["data"] = data
        self.intpr.symtable["d"] = data
        self.intpr.symtable["asset"] = asset
        self.intpr.symtable["a"] = asset
        for n in ["data", "d", "asset", "a"]:
            self.intpr.readonly_symbols.add(n)
        ak = group.get_manifest_name()+"."+asset.resolve_asset_id()
        for i, a in enumerate(ls):
            key = f"{ak}.actions[{i}]"
            try:
                self.handle(key, a, data)
            except Exception as e:
                self.logger.error(
                    f"Failed to handle action {type(a)} at {key}", exc_info=e)


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


class PluginsGroup(AssetsGroup):
    def get_folder(self, asset: Asset) -> Path:
        return Path("plugins")
    
    def get_manifest_name(self) -> str:
        return "plugins"
    
    @property
    def unit_name(self) -> str:
        return "plugin"

class ModsGroup(AssetsGroup):
    def get_folder(self, asset: Asset) -> Path:
        return Path("mods")
    
    def get_manifest_name(self) -> str:
        return "mods"
    
    @property
    def unit_name(self) -> str:
        return "mod"


class DatapacksGroup(AssetsGroup):
    def get_folder(self, asset: Asset) -> Path:
        return Path("world") / "datapacks"

    def get_manifest_name(self) -> str:
        return "datapacks"

    @property
    def unit_name(self) -> str:
        return "datapack"

class CustomsGroup(AssetsGroup):
    def get_folder(self, asset: Asset) -> Path:
        if asset.folder is None:
            raise ValueError("No folder set for custom asset")
        return asset.folder

    def get_manifest_name(self) -> str:
        return "customs"

    @property
    def unit_name(self) -> str:
        return "custom asset"


class AssetInstaller:
    def __init__(self, installer: "Installer", temp_folder: Path, logger: logging.Logger, session: requests.Session) -> None:
        self.game_version = installer.manifest.mc_version
        self.registry = installer.registries
        self.auth = installer.auth
        self.debug_enabled = installer.debug
        self.temp_folder = temp_folder
        self.logger = logger
        _user_agent: str | bytes = session.headers["User-Agent"]
        if isinstance(_user_agent, bytes):
            user_agent = _user_agent.decode("utf-8")
        elif isinstance(_user_agent, str):
            user_agent = _user_agent
        else:
            raise ValueError("Unknown user-agent")
        self.session = session
        self.github = Github(auth=Auth.Token(self.auth.github)
                             if self.auth.github else None, user_agent=user_agent)
        self.modrinth = modrinth.Modrinth(session)
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


class LateInit(Generic[T]):
    def __init__(self):
        self._value: Optional[T] = None
        self._value_set: bool = False

    def __get__(self, instance, owner) -> T:
        if not self._value_set:
            raise AttributeError(
                "LateInit variable accessed before initialization")
        return self._value  # type: ignore

    def __set__(self, instance, value: T) -> None:
        self._value = value
        self._value_set = True

    def __delete__(self, instance) -> None:
        self._value = None
        self._value_set = False

class UpdateStatus(Enum):
    UP_TO_DATE = False
    AHEAD = False
    OUTDATED = True


AT = TypeVar("AT", bound=Asset)
CT = TypeVar("CT", bound=FilesCache)
DT = TypeVar("DT", bound=DownloadData)


class AssetProvider(ABC, Generic[AT, CT, DT]):
    _logger: logging.Logger | None = None
    debug_enabled: LateInit[bool] = LateInit()

    def setup(self, assets: AssetInstaller):
        self.debug_enabled = assets.debug_enabled

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
        self.logger.debug(f"Downloading {total_size} bytes to {out_path.resolve()}")
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
        self.download_file(requests.Session(), str(asset.url), out)
        return DownloadData(files=[out], primary_file=out)
    
    def has_update(self, assets: AssetInstaller, asset: DirectUrlAsset, group: AssetsGroup, cached: FilesCache) -> UpdateStatus:
        raise NotImplementedError

class ModrinthProvider(AssetProvider[ModrinthAsset, ModrinthCache, ModrinthData]):
    def get_logger_name(self):
        return "Modrinth"
    
    def get_version(self, assets: AssetInstaller, project: modrinth.Project, asset: ModrinthAsset):
        self.debug(f"Getting project versions..")
        game_versions = [] if asset.ignore_game_version else [assets.game_version]
        vers = assets.modrinth.get_versions(
            asset.project_id, ["spigot", "paper"], game_versions)
        if not vers:
            raise ValueError(
                f"Cannot find versions for project {asset.project_id}")
        name_pattern = asset.version_name_pattern
        filtered: list[modrinth.Version] = []
        self.debug(f"Got {len(vers)} versions from {project.title}")
        for ver in vers:
            # ignoring mc version currently
            if asset.channel and asset.channel != ver.version_type:
                continue
            if asset.version_is_id and asset.version != ver.id:
                continue
            if name_pattern and not name_pattern.search(ver.name):
                continue
            filtered.append(ver)
        if len(filtered) == 0:
            raise ValueError("No valid versions found")
        if asset.version == "latest":
            return filtered[0]
        # at this moment version is not latest and not an version id, so this is version_number
        ver = next(
            (v for v in filtered if v.version_number == asset.version), None)
        if not ver:
            raise ValueError(
                f"Failed to find valid version with number {asset.version} out of {len(filtered)}")
        return ver
    
    def download(self, assets: AssetInstaller, asset: ModrinthAsset, group: AssetsGroup) -> ModrinthData:
        self.debug(f"Getting project {asset.project_id}")
        project = assets.modrinth.get_project(asset.project_id)
        if not project:
            raise ValueError(f"Unknown project {asset.project_id}")
        ver = self.get_version(assets, project, asset)
        self.logger.info(f"Found version {ver.name!r} ({ver.version_number})")

        folder = group.get_folder(asset)

        def download_version(ver: modrinth.Version):
            # TODO use_primary and file_name_pattern properties here to return multiple files
            primary = ver.get_primary()
            if not primary:
                self.logger.warning(
                    f"⚠ No primary file in version '{ver.name}'")
                return None
            out = folder / primary.filename
            self.info(
                f"🌐 Downloading primary file {primary.filename} from version '{ver.name}'")
            self.download_file(assets.modrinth.session, str(primary.url), out)
            return [out]
        
        files = download_version(ver)
        if not files:
            raise ValueError(f"No valid files found in version {ver}")
        self.info(f"✅ Downloaded {len(files)} files from version {ver.name}")
        return ModrinthData(ver, project, files=files)
    
    def supports_update_checking(self) -> bool:
        return True
    
    def has_update(self, assets: AssetInstaller, asset: ModrinthAsset, group: AssetsGroup, cached: ModrinthCache) -> UpdateStatus:
        self.debug(f"Getting project {asset.project_id}")
        project = assets.modrinth.get_project(asset.project_id)
        if not project:
            raise ValueError(f"Unknown project {asset.project_id}")
        ver = self.get_version(assets, project, asset)
        self.logger.info(f"Found version {ver.name!r} ({ver.version_number})")
        # maybe semver when possible?
        if cached.version_id != ver.id:
            return UpdateStatus.OUTDATED
        return UpdateStatus.UP_TO_DATE


class GithubLikeProvider(AssetProvider[AT, CT, DT]):
    def __init__(self) -> None:
        super().__init__()
        self.repo_cache: dict[str, Repository] = {}
    
    def get_logger_name(self):
        return "Github"

    def get_repo(self, assets: AssetInstaller, name: str):
        if name in self.repo_cache:
            return self.repo_cache[name]
        try:
            repo = assets.github.get_repo(name)
        except UnknownObjectException:
            raise ValueError(f"Unknown repository {name}")
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
        self.info(f"Found release {release.title!r}")
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
        self.info(f"Found release {release.title!r}")
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
                self.download_github_file(assets, artifact.archive_download_url, tmp)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 410:
                    raise utils.FriendlyException("Artifact expired") from None
                else: raise e
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


class UpdateResult(Enum):
    SKIPPED = 0
    FAILED = 1
    UPDATED = 2
    UP_TO_DATE = 3
    FOUND = 4


class Installer:
    def __init__(self, manifest: Manifest, manifest_path: Path,
                 server_folder: Path, auth: Authorization, debug: bool,
                 registries: Registries, logger: logging.Logger) -> None:
        self.registries = registries
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.folder = server_folder
        self.auth = auth
        self.debug = debug
        self.logger = logger
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "BoBkiNN/mc-server-installer"
        self.cache = CacheStore(
            self.folder / ".install_cache.json", manifest, self.registries, debug, self.folder)
        self.assets = AssetInstaller(self, self.folder / "tmp", self.logger, self.session)
        self.mods_folder = self.folder / "mods"
        self.plugins_folder = self.folder / "plugins"

        self.install_notes: dict[str, tuple[str, AssetsGroup]] = {}
        """Dict of asset id to note. Printed at installation finish"""
    
    def setup_providers(self):
        reg = self.registries.get_registry(AssetProvider)
        if reg is None:
            raise ValueError("Registry providers is not set!")
        for k, v in reg.all().items():
            try:
                v.setup(self.assets)
            except Exception as e:
                raise ValueError(f"Failed to setup provider {k!r}") from e

    def prepare(self, validate: bool):
        self.setup_providers()
        self.cache.load(self.registries)
        if validate:
            self.cache.check_all_assets(self.manifest)
        self.logger.info(
            f"✅ Prepared installer for MC {self.manifest.mc_version}")

    def shutdown(self):
        self.cache.save()
        self.assets.clear_temp()
        self.session.close()

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
                             PaperChannel.STABLE), None)
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

    def install_paper_core(self, core: PaperCoreManifest) -> CoreCache:
        api = papermc.PaperMcFill(self.session)
        mc = self.manifest.mc_version
        build = self.get_paper_build(api, core, mc)
        download = build.get_default_download()
        jar_name = core.file_name if core.file_name else download.name
        out = self.folder / jar_name
        self.assets.download_file(api.session, str(download.url), out)
        vhash = hashlib.sha256(f"{mc}/{build.id}".encode()).hexdigest()
        data = PaperCoreCache(files=[Path(jar_name)], build_number=build.id)
        return CoreCache(update_time=millis(), data=data, version_hash=vhash, type="paper")

    def install_core(self):
        core = self.manifest.core
        cache = self.cache.check_core(core, self.manifest.mc_version)
        if cache:
            self.logger.info(f"⏩ Skipping core as it already installed")
            return cache
        self.logger.info(f"🔄 Downloading core {core.display_name()}..")
        i: CoreCache
        if isinstance(core, PaperCoreManifest):
            i = self.install_paper_core(core)
        else:
            raise ValueError("Unsupported core")
        self.logger.info(f"✅ Installed core {i.display_name()}")
        self.cache.store_core(i)

    def download_asset(self, asset: Asset, group: AssetsGroup):
        reg = self.registries.get_registry(AssetProvider)
        if reg is None:
            raise ValueError("Registry providers is not set!")
        provider = reg.get(asset.get_type())
        if provider:
            data = provider.download(self.assets, asset, group)
        else:
            raise ValueError(f"Unsupported asset type {type(asset)}")
        return data

    def install(self, asset: Asset, group: AssetsGroup) -> tuple[AssetCache, bool]:
        asset_id = asset.resolve_asset_id()
        asset_hash = asset.stable_hash()
        cached = self.cache.check_asset(
            asset_id, asset_hash) if asset.caching else None
        if cached:
            self.logger.info(
                f"⏩ Skipping {group.unit_name} '{asset_id}' as it already installed")
            return cached, True

        self.logger.info(f"🔄 Downloading {group.unit_name} {asset_id}")
        asset_folder = group.get_folder(asset)
        target_folder = asset_folder if asset_folder.is_absolute() else self.folder / \
            asset_folder

        if not target_folder.exists():
            target_folder.mkdir(parents=True, exist_ok=True)
        try:
            data: DownloadData = self.download_asset(asset, group)
        except utils.FriendlyException as e:
            raise utils.FriendlyException(f"Asset download failed: {e}") from None
        except Exception as e:
            raise ValueError(f"Exception downloading asset {asset_id}") from e
        data.files = [p.relative_to(
            self.folder) if not p.is_absolute() else p for p in data.files]

        if asset.actions:
            logger = logging.getLogger("Expr#"+asset_id)
            logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
            exprs = ExpressionProcessor(logger, self.folder)
            exprs.process(asset, group, data)

        cache = data.create_cache()
        result = AssetCache.create(asset_id, asset_hash, millis(), cache)
        if asset.caching:
            self.cache.store_asset(result)
        return result, False
    
    def prepare_asset_list(self, ls: Sequence[Asset], group: AssetsGroup):
        def filter_asset(a: Asset) -> bool:
            asset_id = a.resolve_asset_id()
            if_code = a.if_
            ak = group.get_manifest_name()+"."+asset_id
            if if_code:
                logger = logging.getLogger("Expr#"+asset_id)
                logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
                exprs = ExpressionProcessor(logger, self.folder)
                v = exprs.eval_if(ak+".if", if_code)
                if v is False:
                    return False
            return True
        return [a for a in ls if filter_asset(a)]

    def install_list(self, ls: Sequence[Asset], group: AssetsGroup):
        if not ls:
            return
        o_total = len(ls)
        ls = self.prepare_asset_list(ls, group)
        total = len(ls)
        entry_name = group.unit_name
        excluded = o_total - total
        if excluded > 0:
            self.logger.info(f"🔄 Installing {total} {entry_name}(s) ({excluded} excluded)")
        else:
            self.logger.info(f"🔄 Installing {total} {entry_name}(s)")
        failed: list[Asset] = []
        cached: list[Asset] = []
        for a in ls:
            key = a.resolve_asset_id()
            if isinstance(a, NoteAsset):
                self.install_notes[key] = a.note, group
                self.logger.info(f"🚩 {entry_name.capitalize()} {key} requires manual installation. See reason at end of installation")
                continue
            try:
                _, is_cached = self.install(a, group)
            except utils.FriendlyException as e:
                self.logger.error(f"Exception installing asset {key!r}: {e}")
                failed.append(a)
                continue
            except Exception as e:
                self.logger.error(
                    f"Exception installing asset {key!r}", exc_info=e)
                failed.append(a)
                continue
            if is_cached:
                cached.append(a)
            self.cache.save()
        cached_str = f" ({len(cached)} cached)" if cached else ""
        if failed:
            self.logger.info(
                f"⚠  Installed {total-len(failed)}/{total}{cached_str} {entry_name}(s)")
        else:
            self.logger.info(
                f"✅ Installed {total}/{total}{cached_str} {entry_name}(s)")

    def install_mods(self):
        self.install_list(self.manifest.mods, ModsGroup())

    def install_plugins(self):
        self.install_list(self.manifest.plugins, PluginsGroup())

    def install_datapacks(self):
        self.install_list(self.manifest.datapacks, DatapacksGroup())

    def install_customs(self):
        self.install_list(self.manifest.customs, CustomsGroup())
    
    def show_notes(self):
        if not self.install_notes:
            return
        d = self.install_notes
        self.logger.info(f"🚩 You have {len(d)} note(s) from assets thats need to be downloaded manually")
        self.logger.info("🚩 You can ignore this messages if you installed them.")
        for (v, g) in d.values():
            entry_name = g.unit_name
            self.logger.info(f"🚩 {entry_name.capitalize()}: {v}")
    
    def check_update(self, asset: Asset, group: AssetsGroup, cached: AssetCache) -> UpdateStatus:
        reg = self.registries.get_registry(AssetProvider)
        if not reg:
            raise ValueError("Failed to find providers registry!")
        key = asset.get_type()
        provider = reg.get(key)
        if not provider:
            raise ValueError(f"Unknown provider {key!r}")
        try:
            return provider.has_update(self.assets, asset, group, cached.data)
        except NotImplementedError as e:
            raise ValueError(f"Provider {key!r} do not implemented update checking")
        except Exception as e:
            raise ValueError(f"Exception checking update for {group.unit_name} {asset.resolve_asset_id()}") from e
    
    def update_lifecycle(self, asset: Asset, group: AssetsGroup, dry: bool) -> UpdateResult:
        """
        Returns True if successfully installed and False if not <br>
        Returns None if no updates available or dry run
        """
        reg = self.registries.get_registry(AssetProvider)
        if not reg:
            raise ValueError("Failed to find providers registry!")
        key = asset.get_type()
        provider = reg.get(key)
        if not provider:
            raise ValueError(f"Unknown provider {key!r}")
        if not provider.supports_update_checking():
            self.logger.debug(f"Provider {key!r} does not support update checking.")
            return UpdateResult.SKIPPED
        asset_id = asset.resolve_asset_id()
        asset_hash = asset.stable_hash()
        cached = self.cache.check_asset(asset_id, asset_hash) if asset.caching else None
        if not cached:
            return UpdateResult.SKIPPED
        self.logger.info(f"🔁 Checking {asset_id} for updates")
        status = self.check_update(asset, group, cached)
        if status != UpdateStatus.OUTDATED:
            self.logger.info(f"💠 No new updates for {asset_id}")
            return UpdateResult.UP_TO_DATE
        self.logger.info(f"💠 New update found for {group.unit_name} {asset_id}")
        if asset.is_latest() is False: # fixed version
            self.logger.debug(f"Skipping {group.unit_name} {asset_id} as it has fixed version")
            return UpdateResult.FOUND
        if dry:
            self.logger.debug("Dry run, do not installing update")
            return UpdateResult.FOUND
        self.cache.invalidate_asset(asset, reason=InvalidReason("outdated", "New version is found"))
        try:
            self.install(asset, group)
        except Exception as e:
            self.logger.error(f"❌ Failed to install update for {group.unit_name} {asset_id}", exc_info=e)
            return UpdateResult.FAILED
        self.cache.save()
        return UpdateResult.UPDATED
        
    def update_list(self, assets: Sequence[Asset], group: AssetsGroup, dry: bool):
        filtered: list[Asset] = []
        for asset in assets:
            asset_id = asset.resolve_asset_id()
            if not asset.caching:
                self.logger.debug(f"Skipping asset {asset_id} as it does caching disabled")
                continue
            if asset.is_latest() is None:
                self.logger.debug(
                    f"Skipping asset {asset_id} as it has fixed version")
            filtered.append(asset)
        self.logger.info(f"💠 Checking updates for {len(filtered)} {group.unit_name}(s)")
        results: dict[str, UpdateResult] = {}

        for asset in filtered:
            asset_id = asset.resolve_asset_id()
            if isinstance(asset, NoteAsset): 
                results[asset_id] = UpdateResult.SKIPPED
                continue
            try:
                r = self.update_lifecycle(asset, group, dry)
            except Exception as e:
                self.logger.error(
                    f"❌ Failed to complete update lifecycle for {group.unit_name} {asset_id}", exc_info=e)
                results[asset_id] = UpdateResult.FAILED
                continue
            results[asset_id] = r
        up_to_date = sum((1 for r in results.values() if r == UpdateResult.UP_TO_DATE))
        updated = sum((1 for r in results.values() if r == UpdateResult.UPDATED))
        failed = sum((1 for r in results.values() if r == UpdateResult.FAILED))
        found = sum((1 for r in results.values() if r == UpdateResult.FOUND))
        self.logger.info(f"✅ Completed update check for {group.unit_name}s.\n ✅ No updates: {up_to_date}.\n ✅ Updated: {updated} (found {found}).\n ❌ Failed: {failed}")
    
    def update_all(self, dry: bool):
        self.update_list(self.manifest.mods, ModsGroup(), dry)
        self.update_list(self.manifest.plugins, PluginsGroup(), dry)
        self.update_list(self.manifest.datapacks, DatapacksGroup(), dry)
        self.update_list(self.manifest.customs, CustomsGroup(), dry)
    
    def update_core(self, dry: bool):
        core = self.manifest.core
        cache = self.cache.check_core(core, self.manifest.mc_version)
        if not cache:
            return
        self.logger.info("💠 Checking core for updates")
        new_update: int | None = None
        if isinstance(cache.data, PaperCoreCache):
            api = papermc.PaperMcFill(self.session)
            mc = self.manifest.mc_version
            build = self.get_paper_build(api, core, mc)
            bn = cache.data.build_number
            if bn < build.id:
                new_update = build.id
        else:
            raise ValueError("Unknown core cache to update from")
        if not new_update:
            self.logger.info("No new core updates")
            return
        self.logger.info(f"💠 Found new core update: #{new_update}")
        if dry:
            self.logger.info(f"Dry mode enabled, not installing core update")
            return
        self.cache.invalidate_core()
        self.install_core()  


ROOT_REGISTRY = Registries()
CACHES_REGISTRY = ROOT_REGISTRY.create_model_registry(
    "asset_cache", FilesCache)
CACHES_REGISTRY.register_models(FilesCache, GithubReleaseCache,
                                GithubActionsCache, ModrinthCache,
                                PaperCoreCache, JenkinsCache)
FILE_SELECTORS = ROOT_REGISTRY.create_model_registry(
    "file_selectors", FileSelector)
FILE_SELECTORS.register_models(AllFilesSelector, SimpleJarSelector,
                               RegexFileSelector)
ASSETS = ROOT_REGISTRY.create_model_registry("assets", Asset)
ASSETS.register_models(ModrinthAsset, GithubReleasesAsset,
                          DirectUrlAsset, GithubActionsAsset,
                          JenkinsAsset, NoteAsset)

PROVIDERS = ROOT_REGISTRY.create_registry("providers", AssetProvider)
PROVIDERS.register("jenkins", JenkinsProvider())
PROVIDERS.register("url", DirectUrlProvider())
PROVIDERS.register("modrinth", ModrinthProvider())
PROVIDERS.register("github", GithubReleasesProvider())
PROVIDERS.register("github-actions", GithubActionsProvider())

LOG_FORMATTER = colorlog.ColoredFormatter(
    '%(log_color)s[%(asctime)s][%(name)s/%(levelname)s]: %(message)s',
    datefmt='%H:%M:%S',
    log_colors={
        "DEBUG": "light_cyan",
        "WARNING": "light_yellow",
        "ERROR": "light_red"
    }
)


def setup_logging(debug: bool):
    logger = logging.getLogger()  # Root logger
    # logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Create console handler
    console_handler = colorlog.StreamHandler(sys.stdout)

    console_handler.setFormatter(LOG_FORMATTER)

    # Avoid duplicate handlers if script is reloaded
    if not logger.handlers:
        logger.addHandler(console_handler)
    else:
        logger.handlers.clear()
        logger.addHandler(console_handler)


DEFAULT_MANIFEST_PATHS = ["manifest.json", "manifest.yml",
                          "manifest.yaml", "manifest.json5", "manifest.jsonc"]


def select_manifest_path(entered: Path | None) -> Path | None:
    if entered is not None:
        return entered
    else:
        for n in DEFAULT_MANIFEST_PATHS:
            p = Path(n)
            if p.is_file():
                return p
        return None

@click.group()
def main():
    """Minecraft Server Installer made by BoBkiNN"""
    pass


# Install lifecycle:
# Check cache -> download files -> do actions -> store cache

@main.command(help="Install server")
@click.option(
    "--manifest",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the manifest file",
)
@click.option(
    "--folder",
    type=click.Path(path_type=Path),
    default=Path(""),
    help="Folder where server is located",
)
@click.option(
    "--github-token",
    type=str,
    default=None,
    help="GitHub token for github assets",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Debug logging switch",
)
def install(manifest: Path | None, folder: Path, github_token: str | None, debug: bool):
    """Installs core and all assets by downloading them and executing actions"""
    setup_logging(debug)
    mfp = select_manifest_path(manifest)
    if not mfp:
        click.echo("No manifest.json found or passed")
        return
    logger = logging.getLogger("Installer")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    auth = Authorization(github=github_token)
    mf = Manifest.load(mfp, ROOT_REGISTRY, logger)
    installer = Installer(mf, mfp, folder, auth, debug, ROOT_REGISTRY, logger)
    installer.logger.info(f"✅ Using manifest {mfp}")
    installer.prepare(True)
    installer.install_core()
    installer.install_mods()
    installer.install_plugins()
    installer.install_datapacks()
    installer.install_customs()
    installer.show_notes()
    installer.shutdown()

# Update lifecycle:
# Check cache -> check update -> if (new update) {invalidate cache -> download files -> do actions} -> store cache

@main.command(help="Generate manifest schema")
@click.option(
    "--out", "-o",
    type=click.Path(path_type=Path),
    default="manifest_schema.json",
    help="Path where to store schema",
)
@click.option(
    "--pretty", "-p",
    is_flag=True,
    help="Indent schema by 2 spaces",
)
def schema(out: Path, pretty: bool):
    """Generates JSON schema for manifest and saves it"""
    click.echo(f"Generating schema to {out}")
    r = Manifest.model_json_schema(
        schema_generator=make_registry_schema_generator(ROOT_REGISTRY))
    out.write_text(json.dumps(r, indent=2 if pretty else None))
    click.echo("Done")


@main.command(help="Update server")
@click.option(
    "--manifest",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the manifest file",
)
@click.option(
    "--folder",
    type=click.Path(path_type=Path),
    default=Path(""),
    help="Folder where server is located",
)
@click.option(
    "--dry",
    is_flag=True,
    help="Dry mode. Checks for updates without installing them",
)
@click.option(
    "--github-token",
    type=str,
    default=None,
    help="GitHub token for github assets",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Debug logging switch",
)
def update(manifest: Path | None, folder: Path, dry: bool, github_token: str | None, debug: bool):
    """Checks cached assets for updates and installs new versions if dry mode disabled"""
    setup_logging(debug)
    mfp = select_manifest_path(manifest)
    if not mfp:
        click.echo("No manifest.json found or passed")
        return
    logger = logging.getLogger("Installer")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    auth = Authorization(github=github_token)
    mf = Manifest.load(mfp, ROOT_REGISTRY, logger)
    installer = Installer(mf, mfp, folder, auth, debug, ROOT_REGISTRY, logger)
    installer.logger.info(f"✅ Using manifest {mfp}")
    installer.prepare(False)
    installer.update_core(dry)
    installer.update_all(dry)
    installer.shutdown()

if __name__ == "__main__":
    main()
