"""本地 WebView HTTP：客户端掐连接时不应把 traceback 打到终端。"""

import errno
import io
import socketserver
import unittest
from unittest.mock import patch

import app


class QuietLocalHttpTest(unittest.TestCase):
    def test_client_disconnects_are_quiet(self):
        self.assertTrue(app.is_client_disconnect(ConnectionResetError(54, "Connection reset by peer")))
        self.assertTrue(app.is_client_disconnect(BrokenPipeError()))
        self.assertTrue(app.is_client_disconnect(ConnectionAbortedError()))
        self.assertTrue(app.is_client_disconnect(OSError(errno.ECONNRESET, "reset")))
        self.assertFalse(app.is_client_disconnect(ValueError("boom")))
        self.assertFalse(app.is_client_disconnect(None))

    def test_handle_error_swallows_connection_reset(self):
        app.install_quiet_local_http()
        buf = io.StringIO()
        server = socketserver.TCPServer
        with patch("sys.stderr", buf):
            try:
                raise ConnectionResetError(54, "Connection reset by peer")
            except ConnectionResetError:
                socketserver.BaseServer.handle_error(object.__new__(server), None, ("127.0.0.1", 1))
        self.assertNotIn("Connection reset by peer", buf.getvalue())

    def test_handle_error_still_prints_other_exceptions(self):
        app.install_quiet_local_http()
        buf = io.StringIO()
        server = socketserver.TCPServer
        with patch("sys.stderr", buf):
            try:
                raise RuntimeError("not a disconnect")
            except RuntimeError:
                socketserver.BaseServer.handle_error(object.__new__(server), None, ("127.0.0.1", 1))
        self.assertIn("not a disconnect", buf.getvalue())
