"""Engine core: rules and data without transport.

No imports of ``http``, ``httpx``, ``threading``, ``socket``. The boundary is
enforced by the test ``tests/test_boundaries.py``, not by a promise.
"""
