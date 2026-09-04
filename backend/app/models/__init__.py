# Deliberately empty: nothing is re-exported here.
#
# app.models.financial and app.models.operational hold the agent-runtime
# entities and are safe to import from anywhere.
#
# app.models.groundtruth holds isolated ground-truth data and must ONLY be
# imported by the synthetic data generator and the evaluation harness — never
# by the matcher, verifier, divergence tracer, AI tools, orchestrator, or any
# API route that serves live case processing. Keeping this __init__.py empty
# means `import app.models` alone can never pull groundtruth in by accident.
