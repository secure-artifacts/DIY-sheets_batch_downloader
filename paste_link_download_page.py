"""独立板块：粘贴链接下载

- 从表格读取「名称列 + 链接列」（可配置，如 A / C）
- 粘贴要下载的链接，按链接列匹配表格第几行
- 下载云端文件夹/文件到本地分类目录
- 可配置回填：数量、状态（正在下载 / 已下载完成）、下载人员、完成日期
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QPalette
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
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sheets_batch_downloader import (
    GoogleClient,
    PublicDownloader,
    call_with_network_retry,
    default_token_path,
    drive_match_keys,
    extension_from_name,
    extract_drive_file_info,
    extract_drive_folder_id,
    friendly_network_error,
    is_drive_folder_url,
    parse_title,
    sanitize_path_part,
    unique_path,
)


def _build_target_path(output_dir, group_name, source_name):
    safe = sanitize_path_part(source_name or "file.jpg")
    if not extension_from_name(safe):
        safe += ".jpg"
    return os.path.join(output_dir, sanitize_path_part(group_name or "未命名"), safe)


def _folder_marker_path(local_dir: str) -> str:
    return os.path.join(local_dir, FOLDER_DONE_MARKER)


def _write_folder_marker(local_dir: str, folder_id: str, url: str, file_count: int, relative_paths: list):
    os.makedirs(local_dir, exist_ok=True)
    payload = {
        "folder_id": folder_id,
        "url": url,
        "file_count": int(file_count),
        "files": list(relative_paths),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(_folder_marker_path(local_dir), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _folder_download_complete(local_dir: str, folder_id: str, remote_files: list) -> bool:
    path = _folder_marker_path(local_dir)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        return False
    if str(marker.get("folder_id") or "") != str(folder_id or ""):
        return False
    for remote in remote_files:
        rel = str(remote.get("relative_path") or remote.get("name") or "")
        if rel and not os.path.isfile(os.path.join(local_dir, rel)):
            return False
    return True


APP_SECTION = "粘贴链接下载"
STATUS_DOWNLOADING = "正在下载"
STATUS_DONE = "已下载完成"
STATUS_FAILED = "下载失败"
STATUS_SKIPPED = "已跳过"
STATUS_PAUSED = "已暂停"
FOLDER_DONE_MARKER = ".diy_folder_done.json"
DRIVE_ID_INDEX = ".diy_drive_ids.json"


def _drive_id_index_path(output_dir: str) -> str:
    return os.path.join(output_dir, DRIVE_ID_INDEX)


def load_drive_id_index(output_dir: str) -> dict:
    """{ file_or_folder_id: {path, kind, name, updated_at} }"""
    path = _drive_id_index_path(output_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_drive_id_index(output_dir: str, index: dict):
    os.makedirs(output_dir, exist_ok=True)
    path = _drive_id_index_path(output_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def register_drive_id(index: dict, drive_id: str, local_path: str, kind: str = "file", name: str = ""):
    if not drive_id:
        return
    index[str(drive_id)] = {
        "path": local_path,
        "kind": kind,
        "name": name or os.path.basename(local_path),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def find_existing_by_drive_id(output_dir: str, index: dict, drive_id: str) -> str:
    """若该 Drive ID 已下载且本地文件/目录仍在，返回路径，否则空。"""
    if not drive_id:
        return ""
    rec = index.get(str(drive_id)) or {}
    path = str(rec.get("path") or "")
    if path and (os.path.isfile(path) or os.path.isdir(path)):
        return path
    # 索引失效时仍可扫 part 残留不算完成
    return ""


def app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def settings_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "DIYDownloader")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "paste_link_settings.json")


def clean_url(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    # 行内可能有名字+链接
    m = re.search(r"https?://[^\s\"'<>]+", raw, re.I)
    if m:
        return m.group(0).rstrip(").,;，。；")
    return raw


def extract_pasted_urls(plain: str, html: str = "") -> list[str]:
    urls = []
    seen = set()

    if html:
        for m in re.finditer(r'href=["\'](https?://[^"\']+)["\']', html, re.I):
            u = clean_url(m.group(1))
            if u.startswith("http") and u not in seen:
                seen.add(u)
                urls.append(u)

    for line in str(plain or "").splitlines():
        u = clean_url(line)
        if u.startswith("http") and u not in seen:
            seen.add(u)
            urls.append(u)
        else:
            for m in re.finditer(r"https?://[^\s\"'<>]+", line, re.I):
                u2 = clean_url(m.group(0))
                if u2.startswith("http") and u2 not in seen:
                    seen.add(u2)
                    urls.append(u2)
    return urls


@dataclass
class MatchedTask:
    paste_url: str
    row_number: int = 0
    name: str = ""
    sheet_url: str = ""
    group_name: str = ""
    status: str = "待匹配"
    file_count: int = 0
    note: str = ""
    matched: bool = False


class SheetIndexWorker(QThread):
    log = Signal(str)
    failed = Signal(str)
    loaded = Signal(list, list)  # index_rows, sheet_titles_with_counts

    def __init__(self, credentials_path, token_path, spreadsheet_id, sheet_name, name_col, link_col):
        super().__init__()
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.name_col = name_col
        self.link_col = link_col

    def run(self):
        try:
            def _load():
                client = GoogleClient(self.credentials_path, self.token_path)
                sheets = [(i.title, i.row_count) for i in client.list_sheets(self.spreadsheet_id)]
                index = client.read_name_link_index(
                    self.spreadsheet_id,
                    self.sheet_name,
                    self.name_col,
                    self.link_col,
                )
                return client, sheets, index

            _client, sheets, index = call_with_network_retry(
                _load, retries=3, delay=1.2, log=self.log.emit
            )
            with_links = sum(1 for r in index if r.get("url"))
            self.log.emit(
                f"已读表格「{self.sheet_name}」：名称列 {self.name_col} / 链接列 {self.link_col}，"
                f"共 {len(index)} 行有内容，其中 {with_links} 行有链接。"
            )
            self.loaded.emit(index, sheets)
        except Exception as exc:
            self.failed.emit(friendly_network_error(exc))


class PasteDownloadWorker(QThread):
    log = Signal(str)
    failed = Signal(str)
    progress = Signal(dict)
    task_update = Signal(int, dict)
    paused_changed = Signal(bool)
    done = Signal()

    def __init__(self, credentials_path, token_path, config: dict, tasks: list[MatchedTask]):
        super().__init__()
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.config = config
        self.tasks = list(tasks)
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._current_idx = -1

    def stop(self):
        self.pause_event.clear()
        self.stop_event.set()

    def pause(self):
        self.pause_event.set()
        self.paused_changed.emit(True)

    def resume(self):
        self.pause_event.clear()
        self.paused_changed.emit(False)

    def is_paused(self) -> bool:
        return self.pause_event.is_set()

    def _wait_while_paused(self):
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(0.2)

    def _write(self, client: GoogleClient, row: int, fields: dict):
        clean = {k: v for k, v in fields.items() if str(k or "").strip()}
        if not clean or row < 2:
            return
        client.write_row_fields(
            self.config["spreadsheet_id"],
            self.config["sheet_name"],
            row,
            clean,
        )

    def _status_fields(self, status: str, count=None, person=None, done_date=None) -> dict:
        cfg = self.config
        fields = {}
        sc = str(cfg.get("status_col") or "").strip()
        if sc:
            fields[sc] = status
        if count is not None:
            cc = str(cfg.get("count_col") or "").strip()
            if cc:
                fields[cc] = int(count)
        if person is not None:
            pc = str(cfg.get("person_col") or "").strip()
            if pc:
                fields[pc] = person
        if done_date is not None:
            dc = str(cfg.get("date_col") or "").strip()
            if dc:
                fields[dc] = done_date
        return fields

    def _resource_id(self, url: str) -> str:
        return extract_drive_folder_id(url) or extract_drive_file_info(url)[0] or ""

    def run(self):
        success = skipped = failed = 0
        id_index = {}
        output_dir = ""
        try:
            client = call_with_network_retry(
                lambda: GoogleClient(self.credentials_path, self.token_path),
                retries=3,
                delay=1.2,
                log=self.log.emit,
            )
            public = PublicDownloader()
            person = str(self.config.get("person_name") or "").strip()
            skip_existing = bool(self.config.get("skip_existing", True))
            dedupe_by_id = bool(self.config.get("dedupe_by_id", True))
            output_dir = self.config["output_dir"]
            group_mode = self.config.get("group_mode") or "按人名"
            today = date.today().isoformat()
            os.makedirs(output_dir, exist_ok=True)
            id_index = load_drive_id_index(output_dir)
            batch_seen_ids = set()

            matched = [t for t in self.tasks if t.matched and t.row_number >= 2]
            self.log.emit(
                f"开始下载 {len(matched)} 条任务（凭据：{client.account_label}；"
                f"断点续传=开；Drive ID 排重={'开' if dedupe_by_id else '关'}）。"
            )
            self.log.emit(f"本地已登记 Drive ID：{len(id_index)} 个。")

            for idx, task in enumerate(self.tasks):
                self._current_idx = idx
                self._wait_while_paused()
                if self.stop_event.is_set():
                    self.log.emit("任务已停止。")
                    break
                if not task.matched or task.row_number < 2:
                    continue

                url = task.paste_url or task.sheet_url
                resource_id = self._resource_id(url)

                if dedupe_by_id and resource_id and resource_id in batch_seen_ids:
                    skipped += 1
                    task.note = f"本批重复 ID，已跳过：{resource_id[:16]}…"
                    try:
                        self._write(
                            client,
                            task.row_number,
                            self._status_fields(STATUS_DONE, count=0, person=person or None, done_date=today),
                        )
                        task.status = STATUS_DONE
                    except Exception:
                        task.status = STATUS_SKIPPED
                    self.task_update.emit(idx, {"status": task.status, "note": task.note})
                    self.log.emit(f"第 {task.row_number} 行跳过：粘贴列表重复 Drive ID {resource_id}")
                    self.progress.emit({"success": success, "skipped": skipped, "failed": failed})
                    continue

                try:
                    self._write(
                        client,
                        task.row_number,
                        self._status_fields(STATUS_DOWNLOADING, person=person or None),
                    )
                    task.status = STATUS_DOWNLOADING
                    self.task_update.emit(idx, {"status": STATUS_DOWNLOADING})
                    self.log.emit(
                        f"第 {task.row_number} 行 [{task.name}] → {STATUS_DOWNLOADING}"
                        + (f" ID={resource_id}" if resource_id else "")
                    )
                except Exception as exc:
                    self.log.emit(f"第 {task.row_number} 行写状态失败：{exc}")

                try:
                    count = 0
                    note = ""

                    if is_drive_folder_url(url):
                        folder_id = extract_drive_folder_id(url)
                        remote_files = client.list_folder_files(folder_id, recursive=True)
                        count = len(remote_files)
                        title = task.name or f"row_{task.row_number}"
                        group_name, _ = parse_title(title, task.row_number, group_mode)
                        task.group_name = group_name
                        local_dir = os.path.join(output_dir, sanitize_path_part(group_name))

                        if skip_existing and _folder_download_complete(local_dir, folder_id, remote_files):
                            skipped += 1
                            if folder_id:
                                register_drive_id(id_index, folder_id, local_dir, "folder", group_name)
                                batch_seen_ids.add(folder_id)
                            self._write(
                                client,
                                task.row_number,
                                self._status_fields(
                                    STATUS_DONE, count=count, person=person or None, done_date=today
                                ),
                            )
                            task.status = STATUS_DONE
                            task.file_count = count
                            self.task_update.emit(
                                idx, {"status": STATUS_DONE, "file_count": count, "note": "文件夹已完整，跳过"}
                            )
                            self.log.emit(f"第 {task.row_number} 行文件夹已完整，跳过")
                            save_drive_id_index(output_dir, id_index)
                            self.progress.emit({"success": success, "skipped": skipped, "failed": failed})
                            continue

                        dl_new = dl_skip = 0
                        for n, remote in enumerate(remote_files, 1):
                            self._wait_while_paused()
                            if self.stop_event.is_set():
                                raise RuntimeError("任务已停止")
                            fid = str(remote.get("id") or "")
                            rel = str(remote.get("relative_path") or remote.get("name") or f"file_{n}")
                            target = os.path.join(local_dir, rel)

                            if dedupe_by_id and fid:
                                existing = find_existing_by_drive_id(output_dir, id_index, fid)
                                if existing and os.path.isfile(existing):
                                    dl_skip += 1
                                    batch_seen_ids.add(fid)
                                    continue
                                if fid in batch_seen_ids:
                                    dl_skip += 1
                                    continue

                            if skip_existing and os.path.isfile(target):
                                if fid:
                                    register_drive_id(id_index, fid, target, "file", rel)
                                    batch_seen_ids.add(fid)
                                dl_skip += 1
                                continue

                            client.download_drive_file(
                                fid, target, self.stop_event, pause_event=self.pause_event
                            )
                            if fid:
                                register_drive_id(id_index, fid, target, "file", rel)
                                batch_seen_ids.add(fid)
                            dl_new += 1
                            if n == 1 or n % 5 == 0 or n == count:
                                self.log.emit(
                                    f"第 {task.row_number} 行 {n}/{count}：新下 {dl_new} / 跳过 {dl_skip} · {rel}"
                                )

                        rels = [str(f.get("relative_path") or f.get("name") or "") for f in remote_files]
                        _write_folder_marker(local_dir, folder_id, url, count, rels)
                        if folder_id:
                            register_drive_id(id_index, folder_id, local_dir, "folder", group_name)
                            batch_seen_ids.add(folder_id)
                        save_drive_id_index(output_dir, id_index)
                        note = f"{local_dir}（新下 {dl_new}，跳过 {dl_skip}）"
                    else:
                        file_id, _ = extract_drive_file_info(url)
                        title = task.name or f"row_{task.row_number}"
                        group_name, _ = parse_title(title, task.row_number, group_mode)
                        task.group_name = group_name

                        if file_id:
                            if dedupe_by_id:
                                existing = find_existing_by_drive_id(output_dir, id_index, file_id)
                                if existing and os.path.isfile(existing):
                                    skipped += 1
                                    batch_seen_ids.add(file_id)
                                    self._write(
                                        client,
                                        task.row_number,
                                        self._status_fields(
                                            STATUS_DONE, count=1, person=person or None, done_date=today
                                        ),
                                    )
                                    task.status = STATUS_DONE
                                    task.file_count = 1
                                    task.note = f"ID 已存在：{existing}"
                                    self.task_update.emit(
                                        idx, {"status": STATUS_DONE, "file_count": 1, "note": task.note}
                                    )
                                    self.log.emit(f"第 {task.row_number} 行按文件 ID 跳过：{file_id}")
                                    self.progress.emit(
                                        {"success": success, "skipped": skipped, "failed": failed}
                                    )
                                    continue

                            source_name = client.get_drive_file_name(file_id)
                            target = _build_target_path(output_dir, group_name, source_name)
                            if skip_existing and os.path.isfile(target):
                                skipped += 1
                                register_drive_id(id_index, file_id, target, "file", source_name)
                                batch_seen_ids.add(file_id)
                                save_drive_id_index(output_dir, id_index)
                                self._write(
                                    client,
                                    task.row_number,
                                    self._status_fields(
                                        STATUS_DONE, count=1, person=person or None, done_date=today
                                    ),
                                )
                                task.status = STATUS_DONE
                                task.file_count = 1
                                self.task_update.emit(
                                    idx, {"status": STATUS_DONE, "file_count": 1, "note": "本地已有"}
                                )
                                self.progress.emit(
                                    {"success": success, "skipped": skipped, "failed": failed}
                                )
                                continue

                            self._wait_while_paused()
                            saved = client.download_drive_file(
                                file_id, target, self.stop_event, pause_event=self.pause_event
                            )
                            register_drive_id(id_index, file_id, saved, "file", source_name)
                            batch_seen_ids.add(file_id)
                            save_drive_id_index(output_dir, id_index)
                            count = 1
                            note = saved
                        else:
                            source_name = public.prepare_name(url)
                            target = _build_target_path(output_dir, group_name, source_name)
                            if skip_existing and os.path.isfile(target):
                                skipped += 1
                                self._write(
                                    client,
                                    task.row_number,
                                    self._status_fields(
                                        STATUS_DONE, count=1, person=person or None, done_date=today
                                    ),
                                )
                                task.status = STATUS_DONE
                                task.file_count = 1
                                self.task_update.emit(idx, {"status": STATUS_DONE, "file_count": 1})
                                self.progress.emit(
                                    {"success": success, "skipped": skipped, "failed": failed}
                                )
                                continue
                            self._wait_while_paused()
                            saved = public.download(url, unique_path(target))
                            count = 1
                            note = saved

                    if resource_id:
                        batch_seen_ids.add(resource_id)

                    self._write(
                        client,
                        task.row_number,
                        self._status_fields(
                            STATUS_DONE, count=count, person=person or None, done_date=today
                        ),
                    )
                    success += 1
                    task.status = STATUS_DONE
                    task.file_count = count
                    task.note = str(note)
                    self.task_update.emit(idx, {"status": STATUS_DONE, "file_count": count, "note": note})
                    self.log.emit(f"第 {task.row_number} 行完成：数量={count}")
                    self.progress.emit({"success": success, "skipped": skipped, "failed": failed})
                    time.sleep(0.05)

                except Exception as exc:
                    if self.stop_event.is_set():
                        try:
                            self._write(
                                client,
                                task.row_number,
                                self._status_fields("已中断", person=person or None),
                            )
                        except Exception:
                            pass
                        self.log.emit("任务已停止（再次开始将按 ID/本地文件断点续传）。")
                        break
                    failed += 1
                    try:
                        self._write(
                            client,
                            task.row_number,
                            self._status_fields(STATUS_FAILED, person=person or None),
                        )
                    except Exception:
                        pass
                    task.status = STATUS_FAILED
                    task.note = str(exc)
                    self.task_update.emit(idx, {"status": STATUS_FAILED, "note": str(exc)})
                    self.log.emit(f"第 {task.row_number} 行失败：{exc}")
                    self.progress.emit({"success": success, "skipped": skipped, "failed": failed})

            if output_dir:
                try:
                    save_drive_id_index(output_dir, id_index)
                except Exception:
                    pass
            self.log.emit(f"全部结束：成功 {success}，跳过 {skipped}，失败 {failed}。")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.done.emit()



class Card(QFrame):
    def __init__(self, title: str, object_name: str = "workCard"):
        super().__init__()
        self.setObjectName(object_name)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(14, 12, 14, 12)
        self.layout.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        self.layout.addWidget(t)


class PasteLinkDownloadPage(QWidget):
    def __init__(self, parent=None, credentials_supplier=None, token_path: str = ""):
        super().__init__(parent)
        self.credentials_supplier = credentials_supplier
        self.token_path = token_path or default_token_path()
        self.sheet_index: list[dict] = []  # from read_name_link_index
        self.tasks: list[MatchedTask] = []
        self.worker = None
        self.sheet_rows = {}
        self._pending_start_after_index = False
        self.build_ui()
        self.connect_signals()
        self.load_settings()

    def credentials_path(self) -> str:
        if callable(self.credentials_supplier):
            try:
                v = str(self.credentials_supplier() or "").strip()
                if v:
                    return v
            except Exception:
                pass
        preferred = os.path.join(app_base_dir(), "谷歌服务账号.json")
        if os.path.exists(preferred):
            return preferred
        return os.path.join(app_base_dir(), "credentials.json")

    def build_ui(self):
        self.setObjectName("pageFill")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        tip = QLabel(
            "粘贴链接后点「开始下载」：自动匹配表格链接列 → 下载 → 回填。\n"
            "支持暂停/继续、按 Drive 文件 ID 排重、文件夹断点续传（缺啥补啥）。凭据用顶部「全局设置」。"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        root.addWidget(tip)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        # ----- 左：设置 -----
        left_scroll = QScrollArea()
        left_scroll.setObjectName("pageFill")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(360)
        left_scroll.setMaximumWidth(440)
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

        cfg = Card("表格与列配置", "settingsCard")
        left.addWidget(cfg)
        g = cfg.layout

        self.spreadsheet_edit = QLineEdit()
        self.spreadsheet_edit.setPlaceholderText("Google 表格 ID")
        self.sheet_combo = QComboBox()
        self.name_col_edit = QLineEdit("A")
        self.link_col_edit = QLineEdit("C")
        self.output_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "Downloads", "粘贴链接下载"))

        def lab(text, w):
            c = QLabel(text)
            c.setObjectName("fieldLabel")
            g.addWidget(c)
            g.addWidget(w)

        lab("表格 ID", self.spreadsheet_edit)
        load_row = QHBoxLayout()
        self.load_sheet_btn = QPushButton("加载工作表 / 读取名称+链接列")
        self.load_sheet_btn.setObjectName("secondaryButton")
        load_row.addWidget(self.load_sheet_btn)
        g.addLayout(load_row)
        lab("工作表", self.sheet_combo)

        cols = QHBoxLayout()
        for text, w in (("名称列", self.name_col_edit), ("链接列", self.link_col_edit)):
            box = QVBoxLayout()
            c = QLabel(text)
            c.setObjectName("fieldLabel")
            box.addWidget(c)
            box.addWidget(w)
            cols.addLayout(box)
        g.addLayout(cols)

        lab("下载目录", self.output_edit)
        out_row = QHBoxLayout()
        pick_out = QPushButton("选择目录")
        pick_out.setObjectName("secondaryButton")
        pick_out.clicked.connect(self.choose_output)
        out_row.addWidget(pick_out)
        g.addLayout(out_row)

        self.folder_mode_combo = QComboBox()
        self.folder_mode_combo.addItems(["按人名", "按编号前缀", "按A列完整名称"])
        lab("本地文件夹命名", self.folder_mode_combo)

        # 回填配置
        bf = Card("回填列配置（均可改）", "settingsCard")
        left.addWidget(bf)
        bg = bf.layout
        self.count_col_edit = QLineEdit("D")
        self.status_col_edit = QLineEdit("E")
        self.person_col_edit = QLineEdit("F")
        self.date_col_edit = QLineEdit("G")
        self.person_name_edit = QLineEdit()
        self.person_name_edit.setPlaceholderText("下载人员姓名")

        for text, w, tip in (
            ("数量回填列（文件夹文件总数）", self.count_col_edit, "如下载文件夹内有 12 个文件则写 12"),
            ("状态回填列（正在下载 / 已下载完成）", self.status_col_edit, "开始写「正在下载」，结束写「已下载完成」"),
            ("下载人员回填列", self.person_col_edit, "写入下方填写的姓名"),
            ("完成日期回填列（YYYY-MM-DD）", self.date_col_edit, "例如 2026-07-27"),
        ):
            c = QLabel(text)
            c.setObjectName("fieldLabel")
            w.setToolTip(tip)
            bg.addWidget(c)
            bg.addWidget(w)

        c = QLabel("下载人员姓名")
        c.setObjectName("fieldLabel")
        bg.addWidget(c)
        bg.addWidget(self.person_name_edit)

        self.skip_check = QCheckBox("本地已有文件则跳过（断点续传）")
        self.skip_check.setChecked(True)
        self.skip_check.setToolTip("文件夹内已下过的文件会跳过，只补未下完的部分")
        bg.addWidget(self.skip_check)

        self.dedupe_id_check = QCheckBox("按 Drive 文件/文件夹 ID 排重（同一内容不重复下）")
        self.dedupe_id_check.setChecked(True)
        self.dedupe_id_check.setToolTip(
            "根据 Google Drive 文件 ID 判断是否已下载过；粘贴列表重复 ID 也只下一次。"
            "登记在下载目录的 .diy_drive_ids.json"
        )
        bg.addWidget(self.dedupe_id_check)

        save_row = QHBoxLayout()
        self.save_btn = QPushButton("保存设置")
        self.save_btn.setObjectName("primaryButton")
        save_row.addWidget(self.save_btn)
        bg.addLayout(save_row)

        self.index_label = QLabel("尚未读取表格索引")
        self.index_label.setObjectName("status")
        self.index_label.setWordWrap(True)
        left.addWidget(self.index_label)
        left.addStretch(1)

        # ----- 右：粘贴 + 任务 + 日志 -----
        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 1)

        paste_card = Card("粘贴要下载的链接")
        right.addWidget(paste_card, 0)
        self.paste_box = QTextEdit()
        self.paste_box.setObjectName("pasteTextBox")
        self.paste_box.setPlaceholderText(
            "每行一个链接，粘贴后直接点「开始下载」：\n"
            "https://drive.google.com/drive/folders/xxxx\n"
            "https://drive.google.com/file/d/yyyy/view\n\n"
            "开始时会自动按「链接列」匹配表格行号并回填。"
        )
        self.paste_box.setMinimumHeight(110)
        paste_card.layout.addWidget(self.paste_box)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("开始下载并回填")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.setToolTip("自动匹配 → 下载 → 回填；支持断点续传与 ID 排重")
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setObjectName("secondaryButton")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setToolTip("暂停当前下载；再点可继续")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("清空任务")
        self.clear_btn.setObjectName("ghostButton")
        btn_row.addWidget(self.start_btn, 1)
        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.clear_btn)
        paste_card.layout.addLayout(btn_row)

        task_card = Card("匹配结果 / 任务")
        right.addWidget(task_card, 2)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["表格行", "名称", "匹配", "粘贴链接", "状态", "数量", "备注"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 64)
        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(2, 56)
        self.table.setColumnWidth(3, 220)
        self.table.setColumnWidth(4, 100)
        self.table.setColumnWidth(5, 56)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        task_card.layout.addWidget(self.table)

        log_card = Card("日志")
        right.addWidget(log_card, 1)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        log_card.layout.addWidget(self.log_box)

        self.status_row = QLabel("先加载表格索引，再粘贴链接并匹配。")
        self.status_row.setObjectName("status")
        root.addWidget(self.status_row)

    def connect_signals(self):
        self.load_sheet_btn.clicked.connect(self.load_index)
        self.save_btn.clicked.connect(self.save_settings)
        self.start_btn.clicked.connect(self.start_download)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.stop_btn.clicked.connect(self.stop_download)
        self.clear_btn.clicked.connect(self.clear_tasks)

    def apply_page_fill(self, bg: str = "#0b1120"):
        def solid(w):
            if w is None:
                return
            c = QColor(bg)
            w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            w.setAutoFillBackground(True)
            pal = w.palette()
            pal.setColor(QPalette.ColorRole.Window, c)
            pal.setColor(QPalette.ColorRole.Base, c)
            w.setPalette(pal)

        solid(self)
        solid(getattr(self, "_left_scroll", None))
        if getattr(self, "_left_scroll", None):
            solid(self._left_scroll.viewport())
        solid(getattr(self, "_left_inner", None))

    def log(self, msg: str):
        self.log_box.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择下载目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def config_dict(self) -> dict:
        return {
            "spreadsheet_id": self.spreadsheet_edit.text().strip(),
            "sheet_name": self.sheet_combo.currentText().strip(),
            "name_col": self.name_col_edit.text().strip() or "A",
            "link_col": self.link_col_edit.text().strip() or "C",
            "count_col": self.count_col_edit.text().strip(),
            "status_col": self.status_col_edit.text().strip(),
            "person_col": self.person_col_edit.text().strip(),
            "date_col": self.date_col_edit.text().strip(),
            "person_name": self.person_name_edit.text().strip(),
            "output_dir": self.output_edit.text().strip(),
            "group_mode": self.folder_mode_combo.currentText(),
            "skip_existing": self.skip_check.isChecked(),
            "dedupe_by_id": self.dedupe_id_check.isChecked(),
        }

    def save_settings(self):
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(self.config_dict(), f, ensure_ascii=False, indent=2)
            self.log(f"设置已保存：{settings_path()}")
            self.status_row.setText("设置已保存")
        except Exception as exc:
            QMessageBox.warning(self, APP_SECTION, f"保存失败：{exc}")

    def load_settings(self):
        path = settings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        self.spreadsheet_edit.setText(data.get("spreadsheet_id", "") or "")
        sheet = data.get("sheet_name", "")
        if sheet:
            if self.sheet_combo.findText(sheet) < 0:
                self.sheet_combo.addItem(sheet)
            self.sheet_combo.setCurrentText(sheet)
        self.name_col_edit.setText(data.get("name_col", "A") or "A")
        self.link_col_edit.setText(data.get("link_col", "C") or "C")
        self.count_col_edit.setText(data.get("count_col", "D") or "D")
        self.status_col_edit.setText(data.get("status_col", "E") or "E")
        self.person_col_edit.setText(data.get("person_col", "F") or "F")
        self.date_col_edit.setText(data.get("date_col", "G") or "G")
        self.person_name_edit.setText(data.get("person_name", "") or "")
        if data.get("output_dir"):
            self.output_edit.setText(data["output_dir"])
        mode = data.get("group_mode", "按人名")
        if self.folder_mode_combo.findText(mode) >= 0:
            self.folder_mode_combo.setCurrentText(mode)
        self.skip_check.setChecked(bool(data.get("skip_existing", True)))
        if hasattr(self, "dedupe_id_check"):
            self.dedupe_id_check.setChecked(bool(data.get("dedupe_by_id", True)))

    def has_running(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def load_index(self):
        if self.has_running():
            QMessageBox.information(self, APP_SECTION, "任务进行中，请稍候。")
            return
        sid = self.spreadsheet_edit.text().strip()
        if not sid:
            QMessageBox.warning(self, APP_SECTION, "请填写表格 ID。")
            return
        self.status_row.setText("正在读取表格名称列 + 链接列…")
        self.load_sheet_btn.setEnabled(False)
        # 统一：先列工作表，再按当前/首选工作表读名称+链接索引
        worker = _SheetListThenIndexWorker(
            self.credentials_path(),
            self.token_path,
            sid,
            self.name_col_edit.text().strip() or "A",
            self.link_col_edit.text().strip() or "C",
            self.sheet_combo,
        )
        worker.log.connect(self.log)
        worker.failed.connect(self._on_fail)
        worker.loaded.connect(self._on_index_loaded)
        worker.finished.connect(lambda: self.load_sheet_btn.setEnabled(True))
        self.worker = worker
        worker.start()

    def _on_fail(self, msg):
        self._pending_start_after_index = False
        text = friendly_network_error(msg) if msg else "未知错误"
        # 多行说明写入日志时压缩成可读几行
        for line in str(text).splitlines():
            if line.strip():
                self.log(f"失败：{line.strip()}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(APP_SECTION)
        box.setText("连接 Google 失败")
        box.setInformativeText(str(text))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
        self.status_row.setText("网络/权限连接失败，请看日志说明")
        self.set_running(False)

    def _on_index_loaded(self, index_rows, sheets):
        self.sheet_index = list(index_rows or [])
        self.sheet_rows = {t: n for t, n in (sheets or [])}
        current = self.sheet_combo.currentText()
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        titles = [t for t, _ in (sheets or [])]
        self.sheet_combo.addItems(titles)
        if current and self.sheet_combo.findText(current) >= 0:
            self.sheet_combo.setCurrentText(current)
        elif titles:
            self.sheet_combo.setCurrentIndex(0)
        self.sheet_combo.blockSignals(False)

        # 若当前工作表与索引不一致（首次加载用了默认名），用当前选中再读一次
        chosen = self.sheet_combo.currentText().strip()
        if chosen and (not self.sheet_index or True):
            # index was built with worker's sheet_name; if we used list-then-index, it's fine
            pass

        with_links = sum(1 for r in self.sheet_index if r.get("url"))
        self.index_label.setText(
            f"索引就绪：工作表「{chosen}」名称列 {self.name_col_edit.text()} / "
            f"链接列 {self.link_col_edit.text()} → {len(self.sheet_index)} 行，{with_links} 条链接。"
        )
        self.status_row.setText(self.index_label.text())
        self.save_settings()
        # 用户点了开始下载但当时还没索引：加载完自动继续匹配并下载
        if self._pending_start_after_index:
            self._pending_start_after_index = False
            self.start_download()

    def match_pasted(self, silent: bool = False) -> int:
        """用粘贴框链接匹配表格链接列。返回匹配成功条数；失败时返回 -1。"""
        if not self.sheet_index:
            if not silent:
                QMessageBox.information(self, APP_SECTION, "请先点击「加载工作表 / 读取名称+链接列」。")
            return -1
        plain = self.paste_box.toPlainText()
        html = self.paste_box.toHtml()
        urls = extract_pasted_urls(plain, html)
        if not urls:
            if not silent:
                QMessageBox.information(self, APP_SECTION, "没有解析到链接，请粘贴 http/https 地址。")
            return -1

        # 建立 key -> sheet row
        key_map = {}
        for row in self.sheet_index:
            for k in row.get("keys") or drive_match_keys(row.get("url") or ""):
                if k and k not in key_map:
                    key_map[k] = row

        tasks = []
        hit = 0
        for url in urls:
            keys = drive_match_keys(url)
            found = None
            for k in keys:
                if k in key_map:
                    found = key_map[k]
                    break
            if found:
                hit += 1
                tasks.append(
                    MatchedTask(
                        paste_url=url,
                        row_number=int(found["row_number"]),
                        name=str(found.get("name") or ""),
                        sheet_url=str(found.get("url") or ""),
                        status="已匹配",
                        matched=True,
                        note=f"链到第 {found['row_number']} 行",
                    )
                )
            else:
                tasks.append(
                    MatchedTask(
                        paste_url=url,
                        status="未匹配",
                        matched=False,
                        note="链接列中找不到相同文件/文件夹",
                    )
                )

        self.tasks = tasks
        self.refresh_table()
        self.log(f"自动匹配：粘贴 {len(urls)} 条，成功 {hit} 条，未匹配 {len(urls) - hit} 条。")
        self.status_row.setText(f"匹配完成：{hit}/{len(urls)}")
        return hit

    def refresh_table(self):
        self.table.setRowCount(len(self.tasks))
        for i, t in enumerate(self.tasks):
            vals = [
                t.row_number if t.row_number else "",
                t.name,
                "是" if t.matched else "否",
                t.paste_url,
                t.status,
                t.file_count if t.file_count else "",
                t.note,
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem("" if v is None else str(v))
                if c == 2:
                    item.setForeground(QColor("#15803d" if t.matched else "#dc2626"))
                if c == 4:
                    if t.status == STATUS_DONE:
                        item.setForeground(QColor("#15803d"))
                    elif t.status == STATUS_DOWNLOADING:
                        item.setForeground(QColor("#38bdf8"))
                    elif t.status == STATUS_FAILED:
                        item.setForeground(QColor("#dc2626"))
                self.table.setItem(i, c, item)

    def on_task_update(self, index: int, fields: dict):
        if index < 0 or index >= len(self.tasks):
            return
        t = self.tasks[index]
        if "status" in fields:
            t.status = fields["status"]
        if "file_count" in fields:
            t.file_count = int(fields["file_count"] or 0)
        if "note" in fields:
            t.note = str(fields["note"])
        self.refresh_table()

    def clear_tasks(self):
        self.tasks = []
        self.refresh_table()
        self.paste_box.clear()
        self.status_row.setText("已清空任务")

    def start_download(self):
        if self.has_running():
            return
        cfg = self.config_dict()
        if not cfg["spreadsheet_id"]:
            QMessageBox.warning(self, APP_SECTION, "请填写表格 ID。")
            return
        if not cfg["output_dir"]:
            QMessageBox.warning(self, APP_SECTION, "请选择下载目录。")
            return

        # 无索引时先自动加载表格，再匹配；有粘贴内容时每次开始都重新匹配（避免旧任务）
        plain = self.paste_box.toPlainText().strip()
        if not plain and not extract_pasted_urls(self.paste_box.toPlainText(), self.paste_box.toHtml()):
            QMessageBox.information(self, APP_SECTION, "请先粘贴要下载的链接。")
            return

        if not self.sheet_index:
            self.log("尚未读取表格索引，先自动加载名称列 + 链接列…")
            self.status_row.setText("正在加载表格索引…")
            self._pending_start_after_index = True
            self.load_index()
            return

        hit = self.match_pasted(silent=False)
        if hit < 0:
            return
        matched = [t for t in self.tasks if t.matched]
        if not matched:
            QMessageBox.information(
                self,
                APP_SECTION,
                "粘贴的链接在表格「链接列」中没有匹配到。\n请确认链接列配置正确，且表中已有相同文件夹/文件链接。",
            )
            return
        if not cfg.get("sheet_name"):
            cfg["sheet_name"] = self.sheet_combo.currentText().strip()
        if not cfg["sheet_name"]:
            QMessageBox.warning(self, APP_SECTION, "请选择工作表。")
            return
        if not cfg.get("person_name") and cfg.get("person_col"):
            self.log("提示：未填下载人员姓名，人员列将写空。")

        self.save_settings()
        self.set_running(True)
        self.status_row.setText("下载中…")
        worker = PasteDownloadWorker(
            self.credentials_path(),
            self.token_path,
            cfg,
            self.tasks,
        )
        worker.log.connect(self.log)
        worker.failed.connect(self._on_fail)
        worker.progress.connect(lambda p: self.status_row.setText(
            f"成功 {p.get('success', 0)} / 跳过 {p.get('skipped', 0)} / 失败 {p.get('failed', 0)}"
        ))
        worker.task_update.connect(self.on_task_update)
        worker.paused_changed.connect(self._on_paused_changed)
        worker.done.connect(self._on_done)
        self.worker = worker
        worker.start()

    def toggle_pause(self):
        if not isinstance(self.worker, PasteDownloadWorker) or not self.worker.isRunning():
            return
        if self.worker.is_paused():
            self.worker.resume()
            self.log("已继续下载。")
            self.status_row.setText("继续下载中…")
        else:
            self.worker.pause()
            self.log("已暂停（可再点「继续」；停止后再次开始会断点续传）。")
            self.status_row.setText("已暂停")

    def _on_paused_changed(self, paused: bool):
        self.pause_btn.setText("继续" if paused else "暂停")

    def stop_download(self):
        if isinstance(self.worker, PasteDownloadWorker):
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.log("已请求停止…")
            self.status_row.setText("正在停止…")

    def set_running(self, running: bool):
        self.load_sheet_btn.setEnabled(not running)
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.pause_btn.setEnabled(running)
        if not running:
            self.pause_btn.setText("暂停")
        self.clear_btn.setEnabled(not running)

    def _on_done(self):
        self.set_running(False)
        if not self.status_row.text().startswith("成功"):
            self.status_row.setText("任务结束")
        self.log("下载线程结束。")

    def request_close(self) -> bool:
        if self.has_running():
            QMessageBox.information(self, APP_SECTION, "请先停止下载任务。")
            return False
        return True


class _SheetListThenIndexWorker(QThread):
    """首次：列出工作表后读取第一个（或当前）表的名称+链接索引。"""
    log = Signal(str)
    failed = Signal(str)
    loaded = Signal(list, list)

    def __init__(self, credentials_path, token_path, spreadsheet_id, name_col, link_col, sheet_combo):
        super().__init__()
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.spreadsheet_id = spreadsheet_id
        self.name_col = name_col
        self.link_col = link_col
        self.sheet_combo = sheet_combo

    def run(self):
        try:
            def _load():
                client = GoogleClient(self.credentials_path, self.token_path)
                sheets = [(i.title, i.row_count) for i in client.list_sheets(self.spreadsheet_id)]
                if not sheets:
                    raise RuntimeError("表格中没有工作表。")
                preferred = ""
                try:
                    preferred = self.sheet_combo.currentText().strip()
                except Exception:
                    pass
                titles = [t for t, _ in sheets]
                sheet_name = preferred if preferred in titles else titles[0]
                index = client.read_name_link_index(
                    self.spreadsheet_id, sheet_name, self.name_col, self.link_col
                )
                return sheets, sheet_name, index

            sheets, sheet_name, index = call_with_network_retry(
                _load, retries=3, delay=1.2, log=self.log.emit
            )
            self.log.emit(f"使用工作表「{sheet_name}」读取名称列 {self.name_col} / 链接列 {self.link_col}")
            with_links = sum(1 for r in index if r.get("url"))
            self.log.emit(f"索引完成：{len(index)} 行，{with_links} 条链接。")
            self.loaded.emit(index, sheets)
        except Exception as exc:
            self.failed.emit(friendly_network_error(exc))
