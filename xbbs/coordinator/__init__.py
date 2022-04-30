# SPDX-License-Identifier: AGPL-3.0-only
import gevent.monkey; gevent.monkey.patch_all()  # noqa isort:skip
import contextlib
import itertools
import json
import operator
import os
import os.path as path
import pathlib
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from hashlib import blake2b

import attr
import gevent
import gevent.event
import gevent.queue
import gevent.time
import gevent.util
import msgpack as msgpk
import toml
import valideer as V
import zmq.green as zmq
from logbook import Logger, StderrHandler

try:
    from psycopg import Connection as PgConnection
except ImportError:
    PgConnection = None

import xbbs.messages as msgs
import xbbs.protocol
import xbbs.util as xutils


def check_call_logged(cmd, **kwargs):
    _kwargs = kwargs.copy()
    if "input" in _kwargs:
        del _kwargs["input"]
    log.info("running command {} (params {})", cmd, _kwargs)
    if "env_extra" in kwargs:
        env = os.environ.copy()
        env.update(kwargs["env_extra"])
        del kwargs["env_extra"]
        kwargs.update(env=env)
    return subprocess.check_call(cmd, **kwargs)


def check_output_logged(cmd, **kwargs):
    _kwargs = kwargs.copy()
    if "input" in _kwargs:
        del _kwargs["input"]
    log.info("running command {} (params {})", cmd, _kwargs)
    return subprocess.check_output(cmd, **kwargs)


# more properties are required than not
with V.parsing(required_properties=True,
               additional_properties=V.Object.REMOVE):
    @V.accepts(x=V.AnyOf(
        xutils.Endpoint(),
        {"bind": xutils.Endpoint(xutils.Endpoint.Side.BIND),
         "connect": xutils.Endpoint(xutils.Endpoint.Side.CONNECT)}))
    def _receive_adaptor(x):
        if isinstance(x, str):
            return {"bind": x, "connect": x}
        return x

    @V.accepts(x="string")
    def _path_exists(x):
        return os.access(x, os.R_OK)

    CONFIG_VALIDATOR = V.parse({
        "command_endpoint": V.AdaptBy(_receive_adaptor),
        "project_base": "string",
        "build_root": V.AllOf("string", path.isabs),
        "intake": V.AdaptBy(_receive_adaptor),
        "worker_endpoint": xutils.Endpoint(xutils.Endpoint.Side.BIND),
        "?artifact_history": V.Nullable("boolean", False),
        # use something like a C identifier, except disallow underscore as a
        # first character too. this is so that we have a namespace for xbbs
        # internal directories, such as collection directories
        "projects": V.Mapping(xutils.PROJECT_REGEX, {
            "git": "string",
            "?description": "string",
            "?classes": V.Nullable(["string"], []),
            "packages": "string",
            "?fingerprint": "string",
            "tools": "string",
            "?incremental": "boolean",
            "?distfile_path": "string",
            "?mirror_root": "string",
            "?default_branch": "string",
        })
    })
    PUBKEY_VALIDATOR = V.parse({
        # I'm only validating the keys that xbbs uses
        "signature-by": "string"
    })

with V.parsing(required_properties=True, additional_properties=None):
    # { job_name: job }
    ARTIFACT_VALIDATOR = V.parse({
        "name": "string",
        "version": "string",
        "architecture": "string",
    })
    JOB_REGEX = re.compile(r"^[a-z]+:.*$")
    GRAPH_VALIDATOR = V.parse(V.Mapping(JOB_REGEX, {
        "up2date": "boolean",
        "unstable": "boolean",
        # TODO(arsen): remove nullability when xbstrap updates
        "?capabilities": V.AdaptBy(xutils.list_to_set),
        "products": {
            "tools": [ARTIFACT_VALIDATOR],
            "pkgs": [ARTIFACT_VALIDATOR],
            "files": [V.AdaptBy(operator.itemgetter("name"), {
                "name": "string",
                "filepath": "string"
            })]
        },
        "needed": {
            "tools": [ARTIFACT_VALIDATOR],
            "pkgs": [ARTIFACT_VALIDATOR]
        }
    }))


@attr.s
class Project:
    name = attr.ib()
    git = attr.ib()
    description = attr.ib()
    classes = attr.ib()
    packages = attr.ib()
    tools = attr.ib()
    base = attr.ib()
    distfile_path = attr.ib(default="xbbs/")
    incremental = attr.ib(default=False)
    fingerprint = attr.ib(default=None)
    current = attr.ib(default=None)
    mirror_root = attr.ib(default=None)
    default_branch = attr.ib(default="master")
    tool_repo_lock = attr.ib(factory=gevent.lock.RLock)


