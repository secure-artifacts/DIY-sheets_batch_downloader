import json
import os
import queue
import re
import sys
import threading
import time
from html import unescape
from html.parser import HTMLParser

from PySide6.QtCore import QThread, Qt, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sheets_batch_downloader import (
    GoogleClient,
    DownloadItem,
    PublicDownloader,
    extract_drive_file_info,
    extension_from_name,
    find_url_in_text,
    parse_title,
    sanitize_path_part,
    unique_path,
)
from video_batch_downloader import VideoBatchPage
from drive_batch_uploader import DriveBatchUploadPage
from version import APP_NAME, APP_VERSION, RELEASES_PAGE
from updater import (
    check_for_update,
    download_release,
    format_size,
    launch_installer_or_replace,
)


APP_TITLE = f"{APP_NAME} v{APP_VERSION}"


def bundle_base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_credentials_path():
    preferred = os.path.join(app_base_dir(), "谷歌服务账号.json")
    if os.path.exists(preferred):
        return preferred
    preferred = os.path.join(bundle_base_dir(), "谷歌服务账号.json")
    if os.path.exists(preferred):
        return preferred
    return os.path.join(app_base_dir(), "credentials.json")


def asset_path(name):
    return os.path.join(bundle_base_dir(), "assets", name).replace("\\", "/")


def logo_path():
    bundled = os.path.join(bundle_base_dir(), "logo.png")
    if os.path.exists(bundled):
        return bundled
    return os.path.join(app_base_dir(), "logo.png")


def build_target_path(output_dir, item, source_name):
    safe_source_name = sanitize_path_part(source_name or "file.jpg")
    if not extension_from_name(safe_source_name):
        safe_source_name += ".jpg"
    return os.path.join(output_dir, item.group_name, safe_source_name)


class LinkHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current_href = ""
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attrs = dict(attrs)
            self.current_href = unescape(attrs.get("href", "") or "")
            self.current_text = []

    def handle_data(self, data):
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current_href:
            text = unescape("".join(self.current_text)).strip()
            self.links.append((text, self.current_href.strip()))
            self.current_href = ""
            self.current_text = []


def clean_pasted_url(url):
    value = unescape(str(url or "")).strip()
    if not value:
        return ""
    found = find_url_in_text(value)
    return found or value


def source_name_from_url(url):
    file_id, _ = extract_drive_file_info(url)
    if file_id:
        return ""
    name = PublicDownloader().prepare_name(url)
    return name if name and name != "file.jpg" else ""


