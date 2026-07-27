"""批量上传到 Google Drive + 写入入库表（对齐原 Apps Script 多任务上传流程）。

布局：左侧设置 / 右侧任务列表、进度与回执链接。
凭据与主界面「表格下载」共用。
视频类型来自「分类目录」表第 D 列（非写死列表）。
"""

from __future__ import annotations

import mimetypes
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

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
    QProgressBar,
    QPushButton,
    QScrollArea,
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
        self.layout.setContentsMargins(16, 14, 16, 14)
        self.layout.setSpacing(8)
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
    item_update = Signal(int, int, str, str)
    task_update = Signal(int, str, str)
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
                        existing = client.find_file_in_folder(folder_id, file_item.name)
                        if existing and self.skip_existing:
                            file_url = existing.get("webViewLink") or ""
                            self.item_update.emit(task.index, fi, "已存在，跳过", file_url)
                            skipped += 1
                            done_files += 1
                            self.progress.emit(done_files, total_files)
                            self.log.emit(f"已存在，跳过：{file_item.name}")
                            self._write_inbound(client, task, file_url)
                            continue

                        if existing and not self.skip_existing:
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
            formatted, display_cat1, display_cat2, display_cat3,
            display_type, "", "", task.uploader,
            copyright_status, file_url, "已上传", formatted,
        ]
        client.insert_inbound_row(self.spreadsheet_id, self.data_sheet, row, insert_before_row=7)


