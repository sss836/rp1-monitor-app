"""Offline URDF illustration, isolated from robot feedback and control APIs."""

import json
import os
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

WEB_ROOT = Path(__file__).with_name('web')
_ASSETS = {
    '/manual-urdf.html': ('manual-urdf.html', 'text/html; charset=utf-8'),
    '/vendor/manual-urdf.bundle.js': ('vendor/manual-urdf.bundle.js', 'text/javascript; charset=utf-8'),
    '/models/rp1-urdf-guide.glb': ('models/rp1-urdf-guide.glb', 'model/gltf-binary'),
    '/models/rp1-urdf-guide.json': ('models/rp1-urdf-guide.json', 'application/json'),
    '/models/rp1-urdf-guide.png': ('models/rp1-urdf-guide.png', 'image/png'),
}
_CSP = ("default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; worker-src 'none'; object-src 'none'; "
        "base-uri 'none'; frame-src 'none'; form-action 'none'")


class GuideAssetServer:
    """An ephemeral loopback-only server with an exact, immutable asset allowlist."""

    def __init__(self, root=WEB_ROOT):
        root = Path(root).resolve()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.asset()

            def do_HEAD(self):
                self.asset(head=True)

            def do_POST(self):
                self.send_error(405)

            def log_message(self, *_):
                pass

            def asset(self, *, head=False):
                parsed = urlsplit(self.path)
                authority = f'127.0.0.1:{self.server.server_port}'
                if self.headers.get('Host') != authority:
                    self.send_error(403)
                    return
                if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or parsed.path not in _ASSETS:
                    self.send_error(404)
                    return
                relative, content_type = _ASSETS[parsed.path]
                path = (root / relative).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    self.send_error(404)
                    return
                try:
                    data = path.read_bytes()
                except OSError:
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', _CSP)
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self.origin = f'http://127.0.0.1:{self._server.server_port}'
        self.page_url = self.origin + '/manual-urdf.html'
        self._thread = threading.Thread(target=partial(self._server.serve_forever, poll_interval=.05),
                                        name='rp1-urdf-assets', daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._server.server_close()
            raise
        self._closed = False

    def allows(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme == 'http' and parsed.netloc == urlsplit(self.origin).netloc
                and parsed.path in _ASSETS and not parsed.query and not parsed.fragment)

    def shutdown(self):
        if not self._closed:
            self._closed = True
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=1)


def _web_view(parent, server):
    """Lazy import: an offscreen/headless Qt session must never start Chromium."""
    from PySide6.QtWebEngineCore import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineSettings,
        QWebEngineUrlRequestInterceptor,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView

    class Interceptor(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):
            if not server.allows(info.requestUrl().toString()):
                info.block(True)

    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, kind, main):
            return url.toString() == server.page_url

        def createWindow(self, kind):
            return None

    profile = QWebEngineProfile(parent)  # unnamed profiles are off the record
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
    interceptor = Interceptor(profile)
    profile.setUrlRequestInterceptor(interceptor)
    view = QWebEngineView(parent)
    page = Page(profile, view)
    page._rp1_interceptor = interceptor
    view.setPage(page)
    settings = page.settings()
    for name in ('JavascriptCanOpenWindows', 'LocalContentCanAccessRemoteUrls',
                 'LocalContentCanAccessFileUrls', 'LocalStorageEnabled', 'AllowRunningInsecureContent'):
        settings.setAttribute(getattr(QWebEngineSettings.WebAttribute, name), False)
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    return view


