"""批量上传到 Google Drive + 写入入库表。

- 左：可折叠设置 + 分类选择 + 拖拽文件夹
- 右：任务列表 / 进度 / 回执
- 分类全部来自表格；拖入本地分类文件夹可自动匹配
- 凭据与主界面共用；token 固定路径，授权一次后自动刷新
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sheets_batch_downloader import GoogleClient, default_token_path


APP_SECTION = "批量上传云端"
DEFAULT_PARENT_FOLDER_ID = "1h4rHJ9dojSrK84BftxKnthJz7QuEgj3v"
DATA_SHEET_NAME = "入库表"
CATEGORY_SHEET_NAME = "分类目录"
LOG_SHEET_NAME = "上传日志"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff"}


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
    source_folder: str = ""


def app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def settings_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "DIYDownloader")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "upload_settings.json")


def default_credentials_path() -> str:
    preferred = os.path.join(app_base_dir(), "谷歌服务账号.json")
    if os.path.exists(preferred):
        return preferred
    return os.path.join(app_base_dir(), "credentials.json")


def guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def list_files_recursive(folder: str) -> list[str]:
    out = []
    for root, _, files in os.walk(folder):
        for name in files:
            # 跳过隐藏/系统
            if name.startswith("."):
                continue
            out.append(os.path.join(root, name))
    return out


def match_path_to_category(rel_parts: list[str], category_rows: list[list[str]]) -> tuple[str, str, str, str]:
    """
    用相对路径段匹配表格分类。
    例：自拍素材/动物植物/xxx.mp4 -> cat1=自拍素材, cat2=动物植物
    优先最长前缀匹配 (c1,c2,c3)。
    """
    parts = [p for p in rel_parts if p and p not in (".", "..")]
    if not parts or not category_rows:
        return "", "", "", ""

    best = ("", "", "", "")
    best_score = -1
    for row in category_rows:
        c1 = str(row[0] if row else "").strip()
        c2 = str(row[1] if len(row) > 1 else "").strip()
        c3 = str(row[2] if len(row) > 2 else "").strip()
        c4 = str(row[3] if len(row) > 3 else "").strip()
        chain = [x for x in (c1, c2, c3) if x]
        if not chain:
            continue
        if len(parts) < len(chain):
            continue
        ok = True
        for i, name in enumerate(chain):
            if parts[i] != name:
                ok = False
                break
        if not ok:
            continue
        score = len(chain)
        # 若路径下一段正好等于 D 列类型，也算加分
        if c4 and len(parts) > len(chain) and parts[len(chain)] == c4:
            score += 0.5
        if score > best_score:
            best_score = score
            best = (c1, c2, c3, c4)
    return best


class DropZone(QFrame):
    """支持拖入文件/文件夹。"""
    paths_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("uploadDropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = QLabel("将文件或「分类文件夹」拖到这里\n也可点下方按钮选择\n文件夹会按目录名自动匹配一/二/三级分类")
        self.hint.setObjectName("dropHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("QFrame#uploadDropZone { border-color: #38bdf8; }")
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("")
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p:
                paths.append(p)
        if paths:
            self.paths_dropped.emit(paths)
        event.acceptProposedAction()


class Card(QFrame):
    def __init__(self, title: str, object_name: str = "workCard"):
        super().__init__()
        self.setObjectName(object_name)
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
            self.log.emit(f"已使用凭据：{client.account_label}（已授权则不会重复弹窗）")
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
                raise RuntimeError("请填写表格 ID")

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
                            self._write_inbound(client, task, file_url)
                            continue
                        if existing and not self.skip_existing:
                            created = existing
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
                                self.spreadsheet_id, self.log_sheet, "INFO",
                                f"文件 [{file_item.name}] 上传成功，路径: {path_label}",
                            )
                        except Exception as log_exc:
                            self.log.emit(f"写上传日志失败：{log_exc}")
                        self.item_update.emit(task.index, fi, "成功", file_url)
                        success += 1
                        done_files += 1
                        self.progress.emit(done_files, total_files)
                        self.log.emit(f"成功：{file_item.name}")
                    except Exception as exc:
                        if self.stop_event.is_set():
                            self.item_update.emit(task.index, fi, "已停止", "")
                            break
                        failed += 1
                        done_files += 1
                        self.progress.emit(done_files, total_files)
                        self.item_update.emit(task.index, fi, f"失败：{exc}", "")
                        self.log.emit(f"失败：{file_item.name} -> {exc}")

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
            display_cat1, display_cat2, display_cat3, display_type = "图片素材", "", "", "图片"
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
    def __init__(
        self,
        parent=None,
        credentials_supplier=None,
        spreadsheet_supplier=None,  # 保留参数兼容旧调用；上传表独立，不再回落
        token_path: str = "",
    ):
        super().__init__(parent)
        self.credentials_supplier = credentials_supplier
        self.token_path = token_path or default_token_path()
        self.worker = None
        self.category_rows: list[list[str]] = []
        self.tasks: list[UploadTask] = []
        self.pending_files: list[str] = []
        self.is_image_mode = False
        self.task_seq = 0
        self.settings_collapsed = False
        self.build_ui()
        self.connect_signals()
        self.load_saved_settings()

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
        """上传专用表格 ID，与「表格下载」页完全独立，不共用。"""
        return self.spreadsheet_edit.text().strip()

    @staticmethod
    def _solid_bg(widget: QWidget, color: str = "#0b1120"):
        """Solid fill so leftover white Base never shows through."""
        if widget is None:
            return
        c = QColor(color)
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        widget.setAutoFillBackground(True)
        pal = widget.palette()
        pal.setColor(QPalette.ColorRole.Window, c)
        pal.setColor(QPalette.ColorRole.Base, c)
        widget.setPalette(pal)

    def apply_page_fill(self, bg: str = "#0b1120"):
        """Called from main window theme switch; keeps left column free of white bands."""
        self._solid_bg(self, bg)
        scroll = getattr(self, "_left_scroll", None)
        inner = getattr(self, "_left_inner", None)
        if scroll is not None:
            scroll.setStyleSheet("")  # drop any transparent override
            self._solid_bg(scroll, bg)
            vp = scroll.viewport()
            if vp is not None:
                vp.setStyleSheet("")
                self._solid_bg(vp, bg)
        if inner is not None:
            self._solid_bg(inner, bg)

    def build_ui(self):
        self.setObjectName("pageFill")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._solid_bg(self, "#0b1120")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        tip = QLabel(
            "凭据请在顶部「⚙ 全局设置」配置（各功能共用）；本页表格 ID 独立（入库/分类目录专用，不与下载页共用）。"
            "设置可保存并折叠。支持拖入已按分类建好的文件夹，自动匹配一/二/三级。"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        root.addWidget(tip)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        # ----- 左侧 -----
        # 不用 transparent：Windows 上 QScrollArea viewport 默认 Base=白，会透出白底
        left_scroll = QScrollArea()
        left_scroll.setObjectName("pageFill")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(360)
        left_scroll.setMaximumWidth(460)
        left_inner = QWidget()
        left_inner.setObjectName("scrollInner")
        left_inner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, 4, 0)
        left.setSpacing(10)
        left_scroll.setWidget(left_inner)
        body.addWidget(left_scroll, 0)
        self._left_scroll = left_scroll
        self._left_inner = left_inner
        self.apply_page_fill("#0b1120")

        # 可折叠设置
        self.settings_toggle = QToolButton()
        self.settings_toggle.setObjectName("collapseBtn")
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setChecked(True)
        self.settings_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_toggle.setText("▾  设置（点此折叠/展开）")
        self.settings_toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        left.addWidget(self.settings_toggle)

        self.settings_body = QFrame()
        self.settings_body.setObjectName("settingsCard")
        sg = QVBoxLayout(self.settings_body)
        sg.setContentsMargins(14, 12, 14, 12)
        sg.setSpacing(8)
        left.addWidget(self.settings_body)

        self.credentials_label = QLabel("")
        self.credentials_label.setObjectName("subtitle")
        self.credentials_label.setWordWrap(True)
        sg.addWidget(self.credentials_label)

        self.spreadsheet_edit = QLineEdit()
        self.spreadsheet_edit.setPlaceholderText("上传专用表格 ID（入库表 / 分类目录所在表格，必填）")
        self.parent_folder_edit = QLineEdit(DEFAULT_PARENT_FOLDER_ID)
        self.data_sheet_edit = QLineEdit(DATA_SHEET_NAME)
        self.category_sheet_edit = QLineEdit(CATEGORY_SHEET_NAME)
        self.log_sheet_edit = QLineEdit(LOG_SHEET_NAME)
        self.uploader_edit = QLineEdit()
        self.uploader_edit.setPlaceholderText("上传者姓名 *")

        for label, widget in [
            ("上传专用表格 ID *", self.spreadsheet_edit),
            ("Drive 父文件夹 ID", self.parent_folder_edit),
            ("入库表名称", self.data_sheet_edit),
            ("分类目录名称", self.category_sheet_edit),
            ("上传日志名称", self.log_sheet_edit),
            ("默认上传者 *", self.uploader_edit),
        ]:
            sg.addWidget(self._labeled(label, widget))

        self.copyright_check = QCheckBox("我保证版权没有问题")
        self.copyright_check.setChecked(True)
        self.skip_existing_check = QCheckBox("云端同名则跳过（仍写入库表）")
        self.skip_existing_check.setChecked(True)
        sg.addWidget(self.copyright_check)
        sg.addWidget(self.skip_existing_check)

        set_btns = QHBoxLayout()
        self.save_settings_btn = QPushButton("保存设置并折叠")
        self.save_settings_btn.setObjectName("primaryButton")
        self.load_cat_btn = QPushButton("加载分类目录")
        self.load_cat_btn.setObjectName("secondaryButton")
        self.sync_cred_btn = QPushButton("刷新凭据")
        self.sync_cred_btn.setObjectName("ghostButton")
        set_btns.addWidget(self.save_settings_btn)
        set_btns.addWidget(self.load_cat_btn)
        set_btns.addWidget(self.sync_cred_btn)
        sg.addLayout(set_btns)

        # 日常操作区（设置折叠后仍可见）
        work = Card("选择分类 / 添加文件", "workCard")
        left.addWidget(work)

        mode_row = QHBoxLayout()
        self.mode_standard_btn = QPushButton("分类上传")
        self.mode_standard_btn.setObjectName("primaryButton")
        self.mode_image_btn = QPushButton("图片直传")
        self.mode_image_btn.setObjectName("secondaryButton")
        mode_row.addWidget(self.mode_standard_btn)
        mode_row.addWidget(self.mode_image_btn)
        work.layout.addLayout(mode_row)

        self.cat_panel = QFrame()
        cat_box = QVBoxLayout(self.cat_panel)
        cat_box.setContentsMargins(0, 0, 0, 0)
        cat_box.setSpacing(6)
        work.layout.addWidget(self.cat_panel)

        self.cat1_combo = QComboBox()
        self.cat2_combo = QComboBox()
        self.cat3_combo = QComboBox()
        self.video_type_combo = QComboBox()
        self.video_type_combo.setEditable(True)
        self.video_type_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for c in (self.cat1_combo, self.cat2_combo, self.cat3_combo, self.video_type_combo):
            c.clear()
            c.addItem("")
        for label, w in [
            ("一级（表 A 列）*", self.cat1_combo),
            ("二级（表 B 列）", self.cat2_combo),
            ("三级（表 C 列）", self.cat3_combo),
            ("类型（表 D 列）", self.video_type_combo),
        ]:
            cat_box.addWidget(self._labeled(label, w))

        self.drop_zone = DropZone()
        work.layout.addWidget(self.drop_zone)

        file_btns = QHBoxLayout()
        self.pick_files_btn = QPushButton("选文件")
        self.pick_files_btn.setObjectName("secondaryButton")
        self.pick_folder_btn = QPushButton("选分类文件夹")
        self.pick_folder_btn.setObjectName("secondaryButton")
        self.pick_folder_btn.setToolTip("选择已按「一级/二级/三级」建好的本地文件夹，自动匹配分类并拆成任务")
        file_btns.addWidget(self.pick_files_btn)
        file_btns.addWidget(self.pick_folder_btn)
        work.layout.addLayout(file_btns)

        self.pending_label = QLabel("待添加：0 个文件")
        self.pending_label.setObjectName("status")
        work.layout.addWidget(self.pending_label)

        self.add_task_btn = QPushButton("加入清单 →")
        self.add_task_btn.setObjectName("primaryButton")
        self.start_btn = QPushButton("开始批量上传")
        self.start_btn.setObjectName("primaryButton")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        self.clear_tasks_btn = QPushButton("清空清单")
        self.clear_tasks_btn.setObjectName("ghostButton")
        for b in (self.add_task_btn, self.start_btn, self.stop_btn, self.clear_tasks_btn):
            work.layout.addWidget(b)

        left.addStretch(1)

        # ----- 右侧 -----
        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 1)

        task_card = Card("任务列表")
        right.addWidget(task_card, 3)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "模式", "分类", "文件", "状态", "文件链接", "文件夹"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for i, w in enumerate([36, 72, 150, 160, 100, 160]):
            self.table.setColumnWidth(i, w)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        task_card.layout.addWidget(self.table)

        prog_card = Card("上传进度")
        right.addWidget(prog_card, 0)
        self.progress_label = QLabel("等待开始")
        self.progress_label.setObjectName("status")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        prog_card.layout.addWidget(self.progress_label)
        prog_card.layout.addWidget(self.progress_bar)

        receipt_card = Card("回执链接")
        right.addWidget(receipt_card, 2)
        self.receipt_box = QTextEdit()
        self.receipt_box.setReadOnly(True)
        self.receipt_box.setObjectName("pasteTextBox")
        self.receipt_box.setPlaceholderText("上传成功后显示文件夹/文件链接，可复制。")
        receipt_card.layout.addWidget(self.receipt_box)
        rb = QHBoxLayout()
        self.copy_receipt_btn = QPushButton("复制全部回执")
        self.copy_receipt_btn.setObjectName("secondaryButton")
        self.clear_receipt_btn = QPushButton("清空回执")
        self.clear_receipt_btn.setObjectName("ghostButton")
        self.open_folder_btn = QPushButton("打开父文件夹")
        self.open_folder_btn.setObjectName("secondaryButton")
        rb.addWidget(self.copy_receipt_btn)
        rb.addWidget(self.clear_receipt_btn)
        rb.addWidget(self.open_folder_btn)
        rb.addStretch()
        receipt_card.layout.addLayout(rb)

        log_card = Card("日志")
        right.addWidget(log_card, 1)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        log_card.layout.addWidget(self.log_box)

        self.status_row = QLabel("保存设置后可折叠左侧配置，日常只需选分类 / 拖文件夹")
        self.status_row.setObjectName("status")
        root.addWidget(self.status_row)

        self.refresh_credentials_label()
        self.log("上传页就绪。设置保存到本机；分类来自表格；支持拖拽文件夹自动匹配。")

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
        self.settings_toggle.toggled.connect(self._on_settings_toggled)
        self.save_settings_btn.clicked.connect(self.save_settings_and_collapse)
        self.load_cat_btn.clicked.connect(self.load_categories)
        self.sync_cred_btn.clicked.connect(self.refresh_credentials_label)
        self.mode_standard_btn.clicked.connect(lambda: self.set_image_mode(False))
        self.mode_image_btn.clicked.connect(lambda: self.set_image_mode(True))
        self.cat1_combo.currentTextChanged.connect(self.on_cat1_changed)
        self.cat2_combo.currentTextChanged.connect(self.on_cat2_changed)
        self.drop_zone.paths_dropped.connect(self.on_paths_dropped)
        self.pick_files_btn.clicked.connect(self.pick_files)
        self.pick_folder_btn.clicked.connect(self.pick_category_folder)
        self.add_task_btn.clicked.connect(self.add_task)
        self.start_btn.clicked.connect(self.start_upload)
        self.stop_btn.clicked.connect(self.stop_upload)
        self.clear_tasks_btn.clicked.connect(self.clear_tasks)
        self.copy_receipt_btn.clicked.connect(self.copy_receipts)
        self.clear_receipt_btn.clicked.connect(self.receipt_box.clear)
        self.open_folder_btn.clicked.connect(self.open_parent_folder)

    def _on_settings_toggled(self, expanded: bool):
        self.settings_body.setVisible(expanded)
        self.settings_toggle.setText("▾  设置（点此折叠）" if expanded else "▸  设置（已折叠，点此展开）")
        self.settings_collapsed = not expanded

    def save_settings_and_collapse(self):
        data = {
            "spreadsheet_id": self.spreadsheet_edit.text().strip(),
            "parent_folder_id": self.parent_folder_edit.text().strip(),
            "data_sheet": self.data_sheet_edit.text().strip(),
            "category_sheet": self.category_sheet_edit.text().strip(),
            "log_sheet": self.log_sheet_edit.text().strip(),
            "uploader": self.uploader_edit.text().strip(),
            "copyright": self.copyright_check.isChecked(),
            "skip_existing": self.skip_existing_check.isChecked(),
            "collapsed": True,
        }
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.log(f"设置已保存：{settings_path()}")
        except Exception as exc:
            QMessageBox.warning(self, APP_SECTION, f"保存失败：{exc}")
            return
        self.settings_toggle.setChecked(False)
        self.status_row.setText("设置已保存并折叠 · 可直接选分类或拖文件夹")

    def load_saved_settings(self):
        path = settings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        self.spreadsheet_edit.setText(data.get("spreadsheet_id", "") or self.spreadsheet_edit.text())
        if data.get("parent_folder_id"):
            self.parent_folder_edit.setText(data["parent_folder_id"])
        if data.get("data_sheet"):
            self.data_sheet_edit.setText(data["data_sheet"])
        if data.get("category_sheet"):
            self.category_sheet_edit.setText(data["category_sheet"])
        if data.get("log_sheet"):
            self.log_sheet_edit.setText(data["log_sheet"])
        if data.get("uploader"):
            self.uploader_edit.setText(data["uploader"])
        self.copyright_check.setChecked(bool(data.get("copyright", True)))
        self.skip_existing_check.setChecked(bool(data.get("skip_existing", True)))
        if data.get("collapsed"):
            self.settings_toggle.setChecked(False)
        self.log("已恢复本机上传设置。")

    def refresh_credentials_label(self):
        path = self.credentials_path()
        token = self.token_path
        token_ok = os.path.exists(token)
        self.credentials_label.setText(
            f"凭据（来自顶部「全局设置」）：{path or '未设置'}\n"
            f"授权缓存：{token}"
            + ("（已有，一般无需重新登录）" if token_ok else "（首次使用会打开浏览器授权一次）")
        )

    def log(self, message: str):
        self.log_box.append(f"[{time.strftime('%H:%M:%S')}] {message}")

    def set_image_mode(self, enabled: bool):
        self.is_image_mode = enabled
        if enabled:
            self.mode_image_btn.setObjectName("primaryButton")
            self.mode_standard_btn.setObjectName("secondaryButton")
            self.cat_panel.setEnabled(False)
        else:
            self.mode_standard_btn.setObjectName("primaryButton")
            self.mode_image_btn.setObjectName("secondaryButton")
            self.cat_panel.setEnabled(True)
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
            return
        self.refresh_credentials_label()
        sid = self.spreadsheet_id()
        if not sid:
            QMessageBox.warning(
                self,
                APP_SECTION,
                "请填写本页「上传专用表格 ID」。\n（与「表格下载」页的表格相互独立，不会自动共用。）",
            )
            self.settings_toggle.setChecked(True)
            return
        self.start_btn.setEnabled(False)
        worker = CategoryLoadWorker(
            self.credentials_path(),
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
        cat1_values, seen1 = [], set()
        video_types, seen_t = [], set()
        for r in self.category_rows:
            c1 = str(r[0] if r else "").strip()
            if c1 and c1 not in seen1:
                seen1.add(c1)
                cat1_values.append(c1)
            c4 = str(r[3] if len(r) > 3 else "").strip()
            if c4 and c4 not in seen_t:
                seen_t.add(c4)
                video_types.append(c4)

        self.cat1_combo.blockSignals(True)
        self.cat1_combo.clear()
        self.cat1_combo.addItem("")
        self.cat1_combo.addItems(cat1_values)
        self.cat1_combo.blockSignals(False)
        self.cat2_combo.clear()
        self.cat2_combo.addItem("")
        self.cat3_combo.clear()
        self.cat3_combo.addItem("")
        self.video_type_combo.blockSignals(True)
        self.video_type_combo.clear()
        self.video_type_combo.addItem("")
        self.video_type_combo.addItems(video_types)
        self.video_type_combo.blockSignals(False)

        self.log(f"表格分类：一级 {len(cat1_values)} · 类型(D列) {len(video_types)}")
        self.status_row.setText(f"分类来自表格：{len(self.category_rows)} 行 · 一级 {len(cat1_values)} · 类型 {len(video_types)}")

    def on_cat1_changed(self, text: str):
        text = (text or "").strip()
        vals, seen = [], set()
        for r in self.category_rows:
            if str(r[0] if r else "").strip() != text:
                continue
            c2 = str(r[1] if len(r) > 1 else "").strip()
            if c2 and c2 not in seen:
                seen.add(c2)
                vals.append(c2)
        self.cat2_combo.blockSignals(True)
        self.cat2_combo.clear()
        self.cat2_combo.addItem("")
        self.cat2_combo.addItems(vals)
        self.cat2_combo.blockSignals(False)
        self.cat3_combo.clear()
        self.cat3_combo.addItem("")
        self._refresh_video_types_for_selection()

    def on_cat2_changed(self, text: str):
        c1 = self.cat1_combo.currentText().strip()
        c2 = (text or "").strip()
        vals, seen = [], set()
        for r in self.category_rows:
            if str(r[0] if r else "").strip() != c1:
                continue
            if str(r[1] if len(r) > 1 else "").strip() != c2:
                continue
            c3 = str(r[2] if len(r) > 2 else "").strip()
            if c3 and c3 not in seen:
                seen.add(c3)
                vals.append(c3)
        self.cat3_combo.clear()
        self.cat3_combo.addItem("")
        self.cat3_combo.addItems(vals)
        self._refresh_video_types_for_selection()

    def _refresh_video_types_for_selection(self):
        c1 = self.cat1_combo.currentText().strip()
        c2 = self.cat2_combo.currentText().strip()
        types, seen = [], set()
        for r in self.category_rows:
            if c1 and str(r[0] if r else "").strip() != c1:
                continue
            if c2 and str(r[1] if len(r) > 1 else "").strip() != c2:
                continue
            c4 = str(r[3] if len(r) > 3 else "").strip()
            if c4 and c4 not in seen:
                seen.add(c4)
                types.append(c4)
        if not types:
            for r in self.category_rows:
                c4 = str(r[3] if len(r) > 3 else "").strip()
                if c4 and c4 not in seen:
                    seen.add(c4)
                    types.append(c4)
        cur = self.video_type_combo.currentText().strip()
        self.video_type_combo.blockSignals(True)
        self.video_type_combo.clear()
        self.video_type_combo.addItem("")
        self.video_type_combo.addItems(types)
        if cur:
            self.video_type_combo.setCurrentText(cur)
        self.video_type_combo.blockSignals(False)

    def on_paths_dropped(self, paths: list):
        files, folders = [], []
        for p in paths:
            if os.path.isfile(p):
                files.append(p)
            elif os.path.isdir(p):
                folders.append(p)
        if folders:
            for folder in folders:
                self._ingest_category_folder(folder)
        if files:
            self.pending_files.extend(files)
            self.pending_files = list(dict.fromkeys(self.pending_files))
            self.pending_label.setText(f"待添加：{len(self.pending_files)} 个文件（再点「加入清单」）")
            self.log(f"拖入文件 {len(files)} 个。")

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", "所有文件 (*.*)")
        if not paths:
            return
        self.pending_files.extend(paths)
        self.pending_files = list(dict.fromkeys(self.pending_files))
        self.pending_label.setText(f"待添加：{len(self.pending_files)} 个文件")

    def pick_category_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择已按分类建好的文件夹")
        if folder:
            self._ingest_category_folder(folder)

    def _ingest_category_folder(self, folder: str):
        """递归扫描文件夹，按相对路径匹配分类，自动生成多个任务。"""
        if not self.category_rows:
            QMessageBox.information(
                self, APP_SECTION,
                "请先「加载分类目录」，才能根据文件夹名自动匹配一/二/三级。",
            )
            return
        uploader = self.uploader_edit.text().strip()
        if not uploader:
            QMessageBox.warning(self, APP_SECTION, "请先在设置中填写默认上传者。")
            return

        all_files = list_files_recursive(folder)
        if not all_files:
            QMessageBox.information(self, APP_SECTION, "文件夹内没有文件。")
            return

        # group by matched (c1,c2,c3,c4, is_image)
        groups: dict[tuple, list[str]] = {}
        unmatched = []
        root = os.path.abspath(folder)
        root_name = os.path.basename(root.rstrip("\\/"))

        for fpath in all_files:
            rel = os.path.relpath(fpath, root)
            parts = rel.replace("\\", "/").split("/")
            dir_parts = parts[:-1]
            # 把根文件夹名也加入匹配链（若本地是「自拍素材/动物/..」整夹拖入）
            chain = [root_name] + dir_parts if root_name else dir_parts
            c1, c2, c3, c4 = match_path_to_category(chain, self.category_rows)
            if not c1:
                # 再试：不用根名，仅用相对目录
                c1, c2, c3, c4 = match_path_to_category(dir_parts, self.category_rows)
            ext = os.path.splitext(fpath)[1].lower()
            is_img = ext in IMAGE_EXTS and self.is_image_mode
            if not c1 and not is_img:
                unmatched.append(fpath)
                # 仍尝试用当前下拉选择
                c1 = self.cat1_combo.currentText().strip()
                c2 = self.cat2_combo.currentText().strip()
                c3 = self.cat3_combo.currentText().strip()
                c4 = self.video_type_combo.currentText().strip()
            key = (c1, c2, c3, c4, is_img)
            groups.setdefault(key, []).append(fpath)

        added = 0
        for (c1, c2, c3, c4, is_img), files in groups.items():
            if not files:
                continue
            if not is_img and not c1:
                continue
            self.task_seq += 1
            task = UploadTask(
                index=self.task_seq,
                is_image_mode=is_img,
                cat1=c1 if not is_img else "图片素材",
                cat2="" if is_img else c2,
                cat3="" if is_img else c3,
                video_type="" if is_img else c4,
                uploader=uploader,
                files=[UploadFileItem(path=p, name=os.path.basename(p)) for p in files],
                source_folder=folder,
            )
            self.tasks.append(task)
            added += 1
            self.log(
                f"自动任务#{task.index}："
                f"{'/'.join([x for x in [c1, c2, c3, c4] if x]) or '图片'} · {len(files)} 文件"
            )

        self.rebuild_table()
        if unmatched:
            self.log(f"有 {len(unmatched)} 个文件未匹配到分类（已尽量归入当前下拉选择）。")
        self.status_row.setText(f"从文件夹自动加入 {added} 个任务")
        self.log(f"文件夹扫描完成：{folder}")

    def add_task(self):
        if not self.pending_files:
            QMessageBox.information(self, APP_SECTION, "请先选择或拖入文件。")
            return
        uploader = self.uploader_edit.text().strip()
        if not uploader:
            QMessageBox.warning(self, APP_SECTION, "请填写上传者。")
            return
        if not self.is_image_mode:
            cat1 = self.cat1_combo.currentText().strip()
            if not cat1:
                QMessageBox.warning(self, APP_SECTION, "请选择一级目录，或拖入带分类名的文件夹。")
                return
            cat2 = self.cat2_combo.currentText().strip()
            cat3 = self.cat3_combo.currentText().strip()
            vtype = self.video_type_combo.currentText().strip()
        else:
            cat1 = cat2 = cat3 = vtype = ""

        self.task_seq += 1
        files = [UploadFileItem(path=p, name=os.path.basename(p)) for p in self.pending_files if os.path.isfile(p)]
        task = UploadTask(
            index=self.task_seq,
            is_image_mode=self.is_image_mode,
            cat1=cat1 if not self.is_image_mode else "图片素材",
            cat2=cat2, cat3=cat3, video_type=vtype,
            uploader=uploader, files=files,
        )
        self.tasks.append(task)
        self.pending_files = []
        self.pending_label.setText("待添加：0 个文件")
        self.rebuild_table()
        self.log(f"已加入任务#{task.index}，{len(files)} 个文件。")

    def clear_tasks(self):
        if self.has_running_worker():
            return
        self.tasks = []
        self.rebuild_table()
        self.progress_bar.setValue(0)
        self.progress_label.setText("等待开始")

    def rebuild_table(self):
        rows = []
        for task in self.tasks:
            mode = "图片" if task.is_image_mode else "分类"
            path_label = " / ".join(
                [x for x in [task.cat1, task.cat2, task.cat3, task.video_type, task.uploader] if x]
            )
            for f in task.files:
                rows.append((task, f, mode, path_label))
        self.table.setRowCount(len(rows))
        for i, (task, f, mode, path_label) in enumerate(rows):
            vals = [task.index, mode, path_label, f.name, f.status, f.file_url, task.folder_url]
            for col, val in enumerate(vals):
                cell = QTableWidgetItem("" if val is None else str(val))
                if col == 4:
                    st = str(f.status or "")
                    if "成功" in st or st == "待上传":
                        cell.setForeground(QColor("#34d399"))
                    elif "失败" in st:
                        cell.setForeground(QColor("#f87171"))
                    elif "跳过" in st:
                        cell.setForeground(QColor("#fbbf24"))
                self.table.setItem(i, col, cell)
        self.status_row.setText(
            f"清单 {len(self.tasks)} 任务 / {sum(len(t.files) for t in self.tasks)} 文件"
        )
        self.refresh_receipt_box()

    def refresh_receipt_box(self):
        lines = []
        for task in self.tasks:
            title = " / ".join([x for x in [task.cat1, task.cat2, task.cat3, task.video_type] if x])
            lines.append(f"【任务#{task.index}】{title or '图片直传'}")
            if task.folder_url:
                lines.append(f"  文件夹：{task.folder_url}")
            for f in task.files:
                if f.file_url:
                    lines.append(f"  · {f.name}\n    {f.file_url}")
            lines.append("")
        text = "\n".join(lines).strip()
        if text:
            self.receipt_box.setPlainText(text)

    def copy_receipts(self):
        text = self.receipt_box.toPlainText().strip()
        if not text:
            QMessageBox.information(self, APP_SECTION, "暂无回执。")
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self.log("回执已复制。")

    def start_upload(self):
        if self.has_running_worker():
            return
        if not self.tasks:
            QMessageBox.information(self, APP_SECTION, "请先加入任务。")
            return
        self.refresh_credentials_label()
        sid = self.spreadsheet_id()
        parent = self.parent_folder_edit.text().strip()
        if not sid or not parent:
            QMessageBox.warning(
                self,
                APP_SECTION,
                "请填写本页「上传专用表格 ID」与「Drive 父文件夹 ID」。\n"
                "上传表格与下载表格相互独立，请分别配置后点「保存设置并折叠」。",
            )
            self.settings_toggle.setChecked(True)
            return

        self.set_running(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("上传中…")
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
            self.log("已请求停止。")

    def on_item_update(self, task_index, file_index, status, file_url):
        for task in self.tasks:
            if task.index == task_index and 0 <= file_index < len(task.files):
                task.files[file_index].status = status
                if file_url:
                    task.files[file_index].file_url = file_url
                break
        self.rebuild_table()

    def on_task_update(self, task_index, status, folder_url):
        for task in self.tasks:
            if task.index == task_index:
                task.status = status
                if folder_url:
                    task.folder_url = folder_url
                break
        self.rebuild_table()

    def on_progress(self, done, total):
        pct = int(done * 100 / total) if total else 0
        self.progress_bar.setValue(pct)
        self.progress_label.setText(f"{done}/{total}（{pct}%）")

    def on_upload_done(self):
        self.progress_label.setText("上传完成")
        self.refresh_receipt_box()
        self.log("上传流程结束。")

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
        fid = self.parent_folder_edit.text().strip()
        if not fid:
            return
        QDesktopServices.openUrl(QUrl(f"https://drive.google.com/drive/folders/{fid}"))

    def show_error(self, message: str):
        self.log(message)
        if "scope" in message.lower() or "insufficient" in message.lower() or "权限" in message:
            message += (
                f"\n\n授权文件位置：\n{self.token_path}\n"
                "仅当权限不足时删除该文件再登录一次即可。"
            )
        QMessageBox.warning(self, APP_SECTION, message)

    def request_close(self) -> bool:
        if not self.has_running_worker():
            return True
        self.stop_upload()
        if self.worker and not self.worker.wait(3000):
            return False
        return True
