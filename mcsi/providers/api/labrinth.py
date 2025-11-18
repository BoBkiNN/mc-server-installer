"""Modrinth API

Modrinth - https://modrinth.com/<br>
Labrinth - https://github.com/modrinth/code/tree/main/apps/labrinth
"""
from enum import Enum
from typing import TypeVar
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError
from requests import Session


class BuildInfo(BaseModel):
    comp_date: str
    git_hash: str
    profile: str

class ApiInfo(BaseModel):
    build_info: BuildInfo
    documentation: str
    name: str
    version: str

class Project(BaseModel):
    id: str
    slug: str
    title: str
    description: str
    versions: list[str]

class VersionType(Enum):
    RELEASE = "release"
    BETA = "beta"
    ALPHA = "alpha"


class ModrinthFile(BaseModel):
    hashes: dict[str, str]
    url: str
    filename: str
    primary: bool
    size: int

class Version(BaseModel):
    id: str
    project_id: str
    author_id: str
    name: str
    version_number: str
    featured: bool
    game_versions: list[str]
    version_type: VersionType
    downloads: int
    files: list[ModrinthFile]

    def get_primary(self):
        return next((f for f in self.files if f.primary), None)

def strlist(ls: list[str]):
    return "["+(",".join([f"\"{v}\"" for v in ls]))+"]"

M = TypeVar("M", bound=BaseModel)

class Modrinth:
    PRODUCTION_URL = "https://api.modrinth.com"
    STAGING_URL = "https://staging-api.modrinth.com"

    def __init__(self, session: Session | None = None, base_url: str = PRODUCTION_URL, user_agent: str = "BoBkiNN/py-modrinth-api") -> None:
        if session is None:
            self.session = Session()
            self.session.headers["User-Agent"] = user_agent
        else:
            self.session = session
        self.base_url = base_url.removesuffix("/")
    
    def _get_root(self, path: str, m: type[M]) -> M | None:
        fp = path.removeprefix("/")
        url = self.base_url + "/" + fp
        r = self.session.get(url)
        if r.status_code == 404:
            return None
        if not r.ok:
            raise ValueError(f"Response returned non-OK code ({r.status_code})")
        data = r.json()
        ret = m.model_validate(data)
        return ret
    
    def _get(self, path: str, m: type[M]) -> M | None:
        fp = path.removeprefix("/")
        url = self.base_url + "/v2/" + fp
        r = self.session.get(url)
        if r.status_code == 404:
            return None
        if not r.ok:
            raise ValueError(
                f"Response returned non-OK code ({r.status_code})")
        data = r.json()
        ret = m.model_validate(data)
        return ret
    
    def _get_list(self, path: str, m: type[M]) -> list[M] | None:
        fp = path.removeprefix("/")
        url = self.base_url + "/v2/" + fp
        r = self.session.get(url)
        if r.status_code == 404:
            return None
        if not r.ok:
            raise ValueError(
                f"Response returned non-OK code ({r.status_code})")
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"Expected json list but got {type(data)}")
        ret = []
        for i, v in enumerate(data):
            try:
                ret.append(m.model_validate(v))
            except ValidationError as e:
                raise ValueError(f"Failed to parse item {i}") from e
            i+=1
        return ret
    
    def get_project(self, id: str):
        return self._get(f"project/{id}", Project)
    
    def get_versions(self, project: str | Project, loaders: list[str] = [], 
                     game_versions: list[str] = []):
        id = project.id if isinstance(project, Project) else project
        path = f"/project/{id}/version"
        d = {}
        if loaders:
            d["loaders"] = strlist(loaders)
        if game_versions:
            d["game_versions"] = strlist(game_versions)
        if d:
            path += "?"+urlencode(d)
        return self._get_list(path, Version)
    
    def get_version(self, id: str):
        return self._get(f"/version/{id}", Version)
    
    def get_info(self):
        return self._get_root("", ApiInfo)

if __name__ == "__main__":
    modrinth = Modrinth(base_url=Modrinth.STAGING_URL)
    print(modrinth.get_info())
    print(modrinth.get_project("cVwGOfhs"))
    print(modrinth.get_versions("cVwGOfhs"))
