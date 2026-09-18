# -*- coding: UTF-8 -*-
"""
Unit tests for :mod:`behave_live_view.steps`: Step definitions and a test run
that is run again in the same process (rerun).
"""

import sys
import types

from behave.step_registry import StepRegistry
from behave_live_view.steps import (
    StepRegistrationTracker, forget_reloadable_step_definitions
)


# -----------------------------------------------------------------------------
# TESTS FOR: forget_reloadable_step_definitions()
# -----------------------------------------------------------------------------
class TestForgetReloadableStepDefinitions:
    """A test run may be run again in the same process."""

    @staticmethod
    def make_step_matcher(filename, line=1):
        from behave.model_type import FileLocation

        class FakeStepMatcher:
            def __init__(self):
                self.location = FileLocation(filename, line)

        return FakeStepMatcher()

    def test_keeps_steps_of_loaded_modules_and_forgets_the_others(
            self, tmp_path):
        # -- REGRESSION: Clearing the registry made the steps of a step
        # library undefined in the next run -- its module stays loaded and
        # is not executed again (only the step files are).
        import behave.runner_util as loaded_module
        from behave.step_registry import StepRegistry
        step_file = tmp_path / "steps" / "my_steps.py"
        registry = StepRegistry()
        library_step = self.make_step_matcher(loaded_module.__file__, 10)
        step_file_step = self.make_step_matcher(str(step_file), 3)
        registry.steps["given"] = [library_step, step_file_step]
        registry.steps["when"] = [self.make_step_matcher(str(step_file), 7)]

        assert forget_reloadable_step_definitions(registry) == 2
        assert registry.steps["given"] == [library_step]
        assert registry.steps["when"] == []

    def test_forgets_steps_of_a_module_that_is_not_loaded_anymore(
            self, tmp_path, monkeypatch):
        import sys
        import types
        from behave.step_registry import StepRegistry
        module_file = tmp_path / "steplib_example.py"
        module_file.write_text(u"# -- EMPTY\n")
        module = types.ModuleType("steplib_example")
        module.__file__ = str(module_file)
        monkeypatch.setitem(sys.modules, "steplib_example", module)
        registry = StepRegistry()
        registry.steps["step"] = [self.make_step_matcher(str(module_file))]

        assert forget_reloadable_step_definitions(registry) == 0
        monkeypatch.delitem(sys.modules, "steplib_example")    # -- RELOAD.
        assert forget_reloadable_step_definitions(registry) == 1
        assert registry.steps["step"] == []

    def test_registering_file_decides_not_the_location_of_the_function(
            self, tmp_path):
        # -- REGRESSION: A step function that is wrapped by a decorator of
        # another (loaded) module has its location in this module. It was
        # kept, the edited step file was ignored (or: AmbiguousStep).
        import behave.runner_util as loaded_module
        from behave.step_registry import StepRegistry
        step_file = str(tmp_path / "steps" / "my_steps.py")
        wrapped_step = self.make_step_matcher(loaded_module.__file__, 10)
        wrapped_step.registered_in = step_file
        library_step = self.make_step_matcher(loaded_module.__file__, 20)
        library_step.registered_in = loaded_module.__file__
        registry = StepRegistry()
        registry.steps["given"] = [wrapped_step, library_step]
        assert forget_reloadable_step_definitions(registry) == 1
        assert registry.steps["given"] == [library_step]

    def test_step_registered_by_a_helper_of_a_loaded_module_is_forgotten(
            self, tmp_path):
        # -- REGRESSION: The step file registers its function with a helper
        # of a loaded module (registered in: this module). The edited step
        # file was ignored by a rerun (or: AmbiguousStep).
        import behave.runner_util as loaded_module
        from behave.step_registry import StepRegistry
        step_file = str(tmp_path / "steps" / "my_steps.py")
        helper_step = self.make_step_matcher(step_file, 5)
        helper_step.registered_in = loaded_module.__file__
        registry = StepRegistry()
        registry.steps["given"] = [helper_step]
        assert forget_reloadable_step_definitions(registry) == 1
        assert registry.steps["given"] == []

    def test_marking_the_registering_file_does_not_scan_all_steps(self):
        # -- HINT: Every behave user pays for it when steps are loaded.
        import time
        from behave.step_registry import StepRegistry
        registry = StepRegistry()
        given = registry.make_decorator("given")
        start = time.time()
        with StepRegistrationTracker(registry):
            for index in range(3000):
                given(u"an example step number %d" % index)(lambda ctx: None)
        elapsed = time.time() - start
        assert len(registry.steps["given"]) == 3000
        assert all(step.registered_in == __file__
                   for step in registry.steps["given"])
        assert elapsed < 20.0, "SLOW: %.1fs" % elapsed

    def test_stacked_step_decorators_are_all_marked(self):
        from behave.step_registry import StepRegistry
        registry = StepRegistry()
        given = registry.make_decorator("given")
        when = registry.make_decorator("when")

        with StepRegistrationTracker(registry):
            @given(u"a stacked example step")
            @when(u"a stacked example step")
            def step_example(context):
                pass

        assert registry.steps["given"][0].registered_in == __file__
        assert registry.steps["when"][0].registered_in == __file__

    def test_step_decorator_remembers_the_registering_file(self):
        from behave.step_registry import StepRegistry
        registry = StepRegistry()
        given = registry.make_decorator("given")

        with StepRegistrationTracker(registry):
            @given(u"an example step for registered_in")
            def step_example(context):
                pass

        step_matcher = registry.steps["given"][0]
        assert step_matcher.registered_in == __file__
        assert step_example.__name__ == "step_example"

    def test_bad_step_definitions_do_not_pile_up(self):
        from behave.step_registry import StepRegistry
        registry = StepRegistry()
        registry.error_handler.bad_step_definitions.append(object())
        forget_reloadable_step_definitions(registry)
        assert not registry.error_handler.bad_step_definitions

    def test_uses_the_registry_of_the_runner_by_default(self):
        from behave.runner import the_step_registry
        old_steps = dict((name, steps[:])
                         for name, steps in the_step_registry.steps.items())
        try:
            the_step_registry.steps["given"].append(
                self.make_step_matcher("/no/such/dir/steps/x_steps.py"))
            assert forget_reloadable_step_definitions() >= 1
            assert not any(
                step.location.filename.endswith("x_steps.py")
                for step in the_step_registry.steps["given"])
        finally:
            the_step_registry.steps = old_steps


