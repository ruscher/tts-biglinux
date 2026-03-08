"""Speech-dispatcher specific utility functions."""

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

def try_restart_speechd() -> bool:
    """Try to restart the speech-dispatcher daemon for current user."""
    # Try systemctl first
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "speech-dispatcher"],
            timeout=5,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        logger.info("Restarted speech-dispatcher via systemctl")
        return True
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Fallback: kill existing and let socket activation restart it on next call
    try:
        subprocess.run(
            ["pkill", "-f", "speech-dispatcher"],
            timeout=3,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        logger.info("Killed speech-dispatcher for auto-restart")
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
