"""批量上传到 Google Drive + 写入入库表（对齐原 Apps Script 多任务上传流程）。"""

from __future__ import annotations

import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sheets_batch_downloader import GoogleClient


APP_SECTION = "批量上传云端"
DEFAULT_PARENT_FOLDER_ID = "1h4rHJ9dojSrK84BftxKnthJz7QuEgj3v"
DATA_SHEET_NAME = "入库表"
CATEGORY_SHEET_NAME = "分类目录"
LOG_SHEET_NAME = "上传日志"
VIDEO_TYPES = ["成片", "素材", "口播", "混剪", "其他"]


@dataclass
class UploadFileItem:
    path: str
    name: str
    status: str = "待上传"
    file_url: str = ""


@dataclass
class UploadTask:
    index: int
    is_image_mode: bool
    cat1: str = ""
    cat2: str = ""
    cat3: str = ""
    video_type: str = ""
    uploader: str = ""
    files: list[UploadFileItem] = field(default_factory=list)
    folder_url: str = ""
    status: str = "待处理"


def app_base_dir() -> str:
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_credentials_path() -> str:
    preferred = os.path.join(app_base_dir(), "谷歌服务账号.json")
    if os.path.exists(preferred):
        return preferred
    return os.path.join(app_base_dir(), "credentials.json")


def guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


