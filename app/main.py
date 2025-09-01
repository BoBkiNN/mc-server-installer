import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from zipfile import ZipFile
from typing import Sequence

import click
import colorlog
import modrinth
import papermc_fill as papermc
import requests
import tqdm
from github import Auth, Github, UnknownObjectException
from github.Artifact import Artifact
from github.GitRelease import GitRelease
from github.Repository import Repository
from github.Workflow import Workflow
from github.WorkflowRun import WorkflowRun
from asteval import Interpreter
from asteval.astutils import ExceptionHolder
from model import *
from model import FilesCache
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
                    lines.append(f"{k}.{loc} {msg} [type={type_}, input={inp}]")
                t = "\n".join(lines)
                self.logger.warning(f"Failed to load asset cache entry {k}: \n{t}")
            self.logger.debug(f"Loaded cache with {len(self.cache.assets)} assets")
        except Exception as e:
            self.logger.error("Exception loading stored cache. Resetting", exc_info=e)
            self.reset()
            return
        if self.cache.mc_version and self.cache.mc_version != self.mf.mc_version:
            self.logger.info(f"Resetting cache due to changed minecraft version {self.cache.mc_version} -> {self.mf.mc_version}")
            self.reset()
        abs_folder = self.folder.resolve()
        if self.cache.server_folder != abs_folder:
            self.logger.warning(f"Server folder differs from cache: {self.cache.server_folder} -> {abs_folder}")
            self.logger.warning("This might mean that all cached data is invalid in current new location, so resetting")
            self.reset()
    
    def invalidate_asset(self, asset: str | AssetManifest, reason: InvalidReason | None = None):
        id = asset.resolve_asset_id() if isinstance(asset, AssetManifest) else asset
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
    
    def check_asset(self, asset: str | AssetManifest, hash: str | None):
        """Returns None if cache is invalid"""
        id = asset.resolve_asset_id() if isinstance(asset, AssetManifest) else asset
        entry = self.cache.assets.get(id, None)
        if not entry:
            return None
        self.logger.debug(f"Checking cached asset {entry.asset_id}({entry.asset_hash}) with actual {hash}")
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
            self.logger.debug(f"Invalidating core due to changed type {cached.type} -> {core.type}")
            self.invalidate_core()
            return None
        vhash = core.hash_from_ver(mc_ver)
        if vhash is not None and cached.version_hash != vhash:
            # hash is provided and not matching
            self.logger.debug(f"Invalidating core due to changed version hash {cached.version_hash} -> {vhash}")
            self.invalidate_core()
            return None
        return cached


class Authorizaition(BaseModel):
    github: str | None = None

@dataclass
class DownloadOptions:
    asset: AssetManifest
    version: str | None
    folder: Path
    selector: FileSelector

    def require_version(self, provider: Provider):
        if self.version is None:
            raise ValueError(f"provider {provider.type} requires version to be specified in manifest")
        return self.version

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

    def create_cache(self) -> FilesCache:
        return ModrinthCache(files=self.files, version_id=self.version.id, version_number=self.version.version_number)


def is_valid_path(path_str: str) -> bool:
    try:
        Path(path_str)  # will raise ValueError if fundamentally broken
    except ValueError:
        return False

    if os.name == "nt":  # Windows-specific rules
        # forbidden characters
        if re.search(r'[<>:"/\\|?*]', path_str):
            return False

        # reserved names (case-insensitive)
        reserved = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        name = Path(path_str).stem.upper()
        if name in reserved:
            return False

        # # path length (WinAPI limit is 260 by default)
        # if len(path_str) >= 260:
        #     return False

    else:  # POSIX
        # only forbidden character is null byte
        if "\x00" in path_str:
            return False

    return True

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

    def check_path(self, text: str) -> Path | None:
        if is_valid_path(text):
            return Path(text)
        else:
            return None
        
    def handle(self, key: str, action: BaseAction, data: DownloadData):
        # TODO return bool or enum stating error or ok
        if_code = action.if_
        if if_code:
            v = self.eval(if_code, key+".if", str(if_code))
            if isinstance(v, ExceptionHolder):
                self.logger.error("Failed to process if statement, see above errors for details")
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
            if not b:
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
                folder = self.eval_template(action.folder, key+".folder", action.folder.root)
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
    
    def process(self, asset: AssetManifest, data: DownloadData):
        ls = asset.actions
        if not ls:
            return data
        self.intpr.symtable["data"] = data
        self.intpr.symtable["d"] = data
        self.intpr.symtable["asset"] = asset
        self.intpr.symtable["a"] = asset
        for n in ["data", "d", "asset", "a"]:
            self.intpr.readonly_symbols.add(n)
        ak = asset.get_manifest_group()+"."+asset.resolve_asset_id()
        for i, a in enumerate(ls):
            key = f"{ak}.actions[{i}]"
            try:
                self.handle(key, a, data)
            except Exception as e:
                self.logger.error(f"Failed to handle action {type(a)} at {key}", exc_info=e)
            

