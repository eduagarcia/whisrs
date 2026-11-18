"""Open the whisrs window, or bring an open one to the front.

One window at a time: a second launch (the tray icon, the app launcher)
finds the first through an abstract unix socket, asks it to come forward,
and exits.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import threading

SOCKET_NAME = "\0whisrs-ui-" + str(os.getuid())
WEB = pathlib.Path(__file__).resolve().parent / "web"


def focus_own_window() -> None:
    """Bring this process's window forward on Hyprland (nothing elsewhere)."""
    try:
        clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True,
                                            text=True, timeout=3).stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return
    for c in clients:
        if c.get("pid") == os.getpid():
            address = c.get("address")
            # Hyprland 0.57 takes Lua dispatches; older ones the classic form.
            for cmd in (f"hl.dsp.focus({{ window = 'address:{address}' }})",
                        f"focuswindow address:{address}"):
                r = subprocess.run(["hyprctl", "dispatch", cmd], capture_output=True, text=True)
                if r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower():
                    return


def claim_instance() -> socket.socket | None:
    """The listening socket of the first instance, or `None` after asking
    the running one to show itself."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(SOCKET_NAME)
    except OSError:
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(SOCKET_NAME)
            c.sendall(b"show\n")
            c.close()
            return None
        except OSError:
            return None
    s.listen(4)
    return s


def serve(sock: socket.socket, window) -> None:
    while True:
        conn, _ = sock.accept()
        with conn:
            if conn.recv(64).strip() == b"show":
                try:
                    window.restore()
                    window.show()
                except Exception:  # noqa: BLE001 - best effort
                    pass
                focus_own_window()


def main() -> int:
    sock = claim_instance()
    if sock is None:
        return 0

    # The window's app id (Hyprland's class): matches whisrs-ui.desktop.
    try:
        from gi.repository import GLib

        GLib.set_prgname("whisrs-ui")
        GLib.set_application_name("whisrs")
    except ImportError:
        pass

    import webview

    from .api import Api

    api = Api()
    window = webview.create_window(
        "whisrs",
        url=(WEB / "index.html").as_uri(),
        js_api=api,
        width=1240,
        height=820,
        min_size=(860, 560),
        background_color="#16202B",
        text_select=True,
    )
    api._window = window
    threading.Thread(target=serve, args=(sock, window), daemon=True).start()
    debug = os.environ.get("WHISRS_UI_DEBUG") == "1"
    webview.start(gui="gtk", debug=debug, private_mode=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