@attr.s
class Xbbs:
    project_base = attr.ib()
    collection_dir = attr.ib()
    tmp_dir = attr.ib()
    build_root = attr.ib()
    intake_address = attr.ib()
    worker_endpoint = attr.ib(default=None)
    intake = attr.ib(default=None)
    projects = attr.ib(factory=dict)
    project_greenlets = attr.ib(factory=list)
    zmq = attr.ib(default=zmq.Context.instance())
    outgoing_job_queue = attr.ib(factory=lambda: gevent.queue.Queue(1))
    db: PgConnection = attr.ib(default=None)

    @classmethod
    def create(cls, cfg):
        pbase = cfg["project_base"]
        inst = Xbbs(
            project_base=pbase,
            collection_dir=path.join(pbase, "_coldir"),
            tmp_dir=path.join(pbase, "_tmp"),
            build_root=cfg["build_root"],
            intake_address=cfg["intake"]["connect"]
        )
        os.makedirs(inst.collection_dir, exist_ok=True)
        os.makedirs(inst.tmp_dir, exist_ok=True)
        return inst


@attr.s
class Artifact:
    Kind = Enum("Kind", "TOOL PACKAGE FILE")
    kind = attr.ib()
    name = attr.ib()
    version = attr.ib()
    architecture = attr.ib()
    received = attr.ib(default=False, eq=False, order=False)
    failed = attr.ib(default=False, eq=False, order=False)


@attr.s
class Job:
    # TODO(arsen): RUNNING is actually waiting to finish: it might say it's
    # running, but it's proobably not, and is instead stuck in the pipeline
    unstable = attr.ib()
    deps = attr.ib(factory=list)
    products = attr.ib(factory=list)
    capabilities = attr.ib(factory=set)
    status = attr.ib(default=msgs.JobStatus.WAITING)
    exit_code = attr.ib(default=None)
    run_time = attr.ib(default=None)

    def fail(self, graph):
        if self.status is msgs.JobStatus.RUNNING:
            self.status = msgs.JobStatus.WAITING_FOR_DONE
        else:
            self.status = msgs.JobStatus.IGNORED_FAILURE if self.unstable \
                    else msgs.JobStatus.FAILED

        for prod in self.products:
            if prod.failed:
                continue
            prod.failed = True
            prod.received = True
            for job in graph.values():
                if prod in job.deps:
                    job.fail(graph)


@attr.s
class Build:
    name = attr.ib()
    repository = attr.ib()
    build_directory = attr.ib()
    incremental = attr.ib()

    revision = attr.ib(default=None)
    state = attr.ib(default=msgs.BuildState.SCHEDULED)
    jobs = attr.ib(factory=dict)
    success = attr.ib(default=True)
    ts = attr.ib(factory=lambda: datetime.now(timezone.utc))

    tool_set = attr.ib(factory=dict)
    file_set = attr.ib(factory=dict)
    pkg_set = attr.ib(factory=dict)
    commits_object = attr.ib(factory=dict)

    artifact_received = attr.ib(factory=gevent.event.Event)

    @classmethod
    def create(cls, inst, project):
        inst = cls(
            name=project.name,
            repository=project.git,
            # TODO(arsen): this should be stored in project as base_directory
            build_directory=path.join(inst.project_base, project.name),
            incremental=project.incremental
        )
        inst.build_directory = path.join(
            inst.build_directory, inst.ts.strftime(xutils.TIMESTAMP_FORMAT)
        )
        os.makedirs(inst.build_directory)
        inst.store_status()
        return inst

    def update_state(self, state):
        self.state = state
        self.store_status()

    def log(self, job=None):
        return path.join(self.build_directory, f"{job}.log")

    def info(self, job):
        return path.join(self.build_directory, f"{job}.info")

    def store_status(self, *, success=None, length=None):
        coordfile = path.join(self.build_directory, "coordinator")
        job_info = {}
        for name, job in self.jobs.items():
            current = {
                "status": job.status.name,
                "deps": [attr.asdict(x) for x in job.deps],
                "products": [attr.asdict(x) for x in job.products]
            }
            job_info[name] = current
            if job.exit_code is not None:
                current.update(exit_code=job.exit_code)
            if job.run_time is not None:
                current.update(run_time=job.run_time)
        # TODO(arsen): store more useful graph
        state = {
            "state": self.state.name,
            "jobs": job_info,
            "incremental": self.incremental,
            "commits_object": self.commits_object,
            "revision": self.revision,
        }
        if success is not None:
            state.update(success=success, run_time=length)
        with open(coordfile, "w") as csf:
            json.dump(state, csf, indent=4, cls=ArtifactEncoder)

    def set_graph(self, project, revision, graph, commits_object):
        graph = GRAPH_VALIDATOR.validate(graph)

        self.revision = revision
        self.commits_object = commits_object
        tools = self.tool_set
        pkgs = self.pkg_set
        files = self.file_set
        arch_set = set()
        for job, info in graph.items():
            # TODO(arsen): circ dep detection (low prio: handled in xbstrap)
            job_val = Job(
                unstable=info["unstable"],
                capabilities=info["capabilities"]
            )

            def _handle_artifact(kind, reqset, x):
                name = x["name"]
                aset = {
                    Artifact.Kind.TOOL: tools,
                    Artifact.Kind.PACKAGE: pkgs,
                    Artifact.Kind.FILE: files,
                }[kind]
                if name not in aset:
                    aset[name] = Artifact(kind, **x)
                reqset.append(aset[name])

                ta = aset[name].architecture
                if ta == "noarch":
                    return  # dealt with later

                if bool(arch_set) and ta not in arch_set:
                    raise RuntimeError("multiarch builds unsupported")

                arch_set.add(ta)

            for x in info["needed"]["tools"]:
                _handle_artifact(Artifact.Kind.TOOL, job_val.deps, x)
            for x in info["needed"]["pkgs"]:
                _handle_artifact(Artifact.Kind.PACKAGE, job_val.deps, x)
            for x in info["products"]["tools"]:
                _handle_artifact(Artifact.Kind.TOOL, job_val.products, x)
            for x in info["products"]["pkgs"]:
                _handle_artifact(Artifact.Kind.PACKAGE, job_val.products, x)

            for fname in info["products"]["files"]:
                artifact = Artifact(Artifact.Kind.FILE,
                                    architecture=None,
                                    name=fname,
                                    version=None)
                job_val.products.append(artifact)
                files[fname] = artifact

            if info["up2date"]:
                job_val.status = msgs.JobStatus.UP_TO_DATE
                for prod in job_val.products:
                    prod.received = True
                    prod.failed = False

            self.jobs[job] = job_val

        for x in itertools.chain(tools.values(), pkgs.values()):
            if x.architecture == "noarch":
                # TODO(arsen): decide whether to use list or set globally
                x.architecture = list(arch_set)

        self.store_status()


class ArtifactEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Artifact.Kind):
            return obj.name
        return super().default(obj)


def solve_project(inst, projinfo):
    build = projinfo.current
    while True:
        build.artifact_received.clear()
        some_waiting = False
        for name, job in build.jobs.items():
            if all([x.received for x in job.products]) and \
               job.status is msgs.JobStatus.RUNNING:
                job.status = msgs.JobStatus.WAITING_FOR_DONE

            if not job.status.terminating:
                some_waiting = True

            if job.status is not msgs.JobStatus.WAITING:
                continue

            failed = False
            satisfied = True
            for dep in job.deps:
                if not dep.received:
                    satisfied = False
                if dep.failed:
                    failed = True

            if failed:
                job.fail(build.jobs)
                # This failure means that our artifacts might have changed -
                # trigger a rescan
                build.artifact_received.set()
                continue

            if not satisfied:
                continue

            def _produce_artifact_obj(x):
                return {
                    "architecture": x.architecture,
                    "version": x.version
                }

            needed_tools = {x.name: _produce_artifact_obj(x)
                            for x in job.deps
                            if x.kind is Artifact.Kind.TOOL}
            needed_pkgs = {x.name: _produce_artifact_obj(x)
                           for x in job.deps
                           if x.kind is Artifact.Kind.PACKAGE}
            prod_tools = {x.name: _produce_artifact_obj(x)
                          for x in job.products
                          if x.kind is Artifact.Kind.TOOL}
            prod_pkgs = {x.name: _produce_artifact_obj(x)
                         for x in job.products
                         if x.kind is Artifact.Kind.PACKAGE}
            prod_files = [x.name for x in job.products
                          if x.kind is Artifact.Kind.FILE]
            keys = {}
            if projinfo.fingerprint:
                pubkey = path.join(projinfo.base,
                                   f"{projinfo.fingerprint}.plist")
                # XXX: this is not cooperative, and should be okay because
                # it's a small amount of data
                with open(pubkey, "rb") as pkf:
                    keys = {projinfo.fingerprint: pkf.read()}

            job.status = msgs.JobStatus.RUNNING
            jobreq = msgs.JobMessage(
                project=build.name,
                job=name,
                repository=build.repository,
                revision=build.revision,
                output=inst.intake_address,
                build_root=inst.build_root,
                needed_tools=needed_tools,
                needed_pkgs=needed_pkgs,
                prod_pkgs=prod_pkgs,
                prod_tools=prod_tools,
                prod_files=prod_files,
                tool_repo=projinfo.tools,
                pkg_repo=projinfo.packages,
                commits_object=build.commits_object,
                xbps_keys=keys,
                mirror_root=projinfo.mirror_root,
                distfile_path=projinfo.distfile_path,
            )
            log.debug("sending job request {}", jobreq)
            build.store_status()
            inst.outgoing_job_queue.put((job.capabilities, jobreq.pack()))

        # TODO(arsen): handle the edge case in which workers are dead
        if not some_waiting:
            assert all(x.received for x in build.tool_set.values())
            assert all(x.received for x in build.file_set.values())
            assert all(x.received for x in build.pkg_set.values())
            assert all(x.status.terminating for x in build.jobs.values())
            return all(x.status.successful for x in build.jobs.values())

        build.artifact_received.wait()

