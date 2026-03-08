"""Speech-dispatcher specific utility functions."""

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

def try_restart_speechd() -> bool:
    """Try to forcefully restart the speech-dispatcher daemon and its modules."""
    # 0. Stop the socket to prevent auto-activation during cleanup
    try:
        subprocess.run(["systemctl", "--user", "stop", "speech-dispatcher.socket"], timeout=3, check=False)
    except Exception:
        pass

    # 1. Kill any lingering modules and the daemon itself forcefully
    modules = ["sd_rhvoice", "sd_espeak-ng", "sd_generic", "sd_dummy", "speech-dispatcher"]
    for mod in modules:
        try:
            subprocess.run(["pkill", "-9", "-f", mod], timeout=2, check=False)
        except Exception:
            pass

    # 2. Restart the service (this will also bring back the socket if configured correctly)
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "speech-dispatcher.service"],
            timeout=5,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Also ensure socket is back up
        subprocess.run(["systemctl", "--user", "start", "speech-dispatcher.socket"], timeout=3, check=False)
        
        time.sleep(2.5)
        logger.info("Performed deep restart of speech-dispatcher and socket")
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
