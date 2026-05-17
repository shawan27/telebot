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

from PySide6.QtCore import QDate, QObject, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
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
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from config import AppConfig, DEFAULT_ALLOWED_MEDIA, parse_date
from main import parse_message_target, run as run_copier
from utils import cleanup_paths


PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = PROJECT_DIR / "config.json"


@dataclass
class SavedSettings:
    api_id: str = ""
    api_hash: str = ""
    session_name: str = "telegram_backfill"
    phone: str = ""
    source_channel: str = ""
    target_channel: str = ""
    start_date: str = "2024-01-01"
    end_date: str = "2026-05-15"
    scan_limit: str = ""
    send_limit: str = ""
    keywords: str = ""
    links_file: str = ""
    retry_attempts: str = "8"
    send_delay: str = "1.5"
    history_wait: str = "1.0"


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


class LoginWorker(QObject):
    result = Signal(str, dict)
    failed = Signal(str)

    def __init__(self, action: str, payload: dict) -> None:
        super().__init__()
        self.action = action
        self.payload = payload

    @Slot()
    def run(self) -> None:
        try:
            asyncio.run(self._run_async())
        except Exception as exc:
            self.failed.emit(str(exc))

    async def _run_async(self) -> None:
        api_id = int(self.payload["api_id"])
        api_hash = self.payload["api_hash"]
        session_path = self.payload["session_path"]
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        try:
            if self.action == "status":
                if await client.is_user_authorized():
                    self.result.emit("connected", await self._account_payload(client))
                else:
                    state = "expired" if Path(f"{session_path}.session").exists() else "not_connected"
                    self.result.emit(state, {})
                return

            if self.action == "send_code":
                sent = await client.send_code_request(self.payload["phone"])
                self.result.emit("code_sent", {"phone_code_hash": sent.phone_code_hash})
                return

            if self.action == "verify_code":
                try:
                    await client.sign_in(
                        phone=self.payload["phone"],
                        code=self.payload["code"],
                        phone_code_hash=self.payload["phone_code_hash"],
                    )
                except SessionPasswordNeededError:
                    self.result.emit("password_needed", {})
                    return
                self.result.emit("connected", await self._account_payload(client))
                return

            if self.action == "verify_password":
                await client.sign_in(password=self.payload["password"])
                self.result.emit("connected", await self._account_payload(client))
                return

            raise ValueError(f"Unknown login action: {self.action}")
        finally:
            await client.disconnect()

    async def _account_payload(self, client: TelegramClient) -> dict:
        me = await client.get_me()
        name = " ".join(part for part in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if part)
        username = getattr(me, "username", None)
        phone = getattr(me, "phone", None)
        display = f"@{username}" if username else (name or phone or "Telegram account")
        return {"display": display, "name": name, "username": username or "", "phone": phone or ""}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Telegram Copier")
        self.resize(1120, 760)

        self.connected = False
        self.phone_code_hash = ""
        self.login_thread: Optional[QThread] = None
        self.login_worker: Optional[LoginWorker] = None
        self.copy_thread: Optional[QThread] = None
        self.copy_worker: Optional[CopierWorker] = None
        self.cancel_event: Optional[Event] = None

        self._build_ui()
        self._load_settings()
        self._sync_filter_state()
        self._set_connected(False, "Not connected")
        self._check_session_status(silent=True)

    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_connect_tab(), "1. Connect")
        self.tabs.addTab(self._build_copy_tab(), "2. Copy")
        self.tabs.addTab(self._build_progress_tab(), "3. Progress")
        self.tabs.addTab(self._build_settings_tab(), "4. Settings")
        self.setCentralWidget(self.tabs)

    def _build_connect_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        intro = QLabel("Connect your Telegram account once. The session is saved locally so future runs do not ask for a code.")
        intro.setWordWrap(True)

        api_box = QGroupBox("Telegram Login")
        form = QFormLayout(api_box)
        self.api_id_input = QLineEdit()
        self.api_hash_input = QLineEdit()
        self.api_hash_input.setEchoMode(QLineEdit.Password)
        self.session_input = QLineEdit("telegram_backfill")
        self.phone_input = QLineEdit()
        self.phone_input.setPlaceholderText("+15551234567")
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Login code from Telegram")
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("2FA password")

        form.addRow("API ID", self.api_id_input)
        form.addRow("API Hash", self.api_hash_input)
        form.addRow("Session name", self.session_input)
        form.addRow("Phone number", self.phone_input)
        form.addRow("Login code", self.code_input)
        form.addRow("2FA password", self.password_input)

        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("font-weight: 600;")
        self.account_label = QLabel("-")
        form.addRow("Status", self.status_label)
        form.addRow("Account", self.account_label)

        buttons = QGridLayout()
        self.save_settings_button = QPushButton("Save Settings")
        self.check_session_button = QPushButton("Check Session")
        self.send_code_button = QPushButton("Send Login Code")
        self.verify_code_button = QPushButton("Verify Code")
        self.verify_password_button = QPushButton("Verify Password")
        self.reset_session_button = QPushButton("Disconnect / Reset Session")
        buttons.addWidget(self.save_settings_button, 0, 0)
        buttons.addWidget(self.check_session_button, 0, 1)
        buttons.addWidget(self.send_code_button, 1, 0)
        buttons.addWidget(self.verify_code_button, 1, 1)
        buttons.addWidget(self.verify_password_button, 2, 0)
        buttons.addWidget(self.reset_session_button, 2, 1)

        self.save_settings_button.clicked.connect(self._save_settings)
        self.check_session_button.clicked.connect(lambda: self._check_session_status(silent=False))
        self.send_code_button.clicked.connect(self._send_login_code)
        self.verify_code_button.clicked.connect(self._verify_code)
        self.verify_password_button.clicked.connect(self._verify_password)
        self.reset_session_button.clicked.connect(self._reset_session)
        self.api_id_input.textChanged.connect(self._on_login_settings_changed)
        self.api_hash_input.textChanged.connect(self._on_login_settings_changed)
        self.session_input.textChanged.connect(self._on_login_settings_changed)

        self.code_input.setEnabled(False)
        self.verify_code_button.setEnabled(False)
        self.password_input.setEnabled(False)
        self.verify_password_button.setEnabled(False)

        safety = QLabel("The API hash, login code, and 2FA password are not written to copy.log.")
        safety.setWordWrap(True)
        safety.setStyleSheet("color: #555;")

        layout.addWidget(intro)
        layout.addWidget(api_box)
        layout.addLayout(buttons)
        layout.addWidget(safety)
        layout.addStretch(1)
        return tab

    def _build_copy_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        step = QLabel("Choose where to copy from and what to copy. Message links are the easiest mode.")
        step.setWordWrap(True)

        channel_box = QGroupBox("Channels")
        channel_form = QFormLayout(channel_box)
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText("@source_channel, optional for public message links")
        self.target_input = QLineEdit()
        self.target_input.setPlaceholderText("@target_channel")
        channel_form.addRow("Source channel", self.source_input)
        channel_form.addRow("Target channel", self.target_input)
        source_help = QLabel(
            "For message links, the app reads the source from public t.me links. "
            "Use Source only as a fallback for plain message IDs or links without a public channel username. "
            "Date range backfill still requires Source."
        )
        source_help.setWordWrap(True)
        source_help.setStyleSheet("color: #555;")

        mode_box = QGroupBox("Copy Mode")
        mode_layout = QVBoxLayout(mode_box)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Copy message links", "Copy from links.txt", "Date range backfill"])
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_layout.addWidget(self.mode_combo)

        self.links_text = QPlainTextEdit()
        self.links_text.setPlaceholderText("Paste Telegram links here. One per line, or comma-separated.")
        mode_layout.addWidget(self.links_text)

        file_row = QHBoxLayout()
        self.links_file_input = QLineEdit()
        self.links_file_input.setReadOnly(True)
        self.select_links_file_button = QPushButton("Select links.txt")
        self.select_links_file_button.clicked.connect(self._choose_links_file)
        file_row.addWidget(self.links_file_input, 1)
        file_row.addWidget(self.select_links_file_button)
        mode_layout.addLayout(file_row)

        self.date_hint_label = QLabel("Date range and limits are in Settings.")
        self.date_hint_label.setStyleSheet("color: #555;")
        mode_layout.addWidget(self.date_hint_label)

        buttons = QHBoxLayout()
        self.dry_run_button = QPushButton("Dry Run")
        self.start_button = QPushButton("Start Copy")
        self.dry_run_button.clicked.connect(lambda: self._start_copy(dry_run=True))
        self.start_button.clicked.connect(lambda: self._start_copy(dry_run=False))
        buttons.addWidget(self.dry_run_button)
        buttons.addWidget(self.start_button)

        storage = QLabel(
            "Media files are downloaded temporarily into tmp_downloads, uploaded, then deleted. "
            "processed.sqlite3 stores message IDs/status only. copy.log stores logs."
        )
        storage.setWordWrap(True)
        storage.setStyleSheet("color: #555;")

        layout.addWidget(step)
        layout.addWidget(channel_box)
        layout.addWidget(source_help)
        layout.addWidget(mode_box)
        layout.addLayout(buttons)
        layout.addWidget(storage)
        layout.addStretch(1)
        self._on_mode_changed(0)
        return tab

    def _build_progress_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        progress_box = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_box)
        form = QFormLayout()
        self.copy_status_label = QLabel("idle")
        self.source_id_label = QLabel("-")
        self.filename_label = QLabel("-")
        self.transfer_label = QLabel("0 / 0 MB")
        self.speed_label = QLabel("0 MB/s")
        form.addRow("Current status", self.copy_status_label)
        form.addRow("Source message ID", self.source_id_label)
        form.addRow("Current file", self.filename_label)
        form.addRow("Transferred", self.transfer_label)
        form.addRow("Speed", self.speed_label)
        progress_layout.addLayout(form)

        self.download_bar = QProgressBar()
        self.upload_bar = QProgressBar()
        self.download_bar.setRange(0, 100)
        self.upload_bar.setRange(0, 100)
        progress_layout.addWidget(QLabel("Download"))
        progress_layout.addWidget(self.download_bar)
        progress_layout.addWidget(QLabel("Upload"))
        progress_layout.addWidget(self.upload_bar)

        counters = QGridLayout()
        self.copied_label = QLabel("0")
        self.skipped_label = QLabel("0")
        self.failed_label = QLabel("0")
        self.scanned_label = QLabel("0")
        counters.addWidget(QLabel("Copied"), 0, 0)
        counters.addWidget(self.copied_label, 0, 1)
        counters.addWidget(QLabel("Skipped"), 0, 2)
        counters.addWidget(self.skipped_label, 0, 3)
        counters.addWidget(QLabel("Failed"), 1, 0)
        counters.addWidget(self.failed_label, 1, 1)
        counters.addWidget(QLabel("Scanned"), 1, 2)
        counters.addWidget(self.scanned_label, 1, 3)
        progress_layout.addLayout(counters)

        action_row = QHBoxLayout()
        self.stop_button = QPushButton("Stop")
        self.clear_temp_button = QPushButton("Clear Temp Downloads")
        self.open_logs_button = QPushButton("Open Logs")
        self.open_folder_button = QPushButton("Open Project Folder")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_copy)
        self.clear_temp_button.clicked.connect(self._clear_temp)
        self.open_logs_button.clicked.connect(self._open_logs)
        self.open_folder_button.clicked.connect(self._open_project_folder)
        action_row.addWidget(self.stop_button)
        action_row.addWidget(self.clear_temp_button)
        action_row.addWidget(self.open_logs_button)
        action_row.addWidget(self.open_folder_button)

        logs_box = QGroupBox("Live Logs")
        logs_box.setCheckable(True)
        logs_box.setChecked(True)
        logs_layout = QVBoxLayout(logs_box)
        self.logs_text = QTextEdit()
        self.logs_text.setReadOnly(True)
        self.logs_text.setLineWrapMode(QTextEdit.NoWrap)
        self.logs_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        logs_layout.addWidget(self.logs_text)
        logs_box.toggled.connect(self.logs_text.setVisible)

        layout.addWidget(progress_box)
        layout.addLayout(action_row)
        layout.addWidget(logs_box, 1)
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        date_box = QGroupBox("Date Range Backfill")
        date_form = QFormLayout(date_box)
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
        date_form.addRow("Start date", self.start_date_input)
        date_form.addRow("End date", self.end_date_input)
        date_form.addRow("Scan limit", self.scan_limit_input)
        date_form.addRow("Send limit", self.send_limit_input)

        filters_box = QGroupBox("Filters")
        filters_layout = QVBoxLayout(filters_box)
        self.copy_everything_check = QCheckBox("Copy everything")
        self.copy_everything_check.setChecked(True)
        self.copy_everything_check.toggled.connect(self._sync_filter_state)
        filters_layout.addWidget(self.copy_everything_check)
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
        filters_layout.addLayout(grid)
        keyword_form = QFormLayout()
        self.keywords_input = QLineEdit()
        self.keywords_input.setPlaceholderText("Optional: PDF, setup, signal")
        keyword_form.addRow("Keywords", self.keywords_input)
        filters_layout.addLayout(keyword_form)

        advanced_box = QGroupBox("Advanced")
        advanced_form = QFormLayout(advanced_box)
        self.retry_attempts_input = QLineEdit("8")
        self.send_delay_input = QLineEdit("1.5")
        self.history_wait_input = QLineEdit("1.0")
        advanced_form.addRow("Retry attempts", self.retry_attempts_input)
        advanced_form.addRow("Send delay seconds", self.send_delay_input)
        advanced_form.addRow("History wait seconds", self.history_wait_input)

        layout.addWidget(date_box)
        layout.addWidget(filters_box)
        layout.addWidget(advanced_box)
        layout.addStretch(1)
        return tab

    def _session_base_path(self) -> Path:
        name = self.session_input.text().strip() or "telegram_backfill"
        clean_name = Path(name).name
        if clean_name.endswith(".session"):
            clean_name = clean_name[:-8]
        return PROJECT_DIR / clean_name

    def _session_file_paths(self) -> list[Path]:
        base = self._session_base_path()
        return [Path(f"{base}.session"), Path(f"{base}.session-journal")]

    def _telegram_payload(self) -> dict:
        api_id = self.api_id_input.text().strip()
        api_hash = self.api_hash_input.text().strip()
        if not api_id or not api_hash:
            raise ValueError("Enter your API ID and API Hash first.")
        return {
            "api_id": api_id,
            "api_hash": api_hash,
            "session_path": str(self._session_base_path()),
            "phone": self.phone_input.text().strip(),
        }

    def _start_login_action(self, action: str, payload: dict) -> None:
        if self.login_thread is not None:
            return
        self.login_worker = LoginWorker(action, payload)
        self.login_thread = QThread()
        self.login_worker.moveToThread(self.login_thread)
        self.login_thread.started.connect(self.login_worker.run)
        self.login_worker.result.connect(self._on_login_result)
        self.login_worker.failed.connect(self._on_login_failed)
        self.login_worker.result.connect(self.login_thread.quit)
        self.login_worker.failed.connect(self.login_thread.quit)
        self.login_thread.finished.connect(self.login_thread.deleteLater)
        self.login_thread.finished.connect(self._on_login_thread_finished)
        self._set_login_busy(True)
        self.login_thread.start()

    def _on_login_thread_finished(self) -> None:
        self.login_thread = None
        self.login_worker = None
        self._set_login_busy(False)

    def _set_login_busy(self, busy: bool) -> None:
        for button in [
            self.save_settings_button,
            self.check_session_button,
            self.send_code_button,
            self.verify_code_button,
            self.verify_password_button,
            self.reset_session_button,
        ]:
            button.setEnabled(not busy)
        if not busy:
            self.verify_code_button.setEnabled(bool(self.phone_code_hash) and not self.connected)
            self.verify_password_button.setEnabled(self.password_input.isEnabled() and not self.connected)
            self.reset_session_button.setEnabled(True)

    def _check_session_status(self, silent: bool) -> None:
        try:
            payload = self._telegram_payload()
        except ValueError:
            if not silent:
                QMessageBox.information(self, "Connect Telegram", "Enter your API ID and API Hash first.")
            return
        self._start_login_action("status", payload)

    def _send_login_code(self) -> None:
        try:
            payload = self._telegram_payload()
            if not payload["phone"]:
                raise ValueError("Enter your phone number first.")
        except ValueError as exc:
            QMessageBox.information(self, "Connect Telegram", str(exc))
            return
        self._save_settings()
        self.status_label.setText("Sending login code...")
        self._start_login_action("send_code", payload)

    def _verify_code(self) -> None:
        code = self.code_input.text().strip().replace(" ", "")
        if not code:
            QMessageBox.information(self, "Verify Code", "Enter the login code from Telegram.")
            return
        payload = self._telegram_payload()
        payload.update({"code": code, "phone_code_hash": self.phone_code_hash})
        self.status_label.setText("Verifying code...")
        self._start_login_action("verify_code", payload)

    def _verify_password(self) -> None:
        password = self.password_input.text()
        if not password:
            QMessageBox.information(self, "Verify Password", "Enter your Telegram 2FA password.")
            return
        payload = self._telegram_payload()
        payload["password"] = password
        self.status_label.setText("Verifying password...")
        self._start_login_action("verify_password", payload)

    @Slot(str, dict)
    def _on_login_result(self, state: str, payload: dict) -> None:
        if state == "connected":
            self._set_connected(True, "Connected")
            self.account_label.setText(self._account_text(payload))
            self.code_input.setEnabled(False)
            self.verify_code_button.setEnabled(False)
            self.password_input.setEnabled(False)
            self.verify_password_button.setEnabled(False)
            self.phone_code_hash = ""
            self._append_log(f"Connected as {self.account_label.text()}")
            return
        if state == "code_sent":
            self.phone_code_hash = payload["phone_code_hash"]
            self._set_connected(False, "Login code sent")
            self.code_input.setEnabled(True)
            self.verify_code_button.setEnabled(True)
            self._append_log("Login code sent. Enter it in the app to continue.")
            return
        if state == "password_needed":
            self._set_connected(False, "2FA password needed")
            self.password_input.setEnabled(True)
            self.verify_password_button.setEnabled(True)
            self._append_log("This Telegram account has 2FA enabled. Enter the password in the app.")
            return
        if state == "expired":
            self._set_connected(False, "Session expired / login needed")
            return
        self._set_connected(False, "Not connected")

    @Slot(str)
    def _on_login_failed(self, message: str) -> None:
        self._set_connected(False, "Login needed")
        QMessageBox.warning(self, "Telegram Login", message)

    def _account_text(self, payload: dict) -> str:
        parts = [payload.get("display") or "Telegram account"]
        phone = payload.get("phone")
        name = payload.get("name")
        if phone:
            parts.append(phone)
        if name and name not in parts[0]:
            parts.append(name)
        return " / ".join(parts)

    def _set_connected(self, connected: bool, status: str) -> None:
        self.connected = connected
        self.status_label.setText(status)
        self.status_label.setStyleSheet(
            "font-weight: 600; color: #116329;" if connected else "font-weight: 600; color: #8a4b00;"
        )
        if not connected:
            self.account_label.setText("-")
        self.dry_run_button.setEnabled(connected and self.copy_thread is None)
        self.start_button.setEnabled(connected and self.copy_thread is None)

    def _on_login_settings_changed(self) -> None:
        if self.connected:
            self._set_connected(False, "Session changed / check needed")

    def _reset_session(self) -> None:
        reply = QMessageBox.question(
            self,
            "Reset Session",
            "Disconnect this Telegram session and delete the local session file?",
        )
        if reply != QMessageBox.Yes:
            return
        for path in self._session_file_paths():
            path.unlink(missing_ok=True)
        self.phone_code_hash = ""
        self.code_input.clear()
        self.password_input.clear()
        self.code_input.setEnabled(False)
        self.verify_code_button.setEnabled(False)
        self.password_input.setEnabled(False)
        self.verify_password_button.setEnabled(False)
        self._set_connected(False, "Not connected")
        self._append_log("Session reset. Connect Telegram again before copying.")

    def _on_mode_changed(self, index: int) -> None:
        self.links_text.setVisible(index == 0)
        self.links_file_input.setVisible(index == 1)
        self.select_links_file_button.setVisible(index == 1)
        self.date_hint_label.setVisible(index == 2)

    def _choose_links_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select message links file",
            str(PROJECT_DIR),
            "Text files (*.txt);;All files (*)",
        )
        if path:
            self.links_file_input.setText(path)

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

    def _settings_from_ui(self) -> SavedSettings:
        return SavedSettings(
            api_id=self.api_id_input.text().strip(),
            api_hash=self.api_hash_input.text().strip(),
            session_name=self.session_input.text().strip() or "telegram_backfill",
            phone=self.phone_input.text().strip(),
            source_channel=self.source_input.text().strip(),
            target_channel=self.target_input.text().strip(),
            start_date=self.start_date_input.date().toString("yyyy-MM-dd"),
            end_date=self.end_date_input.date().toString("yyyy-MM-dd"),
            scan_limit=self.scan_limit_input.text().strip(),
            send_limit=self.send_limit_input.text().strip(),
            keywords=self.keywords_input.text().strip(),
            links_file=self.links_file_input.text().strip(),
            retry_attempts=self.retry_attempts_input.text().strip() or "8",
            send_delay=self.send_delay_input.text().strip() or "1.5",
            history_wait=self.history_wait_input.text().strip() or "1.0",
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
        self.phone_input.setText(settings.phone)
        self.source_input.setText(settings.source_channel)
        self.target_input.setText(settings.target_channel)
        self.start_date_input.setDate(QDate.fromString(settings.start_date, "yyyy-MM-dd"))
        self.end_date_input.setDate(QDate.fromString(settings.end_date, "yyyy-MM-dd"))
        self.scan_limit_input.setText(settings.scan_limit)
        self.send_limit_input.setText(settings.send_limit)
        self.keywords_input.setText(settings.keywords)
        self.links_file_input.setText(settings.links_file)
        self.retry_attempts_input.setText(settings.retry_attempts)
        self.send_delay_input.setText(settings.send_delay)
        self.history_wait_input.setText(settings.history_wait)

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
        if not self.connected:
            raise ValueError("Please connect your Telegram account first.")
        api_id = self.api_id_input.text().strip()
        api_hash = self.api_hash_input.text().strip()
        source = self.source_input.text().strip()
        target = self.target_input.text().strip()
        if not api_id or not api_hash or not target:
            raise ValueError("API ID, API Hash, and Target channel are required.")

        mode_index = self.mode_combo.currentIndex()
        message_links: list[str] = []
        if mode_index == 0:
            message_links = self._parse_links_text(self.links_text.toPlainText())
            if not message_links:
                raise ValueError("Paste at least one Telegram message link.")
        elif mode_index == 1:
            message_links = self._read_links_file(self.links_file_input.text().strip())
            if not message_links:
                raise ValueError("The selected links file has no links.")
        else:
            if not source:
                raise ValueError("Source channel is required for date range backfill.")

        if message_links:
            for link in message_links:
                try:
                    parse_message_target(link, source)
                except ValueError as exc:
                    raise ValueError(
                        f"{exc}\n\nAdd a Source channel fallback or use full public links like https://t.me/channel/123."
                    ) from exc

        keywords = [item.strip() for item in self.keywords_input.text().split(",") if item.strip()]
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
            session_name=str(self._session_base_path()),
            database_path=PROJECT_DIR / "processed.sqlite3",
            temp_dir=PROJECT_DIR / "tmp_downloads",
            log_file=PROJECT_DIR / "copy.log",
            retry_attempts=int(self.retry_attempts_input.text().strip() or "8"),
            send_delay_seconds=float(self.send_delay_input.text().strip() or "1.5"),
            history_wait_seconds=float(self.history_wait_input.text().strip() or "1.0"),
        )

    def _start_copy(self, dry_run: bool) -> None:
        if not self.connected:
            QMessageBox.information(self, "Connect Telegram", "Please connect your Telegram account first.")
            return
        if self.copy_thread is not None:
            return
        try:
            config = self._build_config(dry_run)
        except Exception as exc:
            QMessageBox.warning(self, "Configuration", str(exc))
            return

        self._save_settings()
        self._reset_progress()
        self.cancel_event = Event()
        self.copy_worker = CopierWorker(config, self.cancel_event)
        self.copy_thread = QThread()
        self.copy_worker.moveToThread(self.copy_thread)
        self.copy_thread.started.connect(self.copy_worker.run)
        self.copy_worker.progress.connect(self._on_progress)
        self.copy_worker.log_line.connect(self._append_log)
        self.copy_worker.finished.connect(self.copy_thread.quit)
        self.copy_worker.finished.connect(self._on_copy_finished)
        self.copy_thread.finished.connect(self.copy_thread.deleteLater)
        self.copy_thread.finished.connect(self._on_copy_thread_finished)
        self.copy_thread.start()
        self._set_copy_running(True)
        self.tabs.setCurrentIndex(2)

    def _stop_copy(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.copy_status_label.setText("Stopping...")
            self._append_log("Stop requested. The app will clean up temporary downloads when safe.")

    @Slot(bool, str)
    def _on_copy_finished(self, ok: bool, message: str) -> None:
        self._append_log(message)
        self._set_copy_running(False)
        if not ok and message not in {"Stopped", "Finished"}:
            QMessageBox.warning(self, "Copy finished", message)

    def _on_copy_thread_finished(self) -> None:
        self.copy_thread = None
        self.copy_worker = None
        self.cancel_event = None
        self._set_connected(self.connected, self.status_label.text())

    def _set_copy_running(self, running: bool) -> None:
        self.dry_run_button.setEnabled(self.connected and not running)
        self.start_button.setEnabled(self.connected and not running)
        self.stop_button.setEnabled(running)

    def _reset_progress(self) -> None:
        self.copy_status_label.setText("Starting...")
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
            self.copy_status_label.setText(self._friendly_status(str(status)))
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

    def _friendly_status(self, status: str) -> str:
        return {
            "idle": "Idle",
            "stopped": "Stopped",
            "scanning": "Checking messages...",
            "downloading": "Downloading file...",
            "uploading": "Uploading to target channel...",
            "copied": "Copied successfully",
            "skipped": "Skipped",
            "failed": "Failed",
        }.get(status, status)

    @Slot(str)
    def _append_log(self, line: str) -> None:
        self.logs_text.append(self._friendly_log(line))

    def _friendly_log(self, line: str) -> str:
        lowered = line.lower()
        if "floodwait" in lowered:
            return f"Waiting because Telegram rate-limited the request. {line}"
        if "retry" in lowered:
            return f"Connection dropped, retrying. {line}"
        if "downloading" in lowered:
            return f"Downloading file... {line}"
        if "uploading" in lowered:
            return f"Uploading to target channel... {line}"
        if "copied" in lowered:
            return f"Copied successfully. {line}"
        if "failed" in lowered:
            return f"Failed. {line}"
        if "skip" in lowered:
            return f"Skipped. {line}"
        return line

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

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.copy_thread is not None and self.cancel_event is not None:
            self.cancel_event.set()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