class AssetInstaller:
    def __init__(self, manifest: Manifest, auth: Authorizaition, temp_folder: Path, logger: logging.Logger, session: requests.Session) -> None:
        self.game_version = manifest.mc_version
        self.auth = auth
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
        self.github = Github(auth=Auth.Token(auth.github) if auth.github else None, user_agent=user_agent)
        self.modrinth = modrinth.Modrinth(session)
        self.temp_files: list[Path] = []
        self.repo_cache: dict[str, Repository] = {}
    
    def info(self, msg: object):
        self.logger.info(msg)

    def debug(self, msg: object):
        self.logger.debug(msg)
    
    def get_repo(self, name: str):
        if name in self.repo_cache:
            return self.repo_cache[name]
        try:
            repo = self.github.get_repo(name)
        except UnknownObjectException:
            raise ValueError(f"Unknown repository {name}")
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
        session.headers.update(self.session.headers)
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
        version = options.require_version(provider)
        if version == "latest":
            release = repo.get_latest_release()
        else:
            release = repo.get_release(version)
        self.info(f"✅ Found release {release.title}")
        assets = release.get_assets()
        m = {a.name: a for a in assets}
        names = options.selector.find_targets(list(m))
        files: list[Path] = []
        for k, v in m.items():
            if k not in names: continue
            outPath = options.folder / k
            self.info(f"🌐 Downloading artifact {k} to {outPath}..")
            self.download_github_file(v.url, outPath, True)
            # v.download_asset(str(outPath.resolve())) # type: ignore
            files.append(outPath)
        self.info(f"✅ Downloaded {len(files)} assets from release")
        return GithubReleaseData(repo, release, files=files)
    
    def download_github_actions(self, provider: GithubActionsProvider, 
                                         options: DownloadOptions):
        repo = self.get_repo(provider.repository)
        workflow = repo.get_workflow(provider.workflow)
        runs = workflow.get_runs(branch=provider.branch)  # type: ignore
        run: WorkflowRun | None
        version = options.require_version(provider)
        if version == "latest":
            run = runs[0]
        else:
            number = int(version)
            run = next((r for r in runs if r.run_number == number), None)
        if run is None:
            raise ValueError("No run found")
        ls = run.get_artifacts()
        artifacts: list[Artifact] = []
        if not provider.name_pattern:
            artifacts = [a for a in ls]
        else:
            artifacts = [a for a in ls if provider.name_pattern.search(a.name)]
        if len(artifacts) == 0:
            raise ValueError(f"⚠ No artifacts found in run {run.id}")
        files: list[Path] = []
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
                    files.append(options.folder / name)
                    c += 1
            self.info(f"✅ Extracted {c} files from artifact {artifact.name}")
            self.remove_temp_file(tmp)
        return GithubActionsData(repo, workflow, run, files=files)
    
    def download_modrinth(self, provider: ModrinthProvider, options: DownloadOptions):
        if provider.version_is_id:
            options.require_version(provider)
        self.debug(f"Getting project {provider.project_id}")
        project = self.modrinth.get_project(provider.project_id)
        if not project:
            raise ValueError(f"Unknown project {provider.project_id}")
        self.debug(f"Getting project versions..")
        game_versions = [] if provider.ignore_game_version else [self.game_version]
        vers = self.modrinth.get_versions(provider.project_id, ["spigot", "paper"], game_versions)
        if not vers:
            raise ValueError(f"Cannot find versions for project {provider.project_id}")
        name_pattern = provider.version_name_pattern
        filtered: list[modrinth.Version] = []
        self.debug(f"Got {len(vers)} versions from {project.title}")
        for ver in vers:
            # ignoring mc version currently
            if provider.channel and provider.channel != ver.version_type:
                continue
            if provider.version_is_id and options.version != ver.id:
                continue
            if name_pattern and not name_pattern.search(ver.name):
                continue
            filtered.append(ver)
        if len(filtered) == 0:
            raise ValueError("No valid versions found")
        def download_version(ver: modrinth.Version):
            # TODO use_primary and file_name_pattern properties here to return multiple files
            primary = ver.get_primary()
            if not primary:
                self.logger.warning(
                    f"⚠ No primary file in version '{ver.name}'")
                return None
            out = options.folder / primary.filename
            self.info(
                f"🌐 Downloading primary file {primary.filename} from version '{ver.name}'")
            self.download_file(self.modrinth.session, str(primary.url), out)
            return [out]
        if options.version == "latest":
            ver = filtered[0]
            files = download_version(ver)
            if files:
                self.info(f"✅ Downloaded latest version {ver.name}")
                return ModrinthData(ver, files=files)
            else:
                raise ValueError(f"Failed to download version {ver.name}. See errors above for details")
        # at this moment version is not latest and not an version id, so this is version_number
        ver = next((v for v in filtered if v.version_number == options.version), None)
        if not ver:
            raise ValueError(f"Failed to find valid version with number {options.version} out of {len(filtered)}")
        files = download_version(ver)
        if not files:
            raise ValueError(f"No valid files found in version {ver}")
        self.info(f"✅ Downloaded {len(files)} files")
        return ModrinthData(ver, files=files)
    
    def download_direct_url(self, provider: DirectUrlProvider,
                                options: DownloadOptions):
        if provider.file_name:
            name = provider.file_name
        else:
            path = provider.url.path
            if not path:
                name = options.asset.resolve_asset_id()
            else:
                name = path.split("/")[-1]
        out = options.folder / name
        self.download_file(requests.Session(), str(provider.url), out)
        return DownloadData(files=[out], primary_file=out)


