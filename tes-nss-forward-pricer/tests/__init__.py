"""Test suite for tes-nss-forward-pricer.

Three tiers, separated by marker so the default run stays offline and fast:

``unit``
    Pure numerics and architecture rules. No network, no QuantLib.
``integration``
    Live SUAMECA / datos.gov.co calls. Skipped unless ``-m integration``.
``benchmark``
    Numerical cross-checks against QuantLib. Skipped when QuantLib is absent.
"""