# TODO(arsen): a better log collection system. It should include the output of
# git and xbstrap, too
# this should be a pty assigned to each build, on the other side of which stuff
# is read out and parsed in a manner similar to a terminal.
# the ideal would be properly rendering control sequences same way
# xterm-{256,}color does, since it is widely adopted and assumed.


def load_package_registries(pkg_repo_dir):
    # TODO: multiarch
    pkgs = {}
    repodata_files = []
    try:
        repodata_files = [x for x in pkg_repo_dir.iterdir()
                          if str(x).endswith("-repodata")]
    except FileNotFoundError:
        pass

    # arches = [x.name[:-len("-repodata")] for x in repodata_files]
    log.debug("repodata found: {}", repodata_files)
    if len(repodata_files) > 1:
        raise RuntimeError("multiarch builds unsupported")
    if len(repodata_files) == 0:
        return {}

    rdf = repodata_files[0]
    rd = xutils.read_xbps_repodata(rdf)  # TODO(arsen): TOCTOU with iterdir
    for pkg in rd:
        pkgs[pkg] = rd[pkg]["pkgver"].rpartition("-")[2]

    return pkgs


def _load_version_information(project):
    tool_repo = path.join(project.base, "rolling/tool_repo")
    pkg_repo_dir = pathlib.Path(project.base) / "rolling/package_repo/"

    pkgs = load_package_registries(pkg_repo_dir)

    with project.tool_repo_lock:
        tools = load_tool_registry(tool_repo)

    return {"pkgs": pkgs, "tools": tools}


def run_project(inst, project, delay, incremental):
    @contextlib.contextmanager
    def _current_symlink():
        # XXX: if this fails two coordinators are running, perhaps that
        # should be prevented somehow (lock on start)?
        current_file = path.join(project.base, "current")
        datedir = path.basename(path.normpath(build.build_directory))
        try:
            yield os.symlink(datedir, current_file)
        finally:
            os.unlink(current_file)

    increment = project.incremental
    if incremental is not None:
        increment = incremental

    start = time.monotonic()
    success = False
    length = 0
    build = project.current

    build.incremental = increment

    try:
        with xutils.lock_file(build.build_directory, "coordinator"), \
             tempfile.TemporaryDirectory(dir=inst.tmp_dir) as projdir, \
             _current_symlink():
            gevent.time.sleep(delay)
            build.update_state(msgs.BuildState.FETCH)
            check_call_logged(["git", "init"], cwd=projdir)
            check_call_logged(["git", "remote", "add", "origin",
                               project.git], cwd=projdir)
            check_call_logged(["git", "fetch", "origin"], cwd=projdir)
            # TODO(arsen): support non-master builds
            refspec = f"origin/{project.default_branch}"
            check_call_logged(["git", "checkout", "--detach", refspec],
                              cwd=projdir)
            rev = check_output_logged(["git", "rev-parse", "HEAD"],
                                      cwd=projdir).decode().strip()
            build.update_state(msgs.BuildState.SETUP)

            with tempfile.TemporaryDirectory(dir=inst.tmp_dir) as td:
                distfiles = path.join(projdir, project.distfile_path)
                if path.isdir(distfiles):
                    xutils.merge_tree_into(distfiles, td)
                check_call_logged(["xbstrap", "init", projdir], cwd=td)

                if project.mirror_root:
                    build.update_state(msgs.BuildState.UPDATING_MIRRORS)
                    mirror_build_dir = path.join(project.base, "mirror_build")
                    os.makedirs(mirror_build_dir, exist_ok=True)
                    check_call_logged(["xbstrap-mirror", "-S", projdir,
                                       "update", "--keep-going"],
                                      cwd=mirror_build_dir)

                check_call_logged(["xbstrap", "rolling-versions", "fetch"],
                                  cwd=td)
                check_call_logged(["xbstrap", "variable-commits",
                                   "fetch", "-c"], cwd=td)
                rolling_ids = json.loads(check_output_logged([
                    "xbstrap", "rolling-versions", "determine", "--json"
                ], cwd=td).decode())
                variable_commits = json.loads(check_output_logged([
                    "xbstrap", "variable-commits", "determine", "--json"
                ], cwd=td).decode())

                commits_object = {}

                for k, v in rolling_ids.items():
                    if k not in commits_object:
                        commits_object[k] = {}
                    commits_object[k]["rolling_id"] = v

                for k, v in variable_commits.items():
                    if k not in commits_object:
                        commits_object[k] = {}
                    commits_object[k]["fixed_commit"] = v

                with open(path.join(projdir, "bootstrap-commits.yml"), "w") \
                        as rf:
                    json.dump({
                        "commits": commits_object
                    }, rf)

                build.update_state(msgs.BuildState.CALCULATING)
                xbargs = [
                    "xbstrap-pipeline", "compute-graph",
                    "--artifacts", "--json"
                ]
                stdinarg = {}
                if increment:
                    vi = _load_version_information(project)
                    stdinarg = dict(input=json.dumps(vi).encode())
                    xbargs.extend(["--version-file", "fd:0"])
                    log.debug("verinfo collected: {}", vi)
                # XXX: this code may need some cleaning, for readability sake.
                graph = json.loads(check_output_logged(
                    xbargs,
                    cwd=td,
                    **stdinarg
                ).decode())
            build.set_graph(project, rev, graph, commits_object)

            # XXX: keep last success and currently running directory as links?
            package_repo = path.join(build.build_directory, "package_repo")
            tool_repo = path.join(build.build_directory, "tool_repo")

            roll_base = path.join(project.base, "rolling")
            rpkg_repo = path.join(roll_base, "package_repo")
            rtool_repo = path.join(roll_base, "tool_repo")
            if path.exists(roll_base) and increment:
                build.update_state(msgs.BuildState.SETUP_REPOS)
                log.info("populating build repository with up-to-date pkgs")
                os.makedirs(package_repo)
                os.makedirs(tool_repo)
                for x in build.pkg_set.values():
                    if not x.received:
                        continue
                    arch = x.architecture
                    filearch = arch
                    if isinstance(arch, list):
                        assert len(arch) == 1, \
                               "multiarch support missing, yet demanded?"
                        arch = arch[0]
                        filearch = "noarch"

                    fname = f"{x.name}-{x.version}.{filearch}.xbps"
                    target_file = path.join(package_repo, fname)
                    # does the user deserve an error if they delete a package?
                    env_extra = {
                        "XBPS_ARCH": arch
                    }
                    shutil.copy2(path.join(rpkg_repo, fname),
                                 target_file, follow_symlinks=False)
                    check_call_logged(["xbps-rindex", "-fa", target_file],
                                      env_extra=env_extra)
                    maybe_sign_artifact(inst, target_file, project, arch)
                for x in build.tool_set.values():
                    if not x.received:
                        continue
                    fname = f"{x.name}.tar.gz"
                    target_file = path.join(tool_repo, fname)
                    tool_fname = path.join(rtool_repo, fname)
                    shutil.copy2(tool_fname,
                                 target_file, follow_symlinks=False)
                    update_tool_registry(tool_fname, x)
            else:
                log.debug("wiping rolling repos for non incremental build")
                try:
                    shutil.rmtree(roll_base)
                except FileNotFoundError:
                    pass

            build.update_state(msgs.BuildState.RUNNING)
            success = solve_project(inst, project)
    except Exception:
        log.exception("build failed due to an exception")
    finally:
        length = time.monotonic() - start
        project.current = None
        # Update directly here to not do two writes to disk
        build.state = msgs.BuildState.DONE
        build.store_status(success=success, length=length)

        log.info("job {} done; success? {} in {}s",
                 project.name, success, length)


