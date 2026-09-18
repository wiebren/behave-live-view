# -*- coding: UTF-8 -*-
"""
Detects source files that were changed since they were loaded
(used by the "live" formatter when a test run is run again).

A rerun in the same process reads the feature files, the environment file
and the step files again. But Python modules that they import stay loaded
(``sys.modules``): a changed module is only used after it was reloaded.

* PROJECT MODULES: Loaded modules that are neither part of the Python
  installation (stdlib, site-packages) nor of behave itself.
* RELOAD: Project modules are removed from ``sys.modules``. They are imported
  again when the step files are loaded for the next test run.
* A changed extension module (or the main module) cannot be reloaded.
"""

import importlib
import os.path
import site
import sys
import sysconfig
import time


RELOADABLE_SUFFIXES = (".py",)


def _normalize_path(path):
    return os.path.normcase(os.path.realpath(path))


def select_installation_dirs():
    """Directories whose modules are not watched (Python, packages, behave)."""
    import behave
    dirs = set()
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = sysconfig.get_paths().get(name)
        if path:
            dirs.add(path)
    try:
        dirs.update(site.getsitepackages())
        dirs.add(site.getusersitepackages())
    except AttributeError:
        pass    # -- SOME VIRTUALENVS: Lack these functions.
    dirs.add(os.path.dirname(behave.__file__))
    return tuple(_normalize_path(path) + os.sep for path in dirs if path)


def get_mtime(filename):
    try:
        return os.path.getmtime(filename)
    except OSError:
        return None


def make_display_name(filename):
    """Short name for a file: Relative to the current directory if inside."""
    try:
        relative_name = os.path.relpath(filename)
    except ValueError:
        return filename     # -- WINDOWS: Other drive.
    if relative_name.startswith(os.pardir):
        return filename
    return relative_name


class SourceWatcher:
    """Knows which source files were changed since they were loaded."""

    def __init__(self, now=None, base_dir=None):
        self.modules_loaded_at = now or time.time()
        self.feature_mtimes = {}
        self._installation_dirs = None
        #: Relative file names refer to this directory
        #: (test code may change the current directory).
        self.base_dir = base_dir or os.getcwd()

    def resolve(self, filename):
        return os.path.join(self.base_dir, filename)

    # -- PROJECT MODULES:
    def is_project_file(self, filename):
        if self._installation_dirs is None:
            self._installation_dirs = select_installation_dirs()
        path = _normalize_path(filename)
        return not path.startswith(self._installation_dirs)

    def select_project_modules(self):
        """Select the loaded project modules, as list of (name, filename)."""
        selected = []
        for name, module in list(sys.modules.items()):
            filename = getattr(module, "__file__", None)
            if not filename or not os.path.isfile(filename):
                continue
            if self.is_project_file(filename):
                selected.append((name, filename))
        return selected

    @staticmethod
    def is_reloadable(name, filename):
        return (name != "__main__" and not name.startswith("__mp_")
                and filename.endswith(RELOADABLE_SUFFIXES))

    def select_changed_modules(self):
        """Select the project modules that were changed since they were loaded.

        :return: Tuple (reloadable, not_reloadable) of filename lists.
        """
        reloadable, not_reloadable = [], []
        for name, filename in self.select_project_modules():
            mtime = get_mtime(filename)
            if mtime is None or mtime <= self.modules_loaded_at:
                continue
            target = (reloadable if self.is_reloadable(name, filename)
                      else not_reloadable)
            if filename not in target:
                target.append(filename)
        return sorted(reloadable), sorted(not_reloadable)

    def reload_project_modules(self, now=None):
        """Forget the project modules: They are imported again when needed.

        HINT: All of them, not only the changed ones. Otherwise an unchanged
        module would keep using the old version of a changed one.
        :return: Names of the modules that were removed.
        """
        removed = []
        for name, filename in self.select_project_modules():
            if self.is_reloadable(name, filename):
                del sys.modules[name]
                removed.append(name)
        importlib.invalidate_caches()
        self.modules_loaded_at = now or time.time()
        return sorted(removed)

    # -- FEATURE FILES:
    def remember_feature(self, filename):
        """Remember the state of a feature file that is shown now."""
        self.feature_mtimes[filename] = get_mtime(self.resolve(filename))

    def select_changed_features(self, locations):
        """Select the feature files of these locations that were changed
        since they are shown ("file" or "file:line" locations).
        """
        changed = []
        for filename in self.select_feature_files(locations):
            if filename not in self.feature_mtimes:
                continue
            mtime = get_mtime(self.resolve(filename))
            if mtime != self.feature_mtimes[filename]:
                changed.append(filename)
        return changed

    @staticmethod
    def select_feature_file(location):
        """Select the feature file of a "file" or "file:line" location."""
        filename, _, line = location.rpartition(":")
        if not filename or not line.isdigit():
            filename = location
        return filename

    @classmethod
    def select_feature_files(cls, locations):
        filenames = []
        for location in locations:
            filename = cls.select_feature_file(location)
            if filename not in filenames:
                filenames.append(filename)
        return filenames

    def check_changes(self, locations):
        """Describe what was changed, as dict of display names.

        * reload:   Changed project modules that can be reloaded.
        * warn:     Changed project modules that cannot be reloaded.
        * features: Changed feature files of these locations.
        """
        reloadable, not_reloadable = self.select_changed_modules()
        return {
            "reload": [make_display_name(name) for name in reloadable],
            "warn": [make_display_name(name) for name in not_reloadable],
            "features": self.select_changed_features(locations),
        }
