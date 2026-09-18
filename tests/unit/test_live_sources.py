# -*- coding: UTF-8 -*-
"""
Unit tests for :mod:`behave_live_view.sources`:
Detect source files that were changed since they were loaded.
"""

import importlib
import os
import sys
import time

import pytest

from behave_live_view.sources import SourceWatcher, make_display_name


MODULE_NAME = "behave_live_sources_example"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory with one imported module (outside of Python)."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    module_file = project_dir / (MODULE_NAME + ".py")
    module_file.write_text(u"VALUE = 1\n")
    monkeypatch.chdir(project_dir)
    monkeypatch.syspath_prepend(str(project_dir))
    importlib.invalidate_caches()
    module = importlib.import_module(MODULE_NAME)
    loaded_modules = dict(sys.modules)
    yield module_file, module
    # -- RESTORE: A reload forgets all project modules -- here these are
    # the test modules, too (they must not be imported a second time).
    sys.modules.pop(MODULE_NAME, None)
    for name, loaded_module in loaded_modules.items():
        if name != MODULE_NAME:
            sys.modules.setdefault(name, loaded_module)


def touch_later(path, seconds=10):
    future = time.time() + seconds
    os.utime(str(path), (future, future))


class TestProjectModules:
    def test_project_module_is_selected(self, project):
        names = [name for name, _ in SourceWatcher().select_project_modules()]
        assert MODULE_NAME in names

    def test_installed_modules_are_not_selected(self, project):
        names = [name for name, _ in SourceWatcher().select_project_modules()]
        assert "os" not in names
        assert "pytest" not in names
        assert not any(name == "behave" or name.startswith("behave.")
                       for name in names)

    def test_unchanged_module_is_not_reported(self, project):
        reloadable, not_reloadable = SourceWatcher().select_changed_modules()
        assert reloadable == [] and not_reloadable == []

    def test_changed_module_is_reported(self, project):
        module_file, _ = project
        watcher = SourceWatcher()
        touch_later(module_file)
        reloadable, not_reloadable = watcher.select_changed_modules()
        assert [os.path.basename(name) for name in reloadable] == \
               [MODULE_NAME + ".py"]
        assert not_reloadable == []

    def test_reload_makes_the_next_import_use_the_new_code(self, project):
        module_file, old_module = project
        watcher = SourceWatcher()
        module_file.write_text(u"VALUE = 2\n")
        touch_later(module_file)
        removed = watcher.reload_project_modules(now=time.time() + 60)
        assert MODULE_NAME in removed
        assert MODULE_NAME not in sys.modules
        new_module = importlib.import_module(MODULE_NAME)
        assert (old_module.VALUE, new_module.VALUE) == (1, 2)
        # -- LOADED NOW: Not reported as changed anymore.
        assert watcher.select_changed_modules() == ([], [])

    def test_changed_extension_module_cannot_be_reloaded(self, project,
                                                         monkeypatch):
        module_file, module = project
        binary_file = module_file.with_suffix(".so")
        binary_file.write_bytes(b"")
        monkeypatch.setattr(module, "__file__", str(binary_file))
        watcher = SourceWatcher()
        touch_later(binary_file)
        reloadable, not_reloadable = watcher.select_changed_modules()
        assert reloadable == []
        assert [os.path.basename(name) for name in not_reloadable] == \
               [MODULE_NAME + ".so"]
        assert MODULE_NAME not in watcher.reload_project_modules()
        assert MODULE_NAME in sys.modules

    def test_main_module_cannot_be_reloaded(self):
        assert not SourceWatcher.is_reloadable("__main__", "main.py")
        assert not SourceWatcher.is_reloadable("__mp_main__", "main.py")
        assert SourceWatcher.is_reloadable("steps_util", "steps_util.py")


class TestFeatureFiles:
    @pytest.mark.parametrize("location, expected", [
        ("features/a.feature", "features/a.feature"),
        ("features/a.feature:12", "features/a.feature"),
        ("C:\\x\\a.feature:3", "C:\\x\\a.feature"),
        ("C:\\x\\a.feature", "C:\\x\\a.feature"),
    ])
    def test_select_feature_file(self, location, expected):
        assert SourceWatcher.select_feature_file(location) == expected

    def test_changed_feature_file_is_reported(self, tmp_path):
        feature_file = tmp_path / "a.feature"
        feature_file.write_text(u"Feature: A\n")
        other_file = tmp_path / "b.feature"
        other_file.write_text(u"Feature: B\n")
        watcher = SourceWatcher()
        watcher.remember_feature(str(feature_file))
        watcher.remember_feature(str(other_file))
        location = "%s:12" % feature_file
        assert watcher.select_changed_features([location]) == []
        touch_later(feature_file)
        touch_later(other_file)
        # -- ONLY: Feature files of the locations that are run again.
        assert watcher.select_changed_features([location, location]) == \
               [str(feature_file)]

    def test_unknown_feature_file_is_not_reported(self, tmp_path):
        watcher = SourceWatcher()
        assert watcher.select_changed_features(["new.feature:3"]) == []

    def test_check_changes_describes_all_kinds(self, project, tmp_path):
        module_file, _ = project
        feature_file = module_file.parent / "a.feature"
        feature_file.write_text(u"Feature: A\n")
        watcher = SourceWatcher()
        watcher.remember_feature("a.feature")
        touch_later(module_file)
        touch_later(feature_file)
        changes = watcher.check_changes(["a.feature:2"])
        assert changes == {"reload": [MODULE_NAME + ".py"], "warn": [],
                           "features": ["a.feature"]}


def test_make_display_name_is_relative_inside_of_current_directory(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inside = tmp_path / "steps" / "util.py"
    assert make_display_name(str(inside)) == os.path.join("steps", "util.py")
    outside = tmp_path.parent / "other.py"
    assert make_display_name(str(outside)) == str(outside)
