import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault("OSKEN_HUB_TYPE", "eventlet")

from os_ken.base import app_manager
from os_ken.controller import controller


def main():
    controller.CONF(args=sys.argv[1:], project="os-ken")
    try:
        app_manager.AppManager.run_apps(["sdn.chat_qos_controller"])
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
