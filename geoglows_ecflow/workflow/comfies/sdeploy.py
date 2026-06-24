"""
Components of the 'sdeploy' commandline utility

Modified by Aquaveo to work with geoglows_ecflow

Copyright 2024 ECMWF

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os
import re
import sys
import socket
import codecs
import getpass
import datetime
import subprocess
import contextlib
import logging as log
import importlib
import importlib.util
import importlib.machinery
import argparse

from geoglows_ecflow.workflow.comfies.memoize import memoize
from geoglows_ecflow.workflow.comfies.visitor import Visitor
from geoglows_ecflow.workflow.comfies.fs import (
    ensure_dir,
    expand_path,
    real_path,
)
from geoglows_ecflow.workflow.comfies.config import (
    Config,
    PythonConfigFile,
    PythonConfigPath,
)
from geoglows_ecflow.workflow.comfies.config import ConfigNotFoundError
from geoglows_ecflow.workflow.comfies.config import (
    ConfigError,
    ConfigItemNotFoundError,
)
from geoglows_ecflow.workflow.comfies.config import (
    path,
    word,
    pathlist,
    boolean,
)
from geoglows_ecflow.workflow.comfies.templating import (
    TemplateProcessor,
    TemplateError,
)
from geoglows_ecflow.workflow.comfies.sjob import SshHost
from packaging.version import parse as parse_version
from geoglows_ecflow.workflow.comfies.version import __version__
from geoglows_ecflow.workflow.comfies.py2 import basestring
import ecflow


class DeployError(Exception):
    pass


class FileNotFoundError(DeployError):
    pass


class ComfiesVersionError(Exception):
    pass


class PathFinder(object):
    """
    Look for a file in a list of directories
    """

    def __init__(self, srcdirs):
        self._srcdirs = srcdirs

    @memoize
    def find(self, name):
        """
        Given the file name, look for it in
        a list of directories and if found
        return file path.
        """
        for srcdir in self._srcdirs:
            srcpath = os.path.join(srcdir, name)
            if os.path.isfile(srcpath):
                return srcpath
        raise FileNotFoundError(
            name + " not found in {}".format(", ".join(self._srcdirs))
        )


class Text(object):

    def __init__(self, text, source):
        self.text = text
        self.source = source

    def __str__(self):
        return self.text


def read_path(path):
    """
    Read file given it's path, return Text object
    """
    with codecs.open(path, "r", encoding="utf-8") as f:
        t = f.read()
        return Text(t, path)


class FileReader(object):
    """
    Read a file given it's name, return Text object
    """

    def __init__(self, path_finder):
        self._path_finder = path_finder

    def __call__(self, filename):
        path = self._path_finder.find(filename)
        return read_path(path)


class ScriptIncludesFinder(object):
    """
    Parse script recursively and look for includes.
    """

    def __init__(self, path_finder):
        self._path_finder = path_finder

    @memoize
    def search(self, srcpath, expected=None, tag="%"):
        """
        Return paths of all ecFlow include files
        required by the ecFlow script.

        parameters:
          srcpath -- ecFlow script path.
          expected -- raise FileNotFoundError exception if any of the include
                      files from this list cannot be found. Ignore any
                      not found include files if they're not on this list.
        """
        # look for %include and %includenopp directives
        # TODO: implement handling of %ecfmicro directives.
        include_pattern = tag + r"include\S*\s*<(.+)>"
        found = set()
        with open(srcpath, "r") as file:
            for line in file:
                if line[:1] != tag:
                    continue
                match = re.match(include_pattern, line)
                if match is None:
                    continue
                include_file = match.group(1)
                try:
                    include_path = self._path_finder.find(include_file)
                except FileNotFoundError as e:
                    if expected is None or include_file in expected:
                        raise FileNotFoundError(
                            "{} (required by {})".format(str(e), srcpath)
                        )
                if include_path in found:
                    continue
                found.add(include_path)
                found.update(self.search(include_path))
        return list(found)


# ----------------------------
# Suite scripts finder.
# ----------------------------


class MissingSourceError(Exception):
    def __init__(self, message, filename):
        super(MissingSourceError, self).__init__(message)
        self.filename = filename


class MissingTaskScriptError(MissingSourceError):
    pass


class MissingIncludeError(MissingSourceError):
    pass


class SuiteScriptsFinder(Visitor):
    """
    Find all task scripts and includes of a suite.
    """

    def __init__(self, script_finder, ecflow_includes_finder):
        self._script_finder = script_finder
        self._ecflow_includes_finder = ecflow_includes_finder
        self.reset()

    def reset(self):
        self._scripts_found = set()
        self._scripts_missing = set()
        self._includes_found = set()
        self._filenames = None

    def find_scripts(self, start_node, filenames=None):
        """
        Walks the suite tree starting at start_node
        and for each Task node finds corresponding
        task script + include files.
        Returns two lists of unique source paths
        (list of task scripts and list of include files)
        """
        self.reset()
        self._filenames = filenames
        self.visit(start_node)
        scripts_srcpaths = sorted(list(self._scripts_found))
        includes_srcpaths = sorted(list(self._includes_found))
        if self._filenames is not None:
            # client wants to locate only some suite scripts
            scripts_srcpaths, remaining_filenames = match_files_to_paths(
                scripts_srcpaths, filenames
            )
            includes_srcpaths, remaining_filenames = match_files_to_paths(
                includes_srcpaths, remaining_filenames
            )
            if remaining_filenames:
                log.error(
                    "unexpected target file(s): {}".format(
                        ", ".join(remaining_filenames)
                    )
                )
                raise FileNotFoundError(remaining_filenames[0])
        return scripts_srcpaths, includes_srcpaths

    def visit_NodeContainer(self, container):
        # handler for nodes of type NodeContainer (i.e. suite or family)
        for child in container.nodes:
            self.visit(child)

    def visit_Task(self, task):
        # handler for nodes of type Task
        extn = task.get_variable("ECF_EXTN")
        if not extn.name():
            extn = ".ecf"
        script_name = task.name() + extn.value()
        try:
            script_path = self._script_finder.find(script_name)
        except FileNotFoundError as e:
            if self._filenames:
                # Client requested specific files to be found
                if script_name in self._filenames:
                    # Task script requested by client is missing
                    raise
                else:
                    # Task script missing but not requested,
                    # so ignore it, don't even print warning.
                    return
            else:
                # There is a task with missing script
                # Print a warning but continue; it is
                # normal to have tasks without scripts.
                if script_name not in self._scripts_missing:
                    log.warn(str(e))
                    self._scripts_missing.add(script_name)
                return
        self._scripts_found.add(script_path)
        # try:
        includes_paths = self._ecflow_includes_finder.search(
            script_path, expected=self._filenames
        )
        self._includes_found.update(includes_paths)


class ScriptsInstaller(object):
    def __init__(self, reader, processor, writer, dry_run=False):
        self._read = reader
        self._process = processor
        self._write = writer
        self._dry_run = dry_run

    def install(self, srcpaths, destdir):
        if not srcpaths:
            log.info("nothing to deploy.")
            return
        for srcpath in srcpaths:
            filename = os.path.basename(srcpath)
            destpath = os.path.join(destdir, filename)
            if not self._dry_run:
                if self._write == simple_file_writer:
                    # ugly. TODO: Implement abstract dir creation.
                    ensure_dir(destdir)
            log.info("{} --> {}".format(srcpath, destpath))
            note = "#\n# Deployed by {user}@{host} on {date} from {src}\n#\n".format(
                user=getpass.getuser(),
                host=socket.getfqdn(),
                date=datetime.datetime.now(),
                src=os.path.abspath(srcpath),
            )
            text = self._read(srcpath)
            try:
                processed = self._process(text)
            except TemplateError as e:
                raise DeployError(str(e))
            if not self._dry_run:
                self._write(processed, destpath)


class CrossUserFileWriter(object):
    def __init__(self, sudo):
        self._sudo = sudo

    def __call__(self, text, dest_path, append=False):
        """
        Write contents of the text (string)
        to the dest_path file.
        """
        dest_dir = os.path.dirname(dest_path)
        if append:
            redir = ">>"
        else:
            redir = ">"
        cmd = "mkdir -p {dest_dir}; cat {redir} {dest_path}".format(
            dest_dir=dest_dir, redir=redir, dest_path=dest_path
        )
        self._sudo.execute(cmd, stdin_str=text)


def simple_file_writer(text, dest_path, append=False):
    if append:
        mode = "ab"
    else:
        mode = "wb"
    with open(dest_path, mode) as f:
        f.write(text.encode("utf-8"))


def match_files_to_paths(paths, filenames):
    """
    From a list of paths selects a subset corresponding to file names.
    Returns two lists:
      - list of selected paths
      - list of remaining files for which there were no corresponding paths
    """
    selected_paths = []
    remaining_filenames = set(filenames)
    for path in paths:
        filename = os.path.basename(path)
        if filename in remaining_filenames:
            selected_paths.append(path)
            remaining_filenames.discard(filename)
    return selected_paths, list(remaining_filenames)


def module_exists(name, path):
    """
    Test if module exists in path.
    If module doesn't exist return False.

    Walk the dotted name one component at a time, descending into each
    package's directory, and return the final source path (truthy) or False.
    """
    for x in name.split("."):
        spec = importlib.machinery.PathFinder.find_spec(x, [path])
        if spec is None:
            return False
        if spec.submodule_search_locations:
            # x is a package; search the next component inside its directory
            path = list(spec.submodule_search_locations)[0]
        else:
            # x is a module; its origin is the source file
            path = spec.origin
    return path


def find_module_dir(name, dirs):
    """
    Return directory containing module,
    Raise ImportError if none of the directories contains the module.
    """
    for path in dirs:
        if module_exists(name, path):
            return path
        continue
    raise ImportError


def prepend_root_dirs(paths, roots):
    """
    Append root directories (to relative paths only)
    """
    new_paths = []
    for root in roots:
        for path in paths:
            if not os.path.isabs(path):
                path = os.path.join(root, path)
            if path not in new_paths:
                new_paths.append(path)
    return new_paths


class DeployConfigFile(PythonConfigFile):
    search_path = [
        os.getcwd(),
        os.path.join(os.environ["HOME"], ".comfies", "sdeploy"),
    ]


class DeployConfigPath(PythonConfigPath):
    pass


# -------------------------------------------------------------
# helper classes for generating trimurti/sjob ecFlow variables
# -------------------------------------------------------------


class TrimurtiVars(object):
    """
    Generator of ecFlow variables for trimurti job manager
    """

    def __init__(self, config):
        self._config = config

    def commands(self):
        """
        Return ECF_JOB_CMD and ECF_KILL_CMD variables set to trimurti
        """
        executable = self._config.get(
            "manager.executable", default="/home/ma/emos/bin/trimurti"
        )
        trimurti_cmd = executable
        submit_cmd = "%TRIMURTI% %USER% %JOBHOST% %ECF_JOB% %ECF_JOBOUT%"
        status_cmd = (
            "%TRIMURTI% %USER% %JOBHOST% %ECF_JOB% %ECF_JOBOUT% status"
        )
        kill_cmd = (
            "%TRIMURTI% %USER% %JOBHOST% %ECF_RID% %ECF_JOB% %ECF_JOBOUT% kill"
        )
        return [
            ecflow.Variable("TRIMURTI", trimurti_cmd),
            ecflow.Variable("ECF_JOB_CMD", submit_cmd),
            ecflow.Variable("ECF_STATUS_CMD", status_cmd),
            ecflow.Variable("ECF_KILL_CMD", kill_cmd),
        ]

    def _dest(self, desttype):
        """
        For a given job destination type (e.g. 'default', 'local', etc.)
        get the destination parameters from the config and generate
        a list of corresponding ecFlow variables.
        """
        config = self._config.section("destinations." + desttype)
        host = config.get("host", None)
        bkup_host = config.get("bkup_host", None)
        sthost = config.get("sthost", None)
        bkup_sthost = config.get("bkup_sthost", None)
        user = config.get("user", None)
        queue = config.get("queue", None)
        account = config.get("account", None)
        outdir = config.get("outdir", None)
        logserver = config.get("logserver", None)
        ncpus = config.get("ncpus", None)
        mem = config.get("mem", None)
        logport = config.get("logport", 9316)
        v = []
        if host is not None:
            v.append(ecflow.Variable("JOBHOST", host))
        if bkup_host is not None:
            v.append(ecflow.Variable("BKUP_HOST", bkup_host))
        if sthost is not None:
            v.append(ecflow.Variable("STHOST", sthost))
        if bkup_sthost is not None:
            v.append(ecflow.Variable("BKUP_STHOST", bkup_sthost))
        if user is not None:
            v.append(ecflow.Variable("USER", user))
        if queue is not None:
            v.append(ecflow.Variable("QUEUE", queue))
        if account is not None:
            v.append(ecflow.Variable("ACCOUNT", account))
        if outdir is not None:
            v.append(ecflow.Variable("ECF_OUT", outdir))
            default_logdir = outdir
        if logserver is not None:
            v.append(ecflow.Variable("ECF_LOGHOST", logserver))
            v.append(ecflow.Variable("ECF_LOGPORT", logport))
        if ncpus is not None:
            v.append(ecflow.Variable("NCPUS", ncpus))
        if nodes is not None:
            v.append(ecflow.Variable("NODES", nodes))
        if mem is not None:
            v.append(ecflow.Variable("MEM", mem))
        return v

    def dest(self, desttype, fallback=None):
        try:
            vars = self._dest(desttype)
        except ConfigItemNotFoundError as config_item_not_found_error:
            if fallback is None:
                raise config_item_not_found_error
            if fallback == "PARENT":
                return []
            if isinstance(fallback, basestring):
                # fallback is given as a destination name
                vars = self._dest(fallback)
            elif isinstance(fallback, list):
                # fallback is given as a list of ecFlow variables
                vars = fallback
            else:
                raise ValueError(
                    "Invalid fallback destination ({})".format(fallback)
                )
        return vars


class TroikaVars(object):
    """
    Generator of ecFlow variables for trimurti job manager
    """

    def __init__(self, config):
        self._config = config

    def commands(self):
        """
        Return ECF_JOB_CMD and ECF_KILL_CMD variables set to trimurti
        """
        troika_cmd = self._config.get(
            "manager.executable", default="/usr/local/bin/troika"
        )
        troika_config = self._config.get(
            "manager.config", default="/opt/troika/etc/troika.yml"
        )
        troika_from_server = self._config.get(
            "manager.settings_from_server", type=boolean, default=False
        )
        submit_cmd = "%TROIKA% -vv -c %TROIKA_CONFIG% submit -u %USER% -o %ECF_JOBOUT% %REMOTE_HOST% %ECF_JOB%"
        status_cmd = (
            "%TROIKA% -vv -c %TROIKA_CONFIG% monitor %REMOTE_HOST% %ECF_JOB%"
        )
        kill_cmd = (
            "%TROIKA% -vv -c %TROIKA_CONFIG% kill  %REMOTE_HOST% %ECF_JOB%"
        )
        if troika_from_server:
            return []
        else:
            return [
                ecflow.Variable("TROIKA", troika_cmd),
                ecflow.Variable("TROIKA_CONFIG", troika_config),
                ecflow.Variable("ECF_JOB_CMD", submit_cmd),
                ecflow.Variable("ECF_STATUS_CMD", status_cmd),
                ecflow.Variable("ECF_KILL_CMD", kill_cmd),
            ]

    def _dest(self, desttype):
        """
        For a given job destination type (e.g. 'default', 'local', etc.)
        get the destination parameters from the config and generate
        a list of corresponding ecFlow variables.
        """
        config = self._config.section("destinations." + desttype)
        host = config.get("host", None)
        bkup_host = config.get("bkup_host", None)
        sthost = config.get("sthost", None)
        bkup_sthost = config.get("bkup_sthost", None)
        user = config.get("user", None)
        queue = config.get("queue", None)
        account = config.get("account", None)
        outdir = config.get("outdir", None)
        logserver = config.get("logserver", None)
        ncpus = config.get("ncpus", None)
        mem = config.get("mem", None)
        logport = config.get("logport", 9316)
        v = []
        if host is not None:
            v.append(ecflow.Variable("REMOTE_HOST", host))
        if bkup_host is not None:
            v.append(ecflow.Variable("BKUP_HOST", bkup_host))
        if sthost is not None:
            v.append(ecflow.Variable("STHOST", sthost))
        if bkup_sthost is not None:
            v.append(ecflow.Variable("BKUP_STHOST", bkup_sthost))
        if user is not None:
            v.append(ecflow.Variable("USER", user))
        if queue is not None:
            v.append(ecflow.Variable("QUEUE", queue))
        if account is not None:
            v.append(ecflow.Variable("ACCOUNT", account))
        if outdir is not None:
            v.append(ecflow.Variable("ECF_OUT", outdir))
            default_logdir = outdir
        if logserver is not None:
            v.append(ecflow.Variable("ECF_LOGHOST", logserver))
            v.append(ecflow.Variable("ECF_LOGPORT", logport))
        if ncpus is not None:
            v.append(ecflow.Variable("NCPUS", ncpus))
        if mem is not None:
            v.append(ecflow.Variable("MEM", mem))
        return v

    def dest(self, desttype, fallback=None):
        try:
            vars = self._dest(desttype)
        except ConfigItemNotFoundError as config_item_not_found_error:
            if fallback is None:
                raise config_item_not_found_error
            if fallback == "PARENT":
                return []
            if isinstance(fallback, basestring):
                # fallback is given as a destination name
                vars = self._dest(fallback)
            elif isinstance(fallback, list):
                # fallback is given as a list of ecFlow variables
                vars = fallback
            else:
                raise ValueError(
                    "Invalid fallback destination ({})".format(fallback)
                )
        return vars


class SjobVars(object):
    """
    Generator of ecFlow variables for 'sjob' job manager
    """

    def __init__(self, config):
        self._config = config

    def commands(self):
        """
        Return ECF_JOB_CMD and ECF_KILL_CMD variables set to sjob
        """
        executable = self._config.get("manager.executable", default="sjob")
        submit_cmd = (
            executable
            + " submit -v %JOBDEST% %ECF_JOB% %ECF_JOBOUT% > %ECF_JOB%.jobinfo 2>&1"
        )
        status_cmd = (
            executable + " status -v %ECF_JOB%.jobinfo > %ECF_JOB%.stat 2>&1"
        )
        kill_cmd = (
            executable + " kill -v %ECF_JOB%.jobinfo >> %ECF_JOB%.jobinfo 2>&1"
        )
        return [
            ecflow.Variable("ECF_JOB_CMD", submit_cmd),
            ecflow.Variable("ECF_STATUS_CMD", status_cmd),
            ecflow.Variable("ECF_KILL_CMD", kill_cmd),
        ]

    def _dest(self, desttype):
        """
        For a given job destination type (e.g. 'default', 'local', etc.)
        get the destination parameters from the config and generate
        a list of corresponding ecFlow variables.
        """
        v = []
        try:
            config = self._config.section("destinations." + desttype)
        except ConfigItemNotFoundError:
            return v

        host = config.get("host", None)
        bkup_host = config.get("bkup_host", None)
        sthost = config.get("sthost", None)
        bkup_sthost = config.get("bkup_sthost", None)
        user = config.get("user", None)
        queue = config.get("queue", None)
        account = config.get("account", None)
        outdir = config.get("outdir", None)
        logserver = config.get("logserver", None)
        ncpus = config.get("ncpus", None)
        mem = config.get("mem", None)
        logport = config.get("logport", 9316)
        v = []
        if host is not None:
            v.append(ecflow.Variable("JOBHOST", host))
        if bkup_host is not None:
            v.append(ecflow.Variable("BKUP_HOST", bkup_host))
        if sthost is not None:
            v.append(ecflow.Variable("STHOST", sthost))
        if bkup_sthost is not None:
            v.append(ecflow.Variable("BKUP_STHOST", bkup_sthost))
        if user is not None:
            v.append(ecflow.Variable("USER", user))
        if queue is not None:
            v.append(ecflow.Variable("QUEUE", queue))
        if account is not None:
            v.append(ecflow.Variable("ACCOUNT", account))
        if outdir is not None:
            v.append(ecflow.Variable("ECF_OUT", outdir))
            default_logdir = outdir
        if logserver is not None:
            v.append(ecflow.Variable("ECF_LOGHOST", logserver))
            v.append(ecflow.Variable("ECF_LOGPORT", logport))
        if ncpus is not None:
            v.append(ecflow.Variable("NCPUS", ncpus))
        if mem is not None:
            v.append(ecflow.Variable("MEM", mem))
        return v

    def dest(self, desttype, fallback=None):
        """
        Return ecFlow variables corresponding the job destination (desttype).
        This method is to be called in the suite builder. The returned variables
        should be attached to the ecFlow family in the suite - this will determine
        the destination of the tasks in the family.
        In case of 'sjob' we return only one variable - the "JOBDEST" variable.

        If the destination specified as 'desttype' parameter has not been defined by
        the user in the 'destinations' section of the deployment config, the destination
        given as the 'fallback' parameter will be used. If 'fallback' is 'PARENT'
        we return an empty list and the variables defined up the node tree will apply.
        """
        try:
            config = self._config.section("destinations." + desttype)
            destname = config.get("name")
            vars = [ecflow.Variable("JOBDEST", destname)]
            vars = vars + self._dest(desttype)
        except:
            try:
                destname = self._config.get("destinations." + desttype)
                vars = [ecflow.Variable("JOBDEST", destname)]
            except ConfigItemNotFoundError as config_item_not_found_error:
                if fallback is None:
                    raise config_item_not_found_error
                if fallback == "PARENT":
                    return []
                if isinstance(fallback, basestring):
                    # fallback is given as a destination name
                    destname = self._config.get("destinations." + fallback)
                    vars = [ecflow.Variable("JOBDEST", destname)]
                elif isinstance(fallback, list):
                    # fallback is given as a list of ecFlow variables (just one variable)
                    vars = fallback
                else:
                    raise ValueError(
                        "Invalid fallback destination ({})".format(fallback)
                    )
        return vars


# ----------------------------------
# The base class for suite builders.
# ----------------------------------


class BaseBuilder(object):

    # Default ecflow module is comfies.ooflow (ecflow extension).
    # The suite developers may want to use a different implementation
    # of ecFlow API by setting the 'ecflow_module' class attribute
    # in the derived class.

    ecflow_module = "comfies.ooflow"

    # Minimum version of comfies package required to correctly build the
    # suite. It can be set in the builder to a SemVer string (e.g. '1.2.3')

    comfies_minimum_version = "any"

    # Search paths for the suite's scripts and includes.
    # Subclasses of BaseBuilder should set these lists.

    scripts = []
    includes = []

    def __init__(self, config):
        globals()["ecflow"] = importlib.import_module(self.ecflow_module)
        if self.comfies_minimum_version != "any" and parse_version(
            self.comfies_minimum_version
        ) > parse_version(__version__):
            raise ComfiesVersionError(
                "This suite needs version {}"
                " or later of comfies package".format(
                    self.comfies_minimum_version
                )
            )

        # create minimal suite

        suite_name = config.get("name", type=word)
        defdir = expand_path(config.get("target.root", type=path))
        if ":" in defdir:
            defdir = defdir.split(":")[1]
        ecf_home = expand_path(config.get("jobs.root", type=path))
        ecf_files = os.path.join(defdir, "ecf")
        ecf_include = os.path.join(defdir, "inc")
        ecf_extn = config.get("script_extension", ".sms")

        suite_as_family = config.get(
            "suite_as_family", type=boolean, default=False
        )
        if suite_as_family:
            project_name = config.get("project_name", type=word)
            super_suite = ecflow.Suite(project_name)
            suite = ecflow.Family(suite_name)
            suite.add_variable("SUITE", f"/{project_name}/{suite_name}")
        else:
            suite = ecflow.Suite(suite_name)

        suite.add_defstatus(ecflow.DState.suspended)
        suite.add_variable("DEFDIR", defdir)
        suite.add_variable("ECF_HOME", ecf_home)
        suite.add_variable("ECF_OUT", "%ECF_HOME%")
        suite.add_variable("ECF_FILES", ecf_files)
        suite.add_variable("ECF_INCLUDE", ecf_include)
        suite.add_variable("ECF_EXTN", ecf_extn)

        # Default (suite-level) job manager settings.
        # out of the box, sdeploy framework supports:
        # 'trimurti' and 'sjob' managers
        # Other managers need to be handled
        # in classes derived from BaseBuilder.

        # The job manager; default is 'sjob'
        job_manager = config.get("jobs.manager", "sjob")
        # The config section with job destination(s)
        jobs_config = config.section("jobs")
        # Create self.jobvars (ecFlow variables generator) appropriate
        # for the selected job manager
        jobvars = None
        try:
            job_manager = config.section("jobs.manager")
            job_manager_name = job_manager.get("name")
        except:
            job_manager_name = config.get("jobs.manager", "sjob")
        if job_manager_name == "sjob":
            jobvars = SjobVars(jobs_config)
        if job_manager_name == "trimurti":
            jobvars = TrimurtiVars(jobs_config)
        if job_manager_name == "troika":
            jobvars = TroikaVars(jobs_config)
        if jobvars is not None:
            # Generate and add ECF_JOB_CMD and ECF_KILL_CMD variables to the suite
            for var in jobvars.commands():
                suite.add_variable(var)
            # Generate and add ecFlow variables corresponding to the 'default' job destination
            for var in jobvars.dest("default"):
                suite.add_variable(var)
        # add defs, suite, config and jobvars attributes
        # to be used by builder classes derived from BaseBuilder.
        self.defs = ecflow.Defs()
        self.suite = suite
        if suite_as_family:
            super_suite.add(suite)
            self.defs.add_suite(super_suite)
        else:
            self.defs.add_suite(self.suite)
        self.config = config
        if jobvars is not None:
            # Store jobvars generator as an attribute, so that it can be used
            # in derived builder classes to create non-default job destination
            # variables deeper in the suite tree. For example:
            # node.add(self.jobvars.dest('local'))
            self.jobvars = jobvars

    def __call__(self):
        log.info("Generating suite definition...")
        try:
            self.build()
        except ConfigError as e:
            raise DeployError("suite generation failed: {}".format(e))
        log.info("Validating suite structure...")
        self.defs.check()

    def build(self):
        """
        To be implemented by the suite designer
        in the derived class. This is the place where
        suite parts should be instantiated and wired together.
        """
        raise NotImplementedError()


# ----------------------------------------------
# Components of taskcfg app.
# The 'taskcfg' command translates a section
# of deploy config file into a KSH arary, which
# can then be included by task scripts.
# TODO: This is obsolete feature from the old
# SMS suite - templating should be used instead.
# ----------------------------------------------


def _dict_to_ksh_assoc_array(name, data):
    """From python dict, generate KSH associative array"""
    lines = []
    lines.append(name + "=(\n")
    for k, v in data.items():
        lines.append("  [{}]={}\n".format(k, v))
    lines.append(")\n")
    text = "".join(lines)
    return text


def section_to_ksh(section):
    lines = []
    chunks = []
    for key, value in section.items():
        if isinstance(value, dict):
            chunks.append(_dict_to_ksh_assoc_array(key, value))
        else:
            lines.append("{}={}\n".format(key, value))
    lines.sort()
    text = "".join(lines) + "".join(chunks)
    return text


def read_deploy_config(name):
    log.info('Reading config "{}"'.format(name))
    # first try if 'name' is a file path and use it
    try:
        source = DeployConfigPath(name)
    except ConfigNotFoundError:
        # then try to find config in usual places
        source = DeployConfigFile(name)
    log.info("config path is: {}".format(source.origin))
    config = Config(source)
    return config


def taskcfg(configname, sectionname):
    config = read_deploy_config(configname)
    section_data = config.data(sectionname)
    print("section_data " + section_data)
    print(section_to_ksh(section_data))


@contextlib.contextmanager
def _remember_cwd():
    curdir = os.getcwd()
    try:
        yield
    finally:
        os.chdir(curdir)


def sdeploy(config_path, target_files=[], params_values={}, dry_run=True):
    """
    Deploy the suite.

    Args:
      config_path: deployment config file path (or config name)
      target_files: list of files to deploy; if empty, all files will be deployed
      params_values: param/value dict; these override params from config file
      dry_rum: if True, no actual files will be
               created in target dir; useful for testing

    Returns:
      None

    Raises:
      DeployError: deploying the suite failed
    """
    with _remember_cwd():
        _sdeploy(config_path, target_files, params_values, dry_run)


def _sdeploy(config_path, target_files, params_values, dry_run):

    try:
        config = read_deploy_config(config_path)
    except ConfigError as e:
        log.error(e)
        raise DeployError(str(e))

    # Config parameters provided using '--set' commandline option
    # override the ones read from the config file

    for param, value in params_values.items():
        config.set(param, value)

    # Change current directory to where config is
    # in case config refers to the suite source by
    # relative path.
    os.chdir(os.path.dirname(real_path(config.origin)))
    try:
        # source.root can be a single path or a list of comma-separated paths
        # Each path can have 'host:' prefix (which is not used by sdeploy).
        srcdirs = config.get("source.root", type=pathlist)
        log.info("source.root is: {}".format(",".join(srcdirs)))
        # remove host names (if any).
        srcdirs = [real_path(d.split(":", 2)[-1]) for d in srcdirs]
        log.info("srcdir: {}".format(",".join(srcdirs)))
        builder_module_path = config.get("source.builder", type=word)
        target = config.get("target.root", type=path)
        if ":" in target:
            target_host = target.split(":")[0]
            destdir = expand_path(target.split(":")[1])
        else:
            destdir = expand_path(config.get("target.root", type=path))
            target_host = "localhost"
        user_name = config.get(
            "target.owner", type=word, default=getpass.getuser()
        )
        # TODO: get rid of the TASK section..
        # Before templating was implemented, this section was used
        # to generate an include file (task.inc) which was then included
        # by every task script. Probably better to use templating instead.
        task_section = config.data("task", default=None)
    except (AttributeError, ConfigError) as e:
        log.error(str(e))
        raise DeployError(str(e))

    # ----------------------
    # compose the deployer
    # ----------------------

    log.info(f'Loading suite builder module "{builder_module_path}"')
    srcdir = srcdirs[0]
    builder_module = importlib.import_module(builder_module_path)
    try:
        suite_builder = builder_module.Builder(config)
    except (ConfigError, ComfiesVersionError) as e:
        log.error(e)
        raise DeployError(str(e))

    # Generate suite tree now (doing it now in case the suite builder
    # is modifying the 'scripts' and 'includes' search paths).
    try:
        suite_builder()
    except DeployError as e:
        log.error(e)
        raise DeployError(str(e))
    # suite builder class defines default script and include search paths
    default_scripts_search_path = ":".join(suite_builder.scripts)
    default_includes_search_path = ":".join(suite_builder.includes)
    # User may modify the default search path in his deploy config file;
    # User's path specification may contain reference to the default search path, e.g.
    # 'myscripts:{scripts}' - we need to substitute the "{scripts}" parameter.
    try:
        scripts_search_path = config.get("source.scripts", "{scripts}").format(
            scripts=default_scripts_search_path
        )
        includes_search_path = config.get(
            "source.includes", "{includes}"
        ).format(includes=default_includes_search_path)
    except ConfigError as e:
        log.error(e)
        raise DeployError(str(e))
    scripts = scripts_search_path.split(":")
    includes = includes_search_path.split(":")
    # expand '~' if present
    scripts = [os.path.expanduser(d) for d in scripts]
    includes = [os.path.expanduser(d) for d in includes]
    # remove empty strings
    scripts = list(filter(None, scripts))
    includes = list(filter(None, includes))
    # Append source roots to relative paths
    # (but only if there are multiple source roots - not necessary
    # if there's only a single root, also sdeploy log messages will be less cluttered)
    if len(srcdirs) > 1:
        scripts = prepend_root_dirs(scripts, srcdirs)
        includes = prepend_root_dirs(includes, srcdirs)

    script_path_finder = PathFinder(scripts)
    include_path_finder = PathFinder(includes)
    script_includes_finder = ScriptIncludesFinder(include_path_finder)
    suite_scripts_finder = SuiteScriptsFinder(
        script_path_finder, script_includes_finder
    )

    include_reader = FileReader(include_path_finder)
    processor = TemplateProcessor(include_reader, config=config)
    if user_name == getpass.getuser() and "localhost" in target_host:
        file_writer = simple_file_writer
    else:
        # User who is deploying the suite is not the one running the suite.
        # We need a file writer that can write files as different user.
        commands_over_ssh = SshHost(name=target_host, user=user_name)
        file_writer = CrossUserFileWriter(sudo=commands_over_ssh)

    scripts_installer = ScriptsInstaller(
        reader=read_path,
        processor=processor,
        writer=file_writer,
        dry_run=dry_run,
    )

    # ------------------
    # run the deployer
    # ------------------

    # generate and deploy the suite definition file

    suite = suite_builder.suite
    def_path = os.path.join(destdir, suite.name() + ".def")
    def_filename = os.path.basename(def_path)
    write_def = True
    if not target_files:
        target_files = None
    if target_files != None:
        if def_filename not in target_files:
            write_def = False
        else:
            target_files.remove(def_filename)
    if write_def:
        if not dry_run:
            if user_name == getpass.getuser() and "localhost" in target_host:
                # ugly. TODO: implement generic "session" class
                # for executing commands locally or remotely
                ensure_dir(destdir)
            file_writer(str(suite_builder.defs), def_path)
        log.info("suite definition written as {}".format(def_path))

    # deploy task scripts and include files

    log.info("Deploying scripts and include files...")
    log.info("source root of the suite: " + srcdir)
    log.info("scripts search path: " + ", ".join(scripts))
    log.info("includes search path: " + ", ".join(includes))
    # walk the suite tree; find scripts and includes for all tasks.
    try:
        scripts_srcpaths, includes_srcpaths = (
            suite_scripts_finder.find_scripts(suite, filenames=target_files)
        )
    except FileNotFoundError as e:
        log.error(str(e))
        raise DeployError(str(e))
    log.info("target root: " + destdir)
    scripts_destdir = os.path.join(destdir, "ecf")
    includes_destdir = os.path.join(destdir, "inc")
    try:
        log.info("deploying task scripts:")
        scripts_installer.install(scripts_srcpaths, scripts_destdir)
        log.info("deploying include files:")
        scripts_installer.install(includes_srcpaths, includes_destdir)
    except (DeployError, ConfigError) as e:
        log.error(str(e))
        raise DeployError(str(e))

    if task_section is not None:
        # TODO: replace task.cfg include file generation with templating
        taskcfg_path = os.path.join(includes_destdir, "task.cfg")
        log.info("Generating common task include file " + taskcfg_path)
        ksh_code = section_to_ksh(task_section)
        if not dry_run:
            if user_name == getpass.getuser():
                ensure_dir(includes_destdir)
            file_writer(ksh_code, taskcfg_path)

    # Copy deployment config file to the target directory, for the record
    # (unless the config did not come from a file)
    if os.path.isfile(config.origin):
        config_copy_path = os.path.join(destdir, "sdeploy_config_copy")
        with open(config.origin, "r") as f:
            file_config_text = "".join(f.readlines())
        cmdline_config_text = "".join(
            [
                "#     {} = {}\n".format(param, value)
                for param, value in params_values
            ]
        )
        log.info(
            "Copying deployment config {} -> {}".format(
                config.origin, config_copy_path
            )
        )
        if not dry_run:
            file_writer(
                "# Copy of {}\n# autogenerated by sdeploy on {}.\n\n".format(
                    config.origin, datetime.datetime.now()
                ),
                config_copy_path,
            )
            if cmdline_config_text:
                file_writer(
                    "# Additional commandline config was:\n",
                    config_copy_path,
                    append=True,
                )
                file_writer(
                    cmdline_config_text + "\n\n", config_copy_path, append=True
                )
            file_writer(file_config_text, config_copy_path, append=True)

    log.info("Deployment complete.")


def main(argv=sys.argv):

    PROG = os.path.basename(argv[0])

    parser = argparse.ArgumentParser(description="Deploy a suite", prog=PROG)

    parser.add_argument(
        "config",
        metavar="CONFIG",
        type=str,
        help="deployment config (path or ID)",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="count",
        default=0,
        help="verbose (-vv = more verbose)",
    )

    parser.add_argument(
        "-d",
        "--dryrun",
        dest="dry_run",
        default=False,
        action="store_true",
        help="dry run",
    )

    parser.add_argument(
        "-s",
        "--set",
        dest="params_values",
        action="append",
        nargs=2,
        metavar=("PARAM", "VALUE"),
        default=[],
        help="set config parameter (can be used more than once)",
    )

    parser.add_argument(
        "target_files",
        metavar="FILE",
        type=str,
        nargs="*",
        help="target file to be deployed (only a file name, not a path!); if none given, all suite files will be deployed.",
    )

    cmdline = parser.parse_args(argv[1:])

    if cmdline.verbose == 1:
        verbosity_level = log.INFO
    elif cmdline.verbose >= 2:
        verbosity_level = log.DEBUG
    else:
        verbosity_level = log.INFO
    log_message_format = PROG + ": %(levelname)s: %(message)s"
    if cmdline.dry_run:
        log_message_format += " (DRY RUN)"
    log.basicConfig(format=log_message_format, level=verbosity_level)

    try:
        sdeploy(
            config_path=cmdline.config,
            target_files=cmdline.target_files,
            params_values=cmdline.params_values,
            dry_run=cmdline.dry_run,
        )
    except DeployError as e:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
