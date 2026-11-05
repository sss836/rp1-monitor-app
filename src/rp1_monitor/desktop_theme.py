"""Shared light surfaces for native dialogs, independent of the OS theme."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

# These rules also belong to the window stylesheet: its transparent QWidget rule
# otherwise takes precedence over application-level styles on child dialogs.
DIALOG_STYLE = """
/* RP1 light dialog surfaces */
QDialog, QMessageBox, QFileDialog, QInputDialog {
    background:#eef5ff; color:#142441;
}
QDialog QLabel, QDialog QCheckBox, QDialog QRadioButton {
    background:transparent; color:#142441;
}
QDialog QLineEdit, QDialog QComboBox, QDialog QSpinBox, QDialog QDoubleSpinBox,
QDialog QTextEdit, QDialog QPlainTextEdit, QDialog QTextBrowser,
QDialog QAbstractItemView {
    background:#ffffff; color:#142441; alternate-background-color:#f3f7fd;
    selection-background-color:#dae8ff; selection-color:#142441;
}
QDialog QPushButton {
    background:#2b59e8; color:#ffffff; border:1px solid #244cca;
    border-radius:5px; padding:9px 13px; font-weight:600;
}
QDialog QPushButton:hover { background:#244cca; }
QDialog QPushButton:focus { border:2px solid #142441; }
QDialog QPushButton:disabled { background:#e4ebf5; color:#65778e; border-color:#c7d3e3; }
QComboBox QAbstractItemView, QMenu {
    background:#ffffff; color:#142441;
    selection-background-color:#dae8ff; selection-color:#142441;
}
QMenu::item:selected { background:#dae8ff; color:#142441; }
QToolTip { background:#ffffff; color:#142441; border:1px solid #a7bcd8; }
"""


def install_light_theme(application):
    """Set one application palette, with no changes to chart/plot backgrounds."""
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#eef5ff",
        QPalette.ColorRole.WindowText: "#142441",
        QPalette.ColorRole.Base: "#ffffff",
        QPalette.ColorRole.AlternateBase: "#f3f7fd",
        QPalette.ColorRole.Text: "#142441",
        QPalette.ColorRole.Button: "#eaf1fc",
        QPalette.ColorRole.ButtonText: "#142441",
        QPalette.ColorRole.ToolTipBase: "#ffffff",
        QPalette.ColorRole.ToolTipText: "#142441",
        QPalette.ColorRole.Highlight: "#2b59e8",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#2b59e8",
        QPalette.ColorRole.LinkVisited: "#4165a0",
        QPalette.ColorRole.PlaceholderText: "#576c88",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#65778e"))
    application.setPalette(palette)
    # Native platform dialogs may follow a dark OS theme and ignore Qt styles.
    application.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    hints = application.styleHints()
    if hasattr(hints, "setColorScheme"):
        hints.setColorScheme(Qt.ColorScheme.Light)
    if DIALOG_STYLE not in application.styleSheet():
        application.setStyleSheet(application.styleSheet() + DIALOG_STYLE)
