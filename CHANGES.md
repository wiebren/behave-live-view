# Version History

## 0.1.0 (unreleased)

* Requires `behave >= 1.4.0` (until it is released: its development version).
  It fixes the captured output that a step shows when it is opened: output of
  the first step(s) of a scenario was missing or cut off (behave #1346), log
  output was missing after a `before_scenario` hook (behave #1347). The
  formats of the command line are taken from the `Configuration`, which
  remembers how it was built (behave #1349).
* Initial version: `LiveRunner` (interactive live view, needs a terminal) and
  `LiveFormatter` (format "live": plain status lines without a terminal).
