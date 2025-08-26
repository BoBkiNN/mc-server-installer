from pydantic import BaseModel, field_validator, Field, HttpUrl
from typing import Annotated, Union, Literal
import yaml
from pathlib import Path


class AssetProvider(BaseModel):
    pass # TODO fallback providers

class ModrinthProvider(AssetProvider):
    project_id: str
    type: Literal["modrinth"]

class GithubReleasesProvider(AssetProvider):
    repository: str
    type: Literal["github"]

class GithubActionsProvider(AssetProvider):
    """Downloads artifact from github actions"""
    repository: str
    branch: str = "master"
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
        file.read_text("utf-8")
        with open(file, "r", encoding="utf-8") as f:
            d = yaml.load(f, yaml.FullLoader)
        try:
            return Manifest.model_validate(d)
        except Exception as e:
            raise ValueError("Failed to load manifest") from e
    
