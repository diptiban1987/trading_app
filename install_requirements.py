import subprocess
import sys
import os
import logging
from typing import List, Tuple

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_command(command: List[str]) -> Tuple[bool, str]:
    """Run a command and return success status and output"""
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True
        )
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def install_package(package: str) -> bool:
    """Install a single package"""
    logger.info(f"Installing {package}...")
    success, output = run_command([sys.executable, "-m", "pip", "install", package])
    if success:
        logger.info(f"Successfully installed {package}")
    else:
        logger.error(f"Failed to install {package}: {output}")
    return success

def main():
    # List of packages to install
    packages = [
        "flask",
        "flask-socketio",
        "pandas",
        "numpy",
        "yfinance",
        "scipy",
        "reportlab",
        "requests",
        "werkzeug"
    ]

    # Special handling for TA-Lib on Windows
    if os.name == 'nt':  # Windows
        logger.info("Detected Windows OS, installing TA-Lib...")
        # First try to install the wheel
        success, _ = run_command([sys.executable, "-m", "pip", "install", "TA_Lib-0.4.24-cp39-cp39-win_amd64.whl"])
        if not success:
            logger.info("Wheel installation failed, trying direct installation...")
            success = install_package("TA-Lib")
    else:
        # For non-Windows systems, try direct installation
        success = install_package("TA-Lib")

    # Install other packages
    for package in packages:
        install_package(package)

if __name__ == "__main__":
    main() 