# -----------------------------------------------------------------------------
# TESTS FOR: StepRegistrationTracker
# -----------------------------------------------------------------------------
HELPER_MODULE_SOURCE = u"""
def register_step(given, step_text, func):
    # -- HELPER: Registers a step function of another file.
    return given(step_text)(func)
"""


class TestStepRegistrationTracker:
    def test_is_needed_to_remember_the_registering_file(self):
        # -- HINT: behave itself does not remember it.
        registry = StepRegistry()
        given = registry.make_decorator("given")

        @given(u"an example step without tracker")
        def step_example(context):
            pass

        step_matcher = registry.steps["given"][0]
        assert getattr(step_matcher, "registered_in", None) in (None, __file__)

    def test_works_with_the_step_decorators_of_behave(self):
        # -- HINT: "from behave import given" is bound to the global registry
        # before a tracker exists; it must see the tracker anyway.
        from behave import given
        from behave.runner import the_step_registry
        old_steps = dict((name, steps[:])
                         for name, steps in the_step_registry.steps.items())
        try:
            with StepRegistrationTracker():
                @given(u"an example step for the tracker of behave-live-view")
                def step_example(context):
                    pass
            step_matcher = the_step_registry.steps["given"][-1]
            assert step_matcher.func is step_example
            assert step_matcher.registered_in == __file__
        finally:
            the_step_registry.steps = old_steps

    def test_helper_of_another_module_is_the_registering_file(self, tmp_path):
        module_file = tmp_path / "steplib_helper_example.py"
        module_file.write_text(HELPER_MODULE_SOURCE)
        module = types.ModuleType("steplib_helper_example")
        module.__file__ = str(module_file)
        exec(compile(HELPER_MODULE_SOURCE, str(module_file), "exec"),
             module.__dict__)
        registry = StepRegistry()
        given = registry.make_decorator("given")

        def step_example(context):
            pass

        with StepRegistrationTracker(registry):
            module.register_step(given, u"a step of a helper", step_example)
        step_matcher = registry.steps["given"][0]
        assert step_matcher.registered_in == str(module_file)
        assert step_matcher.location.filename != str(module_file)

    def test_stop_restores_the_step_registry(self):
        registry = StepRegistry()
        tracker = StepRegistrationTracker(registry)
        assert "add_step_definition" not in vars(registry)
        tracker.start()
        tracker.start()     # -- IDEMPOTENT
        assert "add_step_definition" in vars(registry)
        tracker.stop()
        tracker.stop()      # -- IDEMPOTENT
        assert "add_step_definition" not in vars(registry)

        given = registry.make_decorator("given")

        @given(u"an example step after the tracker was stopped")
        def step_example(context):
            pass
        assert len(registry.steps["given"]) == 1

    def test_registration_problem_is_passed_through(self):
        # -- HINT: An ambiguous step is a problem of the step registry.
        registry = StepRegistry()
        given = registry.make_decorator("given")
        with StepRegistrationTracker(registry):
            given(u"an ambiguous example step")(lambda context: None)
            try:
                given(u"an ambiguous example step")(lambda context: None)
            except Exception:   # pylint: disable=broad-except
                pass    # -- behave < 1.4: raises AmbiguousStep at once.
        assert len(registry.steps["given"]) == 1
