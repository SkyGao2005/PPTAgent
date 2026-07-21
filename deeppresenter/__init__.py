"""DeepPresenter - An agentic and reflective presentation generation system"""

__version__ = "1.1.37"
__author__ = "Hao Zheng"
__email__ = "wszh712811@gmail.com"

import os
import warnings

# DeepPresenter is designed for Linux/macOS. Individual sub-packages (e.g.
# deeppresenter.server.models) may work on other platforms for development
# and testing. The platform gate is enforced at the CLI / web entry points.
if os.name != "posix":
    warnings.warn(
        "DeepPresenter is designed for Linux and macOS. "
        "Some features may not work on this platform.",
        RuntimeWarning,
        stacklevel=2,
    )