def parse_pasted_links(plain_text, html_text, group_mode, keyword=""):
    records = []

    if html_text:
        parser = LinkHTMLParser()
        try:
            parser.feed(html_text)
            for text, href in parser.links:
                url = clean_pasted_url(href)
                if url.startswith("http"):
                    records.append((text.strip(), url))
        except Exception:
            pass

    for raw_line in str(plain_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        url = clean_pasted_url(line)
        if not url.startswith("http"):
            continue
        title = line.replace(url, "").strip(" \t-|：:")
        records.append((title, url))

    seen = set()
    items = []
    keyword_text = str(keyword or "").strip().lower()
    for index, (title, url) in enumerate(records, start=1):
        if url in seen:
            continue
        seen.add(url)
        source_name = title or source_name_from_url(url)
        display_name = source_name or f"粘贴链接-{index}"
        if keyword_text and keyword_text not in display_name.lower() and keyword_text not in url.lower():
            continue
        group_name, file_number = parse_title(display_name, index, group_mode)
        items.append(DownloadItem(index, display_name, url, source_name, "", group_name, file_number))
    return items


class WorkerBase(QThread):
    log = Signal(str)
    failed = Signal(str)

    def __init__(self, credentials_path, token_path):
        super().__init__()
        self.credentials_path = credentials_path
        self.token_path = token_path

    def make_client(self):
        client = GoogleClient(self.credentials_path, self.token_path)
        self.log.emit(f"已使用凭据：{client.account_label}")
        return client


class SheetLoadWorker(WorkerBase):
    loaded = Signal(list)

    def __init__(self, credentials_path, token_path, spreadsheet_id):
        super().__init__(credentials_path, token_path)
        self.spreadsheet_id = spreadsheet_id

    def run(self):
        try:
            client = self.make_client()
            infos = client.list_sheets(self.spreadsheet_id)
            self.loaded.emit([(info.title, info.row_count) for info in infos])
            self.log.emit(f"已加载 {len(infos)} 个工作表。")
        except Exception as exc:
            self.failed.emit(f"加载工作表失败：{exc}")


class PreviewWorker(WorkerBase):
    preview_ready = Signal(list)

    def __init__(self, credentials_path, token_path, settings):
        super().__init__(credentials_path, token_path)
        self.settings = settings

    def run(self):
        try:
            client = self.make_client()
            settings = dict(self.settings)
            scan_all = settings.pop("scan_all", False)
            if scan_all:
                for info in client.list_sheets(settings["spreadsheet_id"]):
                    if info.title == settings["sheet_name"] and info.row_count:
                        settings["end_row"] = info.row_count
                        self.log.emit(f"已刷新结束行：{info.row_count}")
                        break
            items = client.read_items(**settings)
            rows = []
            for item in items[:500]:
                rows.append({
                    "row_number": item.row_number,
                    "folder": item.group_name,
                    "title": item.title,
                    "source_name": item.source_name,
                    "match_name": item.match_name,
                    "url": item.url,
                    "has_link": item.url.startswith("http"),
                })
            self.preview_ready.emit(rows)
            link_count = sum(1 for item in items if item.url.startswith("http"))
            self.log.emit(f"预览完成：匹配 {len(items)} 行，其中 {link_count} 行有链接。")
        except Exception as exc:
            self.failed.emit(f"预览失败：{exc}")


class DownloadWorker(WorkerBase):
    progress = Signal(dict)
    done = Signal()

    def __init__(self, credentials_path, token_path, settings, output_dir, skip_existing, backfill_enabled, backfill_col, pasted_items=None):
        super().__init__(credentials_path, token_path)
        self.settings = settings
        self.output_dir = output_dir
        self.skip_existing = skip_existing
        self.backfill_enabled = backfill_enabled
        self.backfill_col = backfill_col
        self.pasted_items = pasted_items
        self.stop_event = threading.Event()
        self.backfill_disabled = False

    def stop(self):
        self.stop_event.set()

    def try_backfill(self, client, item, source_name):
        if self.pasted_items is not None or not self.backfill_enabled or self.backfill_disabled:
            return
        value = item.match_name or source_name
        try:
            client.write_success_name(
                self.settings["spreadsheet_id"],
                self.settings["sheet_name"],
                item.row_number,
                self.backfill_col,
                value,
            )
        except Exception as exc:
            message = str(exc)
            if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in message or "insufficient authentication scopes" in message:
                self.backfill_disabled = True
                self.log.emit("回填失败：当前授权缺少表格写入权限。请关闭程序，删除 token.json 后重新授权；本次将继续下载但不再回填。")
            else:
                self.log.emit(f"第 {item.row_number} 行回填失败：{exc}")

    def run(self):
        success = 0
        skipped = 0
        failed = 0
        try:
            client = None
            public_downloader = PublicDownloader()
            if self.pasted_items is not None:
                items = list(self.pasted_items)
            else:
                client = self.make_client()
                settings = dict(self.settings)
                scan_all = settings.pop("scan_all", False)
                if scan_all:
                    for info in client.list_sheets(settings["spreadsheet_id"]):
                        if info.title == settings["sheet_name"] and info.row_count:
                            settings["end_row"] = info.row_count
                            self.log.emit(f"已刷新结束行：{info.row_count}")
                            break
                items = client.read_items(**settings)
            self.log.emit(f"准备下载 {len(items)} 行。")

            for item in items:
                if self.stop_event.is_set():
                    self.log.emit("任务已停止。")
                    break

                if not item.url or not item.url.startswith("http"):
                    skipped += 1
                    self.log.emit(f"第 {item.row_number} 行跳过：没有链接")
                    continue

                if re.search(r"drive\.google\.com/(?:drive/(?:u/\d+/)?folders|folderview)", item.url, re.I):
                    skipped += 1
                    self.log.emit(f"第 {item.row_number} 行跳过：文件夹链接")
                    continue

                try:
                    file_id, _ = extract_drive_file_info(item.url)
                    if file_id:
                        if client is None:
                            client = self.make_client()
                        source_name = item.source_name or client.get_drive_file_name(file_id)
                        target_path = build_target_path(self.output_dir, item, source_name)
                        if self.skip_existing and os.path.exists(target_path):
                            skipped += 1
                            self.log.emit(f"已存在，跳过：{target_path}")
                            self.try_backfill(client, item, source_name)
                            continue
                        saved_path = client.download_drive_file(file_id, unique_path(target_path), self.stop_event)
                    else:
                        source_name = item.source_name or public_downloader.prepare_name(item.url)
                        target_path = build_target_path(self.output_dir, item, source_name)
                        if self.skip_existing and os.path.exists(target_path):
                            skipped += 1
                            self.log.emit(f"已存在，跳过：{target_path}")
                            self.try_backfill(client, item, source_name)
                            continue
                        saved_path = public_downloader.download(item.url, unique_path(target_path))

                    success += 1
                    self.try_backfill(client, item, source_name)
                    self.log.emit(f"成功：{saved_path}")
                    self.progress.emit({"success": success, "skipped": skipped, "failed": failed})
                    time.sleep(0.08)
                except Exception as exc:
                    if self.stop_event.is_set():
                        self.log.emit("任务已停止。")
                        break
                    failed += 1
                    self.log.emit(f"第 {item.row_number} 行失败：{exc}")
                    self.progress.emit({"success": success, "skipped": skipped, "failed": failed})

            self.log.emit(f"完成：成功 {success}，跳过 {skipped}，失败 {failed}。")
        except Exception as exc:
            self.failed.emit(f"任务失败：{exc}")
        finally:
            self.done.emit()


class Card(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(18, 16, 18, 16)
        self.layout.setSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        self.layout.addWidget(title_label)


class PasteLinksDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("粘贴链接")
        self.resize(760, 520)
        self.action = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        tip = QLabel("支持从表格复制的超链接、HTML 链接、纯文本 URL。粘贴链接下载不需要填写表格 ID 和回填列。")
        tip.setObjectName("subtitle")
        layout.addWidget(tip)

        self.text_box = QTextEdit()
        self.text_box.setObjectName("pasteTextBox")
        self.text_box.setAcceptRichText(True)
        self.text_box.setPlaceholderText("在这里粘贴链接，例如：\n张三-文件名.mp4  https://drive.google.com/file/d/...\n或直接从 Google 表格复制带超链接的单元格。")
        layout.addWidget(self.text_box, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        layout.addLayout(buttons)
        self.clipboard_btn = QPushButton("从剪贴板读取")
        self.clipboard_btn.setObjectName("secondaryButton")
        self.clear_paste_btn = QPushButton("清空内容")
        self.clear_paste_btn.setObjectName("ghostButton")
        self.preview_btn = QPushButton("预览粘贴")
        self.preview_btn.setObjectName("secondaryButton")
        self.download_btn = QPushButton("下载粘贴")
        self.download_btn.setObjectName("primaryButton")
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("ghostButton")
        buttons.addWidget(self.clipboard_btn)
        buttons.addWidget(self.clear_paste_btn)
        buttons.addStretch()
        buttons.addWidget(self.preview_btn)
        buttons.addWidget(self.download_btn)
        buttons.addWidget(self.cancel_btn)

        self.clipboard_btn.clicked.connect(self.load_clipboard)
        self.clear_paste_btn.clicked.connect(self.text_box.clear)
        self.preview_btn.clicked.connect(lambda: self.finish("preview"))
        self.download_btn.clicked.connect(lambda: self.finish("download"))
        self.cancel_btn.clicked.connect(self.reject)

    def load_clipboard(self):
        mime = QApplication.clipboard().mimeData()
        if mime.hasHtml():
            self.text_box.setHtml(mime.html())
        elif mime.hasText():
            self.text_box.setPlainText(mime.text())

    def finish(self, action):
        self.action = action
        self.accept()

    def plain_text(self):
        return self.text_box.toPlainText()

    def html_text(self):
        return self.text_box.toHtml()


class UpdateCheckWorker(QThread):
    found = Signal(object)
    none = Signal()
    failed = Signal(str)

    def __init__(self, silent=False):
        super().__init__()
        self.silent = silent

    def run(self):
        try:
            info = check_for_update()
            if info:
                self.found.emit(info)
            else:
                self.none.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateDownloadWorker(QThread):
    progress = Signal(int, int)
    finished_path = Signal(str, object)
    failed = Signal(str)

    def __init__(self, release_info):
        super().__init__()
        self.release_info = release_info

    def run(self):
        try:
            path = download_release(
                self.release_info,
                progress_callback=lambda d, t: self.progress.emit(d, t),
            )
            self.finished_path.emit(path, self.release_info)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        icon_file = logo_path()
        if os.path.exists(icon_file):
            self.setWindowIcon(QIcon(icon_file))
        self.resize(1240, 820)
        self.setMinimumSize(1120, 760)
        self.sheet_rows = {}
        self.worker = None
        self.update_worker = None
        self.config_queue = []
        self.running_all_configs = False
        self.preview_pasted_items = []
        self.token_file_path = os.path.join(app_base_dir(), "token.json")
        self.config_file_path = os.path.join(app_base_dir(), "diy_downloader_configs.json")
        self.configs = {}
        self.pending_release = None

        self.build_ui()
        self.apply_style()
        self.load_configs()
        # 启动后静默检查更新
        self.schedule_startup_update_check()

    def build_ui(self):
        root = QWidget()
        root.setObjectName("rootWidget")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 14, 18, 14)
        root_layout.setSpacing(10)
        self.setCentralWidget(root)

        header = QHBoxLayout()
        header.setSpacing(10)
        root_layout.addLayout(header)

        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel(f"v{APP_VERSION} · 表格下载 · 视频下载 · 云端上传 · 自动更新")
        subtitle.setObjectName("subtitle")
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.theme_check = QCheckBox("暗黑模式")
        self.theme_check.setChecked(True)
        self.update_btn = QPushButton("检查更新")
        self.update_btn.setObjectName("secondaryButton")
        self.guide_btn = QPushButton("使用说明")
        self.guide_btn.setObjectName("ghostButton")
        self.help_btn = QPushButton("查看帮助")
        self.help_btn.setObjectName("ghostButton")
        header.addWidget(self.theme_check)
        header.addWidget(self.update_btn)
        header.addWidget(self.guide_btn)
        header.addWidget(self.help_btn)

        self.main_tabs = QTabWidget()
        self.main_tabs.setObjectName("mainTabs")
        root_layout.addWidget(self.main_tabs, 1)

        sheets_page = QWidget()
        sheets_layout = QVBoxLayout(sheets_page)
        sheets_layout.setContentsMargins(12, 12, 12, 12)
        sheets_layout.setSpacing(10)
        self.main_tabs.addTab(sheets_page, "表格 / 粘贴链接")

        self.video_page = VideoBatchPage()
        self.main_tabs.addTab(self.video_page, "YouTube / FB 视频")

        self.upload_page = DriveBatchUploadPage(
            credentials_supplier=lambda: self.credentials_edit.text().strip(),
            token_path=self.token_file_path,
        )
        self.main_tabs.addTab(self.upload_page, "批量上传云端")

        compact_panel = QFrame()
        compact_panel.setObjectName("compactPanel")
        panel_layout = QGridLayout(compact_panel)
        panel_layout.setContentsMargins(14, 12, 14, 12)
        panel_layout.setHorizontalSpacing(10)
        panel_layout.setVerticalSpacing(8)
        sheets_layout.addWidget(compact_panel)

        self.credentials_edit = QLineEdit(default_credentials_path())
        self.output_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "Downloads", "批量下载"))
        self.spreadsheet_edit = QLineEdit()
        self.sheet_combo = QComboBox()
        self.name_col_edit = QLineEdit("A")
        self.link_col_edit = QLineEdit("P")
        self.backfill_col_edit = QLineEdit("Q")
        self.start_spin = QSpinBox()
        self.start_spin.setRange(1, 999999)
        self.start_spin.setValue(2)
        self.start_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.end_spin = QSpinBox()
        self.end_spin.setRange(1, 999999)
        self.end_spin.setValue(100)
        self.end_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.folder_mode_combo = QComboBox()
        self.folder_mode_combo.addItems(["按人名", "按编号前缀", "按A列完整名称"])
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("例如：张三 或 张三-李四")
        self.config_combo = QComboBox()
        self.config_combo.setMinimumWidth(150)
        self.config_name_edit = QLineEdit()
        self.config_name_edit.setPlaceholderText("方案名称")
        self.save_config_btn = QPushButton("保存方案")
        self.save_config_btn.setObjectName("primaryButton")
        self.delete_config_btn = QPushButton("删除方案")
        self.delete_config_btn.setObjectName("ghostButton")

        self.add_compact_field(panel_layout, "配置", self.config_combo, 0, 0)
        self.add_compact_field(panel_layout, "方案名", self.config_name_edit, 0, 1, 1, 2)
        panel_layout.addWidget(self.save_config_btn, 0, 3)
        panel_layout.addWidget(self.delete_config_btn, 0, 4)
        self.run_all_btn = QPushButton("执行所有方案")
        self.run_all_btn.setObjectName("secondaryButton")
        panel_layout.addWidget(self.run_all_btn, 0, 5)

        self.add_compact_field(panel_layout, "凭据文件", self.credentials_edit, 1, 0, 1, 3)
        self.add_button(panel_layout, "选择", self.choose_credentials, 1, 3)
        self.add_compact_field(panel_layout, "下载目录", self.output_edit, 1, 4, 1, 3)
        self.add_button(panel_layout, "选择", self.choose_output_dir, 1, 7)

        self.add_compact_field(panel_layout, "表格 ID", self.spreadsheet_edit, 2, 0, 1, 3)
        self.add_button(panel_layout, "加载工作表", self.load_sheets, 2, 3)
        self.add_compact_field(panel_layout, "工作表", self.sheet_combo, 2, 4, 1, 2)
        self.add_compact_field(panel_layout, "名称列", self.name_col_edit, 2, 6)
        self.add_compact_field(panel_layout, "链接列", self.link_col_edit, 2, 7)

        self.add_compact_field(panel_layout, "起始行", self.start_spin, 3, 0)
        self.add_compact_field(panel_layout, "结束行", self.end_spin, 3, 1)
        self.add_compact_field(panel_layout, "文件夹命名", self.folder_mode_combo, 3, 2, 1, 2)
        self.add_compact_field(panel_layout, "只下载包含", self.keyword_edit, 3, 4, 1, 2)
        self.add_compact_field(panel_layout, "回填列", self.backfill_col_edit, 3, 6)

        self.scan_all_check = QCheckBox("扫描整个工作表")
        self.scan_all_check.setChecked(True)
        self.skip_existing_check = QCheckBox("已下载过则跳过")
        self.skip_existing_check.setChecked(True)
        self.backfill_check = QCheckBox("下载成功后回填表格")
        self.backfill_check.setChecked(True)
        options = QHBoxLayout()
        options.setSpacing(12)
        options.addWidget(self.scan_all_check)
        options.addWidget(self.skip_existing_check)
        options.addWidget(self.backfill_check)
        options.addStretch()
        panel_layout.addLayout(options, 3, 7)

        for col in range(8):
            panel_layout.setColumnStretch(col, 1)
        panel_layout.setColumnStretch(2, 2)
        panel_layout.setColumnStretch(5, 2)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        sheets_layout.addLayout(actions)
        self.refresh_btn = QPushButton("刷新表格")
        self.refresh_btn.setObjectName("secondaryButton")
        self.preview_btn = QPushButton("预览读取")
        self.preview_btn.setObjectName("secondaryButton")
        self.start_btn = QPushButton("开始下载")
        self.start_btn.setObjectName("primaryButton")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        self.open_folder_btn = QPushButton("打开文件夹")
        self.open_folder_btn.setObjectName("secondaryButton")
        self.paste_links_btn = QPushButton("粘贴链接下载")
        self.paste_links_btn.setObjectName("secondaryButton")
        self.clear_log_btn = QPushButton("清空日志")
        self.clear_log_btn.setObjectName("ghostButton")
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.preview_btn)
        actions.addWidget(self.start_btn)
        actions.addWidget(self.stop_btn)
        actions.addWidget(self.paste_links_btn)
        actions.addWidget(self.open_folder_btn)
        actions.addWidget(self.clear_log_btn)
        actions.addStretch()

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        sheets_layout.addLayout(bottom, 4)
        preview_card = Card("预览")
        log_card = Card("日志")
        bottom.addWidget(preview_card, 7)
        bottom.addWidget(log_card, 3)

        self.preview_table = QTableWidget(0, 7)
        self.preview_table.setHorizontalHeaderLabels(["行号", "文件夹", "回填名", "源文件名", "A列名称", "链接状态", "链接"])
        header = self.preview_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.preview_table.setColumnWidth(0, 54)
        self.preview_table.setColumnWidth(1, 150)
        self.preview_table.setColumnWidth(2, 100)
        self.preview_table.setColumnWidth(3, 300)
        self.preview_table.setColumnWidth(4, 200)
        self.preview_table.setColumnWidth(5, 78)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.verticalHeader().setDefaultSectionSize(24)
        self.preview_table.setAlternatingRowColors(True)
        preview_card.layout.addWidget(self.preview_table)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumWidth(420)
        log_card.layout.addWidget(self.log_box)

        self.status_row = QLabel("等待开始")
        self.status_row.setObjectName("status")
        sheets_layout.addWidget(self.status_row)

        self.preview_btn.clicked.connect(self.preview_items)
        self.refresh_btn.clicked.connect(self.refresh_sheet_info)
        self.start_btn.clicked.connect(self.start_download)
        self.stop_btn.clicked.connect(self.stop_download)
        self.paste_links_btn.clicked.connect(self.open_paste_links_dialog)
        self.open_folder_btn.clicked.connect(self.open_output_folder)
        self.clear_log_btn.clicked.connect(self.log_box.clear)
        self.guide_btn.clicked.connect(self.show_usage_guide)
        self.help_btn.clicked.connect(self.show_help)
        self.update_btn.clicked.connect(lambda: self.check_updates(silent=False))
        self.theme_check.toggled.connect(self.apply_style)
        self.sheet_combo.currentTextChanged.connect(self.sync_sheet_end_row)
        self.config_combo.currentTextChanged.connect(self.apply_selected_config)
        self.save_config_btn.clicked.connect(self.save_current_config)
        self.delete_config_btn.clicked.connect(self.delete_current_config)
        self.run_all_btn.clicked.connect(self.start_all_configs)

    def add_file_row(self, layout, label, edit, handler, button_text="选择"):
        row = QHBoxLayout()
        row.setSpacing(12)
        text = QLabel(label)
        text.setObjectName("inlineLabel")
        text.setFixedWidth(92)
        row.addWidget(text)
        row.addWidget(edit, 1)
        button = QPushButton(button_text)
        button.setObjectName("secondaryButton")
        button.setMinimumWidth(92)
        button.clicked.connect(handler)
        row.addWidget(button)
        layout.addLayout(row)

    def add_button(self, grid, text, handler, row, col):
        field = QFrame()
        field.setObjectName("fieldBox")
        box = QVBoxLayout(field)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        caption = QLabel(" ")
        caption.setObjectName("fieldLabel")
        button = QPushButton(text)
        button.setObjectName("secondaryButton")
        button.setMinimumWidth(96)
        button.clicked.connect(handler)
        box.addWidget(caption)
        box.addWidget(button)
        grid.addWidget(field, row, col)
        return button

    def add_compact_field(self, grid, label, widget, row, col, row_span=1, col_span=1):
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

    def add_grid_field(self, grid, label, widget, row, col, row_span=1, col_span=1):
        return self.add_compact_field(grid, label, widget, row, col, row_span, col_span)

    def current_config_data(self):
        return {
            "credentials": self.credentials_edit.text(),
            "output_dir": self.output_edit.text(),
            "spreadsheet_id": self.spreadsheet_edit.text(),
            "sheet_name": self.sheet_combo.currentText(),
            "name_col": self.name_col_edit.text(),
            "link_col": self.link_col_edit.text(),
            "backfill_col": self.backfill_col_edit.text(),
            "start_row": self.start_spin.value(),
            "end_row": self.end_spin.value(),
            "folder_mode": self.folder_mode_combo.currentText(),
            "keyword": self.keyword_edit.text(),
            "scan_all": self.scan_all_check.isChecked(),
            "skip_existing": self.skip_existing_check.isChecked(),
            "backfill": self.backfill_check.isChecked(),
        }

    def load_configs(self):
        try:
            if os.path.exists(self.config_file_path):
                with open(self.config_file_path, "r", encoding="utf-8") as f:
                    self.configs = json.load(f)
            else:
                self.configs = {}
        except Exception as exc:
            self.configs = {}
            self.log(f"读取配置失败：{exc}")

        self.refresh_config_combo()

    def refresh_config_combo(self):
        current = self.config_combo.currentText()
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        self.config_combo.addItem("不使用配置")
        for name in sorted(self.configs.keys()):
            self.config_combo.addItem(name)
        if current in self.configs:
            self.config_combo.setCurrentText(current)
        self.config_combo.blockSignals(False)

    def write_configs(self):
        with open(self.config_file_path, "w", encoding="utf-8") as f:
            json.dump(self.configs, f, ensure_ascii=False, indent=2)

    def save_current_config(self):
        name = self.config_name_edit.text().strip() or self.config_combo.currentText().strip()
        if not name or name == "不使用配置":
            QMessageBox.warning(self, APP_TITLE, "请先填写配置方案名称。")
            return
        self.configs[name] = self.current_config_data()
        self.write_configs()
        self.refresh_config_combo()
        self.config_combo.setCurrentText(name)
        self.config_name_edit.setText(name)
        self.log(f"已保存配置方案：{name}")

    def delete_current_config(self):
        name = self.config_combo.currentText().strip()
        if not name or name == "不使用配置" or name not in self.configs:
            QMessageBox.information(self, APP_TITLE, "请选择要删除的配置方案。")
            return
        if QMessageBox.question(self, APP_TITLE, f"确定删除配置方案“{name}”吗？") != QMessageBox.StandardButton.Yes:
            return
        del self.configs[name]
        self.write_configs()
        self.refresh_config_combo()
        self.config_name_edit.clear()
        self.log(f"已删除配置方案：{name}")

    def apply_selected_config(self, name):
        if not name or name == "不使用配置" or name not in self.configs:
            return
        cfg = self.configs[name]
        self.config_name_edit.setText(name)
        self.credentials_edit.setText(cfg.get("credentials", self.credentials_edit.text()))
        self.output_edit.setText(cfg.get("output_dir", self.output_edit.text()))
        self.spreadsheet_edit.setText(cfg.get("spreadsheet_id", ""))
        saved_sheet = cfg.get("sheet_name", "")
        if saved_sheet and self.sheet_combo.findText(saved_sheet) < 0:
            self.sheet_combo.addItem(saved_sheet)
        if saved_sheet:
            self.sheet_combo.setCurrentText(saved_sheet)
        self.name_col_edit.setText(cfg.get("name_col", "A"))
        self.link_col_edit.setText(cfg.get("link_col", "P"))
        self.backfill_col_edit.setText(cfg.get("backfill_col", "Q"))
        self.start_spin.setValue(int(cfg.get("start_row", 2) or 2))
        self.end_spin.setValue(int(cfg.get("end_row", 100) or 100))
        folder_mode = cfg.get("folder_mode", "按人名")
        if self.folder_mode_combo.findText(folder_mode) >= 0:
            self.folder_mode_combo.setCurrentText(folder_mode)
        self.keyword_edit.setText(cfg.get("keyword", ""))
        self.scan_all_check.setChecked(bool(cfg.get("scan_all", True)))
        self.skip_existing_check.setChecked(bool(cfg.get("skip_existing", True)))
        self.backfill_check.setChecked(bool(cfg.get("backfill", True)))
        self.log(f"已切换配置方案：{name}")

    def apply_style(self):
        dropdown_arrow = asset_path("dropdown_arrow.svg")
        checkmark = asset_path("checkmark.svg")
        dark = not hasattr(self, "theme_check") or self.theme_check.isChecked()
        if dark:
            colors = {
                "bg": "#0b1120", "panel": "#111a2e", "panel2": "#0d1628", "line": "#263855",
                "text": "#d4deec", "muted": "#93a4ba", "title": "#e8eef8", "accent": "#2f6df6",
                "accent2": "#38bdf8", "soft": "#16243b", "table": "#0a1324", "table_alt": "#101b31",
                "header": "#172743", "danger": "#7f1d1d", "danger_border": "#ef4444", "log": "#050b16",
                "dialog_bg": "#f8fafc", "dialog_text": "#172033",
            }
        else:
            colors = {
                "bg": "#eef5fb", "panel": "#ffffff", "panel2": "#f7fbff", "line": "#c9d9ee",
                "text": "#0f1f35", "muted": "#55708f", "title": "#10213a", "accent": "#2563eb",
                "accent2": "#0ea5e9", "soft": "#eaf3ff", "table": "#ffffff", "table_alt": "#f3f8ff",
                "header": "#e5f0ff", "danger": "#fee2e2", "danger_border": "#f87171", "log": "#08111f",
                "dialog_bg": "#ffffff", "dialog_text": "#172033",
            }
        danger_text = "#fee2e2" if dark else "#991b1b"
        self.setStyleSheet(f'''
            QMainWindow {{ background: {colors["bg"]}; color: {colors["text"]}; font-family: "Microsoft YaHei UI", "Microsoft YaHei", Arial, sans-serif; font-size: 13px; }}
            QWidget#rootWidget {{ background: {colors["bg"]}; }}
            QLabel {{ background: transparent; color: {colors["text"]}; }}
            QLabel#title {{ font-size: 23px; font-weight: 900; color: {colors["title"]}; }}
            QLabel#subtitle {{ color: {colors["muted"]}; font-size: 12px; }}
            QFrame#compactPanel, QFrame#card {{ background: {colors["panel"]}; border: 1px solid {colors["line"]}; border-radius: 14px; }}
            QLabel#cardTitle {{ font-size: 16px; font-weight: 900; color: {colors["title"]}; }}
            QLabel#inlineLabel, QLabel#fieldLabel {{ color: {colors["muted"]}; font-weight: 800; background: transparent; }}
            QLabel#fieldLabel {{ font-size: 12px; }}
            QFrame#fieldBox {{ background: transparent; border: 0; }}
            QLineEdit, QComboBox, QSpinBox {{ background: {colors["panel2"]}; color: {colors["text"]}; border: 1px solid {colors["line"]}; border-radius: 10px; padding: 6px 10px; min-height: 22px; selection-background-color: {colors["accent"]}; }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {colors["accent2"]}; background: {colors["soft"]}; }}
            QLineEdit::placeholder {{ color: {colors["muted"]}; }}
            QComboBox {{ padding-right: 34px; }}
            QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; width: 30px; border-left: 1px solid {colors["line"]}; border-top-right-radius: 10px; border-bottom-right-radius: 10px; background: {colors["soft"]}; }}
            QComboBox::down-arrow {{ image: url("{dropdown_arrow}"); width: 12px; height: 8px; }}
            QComboBox::drop-down:hover {{ background: {colors["header"]}; }}
            QComboBox QAbstractItemView {{ background: {colors["panel"]}; color: {colors["text"]}; border: 1px solid {colors["line"]}; selection-background-color: {colors["accent"]}; outline: 0; }}
            QSpinBox::up-button, QSpinBox::down-button {{ width: 0px; height: 0px; border: 0; }}
            QPushButton {{ background: {colors["accent"]}; color: #ffffff; border: 0; border-radius: 10px; padding: 7px 14px; font-weight: 800; min-height: 24px; }}
            QPushButton:hover {{ background: #1d4ed8; }}
            QPushButton:disabled {{ background: #64748b; color: #cbd5e1; }}
            QPushButton#secondaryButton {{ background: {colors["soft"]}; color: {colors["text"]}; border: 1px solid {colors["line"]}; }}
            QPushButton#secondaryButton:hover, QPushButton#ghostButton:hover {{ border: 1px solid {colors["accent2"]}; }}
            QPushButton#ghostButton {{ background: transparent; color: {colors["text"]}; border: 1px solid {colors["line"]}; }}
            QPushButton#dangerButton {{ background: {colors["danger"]}; color: {danger_text}; border: 1px solid {colors["danger_border"]}; }}
            QCheckBox {{ color: {colors["text"]}; font-weight: 800; spacing: 7px; }}
            QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {colors["line"]}; background: {colors["panel2"]}; }}
            QCheckBox::indicator:checked {{ image: url("{checkmark}"); background: {colors["accent"]}; border: 1px solid {colors["accent"]}; }}
            QTableWidget {{ background: {colors["table"]}; alternate-background-color: {colors["table_alt"]}; color: {colors["text"]}; border: 1px solid {colors["line"]}; border-radius: 10px; gridline-color: {colors["line"]}; selection-background-color: {colors["accent"]}; selection-color: #ffffff; font-size: 12px; }}
            QHeaderView::section {{ background: {colors["header"]}; color: {colors["text"]}; padding: 6px; border: 0; border-right: 1px solid {colors["line"]}; font-weight: 900; }}
            QTextEdit {{ background: {colors["log"]}; color: #66f5a1; border: 1px solid {colors["line"]}; border-radius: 12px; padding: 10px; font-family: Consolas, "Microsoft YaHei UI"; font-size: 12px; }}
            QTextEdit#pasteTextBox {{ background: {colors["panel2"]}; color: {colors["text"]}; border: 1px solid {colors["line"]}; border-radius: 12px; padding: 10px; font-family: "Microsoft YaHei UI", "Microsoft YaHei", Arial, sans-serif; font-size: 13px; }}
            QLabel#status {{ color: {colors["muted"]}; font-weight: 800; }}
            QTabWidget#mainTabs {{ background: transparent; }}
            QTabWidget#mainTabs::pane {{ border: 1px solid {colors["line"]}; border-radius: 12px; top: -1px; background: {colors["panel"]}; }}
            QTabWidget#mainTabs QTabBar::tab {{ background: {colors["soft"]}; color: {colors["muted"]}; border: 1px solid {colors["line"]}; border-bottom: 0; border-top-left-radius: 10px; border-top-right-radius: 10px; padding: 8px 18px; margin-right: 4px; font-weight: 800; min-width: 120px; }}
            QTabWidget#mainTabs QTabBar::tab:selected {{ background: {colors["panel"]}; color: {colors["title"]}; border-color: {colors["accent2"]}; }}
            QTabWidget#mainTabs QTabBar::tab:hover {{ color: {colors["title"]}; border-color: {colors["accent2"]}; }}
            QMessageBox {{ background: {colors["dialog_bg"]}; color: {colors["dialog_text"]}; font-family: "Microsoft YaHei UI", "Microsoft YaHei", Arial, sans-serif; }}
            QMessageBox QLabel {{ color: {colors["dialog_text"]}; background: transparent; font-size: 13px; }}
            QMessageBox QPushButton {{ background: {colors["accent"]}; color: #ffffff; border-radius: 9px; padding: 6px 16px; min-width: 64px; }}
            QScrollBar:vertical, QScrollBar:horizontal {{ background: transparent; width: 10px; height: 10px; }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {colors["line"]}; border-radius: 5px; min-height: 30px; min-width: 30px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        ''')

    def log(self, message):
        now = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{now}] {message}")

    def choose_credentials(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择凭据 JSON", self.credentials_edit.text(), "JSON 文件 (*.json);;所有文件 (*.*)")
        if path:
            self.credentials_edit.setText(path)

    def choose_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择下载目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def open_output_folder(self):
        path = self.output_edit.text().strip()
        if not path:
            QMessageBox.information(self, APP_TITLE, "请先选择下载目录。")
            return
        os.makedirs(path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def show_usage_guide(self):
        QMessageBox.information(
            self,
            APP_TITLE,
            "使用步骤：\n"
            "【表格 / 粘贴链接】\n"
            "1. 选择凭据文件和下载目录。\n"
            "2. 填写 Google 表格 ID，点击“加载工作表”或“刷新表格”。\n"
            "3. 设置名称列、链接列、起止行、文件夹命名和回填列。\n"
            "4. 可在“只下载包含”输入人名，例如：张三 或 张三-李四。\n"
            "5. 表格模式下先点“预览读取”确认，再点“开始下载”。\n"
            "6. 也可以点“粘贴链接下载”，粘贴从表格复制的超链接或纯文本链接；这种方式不需要表格 ID 和回填列。\n"
            "   粘贴预览后，主界面的“开始下载”会直接下载当前预览里的粘贴链接。\n"
            "   没有填写表格 ID 时，直接点“开始下载”也会进入粘贴链接下载。\n"
            "7. 多个保存方案可以点“执行所有方案”按顺序自动下载。\n\n"
            "【YouTube / FB 视频】（独立板块）\n"
            "1. 切换到“YouTube / FB 视频”标签页。\n"
            "2. 粘贴单视频、播放列表或多个链接（支持批量）。\n"
            "3. 选择下载模式：自动识别 / 仅单视频 / 展开播放列表。\n"
            "4. 可开启断点续传、列表分子文件夹、列表上限。\n"
            "5. 可直接点“开始下载”：会自动加载视频/播放列表，无需先解析。\n"
            "6. “解析预览”可选，只用于提前查看列表。\n"
            "7. 建议安装 ffmpeg，以便最佳画质音视频合并。\n\n"
            "【批量上传云端】（独立板块）\n"
            "1. 切换到“批量上传云端”标签页。\n"
            "2. 填写表格 ID、Drive 父文件夹 ID，加载「分类目录」。\n"
            "3. 选择分类模式或图片直传，选择本地文件，加入清单。\n"
            "4. 点「开始批量上传」：自动建文件夹、上传文件、写入入库表与上传日志。\n"
            "5. 若提示 Drive 权限不足，删除 token.json 后重新授权。"
        )

    def show_help(self):
        QMessageBox.information(
            self,
            APP_TITLE,
            "规则说明：\n"
            "• 默认读取 P 列链接，A 列用于创建文件夹。\n"
            "• 文件名使用链接单元格显示的源文件名，不再用行号命名。\n"
            "• 粘贴链接支持 HTML 超链接和纯文本 URL；纯 Drive 链接会自动查询真实文件名。\n"
            "• 表格 ID、工作表、回填列只用于表格读取模式；粘贴链接下载不会回填表格。\n"
            "• 下载成功后可把匹配到的人名写入 Q 列，失败不会写入。\n"
            "• 勾选“扫描整个工作表”时，预览和下载都会自动刷新最新结束行。\n"
            "• 勾选“已下载过则跳过”时，本地已有同名文件会跳过。\n"
            "• 如果回填提示权限不足，删除 token.json 后重新授权即可。\n"
            "• YouTube / Facebook 视频板块基于 yt-dlp，与表格下载相互独立。\n"
            "• 支持 YouTube 单视频与播放列表、批量多链接、断点续传。\n"
            "• Facebook 公开 Reels/视频可下载；可识别的清单会尝试展开，私密内容会失败。"
        )

    def refresh_sheet_info(self):
        self.load_sheets()

    def items_to_preview_rows(self, items):
        return [{
            "row_number": item.row_number,
            "folder": item.group_name,
            "match_name": item.match_name,
            "source_name": item.source_name,
            "title": item.title,
            "has_link": bool(item.url and item.url.startswith("http")),
            "url": item.url,
        } for item in items]

    def open_paste_links_dialog(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_TITLE, "当前任务还在运行，请结束后再粘贴链接。")
            return
        dialog = PasteLinksDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        items = parse_pasted_links(
            dialog.plain_text(),
            dialog.html_text(),
            self.folder_mode_combo.currentText(),
            self.keyword_edit.text().strip(),
        )
        if not items:
            QMessageBox.information(self, APP_TITLE, "没有解析到可下载链接。请确认粘贴内容里包含 http/https 链接。")
            return
        if dialog.action == "preview":
            self.preview_pasted_items = list(items)
            self.fill_preview(self.items_to_preview_rows(items[:500]))
            link_count = sum(1 for item in items if item.url.startswith("http"))
            self.status_row.setText(f"粘贴预览完成：显示 {min(len(items), 500)} 行，可直接点开始下载")
            self.log(f"粘贴预览完成：解析 {len(items)} 行，其中 {link_count} 行有链接。可直接点击“开始下载”。")
            return
        self.start_pasted_download(items)

    def start_pasted_download(self, items):
        output_dir = self.output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, APP_TITLE, "请先选择下载目录。")
            return
        self.running_all_configs = False
        self.config_queue = []
        self.set_running_state(True)
        self.status_row.setText("正在下载粘贴链接...")
        worker = DownloadWorker(
            self.credentials_edit.text(),
            self.token_file_path,
            {},
            output_dir,
            self.skip_existing_check.isChecked(),
            False,
            "",
            pasted_items=items,
        )
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_download_finished)
        self.worker = worker
        worker.start()

    def settings_from_config(self, cfg):
        return {
            "spreadsheet_id": str(cfg.get("spreadsheet_id", "")).strip(),
            "sheet_name": str(cfg.get("sheet_name", "")).strip(),
            "start_row": int(cfg.get("start_row", 2) or 2),
            "end_row": int(cfg.get("end_row", 100) or 100),
            "name_col": str(cfg.get("name_col", "A")).strip(),
            "link_col": str(cfg.get("link_col", "P")).strip(),
            "group_mode": str(cfg.get("folder_mode", "按人名")).strip(),
            "keyword": str(cfg.get("keyword", "")).strip(),
            "scan_all": bool(cfg.get("scan_all", True)),
        }

    def set_running_state(self, running):
        self.refresh_btn.setEnabled(not running)
        self.start_btn.setEnabled(not running)
        self.preview_btn.setEnabled(not running)
        self.run_all_btn.setEnabled(not running)
        self.paste_links_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def has_running_worker(self):
        return bool(self.worker and self.worker.isRunning())

    def start_all_configs(self):
        names = [name for name in sorted(self.configs.keys()) if name and name != "不使用配置"]
        if not names:
            QMessageBox.information(self, APP_TITLE, "还没有保存的配置方案。")
            return
        self.config_queue = names
        self.running_all_configs = True
        self.log(f"准备执行 {len(names)} 个配置方案。")
        self.run_next_config()

    def run_next_config(self):
        if not self.config_queue:
            self.running_all_configs = False
            self.set_running_state(False)
            self.status_row.setText("所有配置方案已执行完成")
            self.log("所有配置方案已执行完成。")
            return
        name = self.config_queue.pop(0)
        cfg = self.configs.get(name, {})
        if not cfg.get("spreadsheet_id") or not cfg.get("sheet_name"):
            self.log(f"跳过配置“{name}”：缺少表格 ID 或工作表。")
            self.run_next_config()
            return
        output_dir = str(cfg.get("output_dir", "")).strip()
        if not output_dir:
            self.log(f"跳过配置“{name}”：缺少下载目录。")
            self.run_next_config()
            return
        self.config_combo.setCurrentText(name)
        self.status_row.setText(f"正在执行配置：{name}")
        self.log(f"开始配置方案：{name}")
        self.set_running_state(True)
        worker = DownloadWorker(
            str(cfg.get("credentials", self.credentials_edit.text())),
            self.token_file_path,
            self.settings_from_config(cfg),
            output_dir,
            bool(cfg.get("skip_existing", True)),
            bool(cfg.get("backfill", True)),
            str(cfg.get("backfill_col", "Q")).strip(),
        )
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_download_finished)
        self.worker = worker
        worker.start()

    def settings(self):
        end_row = self.end_spin.value()
        if self.scan_all_check.isChecked():
            end_row = self.sheet_rows.get(self.sheet_combo.currentText(), end_row)
        return {
            "spreadsheet_id": self.spreadsheet_edit.text().strip(),
            "sheet_name": self.sheet_combo.currentText().strip(),
            "start_row": self.start_spin.value(),
            "end_row": end_row,
            "name_col": self.name_col_edit.text().strip(),
            "link_col": self.link_col_edit.text().strip(),
            "group_mode": self.folder_mode_combo.currentText(),
            "keyword": self.keyword_edit.text().strip(),
            "scan_all": self.scan_all_check.isChecked(),
        }

    def load_sheets(self):
        if self.has_running_worker():
            QMessageBox.information(self, APP_TITLE, "当前任务还在运行，请结束后再刷新。")
            return
        if not self.spreadsheet_edit.text().strip():
            QMessageBox.warning(self, APP_TITLE, "请先填写表格 ID。")
            return
        self.pending_sheet_name = self.sheet_combo.currentText()
        self.status_row.setText("正在加载工作表...")
        worker = SheetLoadWorker(self.credentials_edit.text(), self.token_file_path, self.spreadsheet_edit.text().strip())
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.loaded.connect(self.on_sheets_loaded)
        worker.finished.connect(self.on_worker_finished)
        self.worker = worker
        worker.start()

    def on_sheets_loaded(self, sheets):
        current_sheet = getattr(self, "pending_sheet_name", "") or self.sheet_combo.currentText()
        self.sheet_combo.clear()
        self.sheet_rows = {title: row_count for title, row_count in sheets}
        self.sheet_combo.addItems([title for title, _ in sheets])
        if current_sheet and self.sheet_combo.findText(current_sheet) >= 0:
            self.sheet_combo.setCurrentText(current_sheet)
        self.sync_sheet_end_row()
        self.status_row.setText(f"已加载 {len(sheets)} 个工作表")
        self.pending_sheet_name = ""

    def sync_sheet_end_row(self):
        row_count = self.sheet_rows.get(self.sheet_combo.currentText())
        if row_count:
            self.end_spin.setValue(row_count)

    def preview_items(self):
        if not self.sheet_combo.currentText():
            QMessageBox.warning(self, APP_TITLE, "请先加载并选择工作表。")
            return
        if self.has_running_worker():
            QMessageBox.information(self, APP_TITLE, "当前任务还在运行，请结束后再预览。")
            return
        self.preview_pasted_items = []
        self.status_row.setText("正在预览...")
        self.preview_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.run_all_btn.setEnabled(False)
        worker = PreviewWorker(self.credentials_edit.text(), self.token_file_path, self.settings())
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.preview_ready.connect(self.fill_preview)
        worker.finished.connect(self.on_worker_finished)
        self.worker = worker
        worker.start()

    def fill_preview(self, rows):
        self.preview_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row.get("row_number"),
                row.get("folder"),
                row.get("match_name"),
                row.get("source_name"),
                row.get("title"),
                "有链接" if row["has_link"] else "无链接",
                row.get("url"),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem("" if value is None else str(value))
                if col == 5:
                    item.setForeground(QColor("#15803d" if row["has_link"] else "#dc2626"))
                self.preview_table.setItem(row_index, col, item)
        self.status_row.setText(f"预览完成：显示 {len(rows)} 行")

    def on_worker_finished(self):
        if self.sender() is self.worker:
            self.worker = None
        self.preview_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.run_all_btn.setEnabled(True)

    def start_download(self):
        if self.preview_pasted_items:
            self.start_pasted_download(list(self.preview_pasted_items))
            return
        if not self.spreadsheet_edit.text().strip() or not self.sheet_combo.currentText():
            self.status_row.setText("未选择表格，已切换到粘贴链接下载")
            self.open_paste_links_dialog()
            return
        self.preview_pasted_items = []
        output_dir = self.output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, APP_TITLE, "请先选择下载目录。")
            return
        self.running_all_configs = False
        self.config_queue = []
        self.set_running_state(True)
        self.status_row.setText("正在下载...")
        worker = DownloadWorker(
            self.credentials_edit.text(),
            self.token_file_path,
            self.settings(),
            output_dir,
            self.skip_existing_check.isChecked(),
            self.backfill_check.isChecked(),
            self.backfill_col_edit.text().strip(),
        )
        worker.log.connect(self.log)
        worker.failed.connect(self.show_error)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_download_finished)
        self.worker = worker
        worker.start()

    def stop_download(self):
        self.config_queue = []
        self.running_all_configs = False
        if isinstance(self.worker, DownloadWorker):
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.status_row.setText("正在停止...")
            self.log("已请求停止任务。")
        else:
            self.set_running_state(False)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            if isinstance(self.worker, DownloadWorker):
                self.stop_download()
            if not self.worker.wait(3000):
                event.ignore()
                self.status_row.setText("任务仍在停止中，请稍后再关闭")
                self.log("任务仍在停止中，已取消关闭窗口。")
                return
        if hasattr(self, "video_page") and not self.video_page.request_close():
            event.ignore()
            self.log("视频下载任务仍在停止中，已取消关闭窗口。")
            return
        if hasattr(self, "upload_page") and not self.upload_page.request_close():
            event.ignore()
            self.log("云端上传任务仍在停止中，已取消关闭窗口。")
            return
        event.accept()

    def on_progress(self, stats):
        self.status_row.setText(f"成功 {stats['success']}，跳过 {stats['skipped']}，失败 {stats['failed']}")

    def on_download_finished(self):
        if self.running_all_configs and self.config_queue:
            if self.sender() is self.worker:
                self.worker = None
            self.run_next_config()
            return
        finished_all = self.running_all_configs
        self.running_all_configs = False
        self.config_queue = []
        self.set_running_state(False)
        self.worker = None
        if finished_all:
            self.status_row.setText("所有配置方案已执行完成")
            self.log("所有配置方案已执行完成。")
        else:
            self.status_row.setText("任务结束")

    def show_error(self, message):
        self.log(message)
        self.status_row.setText("出现错误")
        QMessageBox.warning(self, APP_TITLE, message)

    def schedule_startup_update_check(self):
        # 延迟一点，避免抢启动 UI
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2500, lambda: self.check_updates(silent=True))
        except Exception:
            self.check_updates(silent=True)

    def check_updates(self, silent=False):
        if self.update_worker and self.update_worker.isRunning():
            if not silent:
                QMessageBox.information(self, APP_TITLE, "正在检查更新，请稍候。")
            return
        if not silent:
            self.status_row.setText("正在检查更新...")
            self.log(f"正在检查更新（当前 v{APP_VERSION}）...")
            self.update_btn.setEnabled(False)
        worker = UpdateCheckWorker(silent=silent)
        worker.found.connect(lambda info, s=silent: self.on_update_found(info, silent=s))
        worker.none.connect(lambda s=silent: self.on_update_none(silent=s))
        worker.failed.connect(lambda msg, s=silent: self.on_update_failed(msg, silent=s))
        worker.finished.connect(lambda: self.update_btn.setEnabled(True))
        self.update_worker = worker
        worker.start()

    def on_update_none(self, silent=False):
        if silent:
            return
        self.status_row.setText(f"已是最新版本 v{APP_VERSION}")
        self.log(f"已是最新版本 v{APP_VERSION}")
        QMessageBox.information(
            self,
            APP_TITLE,
            f"当前已是最新版本 v{APP_VERSION}。\n\n发布页：{RELEASES_PAGE}",
        )

    def on_update_failed(self, message, silent=False):
        self.log(f"检查更新失败：{message}")
        if silent:
            return
        self.status_row.setText("检查更新失败")
        QMessageBox.warning(self, APP_TITLE, f"检查更新失败：\n{message}\n\n可手动打开：\n{RELEASES_PAGE}")

    def on_update_found(self, info, silent=False):
        self.pending_release = info
        size_text = format_size(info.asset_size)
        kind = "安装包" if info.is_installer else "便携版"
        notes = (info.body or "").strip()
        if len(notes) > 600:
            notes = notes[:600] + "..."
        msg = (
            f"发现新版本 v{info.version}（当前 v{APP_VERSION}）\n\n"
            f"文件：{info.asset_name}（{kind}，{size_text}）\n\n"
        )
        if notes:
            msg += f"更新说明：\n{notes}\n\n"
        msg += "是否下载更新？"
        self.status_row.setText(f"发现新版本 v{info.version}")
        self.log(f"发现新版本 v{info.version}：{info.asset_name}")
        if QMessageBox.question(self, APP_TITLE, msg) != QMessageBox.StandardButton.Yes:
            self.log("用户取消下载更新。")
            return
        self.start_update_download(info)

    def start_update_download(self, info):
        self.update_btn.setEnabled(False)
        self.status_row.setText(f"正在下载更新 v{info.version}...")
        self.log(f"开始下载更新：{info.asset_url}")
        worker = UpdateDownloadWorker(info)
        worker.progress.connect(self.on_update_download_progress)
        worker.finished_path.connect(self.on_update_downloaded)
        worker.failed.connect(self.on_update_download_failed)
        worker.finished.connect(lambda: self.update_btn.setEnabled(True))
        self.update_worker = worker
        worker.start()

    def on_update_download_progress(self, downloaded, total):
        if total:
            pct = int(downloaded * 100 / total)
            self.status_row.setText(f"下载更新 {pct}%（{format_size(downloaded)} / {format_size(total)}）")

    def on_update_download_failed(self, message):
        self.log(f"下载更新失败：{message}")
        self.status_row.setText("下载更新失败")
        QMessageBox.warning(self, APP_TITLE, f"下载更新失败：\n{message}")

    def on_update_downloaded(self, path, info):
        self.log(f"更新已下载：{path}")
        self.status_row.setText(f"更新已下载 v{info.version}")
        kind = "安装程序" if info.is_installer else "新版本程序"
        reply = QMessageBox.question(
            self,
            APP_TITLE,
            f"新版本 v{info.version} 已下载完成。\n\n"
            f"文件：{path}\n\n"
            f"是否立即运行{kind}进行安装/替换？\n"
            f"（便携版会自动替换并重启；安装包会打开安装向导）",
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.log("用户选择稍后安装。")
            QMessageBox.information(self, APP_TITLE, f"已保存到：\n{path}\n\n可稍后手动运行安装。")
            return
        try:
            launch_installer_or_replace(path, info.is_installer)
            self.log("已启动安装/替换流程，程序即将退出。")
            if getattr(sys, "frozen", False) and not info.is_installer:
                QApplication.instance().quit()
            elif info.is_installer:
                # 安装包启动后建议退出，避免文件占用
                tip = QMessageBox.question(
                    self,
                    APP_TITLE,
                    "安装程序已启动。\n是否退出当前程序以便完成安装？",
                )
                if tip == QMessageBox.StandardButton.Yes:
                    QApplication.instance().quit()
        except Exception as exc:
            self.log(f"启动安装失败：{exc}")
            QMessageBox.warning(self, APP_TITLE, f"启动安装失败：\n{exc}\n\n请手动运行：\n{path}")


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DIYDownloader.App")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    icon_file = logo_path()
    if os.path.exists(icon_file):
        app.setWindowIcon(QIcon(icon_file))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
