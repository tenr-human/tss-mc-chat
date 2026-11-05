import logging
import os
import sys
import threading

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from os_ken.base import app_manager
from os_ken.controller import controller


def main():
    controller.CONF(args=sys.argv[1:], project="os-ken")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    manager = app_manager.AppManager.get_instance()
    startup_complete = threading.Event()
    startup_error = []

    def start_apps():
        try:
            manager.load_apps(["sdn.chat_qos_controller"])
            contexts = manager.create_contexts()
            manager.instantiate_apps(**contexts)
        except Exception as error:
            startup_error.append(error)
        finally:
            startup_complete.set()

    threading.Thread(target=start_apps, daemon=True).start()
    startup_complete.wait()
    if startup_error:
        raise startup_error[0]

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        manager.close()


if __name__ == "__main__":
    main()
