# SPDX-License-Identifier: AGPL-3.0-only
import re
from enum import Enum

import attr
import msgpack
import valideer as V


class JobStatus(Enum):
    WAITING = (1, "Waiting", "running")
    RUNNING = (2, "Scheduled", "running")  # kinda dumb
    WAITING_FOR_DONE = (3, "Finishing up", "running")
    FAILED = (4, "Failed", "failed", "failed")
    SUCCESS = (5, "Success", "success", "are successful")

    # special values
    PREREQUISITE_FAILED = (100, "Prerequisite failed", "failed",
                           "have failed prerequisites")
    UP_TO_DATE = (101, "Up to date", "success")
    IGNORED_FAILURE = (102, "Ignored failure", "failed",
                       "have failed silently")

    @property
    def pretty(self):
        return self.value[1]

    @property
    def kind(self):
        return self.value[2]

    @property
    def terminating(self):
        return self in [
            JobStatus.FAILED,
            JobStatus.SUCCESS,
            JobStatus.IGNORED_FAILURE,
            JobStatus.UP_TO_DATE
        ]

    @property
    def successful(self):
        return self in [
            JobStatus.SUCCESS,
            JobStatus.IGNORED_FAILURE,
            JobStatus.UP_TO_DATE
        ]

    @property
    def predicative(self):
        if len(self.value) > 3 and self.value[3]:
            return self.value[3]
        else:
            return f"are {self.pretty.lower()}"


class BuildState(Enum):
    SCHEDULED = (0, "Waiting to start...")
    FETCH = (1, "Fetching...")
    SETUP = (2, "Setting up...")
    CALCULATING = (3, "Calculating graph...")
    SETUP_REPOS = (4, "Setting up repositories...")
    RUNNING = (5, "Running...")
    DONE = (6, "Done")

    @property
    def pretty(self):
        return self.value[1]

    @property
    def terminating(self):
        return self in [
            BuildState.DONE
        ]


class BaseMessage:
    _filter = None

    def pack(self):
        return msgpack.dumps(attr.asdict(self, filter=self._filter))

    @classmethod
    def unpack(cls, data):
        x = msgpack.loads(data)
        # use adaption for nested data
        x = cls._validator.validate(x)
        val = cls(**x)
        val._dictvalue = x
        return val


_thing = V.parsing(required_properties=True, additional_properties=None)
_thing.__enter__()


@attr.s
class Heartbeat(BaseMessage):
    # status and statistics
    load = attr.ib()
    fqdn = attr.ib()
    # These are for display purposes on the web interface
    project = attr.ib(default=None)
    job = attr.ib(default=None)

    @staticmethod
    def _filter(x, v):
        return v is not None
    _validator = V.parse({
        "load": V.AdaptTo(tuple, ("number", "number", "number")),
        "fqdn": "string",
        "?project": "string",
        "?job": "string"
    })


@attr.s
class WorkMessage(BaseMessage):
    project = attr.ib()
    git = attr.ib()
    revision = attr.ib()
    _validator = V.parse({
        "project": "string",
        "git": "string",
        "revision": "string"
    })


PKG_TOOL_VALIDATOR = V.parse(V.Mapping("string", "string"))


@attr.s
class JobMessage(BaseMessage):
    project = attr.ib()
    job = attr.ib()
    repository = attr.ib()
    revision = attr.ib()
    output = attr.ib()
    build_root = attr.ib()
    needed_pkgs = attr.ib()
    needed_tools = attr.ib()
    prod_pkgs = attr.ib()
    prod_tools = attr.ib()
    prod_files = attr.ib()
    tool_repo = attr.ib()
    pkg_repo = attr.ib()
    rolling_ids = attr.ib()
    # XXX: maybe it's worth doing something else
    xbps_keys = attr.ib(default=None, repr=False)
    _validator = V.parse({
        "project": "string",
        "job": "string",
        "repository": "string",
        "revision": "string",
        "output": "string",
        "build_root": "string",
        "needed_pkgs": PKG_TOOL_VALIDATOR,
        "needed_tools": PKG_TOOL_VALIDATOR,
        "prod_pkgs": PKG_TOOL_VALIDATOR,
        "prod_tools": PKG_TOOL_VALIDATOR,
        "prod_files": ["string"],
        "tool_repo": "string",
        "pkg_repo": "string",
        "rolling_ids": PKG_TOOL_VALIDATOR,
        "?xbps_keys": V.Mapping(
            re.compile(r"^([a-zA-Z0-9]{2}:){15}[a-zA-Z0-9]{2}$"), bytes
        )
    })


def _is_blake2b_digest(x):
    # digest_size 64B for blake2b
    return isinstance(x, bytes) and len(x) == 64


@attr.s
class ArtifactMessage(BaseMessage):
    project = attr.ib()
    artifact_type = attr.ib()
    artifact = attr.ib()
    success = attr.ib()
    filename = attr.ib(default=None)
    last_hash = attr.ib(default=None)
    _validator = V.parse({
        "project": "string",
        "artifact_type": V.Enum({"tool", "package", "file"}),
        "artifact": "string",
        "success": "boolean",
        "?filename": "?string",
        "?last_hash": V.Nullable(_is_blake2b_digest)
    })


@attr.s
class LogMessage(BaseMessage):
    project = attr.ib()
    job = attr.ib()
    line = attr.ib()
    _validator = V.parse({
        "project": "string",
        "job": "string",
        "line": "string"
    })


def _last_hash_validator(x):
    if x == b"initial":
        return True
    return _is_blake2b_digest(x)


@attr.s
class ChunkMessage(BaseMessage):
    last_hash = attr.ib()
    data = attr.ib()
    _validator = V.parse({
        "last_hash": _last_hash_validator,
        "data": bytes
    })


@attr.s
class JobCompletionMessage(BaseMessage):
    project = attr.ib()
    job = attr.ib()
    exit_code = attr.ib()
    run_time = attr.ib()
    _validator = V.parse({
        "project": "string",
        "job": "string",
        "exit_code": "number",
        "run_time": "number"
    })


@attr.s
class StatusMessage(BaseMessage):
    hostname = attr.ib()
    load = attr.ib()
    projects = attr.ib()
    pid = attr.ib()
    _validator = V.parse({
        "hostname": "string",
        "load": V.AdaptTo(tuple, ("number", "number", "number")),
        "pid": "integer",
        "projects": V.Mapping("string", {
            "git": "string",
            "description": "string",
            "classes": ["string"],
            "running": "boolean",
        })
    })


@attr.s
class ScheduleMessage(BaseMessage):
    project = attr.ib()
    delay = attr.ib()
    _validator = V.parse({
        "project": "string",
        "delay": "number"
    })


_thing.__exit__(None, None, None)
