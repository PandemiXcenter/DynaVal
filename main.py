"""PyInstaller launcher: native settings are loaded before freeze_support."""

from multiprocessing import freeze_support

from dynaval.app import main

if __name__ == "__main__":
    freeze_support()
    main()