def cmd_build(inst, arg):
    "handle starting a new build on a project by name with a time delay"
    msg = msgs.BuildMessage.unpack(arg)
    name = msg.project
    delay = msg.delay
    incremental = msg.incremental

    if name not in inst.projects:
        return 404, msgpk.dumps("unknown project")
    proj = inst.projects[name]
    if proj.current:
        return 409, msgpk.dumps("project already running")

    proj.current = Build.create(inst, proj)
    pg = gevent.spawn(run_project, inst, proj, delay, incremental)
    pg.link(lambda g, i=inst: inst.project_greenlets.remove(g))
    inst.project_greenlets.append(pg)


def cmd_fail(inst, name):
    "fail any unstarted packages"
    name = msgpk.loads(name)
    if name not in inst.projects:
        return 404, msgpk.dumps("unknown project")
    proj = inst.projects[name]
    if not proj.current:
        return 409, msgpk.dumps("project not running")
    for x in proj.current.jobs.values():
        if x.status is not msgs.JobStatus.WAITING:
            continue
        x.fail(proj.current.jobs)
    proj.current.artifact_received.set()


def cmd_status(inst, _):
    projmap = {}
    for x in inst.projects.values():
        projmap[x.name] = {
            "git": x.git,
            "description": x.description,
            "classes": x.classes,
            "running": bool(x.current)
        }
    return msgs.StatusMessage(
        projects=projmap,
        hostname=socket.getfqdn(),
        load=os.getloadavg(),
        pid=os.getpid()
    ).pack()


def command_loop(inst, sock_cmd):
    while True:
        try:
            [command, arg] = sock_cmd.recv_multipart()
            command = command.decode("us-ascii")
            if command not in command_loop.cmds:
                sock_cmd.send_multipart([b"400",
                                         msgpk.dumps("no such command")])
                continue

            code = "200"
            value = command_loop.cmds[command](inst, arg)
            if value is None:
                sock_cmd.send_multipart([b"204", msgpk.dumps("")])
                continue

            if isinstance(value, tuple):
                (code, value) = value
                assert isinstance(code, int)
                code = str(code)

            sock_cmd.send_multipart([code.encode(), value])
        except zmq.ZMQError:
            log.exception("command loop i/o error, aborting")
            return
        except xbbs.protocol.ProtocolError as e:
            log.exception("comand processing error", e)
            sock_cmd.send_multipart([str(e.code).encode(),
                                     msgpk.dumps(f"{type(e).__name__}: {e}")])
        except V.ValidationError as e:
            log.exception("command processing error", e)
            sock_cmd.send_multipart([b"400",
                                     msgpk.dumps(f"{type(e).__name__}: {e}")])
        except Exception as e:
            log.exception("comand processing error", e)
            sock_cmd.send_multipart([b"500",
                                     msgpk.dumps(f"{type(e).__name__}: {e}")])


