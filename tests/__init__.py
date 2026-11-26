"""Tests of Corridoor; run them with ``python -m unittest discover -s tests -t .``."""

import warnings

# The sessions start short-lived child processes and never wait for every pipe
# object; the resulting ResourceWarnings are noise in the test output.
warnings.simplefilter("ignore", ResourceWarning)