class Card(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(18, 16, 18, 16)
        self.layout.setSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        self.layout.addWidget(title_label)


class CategoryLoadWorker(QThread):
    log = Signal(str)
    failed = Signal(str)
    loaded = Signal(list)

    def __init__(self, credentials_path: str, token_path: str, spreadsheet_id: str, sheet_name: str):
        super().__init__()
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name

    def run(self):
        try:
            client = GoogleClient(self.credentials_path, self.token_path)
            self.log.emit(f"已使用凭据：{client.account_label}")
            rows = client.read_category_rows(self.spreadsheet_id, self.sheet_name)
            self.loaded.emit(rows)
            self.log.emit(f"分类目录已加载 {len(rows)} 行。")
        except Exception as exc:
            self.failed.emit(f"加载分类失败：{exc}")


class UploadWorker(QThread):
    log = Signal(str)
    failed = Signal(str)
    item_update = Signal(int, int, str, str)  # task_index, file_index, status, file_url
    task_update = Signal(int, str, str)  # task_index, status, folder_url
    progress = Signal(int, int)
    done = Signal()

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        spreadsheet_id: str,
        parent_folder_id: str,
        data_sheet: str,
        log_sheet: str,
        tasks: list[UploadTask],
        copyright_agreed: bool,
        skip_existing: bool,
    ):
        super().__init__()
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.spreadsheet_id = spreadsheet_id
        self.parent_folder_id = parent_folder_id
        self.data_sheet = data_sheet
        self.log_sheet = log_sheet
        self.tasks = tasks
        self.copyright_agreed = copyright_agreed
        self.skip_existing = skip_existing
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        success = failed = skipped = 0
        total_files = sum(len(t.files) for t in self.tasks)
        done_files = 0
        try:
            client = GoogleClient(self.credentials_path, self.token_path)
            self.log.emit(f"已使用凭据：{client.account_label}")
            if not self.parent_folder_id.strip():
                raise RuntimeError("请填写 Google Drive 父文件夹 ID")
            if not self.spreadsheet_id.strip():
                raise RuntimeError("请填写表格 ID（用于分类目录/入库表/日志）")

            for task in self.tasks:
                if self.stop_event.is_set():
                    self.log.emit("任务已停止。")
                    break
                self.task_update.emit(task.index, "上传中", "")
                date_str = datetime.now().strftime("%Y-%m-%d")
                if task.is_image_mode:
                    path_parts = ["图片素材", date_str, task.uploader]
                else:
                    path_parts = [task.cat1, task.cat2, task.cat3, date_str, task.uploader]

                try:
                    folder_meta = client.get_or_create_folder_path(self.parent_folder_id, path_parts)
                    folder_id = folder_meta["id"]
                    folder_url = folder_meta.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder_id}"
                    task.folder_url = folder_url
                    path_label = "/".join([p for p in path_parts if p])
                    self.log.emit(f"任务#{task.index} 目标目录：{path_label}")
                except Exception as exc:
                    self.task_update.emit(task.index, f"失败：{exc}", "")
                    self.log.emit(f"任务#{task.index} 创建目录失败：{exc}")
                    failed += len(task.files)
                    done_files += len(task.files)
                    self.progress.emit(done_files, total_files)
                    continue

                for fi, file_item in enumerate(task.files):
                    if self.stop_event.is_set():
                        break
                    self.item_update.emit(task.index, fi, "上传中", "")
                    try:
                        # 已存在同名
                        existing = client.find_file_in_folder(folder_id, file_item.name)
                        if existing and self.skip_existing:
                            file_url = existing.get("webViewLink") or ""
                            self.item_update.emit(task.index, fi, "已存在，跳过", file_url)
                            skipped += 1
                            done_files += 1
                            self.progress.emit(done_files, total_files)
                            self.log.emit(f"已存在，跳过：{file_item.name}")
                            # 仍写入入库表（与原脚本复用已存在文件一致，原脚本也会写表）
                            self._write_inbound(client, task, file_url)
                            continue

                        if existing and not self.skip_existing:
                            # 仍复用已存在文件（对齐原脚本）
                            created = existing
                            self.log.emit(f"复用云端同名文件：{file_item.name}")
                        else:
                            created = client.upload_local_file(
                                file_item.path,
                                folder_id,
                                file_name=file_item.name,
                                mime_type=guess_mime(file_item.path),
                            )
                        file_url = created.get("webViewLink") or ""
                        self._write_inbound(client, task, file_url)
                        try:
                            client.append_upload_log(
                                self.spreadsheet_id,
                                self.log_sheet,
                                "INFO",
                                f"文件 [{file_item.name}] 上传成功，路径: {path_label}",
                            )
                        except Exception as log_exc:
                            self.log.emit(f"写上传日志失败：{log_exc}")

                        self.item_update.emit(task.index, fi, "成功", file_url)
                        success += 1
                        done_files += 1
                        self.progress.emit(done_files, total_files)
                        self.log.emit(f"成功：{file_item.name} -> {file_url}")
                    except Exception as exc:
                        if self.stop_event.is_set():
                            self.item_update.emit(task.index, fi, "已停止", "")
                            break
                        failed += 1
                        done_files += 1
                        self.progress.emit(done_files, total_files)
                        self.item_update.emit(task.index, fi, f"失败：{exc}", "")
                        self.log.emit(f"失败：{file_item.name} -> {exc}")
                        try:
                            client.append_upload_log(
                                self.spreadsheet_id,
                                self.log_sheet,
                                "ERROR",
                                f"文件 [{file_item.name}] 上传失败: {exc}",
                            )
                        except Exception:
                            pass

                self.task_update.emit(task.index, "完成", task.folder_url)

            self.log.emit(f"上传结束：成功 {success}，跳过 {skipped}，失败 {failed}。")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.done.emit()

    def _write_inbound(self, client: GoogleClient, task: UploadTask, file_url: str):
        formatted = datetime.now().strftime("%Y-%m-%d")
        copyright_status = "保证版权没有问题" if self.copyright_agreed else "未勾选"
        if task.is_image_mode:
            display_cat1, display_cat2, display_cat3 = "图片素材", "", ""
            display_type = "图片"
        else:
            display_cat1, display_cat2, display_cat3 = task.cat1, task.cat2 or "", task.cat3 or ""
            display_type = task.video_type or ""
        row = [
            formatted,
            display_cat1,
            display_cat2,
            display_cat3,
            display_type,
            "",
            "",
            task.uploader,
            copyright_status,
            file_url,
            "已上传",
            formatted,
        ]
        client.insert_inbound_row(self.spreadsheet_id, self.data_sheet, row, insert_before_row=7)