command_loop.cmds = {
    "build": cmd_build,
    "fail": cmd_fail,
    "status": cmd_status
}


def cmd_chunk(inst, value):
    chunk = msgs.ChunkMessage.unpack(value)
    if chunk.last_hash == b"initial":
        (fd, path) = tempfile.mkstemp(dir=inst.collection_dir)
        os.fchmod(fd, 0o644)
        store = (fd, path, blake2b())
    elif chunk.last_hash not in cmd_chunk.table:
        return
    else:
        store = cmd_chunk.table[chunk.last_hash]
        del cmd_chunk.table[chunk.last_hash]
    digest = blake2b(value).digest()
    cmd_chunk.table[digest] = store
    store[2].update(chunk.data)
    os.write(store[0], chunk.data)


cmd_chunk.table = {}


def maybe_sign_artifact(inst, artifact, project, arch):
    if not project.fingerprint:
        return
    base = project.base
    privkey = path.join(base, f"{project.fingerprint}.rsa")
    pubkey = path.join(base, f"{project.fingerprint}.plist")
    # XXX: this is not cooperative, and should be okay because
    # it's a small amount of data
    with open(pubkey, "rb") as pkf:
        pkeydata = PUBKEY_VALIDATOR.validate(plistlib.load(pkf))
    signed_by = pkeydata["signature-by"]
    env_extra = {
        "XBPS_ARCH": arch
    }
    check_call_logged(["xbps-rindex",
                       "--signedby", signed_by,
                       "--privkey", privkey,
                       "-s", path.dirname(artifact)],
                      env_extra=env_extra)
    check_call_logged(["xbps-rindex",
                       "--signedby", signed_by,
                       "--privkey", privkey,
                       "-S", artifact],
                      env_extra=env_extra)
    # XXX: a sanity check here? extract the key from repodata and compare with
    # "{project.fingerprint}.plist"s key and signer


def load_tool_registry(tool_repo):
    versions = {}
    try:
        dbf = open(path.join(tool_repo, "tools.json"))
        versions = json.load(dbf)
    except FileNotFoundError:
        pass
    return versions


def update_tool_registry(artifact_file, artifact, toolvers=None):
    repo = path.dirname(artifact_file)
    repodata = path.join(repo, "tools.json")
    versions = {}
    dbf = None
    if not toolvers:
        try:
            with open(repodata) as dbf:
                versions = json.load(dbf)
        except FileNotFoundError:
            pass
        except Exception:
            if dbf:
                dbf.close()
            raise
    else:
        versions = toolvers

    versions[artifact.name] = artifact.version
    with tempfile.NamedTemporaryFile(prefix=".", dir=repo, delete=False,
                                     mode="w+") as f:
        json.dump(versions, f, indent=4)
        os.chmod(f.name, 0o644)  # mkstemp doesn't use umask
        os.rename(f.name, repodata)


def record_artifact(inst: Xbbs, run: Build, artifact: Artifact, digest_fn):
    if not inst.db:
        return

    if artifact.kind not in [Artifact.Kind.TOOL, Artifact.Kind.PACKAGE]:
        return

    with inst.db.cursor() as cur, \
         inst.db.transaction():
        cur.execute(
            """
            INSERT INTO artifact_history (
                project_name, build_date,
                artifact_type, artifact_name, artifact_version,
                result_hash
            ) VALUES (%s, %s, %s, %s, %s, %s);
            """, (
                run.name, run.ts,
                artifact.kind.name.lower(), artifact.name, artifact.version,
                digest_fn.digest(),
            )
        )


