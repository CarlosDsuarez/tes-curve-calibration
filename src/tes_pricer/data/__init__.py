"""I/O layer: remote acquisition, raw-schema validation and provenance.

This is the only package allowed to perform network calls. Anything leaving
this layer towards ``tes_pricer.math`` must already have passed through
:mod:`tes_pricer.data.validators`.
"""
