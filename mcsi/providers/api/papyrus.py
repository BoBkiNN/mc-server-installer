"""
Base URL - https://api.purpurmc.org/<br>
Source repo - https://github.com/PurpurMC/papyrus
"""
from pydantic import BaseModel, ValidationError
from enum import Enum
import requests
from requests import Session
from typing import TypeVar


class ErrorResponse(BaseModel):
    error: str


class BuildCommits(BaseModel):
    author: str
    email: str
    description: str
    hash: str
    timestamp: int


# org/purpurmc/papyrus/db/entity/Build.java#L107
class BuildResult(Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


EXPERIMENTAL_TYPE = "experimental"

class Build(BaseModel):
    project: str
    version: str
    build: str
    result: BuildResult
    timestamp: int
    duration: int
    commits: list[BuildCommits] = []
    metadata: dict[str, str] = {}
    md5: str | None = None

    @staticmethod
    def create_download_url(project: str, version: str, build: str, base: str):
        return f"{base}/v2/{project}/{version}/{build}/download"
    
    def get_download_url(self, api: "Papyrus"):
        return Build.create_download_url(self.project, self.version, self.build, api.base_url)
    
    def get_type(self):
        return self.metadata.get("type")
    
    def is_experimental(self):
        t = self.get_type()
        if t == EXPERIMENTAL_TYPE:
            return True
        return False
    
    def get_name(self):
        return f"{self.project}-{self.version}-{self.build}"

class ProjectsResponse(BaseModel):
    projects: list[str] = []

class VersionBuilds(BaseModel):
    latest: str | None = None
    all: list[str] = []

class DetailedVersionBuilds(BaseModel):
    latest: Build | None = None
    all: list[Build] = []


class ProjectVersion(BaseModel):
    project: str
    version: str
    builds: VersionBuilds | DetailedVersionBuilds

class Project(BaseModel):
    project: str
    metadata: dict[str, str] = {}
    versions: list[str] = []


class PapyrusError(Exception):
    pass

class PapyrusValidationError(PapyrusError):
    pass

M = TypeVar("M", bound=BaseModel)

class Papyrus:
    BASE_URL = "https://api.purpurmc.org/"

    def __init__(
        self,
        session: Session | None = None,
        base_url: str = BASE_URL,
        user_agent: str = "BoBkiNN/py-purpur-api"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        if session is None:
            # new session created
            self.session.headers.update({"User-Agent": user_agent})

    def list_projects(self) -> ProjectsResponse:
        url = f"{self.base_url}/v2"
        r = self.session.get(url)
        if r.status_code == 200:
            return self._parse_data(ProjectsResponse, r)
        raise self._handle_error(r)

    def get_project(self, project: str) -> Project | None:
        url = f"{self.base_url}/v2/{project}"
        r = self.session.get(url)
        if r.status_code == 200:
            return self._parse_data(Project, r)
        if r.status_code == 404:
            return None
        raise self._handle_error(r)

    def get_version(self, project: str, version: str, detailed: bool = False) -> ProjectVersion | None:
        url = f"{self.base_url}/v2/{project}/{version}"
        params = {}
        if detailed:
            params["detailed"] = "true"
        r = self.session.get(url, params=params)
        if r.status_code == 200:
            d = self._parse_data(ProjectVersion, r)
            if detailed and not isinstance(d.builds, DetailedVersionBuilds):
                raise ValueError("Requested detailed version but wrong returned")
            return d
        if r.status_code == 404:
            return None
        raise self._handle_error(r)

    def get_build(self, project: str, version: str, build: str) -> Build | None:
        url = f"{self.base_url}/v2/{project}/{version}/{build}"
        r = self.session.get(url)
        if r.status_code == 200:
            return self._parse_data(Build, r)
        if r.status_code == 404:
            return None
        raise self._handle_error(r)
    
    def _parse_data(self, t: type[M], res: requests.Response) -> M:
        try:
            return t.model_validate(res.json())
        except ValidationError as e:
            raise PapyrusValidationError(f"Failed to validate repsonse to {t.__name__}") from e

    def _handle_error(self, response: requests.Response) -> Exception:
        try:
            err = self._parse_data(ErrorResponse, response)
            msg = err.error or f"{response.status_code} {response.reason}"
        except Exception:
            msg = f"{response.status_code} {response.reason}"
        return PapyrusError(f"API request failed: {msg}")
