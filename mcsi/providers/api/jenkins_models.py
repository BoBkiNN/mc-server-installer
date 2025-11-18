from pydantic import BaseModel, HttpUrl
from enum import Enum
import jenkins

class Artifact(BaseModel):
    displayPath: str
    fileName: str
    relativePath: str

class BuildReference(BaseModel):
    # hudson.model.FreeStyleBuild
    number: int
    url: HttpUrl

class Result(Enum):
    # https://javadoc.jenkins.io/hudson/model/Result.html
    SUCCESS = "SUCCESS"
    UNSTABLE = "UNSTABLE"
    FAILURE = "FAILURE"
    NOT_BUILT = "NOT_BUILT"
    ABORTED = "ABORTED"

    def is_complete_build(self):
        return self == Result.SUCCESS or self == Result.UNSTABLE

class Build(BuildReference):
    duration: int
    fullDisplayName: str
    id: str
    result: Result
    timestamp: int
    artifacts: list[Artifact] = []

    @staticmethod
    def get_build(j: jenkins.Jenkins, job: str, number: int):
        try:
            js = j.get_build_info(job, number)
        except jenkins.JenkinsException as e:
            if "not exist" in str(e):
                return None
            raise e
        return Build.model_validate(js)

class Job(BaseModel):
    # hudson.model.FreeStyleProject
    description: str = ""
    name: str
    url: HttpUrl
    buildable: bool
    builds: list[BuildReference] = []
    color: str
    firstBuild: BuildReference | None = None
    lastBuild: BuildReference | None = None
    lastCompletedBuild: BuildReference | None = None
    lastFailedBuild: BuildReference | None = None
    lastStableBuild: BuildReference | None = None
    lastSuccessfulBuild: BuildReference | None = None
    lastUnstableBuild: BuildReference | None = None
    lastUnsuccessfulBuild: BuildReference | None = None
    nextBuildNumber: int
    disabled: bool

    @staticmethod
    def get_job(j: jenkins.Jenkins, job: str):
        try:
            js = j.get_job_info(job)
        except jenkins.JenkinsException as e:
            if "not exist" in str(e):
                return None
            raise e
        return Job.model_validate(js)
