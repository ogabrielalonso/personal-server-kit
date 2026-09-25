"""personal-server-kit: turn an always-on machine into a personal 24x7 server.

Standard library only, Python 3.9 or newer (macOS ships 3.9 with the
command line tools; Ubuntu 24.04 ships 3.12).
"""

VERSION = "0.2.0"
KIT = "pskit"
CONTRACT_VERSION = 0
# Shown to the owner when the laptop must run the installer. Works once the
# repository is reachable by the person installing (see docs/DECISIONS.md, D7).
INSTALL_ONE_LINER = (
    "curl -fsSL https://raw.githubusercontent.com/ogabrielalonso/personal-server-kit/main/install.sh | bash"
)
