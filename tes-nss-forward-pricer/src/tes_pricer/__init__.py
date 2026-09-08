"""Nelson-Siegel-Svensson calibration on Colombian TES and USD/COP forward pricing.

The package is deliberately split along an I/O boundary:

``tes_pricer.data``
    Everything that touches the network or the filesystem: SUAMECA and Socrata
    clients, raw-schema validation, provenance manifests.
``tes_pricer.math``
    Pure numerics. Modules here accept already validated arrays and DataFrames
    and must never import a network client. This is what makes the numerical
    core testable offline and reproducible from a manifest alone.
``tes_pricer.interface``
    Delivery mechanisms: the CLI and the ``xlwings`` bridge.

Submodules are not imported eagerly so that ``import tes_pricer`` stays cheap
and free of optional third-party dependencies.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
