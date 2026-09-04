# Synthetic data generator (PROJECT_SPEC.md section 15).
#
# ISOLATION RULE: nothing under app.datagen except persist.py may import
# app.models.groundtruth or app.db.groundtruth_session. Ground truth is a
# plain dataclass (GenGroundTruth in app.datagen.models) everywhere in the
# generation logic; only persist.py converts it into the isolated ORM model
# at the very end. Enforced by test_datagen_does_not_import_groundtruth_
# outside_persist in tests/test_datagen.py.