class DriveBatchUploadPage(QWidget):
    """独立板块：批量上传本地文件到 Google Drive，并写入入库表。"""

    def __init__(self, parent=None, credentials_supplier=None, token_path: str = ""):
        super().__init__(parent)
        self.credentials_supplier = credentials_supplier  # callable -> path
        self.token_path = token_path or os.path.join(app_base_dir(), "token.json")
        self.worker = None
        self.category_rows: list[list[str]] = []
        self.tasks: list[UploadTask] = []
        self.pending_files: list[str] = []
        self.is_image_mode = False
        self.task_seq = 0
        self.build_ui()
        self.connect_signals()

    def credentials_path(self) -> str:
        if callable(self.credentials_supplier):
            try:
                return str(self.credentials_supplier() or "").strip()
            except Exception:
                pass
        return default_credentials_path()

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        tip = QLabel(
            "本地文件批量上传到 Google Drive：按分类目录建文件夹，写入「入库表」，记录「上传日志」。"
            "逻辑对齐原 Web 多任务上传系统。首次使用若提示权限不足，请删除 token.json 后重新授权（需 Drive 写权限）。"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        root.addWidget(tip)

        settings = QFrame()
        settings.setObjectName("compactPanel")
        grid = QGridLayout(settings)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        root.addWidget(settings)

        self.credentials_edit = QLineEdit(self.credentials_path())
        self.spreadsheet_edit = QLineEdit()
        self.spreadsheet_edit.setPlaceholderText("Google 表格 ID（含 分类目录 / 入库表 / 上传日志）")
        self.parent_folder_edit = QLineEdit(DEFAULT_PARENT_FOLDER_ID)
        self.parent_folder_edit.setPlaceholderText("Drive 父文件夹 ID")
        self.data_sheet_edit = QLineEdit(DATA_SHEET_NAME)
        self.category_sheet_edit = QLineEdit(CATEGORY_SHEET_NAME)
        self.log_sheet_edit = QLineEdit(LOG_SHEET_NAME)
        self.uploader_edit = QLineEdit()
        self.uploader_edit.setPlaceholderText("上传者姓名")

        self._add_field(grid, "凭据文件", self.credentials_edit, 0, 0, 1, 3)
        cred_btn = QPushButton("选择")
        cred_btn.setObjectName("secondaryButton")
        cred_btn.clicked.connect(self.choose_credentials)
        grid.addWidget(self._wrap_button(cred_btn), 0, 3)
        self._add_field(grid, "表格 ID", self.spreadsheet_edit, 0, 4, 1, 2)
        load_cat_btn = QPushButton("加载分类目录")
        load_cat_btn.setObjectName("secondaryButton")
        load_cat_btn.clicked.connect(self.load_categories)
        grid.addWidget(self._wrap_button(load_cat_btn), 0, 6)

        self._add_field(grid, "父文件夹 ID", self.parent_folder_edit, 1, 0, 1, 3)
        self._add_field(grid, "入库表", self.data_sheet_edit, 1, 3)
        self._add_field(grid, "分类目录", self.category_sheet_edit, 1, 4)
        self._add_field(grid, "上传日志", self.log_sheet_edit, 1, 5)
        self._add_field(grid, "上传者", self.uploader_edit, 1, 6)

        for col in range(7):
            grid.setColumnStretch(col, 1)

        # 模式 + 分类
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        root.addLayout(mode_row)
        self.mode_standard_btn = QPushButton("视频/文件分类模式")
        self.mode_standard_btn.setObjectName("primaryButton")
        self.mode_image_btn = QPushButton("图片直传模式")
        self.mode_image_btn.setObjectName("secondaryButton")
        mode_row.addWidget(self.mode_standard_btn)
        mode_row.addWidget(self.mode_image_btn)
        mode_row.addStretch()

        cat_panel = QFrame()
        cat_panel.setObjectName("compactPanel")
        cat_grid = QGridLayout(cat_panel)
        cat_grid.setContentsMargins(14, 12, 14, 12)
        cat_grid.setHorizontalSpacing(10)
        root.addWidget(cat_panel)
        self.cat_panel = cat_panel

        self.cat1_combo = QComboBox()
        self.cat2_combo = QComboBox()
        self.cat3_combo = QComboBox()
        self.video_type_combo = QComboBox()
        self.video_type_combo.addItems(VIDEO_TYPES)
        self._add_field(cat_grid, "一级目录", self.cat1_combo, 0, 0)
        self._add_field(cat_grid, "二级目录", self.cat2_combo, 0, 1)
        self._add_field(cat_grid, "三级目录", self.cat3_combo, 0, 2)
        self._add_field(cat_grid, "视频类型", self.video_type_combo, 0, 3)

        # 文件选择
        file_row = QHBoxLayout()
        file_row.setSpacing(8)
        root.addLayout(file_row)
        self.pick_files_btn = QPushButton("选择文件")
        self.pick_files_btn.setObjectName("secondaryButton")
        self.pick_folder_btn = QPushButton("选择文件夹内全部文件")
        self.pick_folder_btn.setObjectName("secondaryButton")
        self.clear_pending_btn = QPushButton("清空待添加")
        self.clear_pending_btn.setObjectName("ghostButton")
        self.add_task_btn = QPushButton("加入上传清单")
        self.add_task_btn.setObjectName("primaryButton")
        file_row.addWidget(self.pick_files_btn)
        file_row.addWidget(self.pick_folder_btn)
        file_row.addWidget(self.clear_pending_btn)
        file_row.addWidget(self.add_task_btn)
        file_row.addStretch()

        self.pending_label = QLabel("待添加文件：0")
        self.pending_label.setObjectName("status")
        root.addWidget(self.pending_label)

        options = QHBoxLayout()
        options.setSpacing(14)
        root.addLayout(options)
        self.copyright_check = QCheckBox("我保证版权没有问题")
        self.copyright_check.setChecked(True)
        self.skip_existing_check = QCheckBox("云端已有同名文件则跳过上传（仍写入入库表）")
        self.skip_existing_check.setChecked(True)
        options.addWidget(self.copyright_check)
        options.addWidget(self.skip_existing_check)
        options.addStretch()

        actions = QHBoxLayout()
        actions.setSpacing(8)
        root.addLayout(actions)
        self.start_btn = QPushButton("开始批量上传")
        self.start_btn.setObjectName("primaryButton")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        self.clear_tasks_btn = QPushButton("清空清单")
        self.clear_tasks_btn.setObjectName("ghostButton")
        self.open_folder_btn = QPushButton("打开父文件夹")
        self.open_folder_btn.setObjectName("secondaryButton")
        actions.addWidget(self.start_btn)
        actions.addWidget(self.stop_btn)
        actions.addWidget(self.clear_tasks_btn)
        actions.addWidget(self.open_folder_btn)
        actions.addStretch()

        body = QHBoxLayout()
        body.setSpacing(10)
        root.addLayout(body, 5)

        left = Card("上传清单")
        right = Card("日志")
        body.addWidget(left, 6)
        body.addWidget(right, 4)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "模式", "分类/路径", "文件", "状态", "云端链接", "文件夹"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 180)
        self.table.setColumnWidth(4, 110)
        self.table.setColumnWidth(5, 180)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        left.layout.addWidget(self.table)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        right.layout.addWidget(self.log_box)

        self.status_row = QLabel("等待添加任务 · 上传路径：分类/日期/上传者")
        self.status_row.setObjectName("status")
        root.addWidget(self.status_row)

        self.log("上传板块已就绪。请填写表格 ID、父文件夹 ID，加载分类后选择文件。")

    def _wrap_button(self, button: QPushButton) -> QFrame:
        field = QFrame()
        field.setObjectName("fieldBox")
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        caption = QLabel(" ")
        caption.setObjectName("fieldLabel")
        box.addWidget(caption)
        box.addWidget(button)
        return field

    def _add_field(self, grid, label, widget, row, col, row_span=1, col_span=1):
        field = QFrame()
        field.setObjectName("fieldBox")
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        box.addWidget(caption)
        box.addWidget(widget)
        grid.addWidget(field, row, col, row_span, col_span)
        return field

    def connect_signals(self):
        self.mode_standard_btn.clicked.connect(lambda: self.set_image_mode(False))
        self.mode_image_btn.clicked.connect(lambda: self.set_image_mode(True))
        self.cat1_combo.currentTextChanged.connect(self.on_cat1_changed)
        self.cat2_combo.currentTextChanged.connect(self.on_cat2_changed)
        self.pick_files_btn.clicked.connect(self.pick_files)
        self.pick_folder_btn.clicked.connect(self.pick_folder_files)
        self.clear_pending_btn.clicked.connect(self.clear_pending)
        self.add_task_btn.clicked.connect(self.add_task)
        self.start_btn.clicked.connect(self.start_upload)
        self.stop_btn.clicked.connect(self.stop_upload)
        self.clear_tasks_btn.clicked.connect(self.clear_tasks)
        self.open_folder_btn.clicked.connect(self.open_parent_folder)

    def log(self, message: str):
        now = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{now}] {message}")

    def choose_credentials(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择凭据 JSON", self.credentials_edit.text(), "JSON (*.json);;所有文件 (*.*)"
        )
        if path:
            self.credentials_edit.setText(path)

    def set_image_mode(self, enabled: bool):
        self.is_image_mode = enabled
        if enabled:
            self.mode_image_btn.setObjectName("primaryButton")
            self.mode_standard_btn.setObjectName("secondaryButton")
            self.cat_panel.setEnabled(False)
            self.status_row.setText("图片直传模式 · 路径：图片素材/日期/上传者")
        else:
            self.mode_standard_btn.setObjectName("primaryButton")
            self.mode_image_btn.setObjectName("secondaryButton")
            self.cat_panel.setEnabled(True)
            self.status_row.setText("分类模式 · 路径：一级/二级/三级/日期/上传者")
        # 刷新按钮样式
        parent = self.window()
        if parent and hasattr(parent, "apply_style"):
            parent.apply_style()
        else:
            self.mode_image_btn.style().unpolish(self.mode_image_btn)
            self.mode_image_btn.style().polish(self.mode_image_btn)
            self.mode_standard_btn.style().unpolish(self.mode_standard_btn)
            self.mode_standard_btn.style().polish(self.mode_standard_btn)

    def has_running_worker(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def load_categories(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "上传任务进行中，请结束后再加载。")
            return
        sid = self.spreadsheet_edit.text().strip()
        if not sid:
            QMessageBox.warning(self, APP_SECTION, "请先填写表格 ID。")
            return
        cred = self.credentials_edit.text().strip() or self.credentials_path()
        self.log(f"正在加载分类目录：{self.category_sheet_edit.text().strip() or CATEGORY_SHEET_NAME}")
        self.start_btn.setEnabled(False)
        worker = CategoryLoadWorker(
            cred,
            self.token_path,
            sid,
            self.category_sheet_edit.text().strip() or CATEGORY_SHEET_NAME,
        )
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.loaded.connect(self.on_categories_loaded)
        worker.finished.connect(lambda: self.start_btn.setEnabled(True))
        self.worker = worker
        worker.start()

    def on_categories_loaded(self, rows: list):
        self.category_rows = list(rows or [])
        cat1_values = []
        seen = set()
        for r in self.category_rows:
            c1 = r[0] if r else ""
            if c1 and c1 not in seen:
                seen.add(c1)
                cat1_values.append(c1)
        self.cat1_combo.blockSignals(True)
        self.cat1_combo.clear()
        self.cat1_combo.addItem("")
        self.cat1_combo.addItems(cat1_values)
        self.cat1_combo.blockSignals(False)
        self.cat2_combo.clear()
        self.cat3_combo.clear()
        self.log(f"一级目录 {len(cat1_values)} 项可选。")
        self.status_row.setText(f"分类已加载：{len(self.category_rows)} 行")

    def on_cat1_changed(self, text: str):
        text = (text or "").strip()
        cat2_values = []
        seen = set()
        for r in self.category_rows:
            if (r[0] if r else "") != text:
                continue
            c2 = r[1] if len(r) > 1 else ""
            if c2 and c2 not in seen:
                seen.add(c2)
                cat2_values.append(c2)
        self.cat2_combo.blockSignals(True)
        self.cat2_combo.clear()
        self.cat2_combo.addItem("")
        self.cat2_combo.addItems(cat2_values)
        self.cat2_combo.blockSignals(False)
        self.cat3_combo.clear()

    def on_cat2_changed(self, text: str):
        c1 = self.cat1_combo.currentText().strip()
        c2 = (text or "").strip()
        cat3_values = []
        seen = set()
        for r in self.category_rows:
            if (r[0] if r else "") != c1:
                continue
            if (r[1] if len(r) > 1 else "") != c2:
                continue
            c3 = r[2] if len(r) > 2 else ""
            if c3 and c3 not in seen:
                seen.add(c3)
                cat3_values.append(c3)
        self.cat3_combo.clear()
        self.cat3_combo.addItem("")
        self.cat3_combo.addItems(cat3_values)

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件", "", "所有文件 (*.*)")
        if not paths:
            return
        self.pending_files.extend(paths)
        self.pending_files = list(dict.fromkeys(self.pending_files))
        self.pending_label.setText(f"待添加文件：{len(self.pending_files)}")
        self.log(f"已选择 {len(paths)} 个文件。")

    def pick_folder_files(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not folder:
            return
        paths = []
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                paths.append(path)
        if not paths:
            QMessageBox.information(self, APP_SECTION, "该文件夹内没有文件。")
            return
        self.pending_files.extend(paths)
        self.pending_files = list(dict.fromkeys(self.pending_files))
        self.pending_label.setText(f"待添加文件：{len(self.pending_files)}")
        self.log(f"从文件夹加入 {len(paths)} 个文件。")

    def clear_pending(self):
        self.pending_files = []
        self.pending_label.setText("待添加文件：0")

    def add_task(self):
        if not self.pending_files:
            QMessageBox.information(self, APP_SECTION, "请先选择文件。")
            return
        uploader = self.uploader_edit.text().strip()
        if not uploader:
            QMessageBox.warning(self, APP_SECTION, "请填写上传者。")
            return
        if not self.is_image_mode:
            cat1 = self.cat1_combo.currentText().strip()
            if not cat1:
                QMessageBox.warning(self, APP_SECTION, "分类模式请选择一级目录。")
                return
        else:
            cat1 = cat2 = cat3 = ""
        if not self.is_image_mode:
            cat2 = self.cat2_combo.currentText().strip()
            cat3 = self.cat3_combo.currentText().strip()

        self.task_seq += 1
        files = [
            UploadFileItem(path=p, name=os.path.basename(p))
            for p in self.pending_files
            if os.path.isfile(p)
        ]
        if not files:
            QMessageBox.warning(self, APP_SECTION, "没有有效的本地文件。")
            return
        task = UploadTask(
            index=self.task_seq,
            is_image_mode=self.is_image_mode,
            cat1=cat1 if not self.is_image_mode else "图片素材",
            cat2=cat2 if not self.is_image_mode else "",
            cat3=cat3 if not self.is_image_mode else "",
            video_type="" if self.is_image_mode else self.video_type_combo.currentText(),
            uploader=uploader,
            files=files,
        )
        self.tasks.append(task)
        self.rebuild_table()
        self.log(f"已加入任务#{task.index}，共 {len(files)} 个文件。")
        self.clear_pending()

    def clear_tasks(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "上传进行中，无法清空。")
            return
        self.tasks = []
        self.rebuild_table()
        self.log("已清空上传清单。")

    def rebuild_table(self):
        rows = []
        for task in self.tasks:
            mode = "图片直传" if task.is_image_mode else "分类上传"
            if task.is_image_mode:
                path_label = f"图片素材 / {task.uploader}"
            else:
                path_label = " / ".join([x for x in [task.cat1, task.cat2, task.cat3, task.uploader] if x])
            for f in task.files:
                rows.append((task, f, mode, path_label))
        self.table.setRowCount(len(rows))
        for i, (task, f, mode, path_label) in enumerate(rows):
            values = [
                task.index,
                mode,
                path_label,
                f.name,
                f.status,
                f.file_url,
                task.folder_url,
            ]
            for col, val in enumerate(values):
                cell = QTableWidgetItem("" if val is None else str(val))
                if col == 4:
                    st = str(f.status or "")
                    if st.startswith("成功") or st == "待上传":
                        cell.setForeground(QColor("#15803d"))
                    elif "失败" in st:
                        cell.setForeground(QColor("#dc2626"))
                    elif "跳过" in st:
                        cell.setForeground(QColor("#ca8a04"))
                self.table.setItem(i, col, cell)
        self.status_row.setText(f"清单：{len(self.tasks)} 个任务，{sum(len(t.files) for t in self.tasks)} 个文件")

    def start_upload(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "已有任务在运行。")
            return
        if not self.tasks:
            QMessageBox.information(self, APP_SECTION, "请先把文件加入上传清单。")
            return
        if not self.copyright_check.isChecked():
            if QMessageBox.question(
                self, APP_SECTION, "未勾选版权声明，是否仍继续上传？"
            ) != QMessageBox.StandardButton.Yes:
                return
        sid = self.spreadsheet_edit.text().strip()
        parent = self.parent_folder_edit.text().strip()
        if not sid or not parent:
            QMessageBox.warning(self, APP_SECTION, "请填写表格 ID 和父文件夹 ID。")
            return

        self.set_running(True)
        self.status_row.setText("正在上传…")
        self.log("开始批量上传到 Google Drive…")
        worker = UploadWorker(
            credentials_path=self.credentials_edit.text().strip() or self.credentials_path(),
            token_path=self.token_path,
            spreadsheet_id=sid,
            parent_folder_id=parent,
            data_sheet=self.data_sheet_edit.text().strip() or DATA_SHEET_NAME,
            log_sheet=self.log_sheet_edit.text().strip() or LOG_SHEET_NAME,
            tasks=self.tasks,
            copyright_agreed=self.copyright_check.isChecked(),
            skip_existing=self.skip_existing_check.isChecked(),
        )
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.item_update.connect(self.on_item_update)
        worker.task_update.connect(self.on_task_update)
        worker.progress.connect(self.on_progress)
        worker.done.connect(self.on_upload_done)
        worker.finished.connect(self.on_worker_finished)
        self.worker = worker
        worker.start()

    def stop_upload(self):
        if self.worker and hasattr(self.worker, "stop"):
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.log("已请求停止上传。")
            self.status_row.setText("正在停止…")

    def on_item_update(self, task_index: int, file_index: int, status: str, file_url: str):
        for task in self.tasks:
            if task.index == task_index and 0 <= file_index < len(task.files):
                task.files[file_index].status = status
                if file_url:
                    task.files[file_index].file_url = file_url
                break
        self.rebuild_table()

    def on_task_update(self, task_index: int, status: str, folder_url: str):
        for task in self.tasks:
            if task.index == task_index:
                task.status = status
                if folder_url:
                    task.folder_url = folder_url
                break
        self.rebuild_table()

    def on_progress(self, done: int, total: int):
        self.status_row.setText(f"上传进度 {done}/{total}")

    def on_upload_done(self):
        self.status_row.setText("上传任务结束")
        self.log("批量上传流程结束。")

    def on_worker_finished(self):
        if self.sender() is self.worker:
            self.worker = None
        self.set_running(False)

    def set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.add_task_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def open_parent_folder(self):
        folder_id = self.parent_folder_edit.text().strip()
        if not folder_id:
            QMessageBox.information(self, APP_SECTION, "请先填写父文件夹 ID。")
            return
        QDesktopServices.openUrl(QUrl(f"https://drive.google.com/drive/folders/{folder_id}"))

    def show_error(self, message: str):
        self.log(message)
        self.status_row.setText("出现错误")
        # 权限提示
        if "insufficient" in message.lower() or "scope" in message.lower() or "权限" in message:
            message += (
                "\n\n若刚升级支持上传功能，请关闭程序后删除 token.json，"
                "再重新运行并授权（需要 Google Drive 写入权限）。"
            )
        QMessageBox.warning(self, APP_SECTION, message)

    def request_close(self) -> bool:
        if not self.has_running_worker():
            return True
        self.stop_upload()
        if self.worker and not self.worker.wait(3000):
            return False
        return True
