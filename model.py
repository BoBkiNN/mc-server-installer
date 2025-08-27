from pydantic import BaseModel, field_validator, Field, HttpUrl
from typing import Annotated, Union, Literal
import yaml, json5
from pathlib import Path
from modrinth import VersionType


class AssetProvider(BaseModel):
    pass # TODO fallback providers
    # TODO method to get asset id

class ModrinthProvider(AssetProvider):
    project_id: str
    channel: VersionType | None = None
    """If not set, then channel is ignored"""
    version_is_id: bool = False
    """If true, than version is consumed as version id"""
    version_name_pattern: str | None = None
    """RegEx for version name"""
    type: Literal["modrinth"]

class GithubReleasesProvider(AssetProvider):
    repository: str
    type: Literal["github"]

class GithubActionsProvider(AssetProvider):
    """Downloads artifact from github actions"""
    repository: str
    branch: str = "master"
    workflow: str
    name_pattern: str | None = None
    """RegEx for artifact name. All artifacts is downloaded if not set"""
    type: Literal["github-actions"]

class DirectUrlProvider(AssetProvider):
    url: HttpUrl
    type: Literal["url"]

Provider = Annotated[
    Union[ModrinthProvider, GithubReleasesProvider,
          DirectUrlProvider, GithubActionsProvider],
    Field(discriminator="type"),
]

class ModManifest(BaseModel):
    provider: Provider
    version: str

class PluginManifest(BaseModel):
    provider: Provider
    version: str

class DatapackManifest(BaseModel):
    provider: Provider

class Manifest(BaseModel):
    mc_version: str
    paper_build: str | int

    mods: list[ModManifest]
    plugins: list[PluginManifest]
    datapacks: list[DatapackManifest]

    @field_validator('paper_build')
    @classmethod
    def ensure_foobar(cls, v: str | int):
        if isinstance(v, str) and v != "latest":
            raise ValueError("paper_build must be number or 'latest'")
        return v
    
    
    @staticmethod
    def load(file: Path) -> "Manifest":
        ext = file.name.split(".")[-1]
        with open(file, "r", encoding="utf-8") as f:
            if ext in ["json", "yml", "yaml"]:
                d = yaml.load(f, yaml.FullLoader)
            elif ext in ["json5", "jsonc"]:
                d = json5.load(f, encoding="utf-8")
            else:
                raise ValueError(f"Cannot find loader for manifest extension {ext}")
        try:
            return Manifest.model_validate(d)
        except Exception as e:
            raise ValueError("Failed to load manifest") from e
    
