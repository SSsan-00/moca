from subprocess import CalledProcessError

from tomlkit.exceptions import TOMLKitError


DASHBOARD_ERRORS = (OSError, ValueError, RuntimeError, CalledProcessError, KeyError, TypeError, AttributeError, TOMLKitError)
