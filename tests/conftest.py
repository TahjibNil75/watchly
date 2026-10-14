import os

# Settings are read when `app` is first imported; keep the scheduler off.
os.environ.setdefault("MONITORING_ENABLED", "false")
