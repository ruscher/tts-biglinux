"""Speech-dispatcher specific utility functions."""

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

def try_restart_speechd() -> bool:
    """Try to forcefully restart the speech-dispatcher daemon and its modules."""
    # 1. Kill any lingering modules first (they are often the cause of the loop)
    modules = ["sd_rhvoice", "sd_espeak-ng", "sd_generic", "sd_dummy"]
    for mod in modules:
        try:
            subprocess.run(["pkill", "-9", "-f", mod], timeout=2, check=False)
        except Exception:
            pass

    # 2. Try systemctl restart (cleanest way)
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "speech-dispatcher"],
            timeout=5,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2.0)
        logger.info("Restarted speech-dispatcher service and modules")
        return True
    except (OSError, subprocess.TimeoutExpired):
        pass

    # 3. Last resort: kill the main daemon forcefully
    try:
        subprocess.run(["pkill", "-9", "-f", "speech-dispatcher"], timeout=3, check=False)
        time.sleep(2.0)
        logger.info("Killed speech-dispatcher forcefully as last resort")
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