class DriveBatchUploadPage(QWidget):
    """左侧设置 · 右侧任务/进度/回执。凭据与主界面共用。"""

    def __init__(
        self,
        parent=None,
        credentials_supplier=None,
        spreadsheet_supplier=None,
        token_path: str = "",
    ):
        super().__init__(parent)
        self.credentials_supplier = credentials_supplier
        self.spreadsheet_supplier = spreadsheet_supplier
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
                value = str(self.credentials_supplier() or "").strip()
                if value:
                    return value
            except Exception:
                pass
        return default_credentials_path()

    def spreadsheet_id(self) -> str:
        # 优先本页设置；若为空则尝试主界面表格 ID
        local = self.spreadsheet_edit.text().strip()
        if local:
            return local
        if callable(self.spreadsheet_supplier):
            try:
                return str(self.spreadsheet_supplier() or "").strip()
            except Exception:
                pass
        return ""

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        tip = QLabel(
            "左侧为上传设置（凭据与主界面共用）；右侧为任务列表、进度与回执链接。"
            "视频类型来自表格「分类目录」第 D 列，不是写死选项。"
            "若提示 Drive 权限不足，请删除 token.json 后重新授权。"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        root.addWidget(tip)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        # ========== 左侧：设置 ==========
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setMinimumWidth(360)
        left_scroll.setMaximumWidth(440)
        left_inner = QWidget()
        left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, 4, 0)
        left.setSpacing(10)
        left_scroll.setWidget(left_inner)
        body.addWidget(left_scroll, 0)

        settings = Card("设置")
        left.addWidget(settings)
        sg = QVBoxLayout()
        sg.setSpacing(8)
        settings.layout.addLayout(sg)

        cred_tip = QLabel("凭据：与「表格 / 粘贴链接」页共用（主界面顶部凭据文件）")
        cred_tip.setObjectName("fieldLabel")
        cred_tip.setWordWrap(True)
        sg.addWidget(cred_tip)
        self.credentials_label = QLabel("")
        self.credentials_label.setObjectName("subtitle")
        self.credentials_label.setWordWrap(True)
        sg.addWidget(self.credentials_label)

        self.spreadsheet_edit = QLineEdit()
        self.spreadsheet_edit.setPlaceholderText("可留空则使用主界面表格 ID")
        self.parent_folder_edit = QLineEdit(DEFAULT_PARENT_FOLDER_ID)
        self.data_sheet_edit = QLineEdit(DATA_SHEET_NAME)
        self.category_sheet_edit = QLineEdit(CATEGORY_SHEET_NAME)
        self.log_sheet_edit = QLineEdit(LOG_SHEET_NAME)
        self.uploader_edit = QLineEdit()
        self.uploader_edit.setPlaceholderText("上传者姓名 *")

        for label, widget in [
            ("表格 ID", self.spreadsheet_edit),
            ("Drive 父文件夹 ID", self.parent_folder_edit),
            ("入库表名称", self.data_sheet_edit),
            ("分类目录名称", self.category_sheet_edit),
            ("上传日志名称", self.log_sheet_edit),
            ("上传者 *", self.uploader_edit),
        ]:
            sg.addWidget(self._labeled(label, widget))

        load_row = QHBoxLayout()
        self.load_cat_btn = QPushButton("加载分类目录")
        self.load_cat_btn.setObjectName("secondaryButton")
        self.sync_cred_btn = QPushButton("刷新共用凭据")
        self.sync_cred_btn.setObjectName("ghostButton")
        load_row.addWidget(self.load_cat_btn)
        load_row.addWidget(self.sync_cred_btn)
        sg.addLayout(load_row)

        # 模式
        mode_card = Card("上传模式")
        left.addWidget(mode_card)
        mode_row = QHBoxLayout()
        self.mode_standard_btn = QPushButton("分类上传")
        self.mode_standard_btn.setObjectName("primaryButton")
        self.mode_image_btn = QPushButton("图片直传")
        self.mode_image_btn.setObjectName("secondaryButton")
        mode_row.addWidget(self.mode_standard_btn)
        mode_row.addWidget(self.mode_image_btn)
        mode_card.layout.addLayout(mode_row)

        self.cat_panel = QFrame()
        self.cat_panel.setObjectName("fieldBox")
        cat_box = QVBoxLayout(self.cat_panel)
        cat_box.setContentsMargins(0, 0, 0, 0)
        cat_box.setSpacing(8)
        mode_card.layout.addWidget(self.cat_panel)

        self.cat1_combo = QComboBox()
        self.cat2_combo = QComboBox()
        self.cat3_combo = QComboBox()
        self.video_type_combo = QComboBox()
        self.video_type_combo.setEditable(True)
        self.video_type_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.video_type_combo.setPlaceholderText("加载分类后显示（表 D 列）")
        self.video_type_combo.setToolTip(
            "视频类型来自「分类目录」工作表第 D 列的去重结果。\n"
            "不是程序写死的选项。请先点「加载分类目录」。"
        )

        for label, widget in [
            ("一级目录 *", self.cat1_combo),
            ("二级目录", self.cat2_combo),
            ("三级目录", self.cat3_combo),
            ("视频类型（分类目录 D 列）", self.video_type_combo),
        ]:
            cat_box.addWidget(self._labeled(label, widget))

        # 文件
        file_card = Card("选择文件")
        left.addWidget(file_card)
        self.pick_files_btn = QPushButton("选择文件")
        self.pick_files_btn.setObjectName("secondaryButton")
        self.pick_folder_btn = QPushButton("选择文件夹内全部")
        self.pick_folder_btn.setObjectName("secondaryButton")
        self.clear_pending_btn = QPushButton("清空待添加")
        self.clear_pending_btn.setObjectName("ghostButton")
        self.add_task_btn = QPushButton("加入上传清单 →")
        self.add_task_btn.setObjectName("primaryButton")
        for b in (self.pick_files_btn, self.pick_folder_btn, self.clear_pending_btn, self.add_task_btn):
            file_card.layout.addWidget(b)
        self.pending_label = QLabel("待添加文件：0")
        self.pending_label.setObjectName("status")
        file_card.layout.addWidget(self.pending_label)

        opt_card = Card("选项")
        left.addWidget(opt_card)
        self.copyright_check = QCheckBox("我保证版权没有问题")
        self.copyright_check.setChecked(True)
        self.skip_existing_check = QCheckBox("云端同名则跳过（仍写入入库表）")
        self.skip_existing_check.setChecked(True)
        opt_card.layout.addWidget(self.copyright_check)
        opt_card.layout.addWidget(self.skip_existing_check)

        act_card = Card("操作")
        left.addWidget(act_card)
        self.start_btn = QPushButton("开始批量上传")
        self.start_btn.setObjectName("primaryButton")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        self.clear_tasks_btn = QPushButton("清空右侧清单")
        self.clear_tasks_btn.setObjectName("ghostButton")
        self.open_folder_btn = QPushButton("打开父文件夹")
        self.open_folder_btn.setObjectName("secondaryButton")
        for b in (self.start_btn, self.stop_btn, self.clear_tasks_btn, self.open_folder_btn):
            act_card.layout.addWidget(b)

        left.addStretch(1)

        # ========== 右侧：任务 / 进度 / 回执 ==========
        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 1)

        task_card = Card("任务列表")
        right.addWidget(task_card, 3)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "模式", "分类/路径", "文件", "状态", "文件链接", "文件夹"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 72)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(4, 100)
        self.table.setColumnWidth(5, 160)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        task_card.layout.addWidget(self.table)

        prog_card = Card("上传进度")
        right.addWidget(prog_card, 0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("等待开始")
        self.progress_label.setObjectName("status")
        prog_card.layout.addWidget(self.progress_label)
        prog_card.layout.addWidget(self.progress_bar)

        receipt_card = Card("回执链接（文件夹 / 文件）")
        right.addWidget(receipt_card, 2)
        self.receipt_box = QTextEdit()
        self.receipt_box.setReadOnly(True)
        self.receipt_box.setObjectName("pasteTextBox")
        self.receipt_box.setPlaceholderText("上传成功后，这里会汇总任务文件夹链接与文件链接，可复制。")
        receipt_card.layout.addWidget(self.receipt_box)
        receipt_btns = QHBoxLayout()
        self.copy_receipt_btn = QPushButton("复制全部回执")
        self.copy_receipt_btn.setObjectName("secondaryButton")
        self.clear_receipt_btn = QPushButton("清空回执")
        self.clear_receipt_btn.setObjectName("ghostButton")
        receipt_btns.addWidget(self.copy_receipt_btn)
        receipt_btns.addWidget(self.clear_receipt_btn)
        receipt_btns.addStretch()
        receipt_card.layout.addLayout(receipt_btns)

        log_card = Card("日志")
        right.addWidget(log_card, 1)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        log_card.layout.addWidget(self.log_box)

        self.status_row = QLabel("左侧配置设置 → 选文件加入清单 → 右侧查看进度与回执")
        self.status_row.setObjectName("status")
        root.addWidget(self.status_row)

        self.refresh_credentials_label()
        self.log("上传页已就绪。凭据与主界面共用；视频类型将从分类目录 D 列加载。")

    def _labeled(self, label: str, widget: QWidget) -> QFrame:
        field = QFrame()
        field.setObjectName("fieldBox")
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        box.addWidget(caption)
        box.addWidget(widget)
        return field

    def connect_signals(self):
        self.mode_standard_btn.clicked.connect(lambda: self.set_image_mode(False))
        self.mode_image_btn.clicked.connect(lambda: self.set_image_mode(True))
        self.cat1_combo.currentTextChanged.connect(self.on_cat1_changed)
        self.cat2_combo.currentTextChanged.connect(self.on_cat2_changed)
        self.load_cat_btn.clicked.connect(self.load_categories)
        self.sync_cred_btn.clicked.connect(self.refresh_credentials_label)
        self.pick_files_btn.clicked.connect(self.pick_files)
        self.pick_folder_btn.clicked.connect(self.pick_folder_files)
        self.clear_pending_btn.clicked.connect(self.clear_pending)
        self.add_task_btn.clicked.connect(self.add_task)
        self.start_btn.clicked.connect(self.start_upload)
        self.stop_btn.clicked.connect(self.stop_upload)
        self.clear_tasks_btn.clicked.connect(self.clear_tasks)
        self.open_folder_btn.clicked.connect(self.open_parent_folder)
        self.copy_receipt_btn.clicked.connect(self.copy_receipts)
        self.clear_receipt_btn.clicked.connect(self.receipt_box.clear)

    def refresh_credentials_label(self):
        path = self.credentials_path()
        if path and os.path.exists(path):
            self.credentials_label.setText(f"当前凭据：{path}")
        else:
            self.credentials_label.setText(f"当前凭据：{path or '（未设置，请到主界面「表格/粘贴链接」页选择）'}")
        # 若本页表格 ID 为空，尝试同步主界面
        if not self.spreadsheet_edit.text().strip() and callable(self.spreadsheet_supplier):
            try:
                sid = str(self.spreadsheet_supplier() or "").strip()
                if sid:
                    self.spreadsheet_edit.setText(sid)
                    self.log(f"已同步主界面表格 ID：{sid}")
            except Exception:
                pass
        self.log(f"共用凭据：{path}")

    def log(self, message: str):
        now = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{now}] {message}")

    def set_image_mode(self, enabled: bool):
        self.is_image_mode = enabled
        if enabled:
            self.mode_image_btn.setObjectName("primaryButton")
            self.mode_standard_btn.setObjectName("secondaryButton")
            self.cat_panel.setEnabled(False)
            self.status_row.setText("图片直传 · 路径：图片素材/日期/上传者")
        else:
            self.mode_standard_btn.setObjectName("primaryButton")
            self.mode_image_btn.setObjectName("secondaryButton")
            self.cat_panel.setEnabled(True)
            self.status_row.setText("分类上传 · 路径：一级/二级/三级/日期/上传者")
        parent = self.window()
        if parent and hasattr(parent, "apply_style"):
            parent.apply_style()
        else:
            for b in (self.mode_image_btn, self.mode_standard_btn):
                b.style().unpolish(b)
                b.style().polish(b)

    def has_running_worker(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def load_categories(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_SECTION, "上传任务进行中，请结束后再加载。")
            return
        self.refresh_credentials_label()
        sid = self.spreadsheet_id()
        if not sid:
            QMessageBox.warning(self, APP_SECTION, "请填写表格 ID（或在主界面填写后点「刷新共用凭据」）。")
            return
        # 写回编辑框，避免空显示
        if not self.spreadsheet_edit.text().strip():
            self.spreadsheet_edit.setText(sid)
        cred = self.credentials_path()
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
        # 一级
        cat1_values, seen1 = [], set()
        video_types, seen_t = [], set()
        for r in self.category_rows:
            c1 = r[0] if r else ""
            if c1 and c1 not in seen1:
                seen1.add(c1)
                cat1_values.append(c1)
            # D 列 = 视频类型（第 4 列 index 3）
            c4 = r[3] if len(r) > 3 else ""
            c4 = str(c4 or "").strip()
            if c4 and c4 not in seen_t:
                seen_t.add(c4)
                video_types.append(c4)

        self.cat1_combo.blockSignals(True)
        self.cat1_combo.clear()
        self.cat1_combo.addItem("")
        self.cat1_combo.addItems(cat1_values)
        self.cat1_combo.blockSignals(False)
        self.cat2_combo.clear()
        self.cat3_combo.clear()

        # 视频类型：仅来自表格 D 列
        current = self.video_type_combo.currentText().strip()
        self.video_type_combo.blockSignals(True)
        self.video_type_combo.clear()
        self.video_type_combo.addItem("")
        self.video_type_combo.addItems(video_types)
        if current and current in video_types:
            self.video_type_combo.setCurrentText(current)
        self.video_type_combo.blockSignals(False)

        self.log(f"一级目录 {len(cat1_values)} 项；视频类型（D 列）{len(video_types)} 项：{', '.join(video_types) or '（空）'}")
        if not video_types:
            self.log("提示：分类目录 D 列没有类型数据，视频类型可手动输入，或在表格 D 列补充后重新加载。")
        self.status_row.setText(f"分类已加载：{len(self.category_rows)} 行 · 类型 {len(video_types)} 种")

    def on_cat1_changed(self, text: str):
        text = (text or "").strip()
        cat2_values, seen = [], set()
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
        self._refresh_video_types_for_selection()

    def on_cat2_changed(self, text: str):
        c1 = self.cat1_combo.currentText().strip()
        c2 = (text or "").strip()
        cat3_values, seen = [], set()
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
        self._refresh_video_types_for_selection()

    def _refresh_video_types_for_selection(self):
        """按当前一/二级筛选 D 列类型（仍只来自表格）。"""
        c1 = self.cat1_combo.currentText().strip()
        c2 = self.cat2_combo.currentText().strip()
        types, seen = [], set()
        for r in self.category_rows:
            if c1 and (r[0] if r else "") != c1:
                continue
            if c2 and (r[1] if len(r) > 1 else "") != c2:
                continue
            c4 = str(r[3] if len(r) > 3 else "").strip()
            if c4 and c4 not in seen:
                seen.add(c4)
                types.append(c4)
        # 若筛选后为空，回退全部 D 列
        if not types:
            for r in self.category_rows:
                c4 = str(r[3] if len(r) > 3 else "").strip()
                if c4 and c4 not in seen:
                    seen.add(c4)
                    types.append(c4)
        current = self.video_type_combo.currentText().strip()
        self.video_type_combo.blockSignals(True)
        self.video_type_combo.clear()
        self.video_type_combo.addItem("")
        self.video_type_combo.addItems(types)
        if current and (current in types or self.video_type_combo.isEditable()):
            self.video_type_combo.setCurrentText(current)
        self.video_type_combo.blockSignals(False)

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
            QMessageBox.warning(self, APP_SECTION, "请在左侧设置中填写上传者。")
            return
        if not self.is_image_mode:
            cat1 = self.cat1_combo.currentText().strip()
            if not cat1:
                QMessageBox.warning(self, APP_SECTION, "分类模式请选择一级目录。")
                return
            cat2 = self.cat2_combo.currentText().strip()
            cat3 = self.cat3_combo.currentText().strip()
            vtype = self.video_type_combo.currentText().strip()
        else:
            cat1 = cat2 = cat3 = ""
            vtype = ""

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
            video_type=vtype,
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
        self.progress_bar.setValue(0)
        self.progress_label.setText("等待开始")
        self.log("已清空上传清单。")

    def rebuild_table(self):
        rows = []
        for task in self.tasks:
            mode = "图片直传" if task.is_image_mode else "分类上传"
            if task.is_image_mode:
                path_label = f"图片素材 / {task.uploader}"
            else:
                parts = [task.cat1, task.cat2, task.cat3]
                if task.video_type:
                    parts.append(f"[{task.video_type}]")
                parts.append(task.uploader)
                path_label = " / ".join([x for x in parts if x])
            for f in task.files:
                rows.append((task, f, mode, path_label))
        self.table.setRowCount(len(rows))
        for i, (task, f, mode, path_label) in enumerate(rows):
            values = [task.index, mode, path_label, f.name, f.status, f.file_url, task.folder_url]
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
        self.status_row.setText(
            f"清单：{len(self.tasks)} 个任务，{sum(len(t.files) for t in self.tasks)} 个文件"
        )
        self.refresh_receipt_box()

    def refresh_receipt_box(self):
        lines = []
        for task in self.tasks:
            mode = "图片直传" if task.is_image_mode else "分类上传"
            title = " / ".join(
                [x for x in [task.cat1, task.cat2, task.cat3, task.video_type, task.uploader] if x]
            )
            lines.append(f"【任务#{task.index}】{mode} · {title}")
            if task.folder_url:
                lines.append(f"  文件夹：{task.folder_url}")
            for f in task.files:
                if f.file_url:
                    lines.append(f"  · {f.name} → {f.file_url}")
            lines.append("")
        text = "\n".join(lines).strip()
        # 不覆盖用户手动编辑：仅在有内容时更新
        if text:
            self.receipt_box.setPlainText(text)

    def copy_receipts(self):
        text = self.receipt_box.toPlainText().strip()
        if not text:
            QMessageBox.information(self, APP_SECTION, "暂无回执内容。")
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self.log("回执已复制到剪贴板。")
        QMessageBox.information(self, APP_SECTION, "回执链接已复制到剪贴板。")

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
        self.refresh_credentials_label()
        sid = self.spreadsheet_id()
        parent = self.parent_folder_edit.text().strip()
        if not sid or not parent:
            QMessageBox.warning(self, APP_SECTION, "请填写表格 ID 和父文件夹 ID。")
            return
        if not self.spreadsheet_edit.text().strip():
            self.spreadsheet_edit.setText(sid)

        self.set_running(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在上传…")
        self.status_row.setText("正在上传…")
        self.log("开始批量上传到 Google Drive…")
        worker = UploadWorker(
            credentials_path=self.credentials_path(),
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
        pct = int(done * 100 / total) if total else 0
        self.progress_bar.setValue(pct)
        self.progress_label.setText(f"上传进度 {done}/{total}（{pct}%）")
        self.status_row.setText(f"上传进度 {done}/{total}")

    def on_upload_done(self):
        self.status_row.setText("上传任务结束")
        self.progress_label.setText("上传完成")
        self.refresh_receipt_box()
        self.log("批量上传流程结束。")

    def on_worker_finished(self):
        if self.sender() is self.worker:
            self.worker = None
        self.set_running(False)

    def set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.add_task_btn.setEnabled(not running)
        self.load_cat_btn.setEnabled(not running)
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
