import os


# Exercise the real Prometheus collectors throughout the test suite. Production
# remains opt-in because METRICS_ENABLED defaults to false.
os.environ.setdefault("METRICS_ENABLED", "true")
