# -*- coding: UTF-8 -*-
"""
Step definitions and a test run that is run again in the same process.

A rerun loads the step files again (they are executed again), but a module
that is still loaded, like a step library that a step file imports, is not
executed again. Therefore, before a rerun:

* the step definitions that will be registered again must be forgotten
  (otherwise: behave reports them as ambiguous step)
* the step definitions that will NOT be registered again must be kept
  (otherwise: their steps are undefined in the rerun)

To decide this, the file that registers a step definition is remembered
(:class:`StepRegistrationTracker`). It may differ from the file of the step
function, for example if the function is wrapped by a decorator of another
module or if it is registered with a helper of another module.
"""

import os.path
import sys


def normalize_filename(filename):
    return os.path.normcase(os.path.realpath(filename))


def select_step_registry(registry=None):
    if registry is None:
        # pylint: disable=import-outside-toplevel
        from behave.runner import the_step_registry
        registry = the_step_registry
    return registry


class StepRegistrationTracker:
    """Remembers the file that registers a step definition while it is active
    (as attribute "registered_in" of the step matcher).

    .. code-block:: python

        with StepRegistrationTracker():
            runner.run()    # -- Loads the step definitions.

    HINT: Wraps "add_step_definition()" of the step registry OBJECT while
    active. The step decorators look up this method when they are used,
    therefore no class (and no behave code) needs to be patched.
    """
    ATTRIBUTE_NAME = "registered_in"

    def __init__(self, registry=None):
        self.registry = select_step_registry(registry)
        #: Files whose frames are skipped to find the registering file.
        self.internal_files = set([normalize_filename(__file__)])
        registry_module = sys.modules.get(type(self.registry).__module__)
        registry_file = getattr(registry_module, "__file__", None)
        if registry_file:
            self.internal_files.add(normalize_filename(registry_file))
        self._active = False

    # -- CONTEXT MANAGER:
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()

    def start(self):
        if self._active:
            return
        add_step_definition = self.registry.add_step_definition
        tracker = self

        def tracking_add_step_definition(keyword, step_text, func):
            result = add_step_definition(keyword, step_text, func)
            # -- HINT: Frame of the caller (normally: a step decorator).
            tracker.mark_registered_in(func, tracker.find_registering_file(
                sys._getframe(1)))  # pylint: disable=protected-access
            return result

        # -- HINT: Instance attribute, hides the method of the class.
        self.registry.add_step_definition = tracking_add_step_definition
        self._active = True

    def stop(self):
        if self._active:
            self._active = False
            # -- HINT: Removes the instance attribute only.
            self.registry.__dict__.pop("add_step_definition", None)

    # -- INTERNALS:
    def find_registering_file(self, frame):
        """Find the file that registers a step definition: The first caller
        outside of the step registry (its decorators) and this module.
        """
        while frame is not None:
            filename = frame.f_code.co_filename
            if normalize_filename(filename) not in self.internal_files:
                return filename
            frame = frame.f_back
        return None

    def mark_registered_in(self, func, filename):
        if not filename:
            return
        # -- HINT: A new step definition is the last one of its step type
        # (do not scan all of them: many step definitions may exist).
        for step_matchers in self.registry.steps.values():
            step_matcher = step_matchers[-1] if step_matchers else None
            if (getattr(step_matcher, "func", None) is func
                    and getattr(step_matcher, self.ATTRIBUTE_NAME,
                                None) is None):
                try:
                    setattr(step_matcher, self.ATTRIBUTE_NAME, filename)
                except AttributeError:
                    pass    # -- OTHER KIND: Of step matcher.


def forget_reloadable_step_definitions(registry=None):
    """Forget the step definitions that are loaded again by the next test run
    in this process: those of step files (they are executed again) and those
    of modules that are not loaded anymore (they are imported again).

    Step definitions of modules that are still loaded are kept, for example
    of a step library that a step file imports. Such a module is not executed
    again, its step definitions would be lost otherwise.

    HINT: The registry object in use must be changed (early binding by the
    runner and the step decorators). A step definition with a known location
    is not registered again, that means its old code would stay in use.

    :return: Number of step definitions that were forgotten.
    """
    registry = select_step_registry(registry)
    loaded_files = set()
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            loaded_files.add(normalize_filename(filename))

    def is_kept(step_matcher):
        # -- HINT: Kept only if neither the file that has registered the step
        # definition nor the file of its function is executed again:
        #   * A function of a step file may be wrapped by a decorator of
        #     a loaded module (location: this module).
        #   * A step file may register its function with a helper of
        #     a loaded module (registered in: this module).
        location = getattr(step_matcher, "location", None)
        filenames = [
            getattr(step_matcher, StepRegistrationTracker.ATTRIBUTE_NAME, None),
            getattr(location, "filename", None)]
        filenames = [filename for filename in filenames if filename]
        if not filenames:
            return False
        return all(normalize_filename(filename) in loaded_files
                   for filename in filenames)

    count = 0
    for step_type, step_matchers in registry.steps.items():
        kept = [step_matcher for step_matcher in step_matchers
                if is_kept(step_matcher)]
        count += len(step_matchers) - len(kept)
        registry.steps[step_type] = kept
    # -- HINT: Bad step definitions are reported again if they still exist.
    error_handler = getattr(registry, "error_handler", None)
    if error_handler is not None:
        error_handler.clear()
    return count
