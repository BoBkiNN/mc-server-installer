import logging
import os
import sys
# Support vendored dependencies for zipapp builds
VENDOR_PATH = os.path.join(os.path.dirname(__file__), "_vendor")
if os.path.isdir(VENDOR_PATH) and VENDOR_PATH not in sys.path:
    sys.path.insert(0, VENDOR_PATH)

import click
import colorlog
from __version__ import __version__
from actions import (ActionHandler, DummyActionHandler, RenameActionHandler,
                     UnzipActionHandler)
from core import AssetProvider, Authorization, Environment, CoreProvider
from common_registries import CommonRegistries as CR
from installer import Installer
from model import *
from model import DummyAction, RenameFile, UnzipFile
from regunion import make_registry_schema_generator


ROOT_REGISTRY = Registries()

CACHES_REGISTRY = ROOT_REGISTRY.create_model_registry(CR.Asset.CACHE, 
                                                      FilesCache)
CACHES_REGISTRY.register_models(FilesCache)

FILE_SELECTORS = ROOT_REGISTRY.create_model_registry(
    CR.FILE_SELECTOR, FileSelector)
FILE_SELECTORS.register_models(AllFilesSelector, SimpleJarSelector,
                               RegexFileSelector)

ASSETS = ROOT_REGISTRY.create_model_registry(CR.Asset.MODEL, Asset)
ASSETS.register_models(NoteAsset)

ROOT_REGISTRY.create_registry(CR.Asset.PROVIDER, AssetProvider)
ROOT_REGISTRY.create_registry(CR.Core.PROVIDER, CoreProvider)

ACTIONS = ROOT_REGISTRY.create_model_registry(CR.Action.MODEL, BaseAction)
ACTIONS.register_models(DummyAction, RenameFile, UnzipFile)

ACTION_HANDLERS = ROOT_REGISTRY.create_registry(
    CR.Action.HANDLER, ActionHandler)
ACTION_HANDLERS.register("dummy", DummyActionHandler())
ACTION_HANDLERS.register("rename", RenameActionHandler())
ACTION_HANDLERS.register("unzip", UnzipActionHandler())

ROOT_REGISTRY.create_model_registry(CR.Core.MODEL, CoreManifest)
ROOT_REGISTRY.create_model_registry(CR.Core.CACHE, CoreCache)

def load_providers(env: Environment):
    from providers import (direct_url, github_provider, jenkins_provider,
                           modrinth, paper, bungeecord)
    ls = [direct_url, modrinth, github_provider, jenkins_provider, paper, bungeecord]
    for m in ls:
        m.setup(env.registries, env)


def display_id_conflicts(logger: logging.Logger, ls: list[AssetConflict]):
    if not ls:
        return
    t = "\n".join([f" - {c}" for c in ls])
    logger.warning(
        f"Detected {len(ls)} asset_id conflict(s). Second asset will replace first:\n{t}")


def display_registry_stats(logger: logging.Logger, reg: Registries):
    providers = reg.get_registry(AssetProvider)
    pls = []
    if providers:
        pls = [p for p in providers.keys()]
    logger.debug(f"Registered {len(pls)} providers: {pls}")


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
@click.option(
    "--profile",
    type=str,
    default=DEFAULT_PROFILE,
    help="Server profile to use",
)
def install(manifest: Path | None, folder: Path, github_token: str | None,
            debug: bool, profile: str):
    """Installs core and all assets by downloading them and executing actions"""
    setup_logging(debug)
    mfp = select_manifest_path(manifest)
    if not mfp:
        click.echo("No manifest.json found or passed")
        return
    logger = logging.getLogger("Installer")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    auth = Authorization(github=github_token)
    env = Environment(auth, profile, ROOT_REGISTRY, debug, folder)
    load_providers(env)
    display_registry_stats(logger, env.registries)
    mf, cls = Manifest.load(mfp, ROOT_REGISTRY, logger)
    display_id_conflicts(logger, cls)
    installer = Installer(mf, mfp, folder, env, logger)
    installer.logger.info(f"✅ Using manifest {mfp} with profile {profile!r}")
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
    env = Environment(Authorization(), DEFAULT_PROFILE, ROOT_REGISTRY, False, Path())
    load_providers(env)
    r = Manifest.model_json_schema(
        schema_generator=make_registry_schema_generator(env.registries))
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
@click.option(
    "--profile",
    type=str,
    default=DEFAULT_PROFILE,
    help="Server profile to use",
)
def update(manifest: Path | None, folder: Path, dry: bool, github_token: str | None,
           debug: bool, profile: str):
    """Checks cached assets for updates and installs new versions if dry mode disabled"""
    setup_logging(debug)
    mfp = select_manifest_path(manifest)
    if not mfp:
        click.echo("No manifest.json found or passed")
        return
    logger = logging.getLogger("Installer")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    auth = Authorization(github=github_token)
    env = Environment(auth, profile, ROOT_REGISTRY, debug, folder)
    load_providers(env)
    display_registry_stats(logger, env.registries)
    mf, cls = Manifest.load(mfp, ROOT_REGISTRY, logger)
    display_id_conflicts(logger, cls)
    installer = Installer(mf, mfp, folder, env, logger)
    installer.logger.info(f"✅ Using manifest {mfp}")
    installer.prepare(False)
    installer.update_core(dry)
    installer.update_all(dry)
    installer.shutdown()


@main.command(help="Dump registries")
@click.option(
    "--out",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("registries.json"),
    help="Output file",
)
def dump(out: Path):
    d = {}
    env = Environment(Authorization(), DEFAULT_PROFILE, ROOT_REGISTRY, False, Path())
    load_providers(env)
    env.registries.dump(d)
    out.write_text(json.dumps(d, indent=2), "utf-8")
    click.echo(f"Registries dumped to {out}")


if __name__ == "__main__":
    main()