def cmd_artifact(inst, value):
    "handle receiving an artifact"
    message = msgs.ArtifactMessage.unpack(value)
    log.debug("received artifact {}", message)
    artifact = None
    target = None
    try:
        if message.project not in inst.projects:
            return
        proj = inst.projects[message.project]
        run = inst.projects[message.project].current
        if not run:
            return

        if message.artifact_type == "tool":
            aset = run.tool_set
        elif message.artifact_type == "file":
            aset = run.file_set
        else:
            assert message.artifact_type == "package"
            aset = run.pkg_set

        repo = path.abspath(path.join(proj.current.build_directory,
                            f"{message.artifact_type}_repo"))
        repo_roll = path.abspath(path.join(
            proj.base, f"rolling/{message.artifact_type}_repo"
        ))
        os.makedirs(repo, exist_ok=True)
        os.makedirs(repo_roll, exist_ok=True)

        if message.artifact not in aset:
            return

        artifact = aset[message.artifact]
        artifact.received = True
        artifact.failed = not message.success
        if not message.success:
            run.artifact_received.set()
            return

        (fd, target, digest_fn) = cmd_chunk.table[message.last_hash]
        del cmd_chunk.table[message.last_hash]
        os.close(fd)

        try:
            record_artifact(inst, run, artifact, digest_fn)
        except Exception:
            log.exception(
                "failed to record artifact build into history ({} {})", run, artifact
            )

        try:
            arch = artifact.architecture
            if isinstance(arch, list):
                assert len(arch) == 1, \
                       "multiarch support missing, yet demanded?"
                arch = arch[0]

            artifact_file = path.join(repo, message.filename)
            artifact_roll = path.join(repo_roll, message.filename)
            shutil.move(target, artifact_file)
            if artifact.kind == Artifact.Kind.PACKAGE:
                env_extra = {
                    "XBPS_ARCH": arch
                }
                check_call_logged(["xbps-rindex", "-fa", artifact_file],
                                  env_extra=env_extra)
                if not path.exists(artifact_roll):
                    shutil.copy2(artifact_file, artifact_roll)
                    # we don't -f this one, because we want the most up-to-date
                    # here
                    check_call_logged(["xbps-rindex", "-a", artifact_roll],
                                      env_extra=env_extra)
                    # clean up rolling repo
                    check_call_logged(["xbps-rindex", "-r", repo_roll],
                                      env_extra=env_extra)
                else:
                    with open(artifact_file, "rb") as f:
                        h1 = xutils.hash_file(f)
                    with open(artifact_roll, "rb") as f:
                        h2 = xutils.hash_file(f)
                    if h1 != h2:
                        log.error("{} hash changed, but pkgver didn't!",
                                  artifact)
                maybe_sign_artifact(inst, artifact_file, proj, arch)
                maybe_sign_artifact(inst, artifact_roll, proj, arch)
            elif artifact.kind == Artifact.Kind.TOOL:
                with proj.tool_repo_lock:
                    toolvers = load_tool_registry(path.dirname(artifact_roll))
                    if not path.exists(artifact_roll) or \
                       toolvers.get(artifact.name, None) != artifact.version:
                        shutil.copy2(artifact_file, artifact_roll)
                        update_tool_registry(artifact_roll, artifact, toolvers)
                    else:
                        with open(artifact_file, "rb") as f:
                            h1 = xutils.hash_file(f)
                        with open(artifact_roll, "rb") as f:
                            h2 = xutils.hash_file(f)
                        if h1 != h2:
                            log.error("{} hash changed, but version didn't!",
                                      artifact)
            elif artifact.kind == Artifact.Kind.FILE:
                shutil.copy2(artifact_file, artifact_roll)
            else:
                assert not "New artifact kind?"
        except Exception as e:
            log.exception("artifact deposit failed", e)
            artifact.failed = True

        run.artifact_received.set()
    except Exception:
        if artifact:
            artifact.failed = True
            run.artifact_received.set()
        raise
    finally:
        if run and not run.state.terminating:
            run.store_status()
        try:
            if target:
                os.unlink(target)
        except FileNotFoundError:
            pass


def cmd_log(inst, value):
    message = msgs.LogMessage.unpack(value)
    if message.project not in inst.projects:
        return
    proj = inst.projects[message.project]
    if not proj.current:
        log.info("dropped log because project {} was not running",
                 message.project)
        return

    # XXX: this is not cooperative, and should be okay because
    # it's a small amount of data
    with open(proj.current.log(message.job), mode="a",
              encoding="utf-8", errors="backslashreplace") as logfile:
        logfile.write(message.line)


def cmd_job(inst, value):
    message = msgs.JobCompletionMessage.unpack(value)
    log.debug("got job message {}", message)
    if message.project not in inst.projects:
        return
    proj = inst.projects[message.project]
    if not proj.current:
        return

    job = proj.current.jobs[message.job]
    if message.exit_code == 0:
        job.status = msgs.JobStatus.SUCCESS
    else:
        job.status = msgs.JobStatus.IGNORED_FAILURE if job.unstable \
                else msgs.JobStatus.FAILED
    job.exit_code = message.exit_code
    job.run_time = message.run_time
    with open(proj.current.info(message.job), "w") as infofile:
        json.dump(message._dictvalue, infofile, indent=4)

    if proj.current and not proj.current.state.terminating:
        proj.current.store_status()

    proj.current.artifact_received.set()


def intake_loop(inst):
    while True:
        try:
            [cmd, value] = inst.intake.recv_multipart()
            cmd = cmd.decode("us-ascii")
            intake_loop.cmds[cmd](inst, value)
        except zmq.ZMQError:
            log.exception("intake pipe i/o error, aborting")
            return
        except Exception as e:
            log.exception("intake pipe error, continuing", e)


