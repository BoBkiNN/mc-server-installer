from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional, TypeVar

from pydantic import AnyUrl, AwareDatetime, BaseModel, ValidationError
from requests import Response, Session


class Channel(Enum):
    ALPHA = 'ALPHA'
    BETA = 'BETA'
    STABLE = 'STABLE'
    RECOMMENDED = 'RECOMMENDED'


class Checksums(BaseModel):
    sha256: str


class Commit(BaseModel):
    message: str
    sha: str
    time: AwareDatetime


class Download(BaseModel):
    checksums: Checksums
    name: str
    size: int
    url: AnyUrl


class ErrorResponse(BaseModel):
    error: str
    message: str
    ok: str


class JavaFlags(BaseModel):
    recommended: list[str] = []


class JavaVersion(BaseModel):
    minimum: int


class Project(BaseModel):
    id: str
    name: str


class ProjectResponse(BaseModel):
    project: Project
    versions: dict[str, list[str]] = {}


class ProjectsResponse(BaseModel):
    projects: list[ProjectResponse] = []


class Status(Enum):
    SUPPORTED = 'SUPPORTED'
    DEPRECATED = 'DEPRECATED'
    UNSUPPORTED = 'UNSUPPORTED'


class Support(BaseModel):
    end: Optional[date] = None
    status: Status


class Build(BaseModel):
    channel: Channel
    commits: list[Commit] = []
    downloads: dict[str, Download]
    id: int
    time: AwareDatetime

    def get_default_download(self):
        return self.downloads["server:default"]


class Java(BaseModel):
    flags: JavaFlags = JavaFlags()
    version: JavaVersion


class Version(BaseModel):
    id: str
    java: Java
    support: Support


class VersionResponse(BaseModel):
    builds: list[int]
    version: Version

@dataclass
class ApiError(Exception):
    error: ErrorResponse

M = TypeVar("M", bound=BaseModel)

class PaperMcFill:
    BASE_URL = "https://fill.papermc.io/v3/"

    def __init__(self, session: Session | None = None, base_url: str = BASE_URL, user_agent: str = "BoBkiNN/papermc-fill") -> None:
        if session is None:
            self.session = Session()
            self.session.headers["User-Agent"] = user_agent
        else:
            self.session = session
        self.base_url = base_url.removesuffix("/")
    
    def _parse_error(self, r: Response):
        try:
            return ErrorResponse.model_validate(r.json())
        except Exception:
            return None

    def _get(self, path: str, m: type[M]) -> M | None:
        fp = path.removeprefix("/")
        url = self.base_url + "/" + fp
        r = self.session.get(url)
        if r.status_code == 404:
            return None
        if not r.ok:
            err = self._parse_error(r)
            if err:
                raise ApiError(err)
            raise ValueError(
                f"Response returned non-OK code ({r.status_code})")
        data = r.json()
        ret = m.model_validate(data)
        return ret

    def _get_list(self, path: str, m: type[M]) -> list[M] | None:
        fp = path.removeprefix("/")
        url = self.base_url + "/" + fp
        r = self.session.get(url)
        if r.status_code == 404:
            return None
        if not r.ok:
            err = self._parse_error(r)
            if err: 
                raise ApiError(err)
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
            i += 1
        return ret
    
    # routes
    
    def get_projects(self):
        return self._get_list(f"/projects/", ProjectResponse)

    def get_project(self, id: str):
        return self._get(f"/projects/{id}", ProjectResponse)

    def get_versions(self, project: str | Project):
        id = project.id if isinstance(project, Project) else project
        return self._get_list(f"/projects/{id}/versions", VersionResponse)

    def get_version(self, project: str | Project, id: str):
        p = project.id if isinstance(project, Project) else project
        return self._get(f"/projects/{p}/versions/{id}", VersionResponse)
    
    def get_builds(self, project: str | Project, version: str | Version):
        p = project.id if isinstance(project, Project) else project
        v = version.id if isinstance(version, Version) else version
        return self._get_list(f"/projects/{p}/versions/{v}/builds/", Build)
    
    def get_latest_build(self, project: str | Project, version: str | Version):
        p = project.id if isinstance(project, Project) else project
        v = version.id if isinstance(version, Version) else version
        return self._get(f"/projects/{p}/versions/{v}/builds/latest", Build)
    
    def get_build(self, project: str | Project, version: str | Version, id: int):
        p = project.id if isinstance(project, Project) else project
        v = version.id if isinstance(version, Version) else version
        return self._get(f"/projects/{p}/versions/{v}/builds/{id}", Build)

if __name__ == "__main__":
    api = PaperMcFill()
    # print(api.get_project("paper"))
    # print(api.get_version("paper", "1.21.8"))
    # print(api.get_latest_build("paper", "1.21.8"))
    print(api.get_build("paper", "1.21.8", 1))
