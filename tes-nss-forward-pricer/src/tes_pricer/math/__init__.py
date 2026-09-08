"""Pure numerical core: day counts, bond maths, NSS, OIS bootstrap, FX, greeks.

Hard architectural rule, enforced by ``ruff`` (``flake8-tidy-imports``
banned-api) and by ``tests/unit/test_architecture.py``: **no module in this
package may import a network client** (``requests``, ``sodapy``) or read data
from a URL. Every function takes already validated arrays or DataFrames as
arguments and returns plain numerical objects.
"""
