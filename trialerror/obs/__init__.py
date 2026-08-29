"""``trialerror.obs`` -- OTel GenAI span emission over a local Arize Phoenix
sink (design Section 4.5, Section 10, Section 12's M12 row).

Package layout:

- :mod:`trialerror.obs.semconv` -- pinned OTel GenAI attribute-name vocabulary
  (zero third-party imports; see its own docstring for why).
- :mod:`trialerror.obs.tracer` -- the process-wide tracer singleton: real OTel
  SDK + OTLP/HTTP-to-Phoenix when the ``obs`` extra is installed, a
  structurally-identical no-op stand-in when it isn't.
- :mod:`trialerror.obs.spans` -- span wrappers for the design's four emission
  points (launch, retrieval, verification/gate, job lifecycle) that other
  modules opt into, WITHOUT ``trialerror/jobs/``, ``trialerror/budget/``, or
  ``trialerror/artifacts/`` ever being edited or monkeypatched.
- :mod:`trialerror.obs.state` -- the durable, cross-process span-drop counter
  behind the ``obs_span_drop_counter`` doctor check.
- :mod:`trialerror.obs.checks` -- the two doctor checks the M2 integration
  contract names (``obs_exporter_reachable``, ``obs_span_drop_counter``).

``trialerror/cli/obs.py`` (auto-discovered CLI group: ``trialerror obs {status,
start-phoenix, smoke}``) is the human/agent-facing surface over all of the
above.
"""

from trialerror.obs.tracer import is_available

__all__ = ["is_available"]
