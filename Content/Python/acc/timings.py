"""Wall-clock phase measurements, including engine ticks between generator yields."""
import time
from contextlib import contextmanager


class Timings:
    def __init__(self):
        self.seconds = {}

    @contextmanager
    def measure(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            self.seconds[name] = self.seconds.get(name, 0.0) + time.monotonic() - started
