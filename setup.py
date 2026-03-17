"""
setup.py
=========
Builds dashboard.py into a standalone Mac .app using py2app.

Usage:
    pip3 install py2app
    python3 setup.py py2app

Output: dist/Zero Hero.app
Drag to Applications folder or keep on Desktop.
"""

from setuptools import setup

APP      = ["dashboard.py"]
APP_NAME = "Zero Hero"

OPTIONS = {
    "argv_emulation": False,
    "iconfile":       None,       # add a .icns file here if you have one
    "plist": {
        "CFBundleName":             APP_NAME,
        "CFBundleDisplayName":      APP_NAME,
        "CFBundleIdentifier":       "com.zerohero.tradingbot",
        "CFBundleVersion":          "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement":              False,
        "NSHighResolutionCapable":  True,
    },
    "packages": [
        "tkinter",
    ],
}

setup(
    app=APP,
    name=APP_NAME,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
