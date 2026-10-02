# behave-live-view

An interactive live view of a [behave](https://github.com/behave/behave) test
run in the terminal: one compact status line per feature, rule, scenario
(or example row) and step -- pending, running, passed, failed -- that you can
expand, filter, open in your editor and run again while the view stays open.

It needs no patched behave: it plugs into behave's runner and formatter
extension-points. It requires `behave >= 1.4.0`.

## Installation

behave v1.4.0 is not released yet. Until it is, install behave from its
repository, too:

```console
$ pip install git+https://github.com/behave/behave@main \
              git+https://github.com/wiebren/behave-live-view
```

Select the runner in your behave config-file -- that is all:

```ini
# -- FILE: behave.ini
[behave]
runner = behave_live_view:LiveRunner
```

or on the command line: `behave -r behave_live_view:LiveRunner`.

`LiveRunner` does not run any tests itself. behave's normal runner does
that; `LiveRunner` only gives the view the main thread of the process (which a
terminal UI needs) and lets the tests run in a background thread.

## What you get

**In a terminal**, the interactive view:

* A tree that is updated while the tests run, collapsed to feature lines.
  A failure is expanded down to its failed step and its error message.
* It follows the execution point until you navigate (`f`: follow again).
* A step can be opened: its table/text, the captured stdout/stderr/log output
  and its error. Hook output is shown with its scenario/feature, an example
  row shows its values.
* Filter by text or `@tag` (`/`), view modes all / failed / not passed (`v`),
  next / previous failure (`n` / `N`), help overlay (`?`).
* Location bar with the feature file line and the step definition; open them
  in your editor or IDE (`o` / `d`): `BEHAVE_EDITOR`, the IDE of the terminal,
  `VISUAL` / `EDITOR`.
* Copy a line with its details, or its location (`y` / `Y`).
* **Rerun** the selected line (`r`) or all failures (`R`) in the same process.
  Step files are loaded again and changed Python modules are detected (the
  view asks to reload them), so you can fix a step and run it again at once.
  A changed feature file is run completely and rebuilt in the view.
* `q` / `ctrl+c` stops a running test run like a KeyboardInterrupt; the view
  stays open until you quit it. Then a summary of the failures and everything
  that was printed meanwhile (like behave's summary) is written.
* The exit status reflects the final state: a failure that passes in a rerun
  is fine, a scenario that never ran is not.

| Key | Action |
|---|---|
| `↑` `↓` / `k` `j` | move |
| `→` `←` / `l` `h`, `enter`, `space` | expand, collapse, toggle |
| `e` / `c` | expand failures / collapse all |
| `/`, `esc` | filter by text or `@tag`, clear filter |
| `v` | view mode: all, failed, not passed |
| `n` / `N` | next / previous failure |
| `f` | follow the execution point |
| `r` / `R` | rerun the selected line / all failures |
| `o` / `d` | open the feature file / the step definition in the editor |
| `y` / `Y` | copy the line with details / its location |
| `?` | help |
| `q`, `ctrl+c` | stop the test run, then quit |

**Without a terminal** (CI, pipes, output files) or with another console
formatter, nothing interactive happens: the "live" formatter writes plain,
append-only status lines -- one line per finished feature, failures expanded:

```
✘ Feature: Alice  1/2 · 1 failed  0.00s  features/alice.feature
  ✘ Scenario: A2
    ✘ When a step fails
      ASSERT FAILED: XFAIL-STEP
✔ Feature: Bob  1/1  0.00s  features/bob.feature
```

So it is safe to keep the runner configured permanently.

## Formatter selection

`LiveRunner` uses the "live" formatter on the console if you did not select
another console formatter (it replaces behave's default formatter).
`behave -f pretty` or `-f plain` show that formatter as usual, without the view.
Formatters that write to an output file (`-f json -o report.json`) are kept.

To use the format name `live` yourself (for example with `--outfile`),
register it:

```ini
# -- FILE: behave.ini
[behave.formatters]
live = behave_live_view:LiveFormatter
```

Another runner class can run the tests (it must be a normal behave runner):
`behave -D live_view.runner=my.package:MyRunner`.

## Parallel test runs

With [behave-parallel-runner](https://github.com/wiebren/behave-parallel-runner),
the features run in worker processes and the view shows all of them while
they run. Let it run the tests:

```ini
# -- FILE: behave.ini
[behave]
runner = behave_live_view:LiveRunner

[behave.userdata]
live_view.runner = behave_parallel_runner:ParallelRunner
```

and use `behave --jobs 4` (or `-D live_view.runner=...` on the command line).

* The "live" formatter of each worker process sends its events to the view
  in the parent process over a local connection (authenticated). The
  userdata parameter `live_view.events` tells the workers where; the
  parallel runner sends the userdata to its workers.
* A rerun (`r` / `R`) starts new worker processes, which load the current
  step files.
* `q` / `ctrl+c` stops the test run like a KeyboardInterrupt stops the
  parallel runner: its worker processes are terminated at once.
* Without a terminal, each worker writes the plain status lines of its
  features; the parallel runner prints them feature by feature.

## Limitations

* **The tests run in a background thread** while the view is shown. Test code
  that needs the main thread (signal handlers, some GUI toolkits) does not
  work in the view; run such tests with another formatter (`-f plain`).
* **Stopping a test run** raises a KeyboardInterrupt in that thread. It
  arrives when the thread executes Python code again: a long blocking call
  (like one `time.sleep(60)`) ends first.
* **`--jobs` needs a parallel test runner** (see: [Parallel test runs](#parallel-test-runs)).
  With behave's own runner, the tests run sequentially.
* A rerun reloads step files and changed Python modules of your project.
  Changes that cannot be reloaded are shown as a warning; restart behave then.

## Development

```console
$ pip install -e ".[testing]"
$ pytest
```

The functional tests run `behave` in a pseudo-terminal to test the
interactive view for real (not on Windows).

## License

BSD-2-Clause, the same license as behave.
