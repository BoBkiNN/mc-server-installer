from pathlib import Path
from typing import Sequence

import utils
from actions import ExpressionProcessor
from core import *
from model import *
from utils import millis


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


class Installer:
    def __init__(self, manifest: Manifest, manifest_path: Path,
                 server_folder: Path, env: Environment, logger: logging.Logger) -> None:
        self.registries = env.registries
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.folder = server_folder
        self.debug = env.debug
        self.env = env
        self.logger = logger
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "BoBkiNN/mc-server-installer"
        self.cache = CacheStore(
            self.folder / ".install_cache.json", manifest, self.env, self.folder)
        self.assets = AssetInstaller(
            self.manifest, env, self.folder / "tmp", self.logger, self.session)
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
        self.cache.load()
        if validate:
            self.cache.check_all_assets(self.manifest)
        self.logger.info(
            f"✅ Prepared installer for MC {self.manifest.mc_version}")

    def shutdown(self):
        self.cache.save()
        self.assets.clear_temp()
        self.session.close()

    def install_core(self):
        core = self.manifest.core
        cache = self.cache.check_core(core)
        if cache:
            self.logger.info(f"⏩ Skipping core as it already installed")
            return cache
        core_type = core.get_type()
        core_provider: CoreProvider[CoreManifest, CoreCache] | None = self.registries.get_entry(CoreProvider, core_type)
        if not core_provider:
            raise ValueError(f"Unknown core provider {core_type!r}")
        self.logger.info(f"🔄 Downloading core {core.display_name()}..")
        cache = core_provider.download(self.assets, core)
        self.logger.info(f"✅ Installed core {cache.display_name()}")
        self.cache.store_core(cache)

    def download_asset(self, asset: Asset, group: AssetsGroup):
        provider = self.registries.get_entry(AssetProvider, asset.get_type())
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
            raise utils.FriendlyException(f"Asset download failed: {e}")
        except Exception as e:
            raise ValueError(f"Exception downloading asset {asset_id}") from e
        data.files = [p.relative_to(
            self.folder) if not p.is_absolute() else p for p in data.files]

        if asset.actions:
            logger = logging.getLogger("Expr#"+asset_id)
            logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
            exprs = ExpressionProcessor(logger, self.folder, self.env)
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
                exprs = ExpressionProcessor(logger, self.folder, self.env)
                v = exprs.eval_if(ak+".if", if_code)
                if v is False:
                    self.logger.debug(f"Skipping asset {asset_id!r} because its condition computed to False")
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
            self.logger.info(
                f"🔄 Installing {total} {entry_name}(s) ({excluded} excluded)")
        else:
            self.logger.info(f"🔄 Installing {total} {entry_name}(s)")
        failed: list[Asset] = []
        cached: list[Asset] = []
        for a in ls:
            key = a.resolve_asset_id()
            if isinstance(a, NoteAsset):
                self.install_notes[key] = a.note, group
                self.logger.info(
                    f"🚩 {entry_name.capitalize()} {key} requires manual installation. See reason at end of installation")
                continue
            try:
                _, is_cached = self.install(a, group)
            except utils.FriendlyException as e:
                if self.debug:
                    self.logger.error(
                        f"Exception installing asset {key!r}: {e}", exc_info=e)
                else:
                    self.logger.error(
                        f"Exception installing asset {key!r}: {e}")
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
        self.logger.info(
            f"🚩 You have {len(d)} note(s) from assets thats need to be downloaded manually")
        self.logger.info(
            "🚩 You can ignore this messages if you installed them.")
        for (v, g) in d.values():
            entry_name = g.unit_name
            self.logger.info(f"🚩 {entry_name.capitalize()}: {v}")

    def check_update(self, asset: Asset, group: AssetsGroup, cached: AssetCache) -> UpdateStatus:
        key = asset.get_type()
        provider = self.registries.get_entry(AssetProvider, key)
        if not provider:
            raise ValueError(f"Unknown provider {key!r}")
        try:
            return provider.has_update(self.assets, asset, group, cached.data)
        except NotImplementedError as e:
            raise ValueError(
                f"Provider {key!r} do not implemented update checking")
        except utils.FriendlyException as e:
            raise utils.FriendlyException(
                f"Exception checking update: {e}") from e
        except Exception as e:
            raise ValueError(
                f"Exception checking update for {group.unit_name} {asset.resolve_asset_id()}") from e

    def update_lifecycle(self, asset: Asset, group: AssetsGroup, dry: bool) -> UpdateResult:
        """
        Returns True if successfully installed and False if not <br>
        Returns None if no updates available or dry run
        """
        key = asset.get_type()
        provider = self.registries.get_entry(AssetProvider, key)
        if not provider:
            raise ValueError(f"Unknown provider {key!r}")
        if not provider.supports_update_checking():
            self.logger.debug(
                f"Provider {key!r} does not support update checking.")
            return UpdateResult.SKIPPED
        asset_id = asset.resolve_asset_id()
        asset_hash = asset.stable_hash()
        cached = self.cache.check_asset(
            asset_id, asset_hash) if asset.caching else None
        if not cached:
            return UpdateResult.SKIPPED
        self.logger.info(f"🔁 Checking {asset_id} for updates")
        status = self.check_update(asset, group, cached)
        if status != UpdateStatus.OUTDATED:
            self.logger.info(f"💠 No new updates for {asset_id}")
            return UpdateResult.UP_TO_DATE
        self.logger.info(
            f"💠 New update found for {group.unit_name} {asset_id}")
        if asset.is_latest() is False:  # fixed version
            self.logger.debug(
                f"Skipping {group.unit_name} {asset_id} as it has fixed version")
            return UpdateResult.FOUND
        if dry:
            self.logger.debug("Dry run, do not installing update")
            return UpdateResult.FOUND
        self.cache.invalidate_asset(asset, reason=InvalidReason(
            "outdated", "New version is found"))
        try:
            self.install(asset, group)
        except Exception as e:
            self.logger.error(
                f"❌ Failed to install update for {group.unit_name} {asset_id}", exc_info=e)
            return UpdateResult.FAILED
        self.cache.save()
        return UpdateResult.UPDATED

    def update_list(self, assets: Sequence[Asset], group: AssetsGroup, dry: bool):
        filtered: list[Asset] = []
        for asset in self.prepare_asset_list(assets, group):
            asset_id = asset.resolve_asset_id()
            if not asset.caching:
                self.logger.debug(
                    f"Skipping asset {asset_id} as it does caching disabled")
                continue
            if asset.is_latest() is None:
                self.logger.debug(
                    f"Skipping asset {asset_id} as it has fixed version")
            filtered.append(asset)
        if len(filtered) == 0 and len(assets) == 0:
            return
        fd = len(assets) - len(filtered)
        if fd > 0:
            self.logger.info(f"No update checking for {group.unit_name}(s) required: filtered {fd} asset(s)")
            return
        self.logger.info(
            f"💠 Checking updates for {len(filtered)} {group.unit_name}(s)")
        results: dict[str, UpdateResult] = {}

        for asset in filtered:
            asset_id = asset.resolve_asset_id()
            if isinstance(asset, NoteAsset):
                results[asset_id] = UpdateResult.SKIPPED
                continue
            try:
                r = self.update_lifecycle(asset, group, dry)
            except Exception as e:
                if isinstance(e, utils.FriendlyException) and not self.debug:
                    self.logger.error(
                        f"❌ Failed to complete update lifecycle for {group.unit_name} {asset_id}: {e}")
                else:
                    self.logger.error(
                        f"❌ Failed to complete update lifecycle for {group.unit_name} {asset_id}", exc_info=e)
                results[asset_id] = UpdateResult.FAILED
                continue
            results[asset_id] = r
        up_to_date = sum((1 for r in results.values()
                         if r == UpdateResult.UP_TO_DATE))
        updated = sum((1 for r in results.values()
                      if r == UpdateResult.UPDATED))
        failed = sum((1 for r in results.values() if r == UpdateResult.FAILED))
        found = sum((1 for r in results.values() if r == UpdateResult.FOUND))
        self.logger.info(
            f"✅ Completed update check for {group.unit_name}s.\n ✅ No updates: {up_to_date}.\n ✅ Updated: {updated} (found {found}).\n ❌ Failed: {failed}")

    def update_all(self, dry: bool):
        self.update_list(self.manifest.mods, ModsGroup(), dry)
        self.update_list(self.manifest.plugins, PluginsGroup(), dry)
        self.update_list(self.manifest.datapacks, DatapacksGroup(), dry)
        self.update_list(self.manifest.customs, CustomsGroup(), dry)

    def update_core(self, dry: bool):
        core = self.manifest.core
        cached = self.cache.check_core(core)
        if not cached:
            return
        if core.is_latest() is None:
            self.logger.debug(
                f"Skipping core update checking as it has fixed version")
            return
        self.logger.info("🔁 Checking core for updates")
        core_type = core.get_type()
        core_provider = self.registries.get_entry(CoreProvider, core_type)
        if not core_provider:
            raise ValueError(f"Unknown core provider {core_type!r}")
        status: UpdateStatus
        new_ver: str
        try:
            status, new_ver = core_provider.has_update(self.assets, core, cached)
        except Exception as e:
            self.logger.error(f"Failed to check {core_type!r} core for updates", exc_info=e)
            return
        if status == UpdateStatus.UP_TO_DATE or status == UpdateStatus.AHEAD:
            self.logger.info("No new core updates")
            return
        old_ver = cached.version_name()
        self.logger.info(f"💠 Found new core update: {old_ver} -> {new_ver}")
        if dry:
            self.logger.info(f"Dry mode enabled, not installing core update")
            return
        self.cache.invalidate_core()
        self.install_core()
