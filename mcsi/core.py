from enum import Enum
from pydantic import BaseModel
from registry import Registries
from dataclasses import dataclass
from abc import ABC, abstractmethod
from pathlib import Path
from model import Asset, FilesCache

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
