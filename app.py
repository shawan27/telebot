from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from typing import Optional

from PySide6.QtCore import QDate, QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config import AppConfig, DEFAULT_ALLOWED_MEDIA, parse_date
from main import run as run_copier
from utils import cleanup_paths


PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = PROJECT_DIR / "config.json"


@dataclass
class SavedSettings:
    api_id: str = ""
    api_hash: str = ""
    session_name: str = "telegram_backfill"
    source_channel: str = ""
    target_channel: str = ""
    start_date: str = "2024-01-01"
    end_date: str = "2026-05-15"
    scan_limit: str = ""
    send_limit: str = ""
    keywords: str = ""
    links_file: str = ""


class QtLogHandler(logging.Handler):
    def __init__(self, signal: Signal) -> None:
        super().__init__()
        self.signal = signal

    def emit(self, record: logging.LogRecord) -> None:
        self.signal.emit(self.format(record))


class CopierWorker(QObject):
    progress = Signal(dict)
    log_line = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, config: AppConfig, cancel_event: Event) -> None:
        super().__init__()
        self.config = config
        self.cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            handler = QtLogHandler(self.log_line)
            asyncio.run(
                run_copier(
                    self.config,
                    progress_callback=self.progress.emit,
                    cancel_event=self.cancel_event,
                    log_handler=handler,
                )
            )
            stopped = self.cancel_event.is_set()
            self.finished.emit(not stopped, "Stopped" if stopped else "Finished")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Telegram Backfill Copier")
        self.resize(1180, 760)

        self.worker_thread: Optional[QThread] = None
        self.worker: Optional[CopierWorker] = None
        self.cancel_event: Optional[Event] = None

        self._build_ui()
        self._load_settings()
        self._sync_filter_state()

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([470, 710])
        self.setCentralWidget(splitter)

    def _build_left_panel(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_setup_tab(), "Setup")
        tabs.addTab(self._build_copy_tab(), "Copy")
        tabs.addTab(self._build_advanced_tab(), "Advanced")

        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.addWidget(tabs)
        layout.addWidget(self._build_action_bar())
        return wrapper

    def _build_setup_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        api_box = QGroupBox("Telegram API")
        api_form = QFormLayout(api_box)
        self.api_id_input = QLineEdit()
        self.api_hash_input = QLineEdit()
        self.api_hash_input.setEchoMode(QLineEdit.Password)
        self.session_input = QLineEdit("telegram_backfill")
        self.save_settings_button = QPushButton("Save Settings")
        self.save_settings_button.clicked.connect(self._save_settings)
        api_form.addRow("API ID", self.api_id_input)
        api_form.addRow("API Hash", self.api_hash_input)
        api_form.addRow("Session name", self.session_input)
        api_form.addRow("", self.save_settings_button)

        channel_box = QGroupBox("Channels")
        channel_form = QFormLayout(channel_box)
        self.source_input = QLineEdit()
        self.target_input = QLineEdit()
        channel_form.addRow("Source channel", self.source_input)
        channel_form.addRow("Target channel", self.target_input)

        helper = QLabel(
            "Settings are saved locally in config.json. The API hash is never written to copy.log."
        )
        helper.setWordWrap(True)
        helper.setStyleSheet("color: #555;")

        layout.addWidget(api_box)
        layout.addWidget(channel_box)
        layout.addWidget(helper)
        layout.addStretch(1)
        return tab

    def _build_copy_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        mode_box = QGroupBox("Mode")
        mode_layout = QVBoxLayout(mode_box)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(
            ["Date range backfill", "Message links", "Message links file"]
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_layout.addWidget(self.mode_combo)

        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self._build_date_mode())
        self.mode_stack.addWidget(self._build_links_mode())
        self.mode_stack.addWidget(self._build_links_file_mode())
        mode_layout.addWidget(self.mode_stack)

        filters_box = self._build_filters_box()
        storage = QLabel(
            "Media files are downloaded temporarily into tmp_downloads and deleted after upload. "
            "processed.sqlite3 stores message IDs/status only. copy.log stores logs."
        )
        storage.setWordWrap(True)
        storage.setStyleSheet("color: #555;")

        layout.addWidget(mode_box)
        layout.addWidget(filters_box)
        layout.addWidget(storage)
        layout.addStretch(1)
        return tab

    def _build_date_mode(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.start_date_input = QDateEdit()
        self.start_date_input.setCalendarPopup(True)
        self.start_date_input.setDisplayFormat("yyyy-MM-dd")
        self.start_date_input.setDate(QDate.fromString("2024-01-01", "yyyy-MM-dd"))
        self.end_date_input = QDateEdit()
        self.end_date_input.setCalendarPopup(True)
        self.end_date_input.setDisplayFormat("yyyy-MM-dd")
        self.end_date_input.setDate(QDate.fromString("2026-05-15", "yyyy-MM-dd"))
        self.scan_limit_input = QLineEdit()
        self.scan_limit_input.setPlaceholderText("empty or 0 = unlimited")
        self.send_limit_input = QLineEdit()
        self.send_limit_input.setPlaceholderText("empty or 0 = unlimited")
        form.addRow("Start date", self.start_date_input)
        form.addRow("End date", self.end_date_input)
        form.addRow("Scan limit", self.scan_limit_input)
        form.addRow("Send limit", self.send_limit_input)
        return widget

    def _build_links_mode(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.links_text = QPlainTextEdit()
        self.links_text.setPlaceholderText(
            "Paste Telegram links here. One per line, or comma-separated."
        )
        layout.addWidget(self.links_text)
        return widget

    def _build_links_file_mode(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        row = QHBoxLayout()
        self.links_file_input = QLineEdit()
        self.links_file_input.setReadOnly(True)
        choose_button = QPushButton("Select links.txt")
        choose_button.clicked.connect(self._choose_links_file)
        row.addWidget(self.links_file_input, 1)
        row.addWidget(choose_button)
        layout.addLayout(row)
        note = QLabel("Blank lines are ignored. Link mode force-recopies already copied messages.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        layout.addWidget(note)
        return widget

    def _build_filters_box(self) -> QGroupBox:
        box = QGroupBox("Filters")
        layout = QVBoxLayout(box)
        self.copy_everything_check = QCheckBox("Copy everything")
        self.copy_everything_check.setChecked(True)
        self.copy_everything_check.toggled.connect(self._sync_filter_state)

        grid = QGridLayout()
        self.include_text_check = QCheckBox("Include text posts")
        self.include_photos_check = QCheckBox("Include photos")
        self.include_videos_check = QCheckBox("Include videos")
        self.include_docs_check = QCheckBox("Include documents")
        self.include_pdfs_check = QCheckBox("Include PDFs")
        self.include_archives_check = QCheckBox("Include ZIP/RAR/other files")
        for checkbox in [
            self.include_text_check,
            self.include_photos_check,
            self.include_videos_check,
            self.include_docs_check,
            self.include_pdfs_check,
            self.include_archives_check,
        ]:
            checkbox.setChecked(True)
        grid.addWidget(self.include_text_check, 0, 0)
        grid.addWidget(self.include_photos_check, 0, 1)
        grid.addWidget(self.include_videos_check, 1, 0)
        grid.addWidget(self.include_docs_check, 1, 1)
        grid.addWidget(self.include_pdfs_check, 2, 0)
        grid.addWidget(self.include_archives_check, 2, 1)

        self.keywords_input = QLineEdit()
        self.keywords_input.setPlaceholderText("Optional: PDF, setup, signal")

        layout.addWidget(self.copy_everything_check)
        layout.addLayout(grid)
        form = QFormLayout()
        form.addRow("Keywords", self.keywords_input)
        layout.addLayout(form)
        return box

    def _build_advanced_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.retry_attempts_input = QLineEdit("8")
        self.send_delay_input = QLineEdit("1.5")
        self.history_wait_input = QLineEdit("1.0")
        form.addRow("Retry attempts", self.retry_attempts_input)
        form.addRow("Send delay seconds", self.send_delay_input)
        form.addRow("History wait seconds", self.history_wait_input)
        return tab

    def _build_action_bar(self) -> QWidget:
        widget = QWidget()
        layout = QGridLayout(widget)
        self.dry_run_button = QPushButton("Dry Run")
        self.start_button = QPushButton("Start Copy")
        self.stop_button = QPushButton("Stop")
        self.clear_temp_button = QPushButton("Clear Temp Downloads")
        self.open_logs_button = QPushButton("Open Logs")
        self.open_folder_button = QPushButton("Open Project Folder")
        self.stop_button.setEnabled(False)

        self.dry_run_button.clicked.connect(lambda: self._start_copy(dry_run=True))
        self.start_button.clicked.connect(lambda: self._start_copy(dry_run=False))
        self.stop_button.clicked.connect(self._stop_copy)
        self.clear_temp_button.clicked.connect(self._clear_temp)
        self.open_logs_button.clicked.connect(self._open_logs)
        self.open_folder_button.clicked.connect(self._open_project_folder)

        layout.addWidget(self.dry_run_button, 0, 0)
        layout.addWidget(self.start_button, 0, 1)
        layout.addWidget(self.stop_button, 0, 2)
        layout.addWidget(self.clear_temp_button, 1, 0)
        layout.addWidget(self.open_logs_button, 1, 1)
        layout.addWidget(self.open_folder_button, 1, 2)
        return widget

    def _build_right_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        progress_box = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_box)

        form = QFormLayout()
        self.status_label = QLabel("idle")
        self.source_id_label = QLabel("-")
        self.filename_label = QLabel("-")
        self.transfer_label = QLabel("0 / 0 MB")
        self.speed_label = QLabel("0 MB/s")
        form.addRow("Current status", self.status_label)
        form.addRow("Source message ID", self.source_id_label)
        form.addRow("Current filename", self.filename_label)
        form.addRow("Transferred", self.transfer_label)
        form.addRow("Approx speed", self.speed_label)

        self.download_bar = QProgressBar()
        self.upload_bar = QProgressBar()
        self.download_bar.setRange(0, 100)
        self.upload_bar.setRange(0, 100)

        counters = QGridLayout()
        self.scanned_label = QLabel("0")
        self.copied_label = QLabel("0")
        self.skipped_label = QLabel("0")
        self.failed_label = QLabel("0")
        counters.addWidget(QLabel("Scanned"), 0, 0)
        counters.addWidget(self.scanned_label, 0, 1)
        counters.addWidget(QLabel("Copied"), 0, 2)
        counters.addWidget(self.copied_label, 0, 3)
        counters.addWidget(QLabel("Skipped"), 1, 0)
        counters.addWidget(self.skipped_label, 1, 1)
        counters.addWidget(QLabel("Failed"), 1, 2)
        counters.addWidget(self.failed_label, 1, 3)

        progress_layout.addLayout(form)
        progress_layout.addWidget(QLabel("Download progress"))
        progress_layout.addWidget(self.download_bar)
        progress_layout.addWidget(QLabel("Upload progress"))
        progress_layout.addWidget(self.upload_bar)
        progress_layout.addLayout(counters)

        logs_box = QGroupBox("Live Logs")
        logs_layout = QVBoxLayout(logs_box)
        self.logs_text = QTextEdit()
        self.logs_text.setReadOnly(True)
        self.logs_text.setLineWrapMode(QTextEdit.NoWrap)
        self.logs_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        logs_layout.addWidget(self.logs_text)

        layout.addWidget(progress_box)
        layout.addWidget(logs_box, 1)
        return wrapper

    def _on_mode_changed(self, index: int) -> None:
        self.mode_stack.setCurrentIndex(index)

    def _sync_filter_state(self) -> None:
        enabled = not self.copy_everything_check.isChecked()
        for checkbox in [
            self.include_text_check,
            self.include_photos_check,
            self.include_videos_check,
            self.include_docs_check,
            self.include_pdfs_check,
            self.include_archives_check,
        ]:
            checkbox.setEnabled(enabled)
            if not enabled:
                checkbox.setChecked(True)

    def _choose_links_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select message links file",
            str(PROJECT_DIR),
            "Text files (*.txt);;All files (*)",
        )
        if path:
            self.links_file_input.setText(path)

    def _settings_from_ui(self) -> SavedSettings:
        return SavedSettings(
            api_id=self.api_id_input.text().strip(),
            api_hash=self.api_hash_input.text().strip(),
            session_name=self.session_input.text().strip() or "telegram_backfill",
            source_channel=self.source_input.text().strip(),
            target_channel=self.target_input.text().strip(),
            start_date=self.start_date_input.date().toString("yyyy-MM-dd"),
            end_date=self.end_date_input.date().toString("yyyy-MM-dd"),
            scan_limit=self.scan_limit_input.text().strip(),
            send_limit=self.send_limit_input.text().strip(),
            keywords=self.keywords_input.text().strip(),
            links_file=self.links_file_input.text().strip(),
        )

    def _save_settings(self) -> None:
        settings = self._settings_from_ui()
        SETTINGS_FILE.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
        self._append_log(f"Saved settings to {SETTINGS_FILE}")

    def _load_settings(self) -> None:
        if not SETTINGS_FILE.exists():
            return
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings = SavedSettings(**{**asdict(SavedSettings()), **raw})
        except Exception as exc:
            QMessageBox.warning(self, "Settings", f"Could not load config.json: {exc}")
            return

        self.api_id_input.setText(settings.api_id)
        self.api_hash_input.setText(settings.api_hash)
        self.session_input.setText(settings.session_name)
        self.source_input.setText(settings.source_channel)
        self.target_input.setText(settings.target_channel)
        self.start_date_input.setDate(QDate.fromString(settings.start_date, "yyyy-MM-dd"))
        self.end_date_input.setDate(QDate.fromString(settings.end_date, "yyyy-MM-dd"))
        self.scan_limit_input.setText(settings.scan_limit)
        self.send_limit_input.setText(settings.send_limit)
        self.keywords_input.setText(settings.keywords)
        self.links_file_input.setText(settings.links_file)

    def _parse_optional_int(self, value: str) -> Optional[int]:
        value = value.strip()
        if not value:
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None

    def _parse_links_text(self, text: str) -> list[str]:
        return [item.strip() for item in re.split(r"[\n,]+", text) if item.strip()]

    def _read_links_file(self, path_value: str) -> list[str]:
        if not path_value:
            raise ValueError("Choose a links.txt file first.")
        path = Path(path_value).expanduser()
        if not path.is_file():
            raise ValueError(f"Links file does not exist: {path}")
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _allowed_media(self) -> set[str]:
        if self.copy_everything_check.isChecked():
            return set(DEFAULT_ALLOWED_MEDIA)

        allowed: set[str] = set()
        if self.include_photos_check.isChecked():
            allowed.add("photo")
        if self.include_videos_check.isChecked():
            allowed.add("video")
        if self.include_docs_check.isChecked():
            allowed.add("document")
        if self.include_pdfs_check.isChecked():
            allowed.add("pdf")
        if self.include_archives_check.isChecked():
            allowed.update({"zip", "other"})
        return allowed

    def _build_config(self, dry_run: bool) -> AppConfig:
        api_id = self.api_id_input.text().strip()
        api_hash = self.api_hash_input.text().strip()
        source = self.source_input.text().strip()
        target = self.target_input.text().strip()
        if not api_id or not api_hash or not source or not target:
            raise ValueError("API ID, API Hash, Source channel, and Target channel are required.")

        mode_index = self.mode_combo.currentIndex()
        message_links: list[str] = []
        if mode_index == 1:
            message_links = self._parse_links_text(self.links_text.toPlainText())
            if not message_links:
                raise ValueError("Paste at least one Telegram message link.")
        elif mode_index == 2:
            message_links = self._read_links_file(self.links_file_input.text().strip())
            if not message_links:
                raise ValueError("The selected links file has no links.")

        keywords = [
            item.strip()
            for item in self.keywords_input.text().split(",")
            if item.strip()
        ]

        return AppConfig(
            api_id=int(api_id),
            api_hash=api_hash,
            source_channel=source,
            target_channel=target,
            start_date=parse_date(self.start_date_input.date().toString("yyyy-MM-dd")),
            end_date=parse_date(self.end_date_input.date().toString("yyyy-MM-dd"), end_of_day=True),
            keywords=keywords,
            allowed_media=self._allowed_media(),
            include_photos=self.include_photos_check.isChecked(),
            include_text_only_keyword_posts=self.include_text_check.isChecked(),
            scan_limit=self._parse_optional_int(self.scan_limit_input.text()),
            send_limit=self._parse_optional_int(self.send_limit_input.text()),
            message_links=message_links,
            dry_run=dry_run,
            session_name=self.session_input.text().strip() or "telegram_backfill",
            database_path=PROJECT_DIR / "processed.sqlite3",
            temp_dir=PROJECT_DIR / "tmp_downloads",
            log_file=PROJECT_DIR / "copy.log",
            retry_attempts=int(self.retry_attempts_input.text().strip() or "8"),
            send_delay_seconds=float(self.send_delay_input.text().strip() or "1.5"),
            history_wait_seconds=float(self.history_wait_input.text().strip() or "1.0"),
        )

    def _start_copy(self, dry_run: bool) -> None:
        if self.worker_thread is not None:
            return
        try:
            config = self._build_config(dry_run)
        except Exception as exc:
            QMessageBox.warning(self, "Configuration", str(exc))
            return

        self._save_settings()
        self._reset_progress()
        self.cancel_event = Event()
        self.worker = CopierWorker(config, self.cancel_event)
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.log_line.connect(self._append_log)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.finished.connect(self._on_thread_finished)
        self.worker_thread.start()
        self._set_running(True)

    def _stop_copy(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.status_label.setText("stopping")
            self._append_log("Stop requested. Waiting for current Telegram operation to unwind.")

    def _on_worker_finished(self, ok: bool, message: str) -> None:
        self._append_log(message)
        self._set_running(False)
        if not ok and message not in {"Stopped", "Finished"}:
            QMessageBox.warning(self, "Copy finished", message)

    def _on_thread_finished(self) -> None:
        self.worker_thread = None
        self.worker = None
        self.cancel_event = None

    def _set_running(self, running: bool) -> None:
        self.dry_run_button.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _reset_progress(self) -> None:
        self.status_label.setText("idle")
        self.source_id_label.setText("-")
        self.filename_label.setText("-")
        self.transfer_label.setText("0 / 0 MB")
        self.speed_label.setText("0 MB/s")
        self.download_bar.setValue(0)
        self.upload_bar.setValue(0)
        for label in [self.scanned_label, self.copied_label, self.skipped_label, self.failed_label]:
            label.setText("0")

    @Slot(dict)
    def _on_progress(self, payload: dict) -> None:
        status = payload.get("status")
        if status:
            self.status_label.setText(str(status))
        if "source_id" in payload:
            self.source_id_label.setText(str(payload["source_id"]))
        if "filename" in payload:
            self.filename_label.setText(str(payload["filename"]))

        for key, label in [
            ("scanned", self.scanned_label),
            ("copied", self.copied_label),
            ("skipped", self.skipped_label),
            ("failed", self.failed_label),
        ]:
            if key in payload:
                label.setText(str(payload[key]))

        if "percent" in payload:
            percent = int(payload.get("percent") or 0)
            if status == "downloading":
                self.download_bar.setValue(percent)
            elif status == "uploading":
                self.upload_bar.setValue(percent)

        if "current_bytes" in payload:
            current = int(payload.get("current_bytes") or 0)
            total = int(payload.get("total_bytes") or 0)
            self.transfer_label.setText(f"{current / 1048576:.2f} / {total / 1048576:.2f} MB")
        if "speed_mbps" in payload:
            self.speed_label.setText(f"{float(payload['speed_mbps']):.2f} MB/s")

    @Slot(str)
    def _append_log(self, line: str) -> None:
        self.logs_text.append(line)

    def _clear_temp(self) -> None:
        temp_dir = PROJECT_DIR / "tmp_downloads"
        cleanup_paths([temp_dir])
        self._append_log(f"Cleared {temp_dir}")

    def _open_logs(self) -> None:
        log_file = PROJECT_DIR / "copy.log"
        if not log_file.exists():
            log_file.touch()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_file)))

    def _open_project_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(PROJECT_DIR)))


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
