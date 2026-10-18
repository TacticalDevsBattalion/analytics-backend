import os

# The background prewarm pass must not run against test stores and sources.
os.environ.setdefault("CACHE_PREWARM_ENABLED", "false")