class UrdfJointGuide(QWidget):
    """set_plan + set_outcome render only model references, never measured angles."""

    def __init__(self, parent=None, *, web_view_factory=None, force_static=False, web_root=WEB_ROOT,
                 allow_expand=True):
        super().__init__(parent)
        self.setMinimumSize(340, 400)
        self.setAccessibleName('RP1 URDF 三维关节引导 · simulated · 非实测')
        self.plan = self.outcome = self.view = self.server = None
        self._ready = self._closed = False
        self._web_view_factory, self._force_static = web_view_factory, force_static
        self._enlarged = None
        self.web_root = Path(web_root)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.badge = QLabel('simulated · RP1 URDF 模型 · 非实测姿态')
        self.badge.setWordWrap(True)
        self.badge.setStyleSheet('background:#dce8fa;color:#234d83;padding:7px;border-radius:6px;font-weight:600;')
        heading = QHBoxLayout()
        heading.addWidget(self.badge, 1)
        self.expand_button = QPushButton('放大模型')
        self.expand_button.setVisible(allow_expand)
        self.expand_button.clicked.connect(self.expand_model)
        heading.addWidget(self.expand_button)
        layout.addLayout(heading)
        self.stack = QStackedWidget()
        self.poster = QLabel()
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setStyleSheet('background:#e7edf5;border-radius:8px;color:#53677c;')
        self.poster.setMinimumHeight(200)
        self.stack.addWidget(self.poster)
        layout.addWidget(self.stack, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color:#53677c;font-size:12px;')
        layout.addWidget(self.status)
        platform = os.environ.get('QT_QPA_PLATFORM', '').lower().split(':')[0]
        headless = platform in ('offscreen', 'minimal') or QGuiApplication.platformName() in ('offscreen', 'minimal')
        if force_static or (web_view_factory is None and headless):
            self._fallback('当前显示环境不支持三维模型')
            return
        try:
            self.server = GuideAssetServer(self.web_root)
            self.destroyed.connect(self.server.shutdown)
            self.view = (web_view_factory or _web_view)(self, self.server)
            self.stack.addWidget(self.view)
            self.view.titleChanged.connect(self._title_changed)
            self.view.loadFinished.connect(self._loaded)
            self.view.load(QUrl(self.server.page_url))
            self.status.setText('正在离线加载真实 URDF 模型…')
            self._show_poster()
        except (ImportError, RuntimeError, OSError) as exc:
            self._fallback(f'三维模型不可用：{type(exc).__name__}')
            if self.server:
                self.server.shutdown()

    @property
    def page_url(self):
        return self.server.page_url if self.server else None

    def _show_poster(self):
        pixmap = QPixmap(str(self.web_root / 'models/rp1-urdf-guide.png'))
        if pixmap.isNull():
            self.poster.setText('RP1 URDF 模型图暂不可用\n不影响实测记录与判定')
        else:
            self.poster.setPixmap(pixmap.scaled(max(260, self.width()), max(200, self.height()-80),
                                               Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        self.stack.setCurrentWidget(self.poster)

    def resizeEvent(self, event):
        if self.stack.currentWidget() is self.poster:
            self._show_poster()
        super().resizeEvent(event)

    def _fallback(self, reason):
        self._ready = False
        self._show_poster()
        self.status.setText(reason + '；显示真实 URDF 静态参考图。静态图不演示当前关节动作。')

    def _loaded(self, ok):
        if not ok and not self._closed:
            self._fallback('三维页面加载失败')

    def _title_changed(self, title):
        if self._closed:
            return
        if title == 'rp1-urdf-ready':
            self._ready = True
            self.stack.setCurrentWidget(self.view)
            self.status.setText('拖动旋转 · 滚轮缩放；模型仅用于引导，不用于实测判定或机械避碰。')
            self._sync()
        elif title.startswith('rp1-urdf-error:'):
            self._fallback(title.removeprefix('rp1-urdf-error:'))

    def set_plan(self, plan, initial_pose_rad=None):
        self.outcome = None  # A prior 28-axis wrist selection cannot leak into a new 24-axis plan.
        if plan is None:
            self.plan = None
        else:
            self.plan = {key: plan.get(key) for key in ('dof', 'joint_names', 'urdf_sha256')}
            self.plan['initial_pose_rad'] = (initial_pose_rad if initial_pose_rad is not None
                                             else plan.get('initial_pose_rad'))
            json.dumps(self.plan, allow_nan=False)
        self._sync()
        if self._enlarged:
            self._enlarged.guide.set_plan(plan, initial_pose_rad)

    def set_outcome(self, outcome):
        self.outcome = ({key: outcome.get(key) for key in
                         ('joint', 'direction', 'requested_rad', 'reference_rad', 'zero_direction', 'check_side_basis')}
                        if outcome else None)
        if self.outcome:
            json.dumps(self.outcome, allow_nan=False)
        self._sync()
        if self._enlarged:
            self._enlarged.guide.set_outcome(outcome)

    def expand_model(self):
        if self._closed:
            return
        if self._enlarged:
            self._enlarged.show()
            self._enlarged.raise_()
            self._enlarged.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('RP1 URDF 模型 · simulated · 非实测')
        dialog.resize(900, 780)
        layout = QVBoxLayout(dialog)
        dialog.guide = UrdfJointGuide(dialog, web_view_factory=self._web_view_factory,
                                     force_static=self._force_static, web_root=self.web_root, allow_expand=False)
        dialog.guide.set_plan(self.plan)
        dialog.guide.set_outcome(self.outcome)
        layout.addWidget(dialog.guide)
        # Keep one owned view for this guide. Closing hides it; reopening reuses the
        # same page, and parent shutdown closes both asset servers deterministically.
        self._enlarged = dialog
        dialog.show()

    def _sync(self):
        if self._ready and not self._closed:
            payload = json.dumps({'plan': self.plan, 'outcome': self.outcome}, allow_nan=False)
            self.view.page().runJavaScript('window.rp1ManualGuide.configure(' + payload + ');')

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._enlarged:
                self._enlarged.guide.shutdown()
                self._enlarged.close()
            if self.view:
                self.view.page().runJavaScript('window.rp1ManualGuide?.shutdown();')
                self.view.stop()
                self.view.close()
        except RuntimeError:
            pass  # Qt may already have destroyed the view while its dialog is closing.
        finally:
            if self.server:
                self.server.shutdown()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