class Installer:
    def __init__(self, manifest: Manifest, manifest_path: Path, 
                 server_folder: Path, auth: Authorizaition, debug: bool,
                 registries: Registries) -> None:
        self.registries = registries
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.folder = server_folder
        self.auth = auth
        self.logger = logging.getLogger("Installer")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "BoBkiNN/mc-server-installer"
        self.cache = CacheStore(self.folder / ".install_cache.json", manifest, self.registries, debug, self.folder)
        self.assets = AssetInstaller(self.manifest, auth, self.folder / "tmp", self.logger, self.session)
        self.exprs = ExpressionProcessor(logging.getLogger("Expr"), self.folder)
        self.mods_folder = self.folder / "mods"
        self.plugins_folder = self.folder / "plugins"
    
    def prepare(self):
        self.cache.load(self.registries)
        self.cache.check_all_assets(self.manifest)
        self.logger.info(
            f"✅ Prepared installer for MC {self.manifest.mc_version}")

    def shutdown(self):
        self.cache.save()
        self.assets.clear_temp()
        self.session.close()
    
    def install_paper_core(self, core: PaperCoreManifest) -> CoreCache:
        api = papermc.PaperMcFill(self.session)
        mc = self.manifest.mc_version
        build: papermc.Build | None
        if core.build == PaperLatestBuild.LATEST:
            build = api.get_latest_build("paper", mc)
        elif core.build == PaperLatestBuild.LATEST_STABLE:
            builds = api.get_builds("paper", mc)
            if builds == None:
                build = None
            else:
                build = next((b for b in builds if b.channel == PaperChannel.STABLE), None)
        elif core.channels:
            builds = api.get_builds("paper", mc)
            if builds == None:
                build = None
            else:
                build = next((b for b in builds if b.channel in core.channels), None)
        else:
            build = api.get_build("paper", mc, core.build)
        if build is None:
            raise ValueError(f"Failed to find paper build {core.build} for MC {mc}")
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
    
    def download_asset(self, provider: Provider, options: DownloadOptions):
        d_data: DownloadData
        if isinstance(provider, GithubReleasesProvider):
            d_data = self.assets.download_github_release(provider, options)
        elif isinstance(provider, GithubActionsProvider):
            d_data = self.assets.download_github_actions(provider, options)
        elif isinstance(provider, ModrinthProvider):
            d_data = self.assets.download_modrinth(provider, options)
        elif isinstance(provider, DirectUrlProvider):
            d_data = self.assets.download_direct_url(provider, options)
        else:
            raise ValueError(f"Unsupported provider {type(provider)}")
        return d_data
    
    def install(self, asset: AssetManifest) -> AssetCache:
        provider = asset.provider
        asset_id = asset.resolve_asset_id()
        asset_hash = asset.stable_hash()
        cached = self.cache.check_asset(asset_id, asset_hash) if asset.caching else None
        if cached:
            self.logger.info(f"⏩ Skipping {asset.type.value} '{asset_id}' as it already installed")
            return cached
        
        self.logger.info(f"🔄 Downloading {asset.type.value} {asset_id}")
        asset_folder = asset.get_base_folder()
        target_folder = asset_folder if asset_folder.is_absolute() else self.folder / asset_folder
        selector = provider.create_file_selector(self.registries)
        options = DownloadOptions(asset, asset.version, target_folder, selector)
        if not target_folder.exists():
            target_folder.mkdir(parents=True, exist_ok=True)
        try:
            d_data = self.download_asset(provider, options)
        except Exception as e:
            raise ValueError(f"Exception downloading asset {asset_id}") from e
        d_data.files = [p.relative_to(
            self.folder) if not p.is_absolute() else p for p in d_data.files]
        
        self.exprs.process(asset, d_data)

        cache = d_data.create_cache()
        result = AssetCache.create(asset_id, asset_hash, millis(), cache)
        if asset.caching:
            self.cache.store_asset(result)
        return result
    
    def install_list(self, ls: Sequence[AssetManifest], entry_name: str):
        if not ls: return
        total = len(ls)
        self.logger.info(f"🔄 Installing {total} {entry_name}(s)")
        failed: list[AssetManifest] = []
        for a in ls:
            key = a.resolve_asset_id()
            try:
                self.install(a)
            except Exception as e:
                self.logger.error(f"Exception installing asset {key!r}", exc_info=e)
                failed.append(a)
                continue
            self.cache.save()
        
        if failed:
            self.logger.info(f"⚠ Installed {len(failed)}/{total} {entry_name}(s)")
        else:
            self.logger.info(f"✅ Installed {total}/{total} {entry_name}(s)")
    
    def install_mods(self):
        self.install_list(self.manifest.mods, "mod")
    
    def install_plugins(self):
        self.install_list(self.manifest.mods, "plugin")
    
    def install_datapacks(self):
        self.install_list(self.manifest.mods, "datapack")
    
    def install_customs(self):
        self.install_list(self.manifest.mods, "custom asset")


