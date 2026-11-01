"""Private PC-only crash stacks; never logs telemetry, passwords or locals."""

import faulthandler
import os
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def _write_frames(log, stack):
    # print_tb also prints source lines, which may contain literal credentials.
    # Persist locations only, without source text, values or frame locals.
    if log.tell() >= 256 * 1024:
        return
    for frame, number in traceback.walk_tb(stack):
        log.write(f'  File "{frame.f_code.co_filename}", line {number}, in {frame.f_code.co_name}\n')


@contextmanager
def desktop_failure_diagnostics(state_dir):
    """Keep native fatal stacks and uncaught Python frame locations per launch.

    This observes errors, without suppressing exceptions or restarting workers.
    Exception messages/values and frame locals are deliberately not persisted.
    """
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    log, path = None, None
    old_hook, old_qt = sys.excepthook, None
    was_enabled = faulthandler.is_enabled()
    configured = False
    try:
        try:
            directory = Path(state_dir) / "desktop-diagnostics"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("desktop diagnostics requires a real directory")
            stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
            path = directory / f"desktop-{stamp}-{os.getpid()}-{uuid4().hex[:8]}.log"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            log = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
            log.write("RP1 desktop diagnostics; frame locations only, no telemetry or locals\n")
            faulthandler.enable(file=log, all_threads=True)

            def exception_hook(kind, value, stack):
                log.write("uncaught_python_type=" + kind.__name__ + "\n")
                _write_frames(log, stack)
                log.flush()
                old_hook(kind, value, stack)

            def qt_message(kind, context, message):
                if kind == QtMsgType.QtFatalMsg:
                    # Known lifecycle failure has a fixed description; never
                    # persist arbitrary Qt strings that could contain a URL.
                    reason = ("QThread destroyed while running" if "QThread" in message
                              and "still running" in message else "Qt fatal message")
                    log.write(reason + "\n")
                    log.flush()
                if old_qt is not None:
                    old_qt(kind, context, message)
                else:
                    sys.__stderr__.write(message + "\n")

            sys.excepthook = exception_hook
            old_qt = qInstallMessageHandler(qt_message)
            configured = True
        except (OSError, ValueError):
            if log is not None:
                log.close()
                log = None
            sys.__stderr__.write("RP1 desktop: private crash log unavailable; terminal diagnostics remain active.\n")
        try:
            yield path if configured else None
        except BaseException as error:
            if log is not None:
                log.write("desktop_exit_exception_type=" + type(error).__name__ + "\n")
                _write_frames(log, error.__traceback__)
            raise
        else:
            if log is not None:
                log.write("desktop_exit=normal\n")
    finally:
        if configured:
            sys.excepthook = old_hook
            qInstallMessageHandler(old_qt)
            if was_enabled:
                faulthandler.enable(file=sys.__stderr__, all_threads=True)
            else:
                faulthandler.disable()
        if log is not None:
            log.close()