intake_loop.cmds = {
    "chunk": cmd_chunk,
    "artifact": cmd_artifact,
    "job": cmd_job,
    "log": cmd_log
}


def _send_ignore_unreachable(sock, *args, **kwargs):
    try:
        sock.send_multipart(*args, **kwargs)
        return
    except zmq.ZMQError as e:
        if e.errno == zmq.EHOSTUNREACH:
            return
        raise


def job_handling_coroutine(inst, rid, request):
    while True:
        try:
            (caps, job) = inst.outgoing_job_queue.get(timeout=60)
        except gevent.queue.Empty:
            # send a null message as a heartbeat
            _send_ignore_unreachable(inst.worker_endpoint, [rid, b"", b""])
            return

        if not caps.issubset(request.capabilities):
            inst.outgoing_job_queue.put((caps, job))

            # The sleep here must happen after a queue put. If it does not,
            # this thread will not yield until the next get causing a deadlock.
            gevent.sleep(1)
            continue

        try:
            inst.worker_endpoint.send_multipart([rid, b"", job])
            return
        except zmq.ZMQError as e:
            if e.errno == zmq.EHOSTUNREACH:
                log.debug("{} unreachable, reusing its job", rid)
                inst.outgoing_job_queue.put((caps, job))
                return
            raise


def job_pull_loop(inst):
    while True:
        try:
            [rid, _, request] = inst.worker_endpoint.recv_multipart()
            request = msgs.JobRequest.unpack(request)
            log.debug("received job request from {}: {}", rid, request)
            gevent.spawn(job_handling_coroutine, inst, rid, request)
        except zmq.ZMQError:
            log.exception("job request i/o error, aborting")
            return
        except Exception as e:
            log.exception("job request error, continuing", e)


def dump_projects(xbbs):
    log.info("force flushing all running build statuses")
    running = 0
    for name, proj in xbbs.projects.items():
        if proj.current and not proj.current.state.terminating:
            proj.current.store_status()
        if not isinstance(proj.current, Build):
            continue
        running += 1
        log.info("project {} running: {}", name, proj.current)
    log.info("running {} project(s)", running)

    log.info("outgoing qsize: {}", xbbs.outgoing_job_queue.qsize())
    log.info("gevent run_info: {}", "\n".join(gevent.util.format_run_info()))
    try:
        x = xbbs.outgoing_job_queue.peek_nowait()
        log.info("last item on queue: {}", x)
    except gevent.queue.Empty:
        pass


def _ipc_chmod(sockurl, perms):
    if not sockurl.startswith("ipc://"):
        return
    os.chmod(sockurl[6:], perms)


def main():
    global log
    StderrHandler().push_application()
    log = Logger("xbbs.coordinator")

    XBBS_CFG_DIR = os.getenv("XBBS_CFG_DIR", "/etc/xbbs")
    with open(path.join(XBBS_CFG_DIR, "coordinator.toml"), "r") as fcfg:
        cfg = CONFIG_VALIDATOR.validate(toml.load(fcfg))

    inst = Xbbs.create(cfg)

    for name, elem in cfg["projects"].items():
        project = Project(
                name, **elem,
                base=path.join(inst.project_base, name)
        )
        inst.projects[name] = project
        os.makedirs(project.base, exist_ok=True)
        log.debug("got project {}", inst.projects[name])

    database_conn = contextlib.nullcontext(None)
    if cfg.get("artifact_history"):
        if PgConnection:
            database_conn = PgConnection.connect()
            log.debug("established PG connection ({})", database_conn)
        else:
            log.error(
                "psycopg3 was not found. In order to use artifact_history, you will"
                " need to install xbbs with the history extras group."
            )

    with database_conn as inst.db, \
         inst.zmq.socket(zmq.REP) as sock_cmd, \
         inst.zmq.socket(zmq.PULL) as inst.intake, \
         inst.zmq.socket(zmq.ROUTER) as inst.worker_endpoint:
        # XXX: potentially make perms overridable? is that useful in any
        #      capacity?
        inst.intake.bind(cfg["intake"]["bind"])
        _ipc_chmod(cfg["intake"]["bind"], 0o664)

        inst.worker_endpoint.bind(cfg["worker_endpoint"])
        inst.worker_endpoint.set(zmq.ROUTER_MANDATORY, 1)
        _ipc_chmod(cfg["worker_endpoint"], 0o664)

        sock_cmd.bind(cfg["command_endpoint"]["bind"])
        _ipc_chmod(cfg["command_endpoint"]["bind"], 0o664)

        dumper = gevent.signal_handler(signal.SIGUSR1, dump_projects, inst)
        log.info("startup")
        intake = gevent.spawn(intake_loop, inst)
        job_pull = gevent.spawn(job_pull_loop, inst)
        try:
            command_loop(inst, sock_cmd)
        finally:
            # XXX: This may not be the greatest way to handle this
            gevent.killall(inst.project_greenlets[:])
            gevent.kill(intake)
            gevent.kill(job_pull)
            dumper.cancel()

# TODO(arsen): make a clean exit