ROOT_REGISTRY = Registries()
CACHES_REGISTRY = ROOT_REGISTRY.create_model_registry("asset_cache", FilesCache)
CACHES_REGISTRY.register_model(FilesCache)
CACHES_REGISTRY.register_model(GithubReleaseCache)
CACHES_REGISTRY.register_model(GithubActionsCache)
CACHES_REGISTRY.register_model(ModrinthCache)
CACHES_REGISTRY.register_model(PaperCoreCache)
FILE_SELECTORS = ROOT_REGISTRY.create_model_registry("file_selectors", FileSelector)
FILE_SELECTORS.register_model(AllFilesSelector)
FILE_SELECTORS.register_model(SimpleJarSelector)
FILE_SELECTORS.register_model(RegexFileSelector)


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
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Create console handler
    console_handler = colorlog.StreamHandler(sys.stdout)

    console_handler.setFormatter(LOG_FORMATTER)

    # Avoid duplicate handlers if script is reloaded
    if not logger.handlers:
        logger.addHandler(console_handler)
    else:
        logger.handlers.clear()
        logger.addHandler(console_handler)

DEFAULT_MANIFEST_PATHS = ["manifest.json", "manifest.yml", "manifest.yaml", "manifest.json5", "manifest.jsonc"]


@click.group()
def main():
    pass

@main.command()
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
    help="GitHub token for github providers",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Debug logging switch",
)
def install(manifest: Path | None, folder: Path, github_token: str | None, debug: bool):
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
            click.echo("No manifest.json found or passed")
            return
    
    auth = Authorizaition(github=github_token)
    mf = Manifest.load(mfp, ROOT_REGISTRY)
    installer = Installer(mf, mfp, folder, auth, debug, ROOT_REGISTRY)
    installer.logger.info(f"✅ Using manifest {mfp}")
    installer.prepare()
    installer.install_core()
    installer.install_mods()
    installer.install_plugins()
    installer.install_datapacks()
    installer.install_customs()
    installer.shutdown()

@main.command
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
    click.echo(f"Generating schema to {out}")
    r = Manifest.model_json_schema(schema_generator=make_registry_schema_generator(ROOT_REGISTRY))
    out.write_text(json.dumps(r, indent=2 if pretty else None))
    click.echo("Done")


if __name__ == "__main__":
    main()