import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


APP_TITLE = "DIY下载器"
# drive 写权限用于「批量上传云端」；旧 token 仅有 drive.readonly 时会要求重新授权
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def default_token_path() -> str:
    """统一 token 路径，避免换目录/exe 后反复授权。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "DIYDownloader")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "token.json")


def is_transient_network_error(exc) -> bool:
    """是否像本机网络/防火墙导致的短暂失败（可重试）。"""
    msg = str(exc or "").lower()
    needles = (
        "connection aborted",
        "permission denied",
        "permissionerror",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection refused",
        "broken pipe",
        "ssl",
        "10054",
        "10060",
        "10061",
        "getaddrinfo",
        "name or service not known",
        "failed to resolve",
        "max retries exceeded",
        "remote end closed",
    )
    return any(n in msg for n in needles)


def friendly_network_error(exc) -> str:
    """把 urllib3/Windows 的难懂错误翻成可操作说明。"""
    raw = str(exc or "")
    low = raw.lower()
    if (
        "connection aborted" in low
        or "permission denied" in low
        or "permissionerror" in low
    ):
        token = default_token_path()
        return (
            "连接 Google 被中断（本机网络/防火墙/代理问题，一般不是表格列填错）。\n"
            f"技术信息：{raw}\n\n"
            "请依次尝试：\n"
            "1. 再点一次「加载工作表」或「开始下载」（已自动重试过仍失败再试）\n"
            "2. 浏览器能否打开 https://drive.google.com 与 https://sheets.google.com\n"
            "3. 暂时关闭 VPN/系统代理，或换手机热点试一次\n"
            "4. 杀毒/防火墙允许 DIY下载器（或 python.exe）访问网络\n"
            f"5. 仍不行：删除授权缓存后重新登录\n   {token}"
        )
    if is_transient_network_error(exc):
        return f"网络异常：{raw}\n请检查网络后重试。"
    return raw


def call_with_network_retry(fn, *, retries: int = 3, delay: float = 1.2, log=None):
    """对 Google API 调用做短暂网络重试。"""
    last_exc = None
    wait = delay
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if (not is_transient_network_error(exc)) or attempt >= retries:
                raise RuntimeError(friendly_network_error(exc)) from exc
            if log:
                try:
                    log(f"网络异常，{wait:.1f}s 后重试（{attempt}/{retries}）：{exc}")
                except Exception:
                    pass
            time.sleep(wait)
            wait = min(wait * 1.6, 8.0)
    raise RuntimeError(friendly_network_error(last_exc)) from last_exc


def token_has_required_scopes(token_path: str) -> bool:
    """检查 token 是否已具备表格 + Drive 写权限（授权一次即可复用）。"""
    if not os.path.exists(token_path):
        return False
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        token_scopes = data.get("scopes") or data.get("scope") or []
        if isinstance(token_scopes, str):
            token_scopes = token_scopes.split()
        have = set(token_scopes)
        has_sheets = "https://www.googleapis.com/auth/spreadsheets" in have
        # full drive 或 drive.file 均可上传
        has_drive = (
            "https://www.googleapis.com/auth/drive" in have
            or "https://www.googleapis.com/auth/drive.file" in have
        )
        return has_sheets and has_drive
    except Exception:
        return False


def require_google_libs():
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Google API 依赖，请先运行：\n"
            "pip install -r requirements_google.txt"
        ) from exc

    return (
        GoogleAuthRequest,
        service_account,
        Credentials,
        InstalledAppFlow,
        build,
        MediaIoBaseDownload,
        MediaFileUpload,
    )


def sanitize_path_part(value: str) -> str:
    text = str(value or "未命名")
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:150] or "未命名"


def parse_title(title: str, fallback_number: int, group_mode: str):
    text = str(title or "").strip()
    number = str(fallback_number)
    group_name = text or "未命名"
    prefix = text or "未命名"

    # 示例：12-ZB-张三-祷告男-李四-不要划走这个视频...-46211-FF-2026-7-9.mp4
    # 兼容早期示例：485-ZB-王五 老太太1
    match = re.match(r"^(\d+)\s*-\s*([^-]+?)\s*-\s*([^-]+)", text)
    if match:
        number = match.group(1)
        prefix = f"{match.group(1)}-{sanitize_path_part(match.group(2))}-{sanitize_path_part(match.group(3))}"
        group_name = match.group(3)
    else:
        number_match = re.match(r"^(\d+)", text)
        if number_match:
            number = number_match.group(1)
        first_part = text.split()[0] if text.split() else ""
        prefix = first_part or text or "未命名"
        group_name = first_part or text or "未命名"

    if group_mode in ("prefix", "按编号前缀"):
        group_name = prefix
    elif group_mode in ("full", "按A列完整名称"):
        group_name = text or "未命名"

    return sanitize_path_part(group_name), sanitize_path_part(number)


def split_keywords(keyword: str):
    text = str(keyword or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[，,、;\n]+", text) if part.strip()]


def looks_like_person_name(segment: str) -> bool:
    text = str(segment or "").strip()
    if not text:
        return False
    # 这些词通常是角色、性别、描述，不当作第二个人名。
    descriptor_words = [
        "男", "女", "牧师", "老师", "祷告", "测试", "简单", "丸子头", "老太太",
        "祝福", "主啊", "视频", "片段", "旁白", "中文", "英文",
    ]
    if any(word in text for word in descriptor_words):
        return False
    # 张三、李四、王五这类短中文片段更像人名。
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", text))


def match_keyword(title: str, url: str, keyword: str):
    haystack = f"{title or ''} {url or ''}".lower()
    title_text = str(title or "")
    for raw_key in split_keywords(keyword):
        key = raw_key.strip()
        key_lower = key.lower()
        if not key_lower:
            continue

        if "-" in key:
            if key_lower in haystack:
                return key
            continue

        if key_lower not in haystack:
            continue

        # 如果只筛“张三”，则排除“张三-李四-女3”这种后面紧跟另一个人名的组合。
        combo = re.search(re.escape(key) + r"\s*-\s*([^-—\s]+)", title_text)
        if combo and looks_like_person_name(combo.group(1)):
            continue

        return key
    return ""


def column_to_number(col_input: str) -> int:
    text = str(col_input or "").strip().upper()
    if not text:
        raise ValueError("列不能为空")
    if text.isdigit():
        return int(text)

    total = 0
    for char in text:
        if char < "A" or char > "Z":
            raise ValueError(f"无效列名：{col_input}")
        total = total * 26 + (ord(char) - 64)
    return total


def number_to_column(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def quote_sheet_name(name: str) -> str:
    return "'" + str(name).replace("'", "''") + "'"


def extract_drive_file_info(url: str):
    text = str(url or "").strip()
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    resource_key = query.get("resourcekey", [""])[0]

    # 文件夹链接交给 extract_drive_folder_id，避免把 folder id 当文件
    if extract_drive_folder_id(text):
        return "", resource_key

    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", text)
    if match:
        return match.group(1), resource_key

    file_id = query.get("id", [""])[0]
    if file_id:
        return file_id, resource_key

    return "", resource_key


def extract_drive_folder_id(url: str) -> str:
    """从 Google Drive 文件夹链接解析 folder id。"""
    text = str(url or "").strip()
    if not text:
        return ""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", text, re.I)
    if match:
        return match.group(1)
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    if re.search(r"folderview|drive\.google\.com", text, re.I):
        # folderview?id=... 或 open?id=... 且路径像文件夹
        fid = (query.get("id") or [""])[0]
        if fid and re.search(r"folder", text, re.I):
            return fid
        # open?id= 可能是文件也可能是文件夹，仅 folderview 可靠
        if "folderview" in text.lower() and fid:
            return fid
    return ""


def is_drive_folder_url(url: str) -> bool:
    return bool(extract_drive_folder_id(url))


def drive_match_keys(url: str) -> set:
    """用于粘贴链接与表格链接列匹配的归一化键（文件/文件夹 ID + 去参 URL）。"""
    text = str(url or "").strip()
    keys = set()
    if not text:
        return keys
    base = text.split("#")[0].split("?")[0].rstrip("/").lower()
    if base:
        keys.add(base)
    keys.add(text.lower())
    folder_id = extract_drive_folder_id(text)
    if folder_id:
        keys.add(f"folder:{folder_id}")
        keys.add(folder_id.lower())
    file_id, _ = extract_drive_file_info(text)
    if file_id:
        keys.add(f"file:{file_id}")
        keys.add(file_id.lower())
    return {k for k in keys if k}


def extension_from_name(name: str) -> str:
    _, ext = os.path.splitext(os.path.basename(str(name or "")))
    if ext and 2 <= len(ext) <= 10:
        return ext
    return ""


def filename_from_content_disposition(header: str) -> str:
    if not header:
        return ""

    match = re.search(r"filename\*=UTF-8''([^;]+)", header, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))

    match = re.search(r'filename="?([^";]+)"?', header, re.I)
    if match:
        return match.group(1).strip()

    return ""


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path

    root, ext = os.path.splitext(path)
    index = 2
    while True:
        candidate = f"{root}_{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
        index += 1


def find_url_in_text(value) -> str:
    text = str(value or "")
    if not text:
        return ""

    text = (
        text.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("\\u003d", "=")
        .replace("\\u0026", "&")
        .replace("\\/", "/")
    )

    patterns = [
        r"https?://[^\s\"'<>\\]+",
        r'"url"\s*:\s*"([^"]+)"',
        r'"uri"\s*:\s*"([^"]+)"',
        r"url=([^\"'&<>\\\s]+)",
        r"q=(https?%3A%2F%2F[^\"'&<>\\\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            raw = match.group(1) if match.lastindex else match.group(0)
            try:
                raw = unquote(raw)
            except Exception:
                pass
            return raw
    return ""


def find_url_in_json(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return find_url_in_text(value)
    if isinstance(value, dict):
        for key in ("hyperlink", "uri", "url"):
            url = find_url_in_json(value.get(key))
            if url:
                return url
        for child in value.values():
            url = find_url_in_json(child)
            if url:
                return url
    if isinstance(value, list):
        for child in value:
            url = find_url_in_json(child)
            if url:
                return url
    return ""


def get_cell_text(cell: dict) -> str:
    if not cell:
        return ""
    if cell.get("formattedValue"):
        return str(cell["formattedValue"])

    effective = cell.get("effectiveValue") or {}
    user_entered = cell.get("userEnteredValue") or {}
    for source in (effective, user_entered):
        for key in ("stringValue", "numberValue", "boolValue", "formulaValue"):
            if key in source:
                return str(source[key])
    return ""


def get_cell_link(cell: dict) -> str:
    if not cell:
        return ""

    if cell.get("hyperlink"):
        return str(cell["hyperlink"]).strip()

    for run in cell.get("textFormatRuns", []) or []:
        link = (((run.get("format") or {}).get("link") or {}).get("uri") or "").strip()
        if link:
            return link

    formula = ((cell.get("userEnteredValue") or {}).get("formulaValue") or "").strip()
    if formula:
        match = re.search(r'HYPERLINK\(\s*"([^"]+)"', formula, re.I)
        if match:
            return match.group(1)

    return find_url_in_json(cell)


@dataclass
class SheetInfo:
    title: str
    row_count: int


@dataclass
class DownloadItem:
    row_number: int
    title: str
    url: str
    source_name: str
    match_name: str
    group_name: str
    file_number: str


class GoogleClient:
    def __init__(self, credentials_path: str, token_path: str):
        (
            GoogleAuthRequest,
            service_account,
            Credentials,
            InstalledAppFlow,
            build,
            MediaIoBaseDownload,
            MediaFileUpload,
        ) = require_google_libs()
        self.MediaIoBaseDownload = MediaIoBaseDownload
        self.MediaFileUpload = MediaFileUpload

        if not os.path.exists(credentials_path):
            raise RuntimeError("找不到凭据 JSON 文件。")

        with open(credentials_path, "r", encoding="utf-8") as f:
            credentials_info = json.load(f)

        if credentials_info.get("type") == "service_account":
            self.creds = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=SCOPES,
            )
            self.account_label = credentials_info.get("client_email", "service_account")
        else:
            creds = None
            # 优先复用已授权 token（刷新即可，不弹浏览器）
            if os.path.exists(token_path):
                try:
                    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
                except Exception:
                    creds = None

            need_browser = False
            if not creds:
                need_browser = True
            elif not creds.valid:
                if creds.refresh_token:
                    try:
                        creds.refresh(GoogleAuthRequest())
                    except Exception:
                        need_browser = True
                else:
                    need_browser = True
            elif not token_has_required_scopes(token_path):
                # 旧 token 权限不够（例如只有 drive.readonly）才重新授权一次
                need_browser = True

            if need_browser:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)

            # 持久化，下次直接用
            try:
                os.makedirs(os.path.dirname(os.path.abspath(token_path)) or ".", exist_ok=True)
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
            except Exception:
                pass

            self.creds = creds
            self.account_label = "OAuth 用户"

        self.sheets = build("sheets", "v4", credentials=self.creds)
        self.drive = build("drive", "v3", credentials=self.creds)
        self._sheet_id_cache = {}

    def list_sheets(self, spreadsheet_id: str):
        result = self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,gridProperties(rowCount)))",
        ).execute()
        infos = []
        for sheet in result.get("sheets", []):
            props = sheet.get("properties", {})
            grid = props.get("gridProperties", {})
            infos.append(SheetInfo(props.get("title", ""), int(grid.get("rowCount", 0) or 0)))
        return infos

    def sheet_end_row(self, spreadsheet_id: str, sheet_name: str, default: int = 5000) -> int:
        for info in self.list_sheets(spreadsheet_id):
            if info.title == sheet_name and info.row_count:
                return max(int(info.row_count), 2)
        return default

    def read_name_row_map(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        name_col: str = "A",
        start_row: int = 2,
    ) -> dict:
        """只读名称列 → {小写名字: 行号}，供粘贴下载回填定位，不遍历链接列。"""
        end_row = self.sheet_end_row(spreadsheet_id, sheet_name)
        col = number_to_column(column_to_number(name_col or "A"))
        range_name = f"{quote_sheet_name(sheet_name)}!{col}{start_row}:{col}{end_row}"
        result = self.sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        ).execute()
        values = result.get("values") or []
        mapping = {}
        for offset, row in enumerate(values):
            text = str(row[0] if row else "").strip()
            if not text:
                continue
            mapping[text.lower()] = start_row + offset
        return mapping

    def read_items(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        start_row: int,
        end_row: int,
        name_col: str,
        link_col: str,
        group_mode: str,
        keyword: str = "",
    ):
        name_col_num = column_to_number(name_col)
        link_col_num = column_to_number(link_col)
        max_col = max(name_col_num, link_col_num)
        range_name = f"{quote_sheet_name(sheet_name)}!A{start_row}:{number_to_column(max_col)}{end_row}"

        result = self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            ranges=[range_name],
            includeGridData=True,
        ).execute()

        grid_data = (((result.get("sheets") or [{}])[0].get("data") or [{}])[0])
        rows = grid_data.get("rowData") or []
        keyword = str(keyword or "").strip().lower()
        items = []

        for offset, row in enumerate(rows):
            row_number = start_row + offset
            values = row.get("values") or []
            name_cell = values[name_col_num - 1] if name_col_num - 1 < len(values) else {}
            link_cell = values[link_col_num - 1] if link_col_num - 1 < len(values) else {}

            title = get_cell_text(name_cell).strip()
            source_name = get_cell_text(link_cell).strip()
            url = get_cell_link(link_cell).strip() or find_url_in_text(source_name)
            matched_name = match_keyword(title, url, keyword)
            if keyword and not matched_name:
                continue

            group_name, file_number = parse_title(title, row_number, group_mode)
            items.append(DownloadItem(row_number, title, url, source_name, matched_name, group_name, file_number))
        return items

    def get_drive_file_name(self, file_id: str):
        metadata = self.drive.files().get(
            fileId=file_id,
            fields="name,mimeType",
            supportsAllDrives=True,
        ).execute()
        return metadata.get("name") or "file"

    def get_drive_folder_name(self, folder_id: str) -> str:
        metadata = self.drive.files().get(
            fileId=folder_id,
            fields="name,mimeType",
            supportsAllDrives=True,
        ).execute()
        return metadata.get("name") or "folder"

    def list_folder_files(self, folder_id: str, recursive: bool = True) -> list:
        """列出文件夹内可下载文件（跳过 Google 在线文档），返回 [{id,name,mimeType,relative_path}, ...]。"""
        folder_mime = "application/vnd.google-apps.folder"
        shortcut_mime = "application/vnd.google-apps.shortcut"
        out = []

        def walk(fid: str, rel_prefix: str = ""):
            page_token = None
            while True:
                result = self.drive.files().list(
                    q=f"'{fid}' in parents and trashed=false",
                    spaces="drive",
                    fields="nextPageToken, files(id,name,mimeType,shortcutDetails)",
                    pageSize=1000,
                    pageToken=page_token or "",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    orderBy="folder,name",
                ).execute()
                for f in result.get("files") or []:
                    mime = f.get("mimeType") or ""
                    name = f.get("name") or "file"
                    safe_name = sanitize_path_part(name)
                    if mime == folder_mime:
                        if recursive:
                            walk(f["id"], f"{rel_prefix}{safe_name}/")
                        continue
                    if mime == shortcut_mime:
                        details = f.get("shortcutDetails") or {}
                        target_id = details.get("targetId") or ""
                        target_mime = details.get("targetMimeType") or ""
                        if not target_id or target_mime == folder_mime:
                            continue
                        if str(target_mime).startswith("application/vnd.google-apps."):
                            continue
                        out.append({
                            "id": target_id,
                            "name": name,
                            "mimeType": target_mime,
                            "relative_path": f"{rel_prefix}{safe_name}",
                        })
                        continue
                    # 在线文档/表格等无法用 get_media 直接下，跳过
                    if str(mime).startswith("application/vnd.google-apps."):
                        continue
                    out.append({
                        "id": f["id"],
                        "name": name,
                        "mimeType": mime,
                        "relative_path": f"{rel_prefix}{safe_name}",
                    })
                page_token = result.get("nextPageToken")
                if not page_token:
                    break

        walk(str(folder_id or "").strip())
        return out

    def download_drive_file(
        self,
        file_id: str,
        target_path: str,
        stop_event: threading.Event,
        pause_event: threading.Event | None = None,
    ):
        """下载到 target_path；先写 .part，成功后再替换，避免半成品被当成已完成。
        pause_event 置位时在分块之间等待（暂停），stop_event 中止。
        """
        parent = os.path.dirname(target_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        part_path = target_path + ".part"
        # 半成品重下：Drive get_media 不便字节续传，删掉 part 重新拉完整文件
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        request = self.drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        try:
            with open(part_path, "wb") as f:
                downloader = self.MediaIoBaseDownload(f, request, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    if stop_event is not None and stop_event.is_set():
                        raise RuntimeError("任务已停止")
                    if pause_event is not None:
                        while pause_event.is_set() and not (stop_event and stop_event.is_set()):
                            time.sleep(0.2)
                        if stop_event is not None and stop_event.is_set():
                            raise RuntimeError("任务已停止")
                    _, done = downloader.next_chunk()
            # 原子替换
            if os.path.exists(target_path):
                try:
                    os.remove(target_path)
                except Exception:
                    pass
            os.replace(part_path, target_path)
        except Exception:
            # 保留 .part 便于识别未完成；完整失败时清理
            if stop_event is not None and stop_event.is_set():
                pass
            raise
        return target_path

    def write_success_name(self, spreadsheet_id: str, sheet_name: str, row_number: int, column: str, value: str):
        if not column:
            return
        cell = f"{quote_sheet_name(sheet_name)}!{number_to_column(column_to_number(column))}{row_number}"
        self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell,
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()

    def write_row_fields(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        row_number: int,
        fields: dict,
    ):
        """一次写入同行多列：fields = { 'E': '正在下载', 'F': 12, ... }"""
        data = []
        for col, value in (fields or {}).items():
            col = str(col or "").strip()
            if not col:
                continue
            data.append({
                "range": f"{quote_sheet_name(sheet_name)}!{number_to_column(column_to_number(col))}{int(row_number)}",
                "values": [[value]],
            })
        if not data:
            return
        self.sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()

    def read_name_link_index(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        name_col: str = "A",
        link_col: str = "C",
        start_row: int = 2,
    ) -> list:
        """读取名称列 + 链接列，返回 [{row_number, name, url, keys}, ...]。"""
        end_row = self.sheet_end_row(spreadsheet_id, sheet_name)
        name_col_num = column_to_number(name_col or "A")
        link_col_num = column_to_number(link_col or "C")
        max_col = max(name_col_num, link_col_num)
        range_name = f"{quote_sheet_name(sheet_name)}!A{start_row}:{number_to_column(max_col)}{end_row}"

        result = self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            ranges=[range_name],
            includeGridData=True,
        ).execute()
        grid_data = (((result.get("sheets") or [{}])[0].get("data") or [{}])[0])
        rows = grid_data.get("rowData") or []
        out = []
        for offset, row in enumerate(rows):
            row_number = start_row + offset
            values = row.get("values") or []
            name_cell = values[name_col_num - 1] if name_col_num - 1 < len(values) else {}
            link_cell = values[link_col_num - 1] if link_col_num - 1 < len(values) else {}
            name = get_cell_text(name_cell).strip()
            source = get_cell_text(link_cell).strip()
            url = (get_cell_link(link_cell) or find_url_in_text(source) or "").strip()
            if not url and not name:
                continue
            out.append({
                "row_number": row_number,
                "name": name,
                "url": url,
                "keys": sorted(drive_match_keys(url)),
            })
        return out

    # ---------- Drive 上传 / 入库表 ----------

    def ensure_sheet(self, spreadsheet_id: str, sheet_name: str, headers=None):
        infos = self.list_sheets(spreadsheet_id)
        titles = {info.title for info in infos}
        if sheet_name in titles:
            return
        body = {"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]}
        self.sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
        if headers:
            self.sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{quote_sheet_name(sheet_name)}!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()

    def read_category_rows(self, spreadsheet_id: str, sheet_name: str = "分类目录"):
        """读取分类目录：A-D 列，空白一级/二级向下继承。返回 [[c1,c2,c3,c4], ...]"""
        result = self.sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A2:D",
        ).execute()
        values = result.get("values") or []
        current_c1 = ""
        current_c2 = ""
        rows = []
        for raw in values:
            padded = list(raw) + [""] * (4 - len(raw))
            c1 = str(padded[0] or "").strip()
            c2 = str(padded[1] or "").strip()
            c3 = str(padded[2] or "").strip()
            c4 = str(padded[3] or "").strip()
            if c1:
                current_c1 = c1
            else:
                c1 = current_c1
            if c2:
                current_c2 = c2
            else:
                c2 = current_c2
            if not c1 and not c2 and not c3 and not c4:
                continue
            rows.append([c1, c2, c3, c4])
        return rows

    def find_child_folder(self, parent_id: str, name: str):
        safe = str(name or "").replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and trashed=false and "
            f"mimeType='application/vnd.google-apps.folder' and name='{safe}'"
        )
        result = self.drive.files().list(
            q=query,
            spaces="drive",
            fields="files(id,name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = result.get("files") or []
        return files[0]["id"] if files else ""

    def create_folder(self, parent_id: str, name: str):
        meta = {
            "name": str(name or "未命名"),
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = self.drive.files().create(
            body=meta,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        return created["id"]

    def get_or_create_folder_path(self, root_folder_id: str, path_parts):
        current = str(root_folder_id or "").strip()
        if not current:
            raise RuntimeError("父文件夹 ID 为空")
        for part in path_parts:
            name = str(part or "").strip()
            if not name:
                continue
            found = self.find_child_folder(current, name)
            if found:
                current = found
            else:
                current = self.create_folder(current, name)
        meta = self.drive.files().get(
            fileId=current,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        return meta

    def list_folder_existing_files(self, folder_id: str) -> dict:
        """单次 API 查询返回文件夹内所有非文件夹文件名 -> {file_name: {id, webViewLink, name}}"""
        query = (
            f"'{folder_id}' in parents and trashed=false and "
            f"mimeType!='application/vnd.google-apps.folder'"
        )
        page_token = None
        existing = {}
        while True:
            result = self.drive.files().list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id,name,webViewLink)",
                pageSize=1000,
                pageToken=page_token or "",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in result.get("files") or []:
                if f.get("name"):
                    existing[f["name"]] = f
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return existing

    def find_file_in_folder(self, folder_id: str, file_name: str):
        safe = str(file_name or "").replace("'", "\\'")
        query = (
            f"'{folder_id}' in parents and trashed=false and "
            f"mimeType!='application/vnd.google-apps.folder' and name='{safe}'"
        )
        result = self.drive.files().list(
            q=query,
            spaces="drive",
            fields="files(id,name,webViewLink)",
            pageSize=5,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = result.get("files") or []
        return files[0] if files else None

    def upload_local_file(self, local_path: str, folder_id: str, file_name: str = "", mime_type: str = "", existing_map: dict | None = None):
        if not os.path.isfile(local_path):
            raise RuntimeError(f"本地文件不存在：{local_path}")
        name = file_name or os.path.basename(local_path)
        if existing_map is not None and name in existing_map:
            return existing_map[name]
        elif existing_map is None:
            existing = self.find_file_in_folder(folder_id, name)
            if existing:
                return existing
        body = {"name": name, "parents": [folder_id]}
        media = self.MediaFileUpload(
            local_path,
            mimetype=mime_type or "application/octet-stream",
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )
        created = self.drive.files().create(
            body=body,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        if existing_map is not None:
            existing_map[name] = created
        return created

    def get_sheet_id(self, spreadsheet_id: str, sheet_name: str) -> int:
        key = f"{spreadsheet_id}:{sheet_name}"
        if key in getattr(self, "_sheet_id_cache", {}):
            return self._sheet_id_cache[key]
        meta = self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        ).execute()
        sheet_id = None
        for sh in meta.get("sheets") or []:
            props = sh.get("properties") or {}
            if props.get("title") == sheet_name:
                sheet_id = props.get("sheetId")
                break
        if sheet_id is None:
            raise RuntimeError(f"找不到工作表：{sheet_name}")
        if not hasattr(self, "_sheet_id_cache"):
            self._sheet_id_cache = {}
        self._sheet_id_cache[key] = sheet_id
        return sheet_id

    def insert_inbound_row(self, spreadsheet_id: str, sheet_name: str, row_values, insert_before_row: int = 7):
        """在指定行上方插入一行并写入入库数据。"""
        self.ensure_sheet(spreadsheet_id, sheet_name)
        sheet_id = self.get_sheet_id(spreadsheet_id, sheet_name)

        index = max(0, int(insert_before_row) - 1)
        self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [{
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": index,
                            "endIndex": index + 1,
                        },
                        "inheritFromBefore": False,
                    }
                }]
            },
        ).execute()
        cell = f"{quote_sheet_name(sheet_name)}!A{insert_before_row}"
        self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell,
            valueInputOption="USER_ENTERED",
            body={"values": [list(row_values)]},
        ).execute()

    def append_upload_log(self, spreadsheet_id: str, sheet_name: str, level: str, message: str):
        self.ensure_sheet(spreadsheet_id, sheet_name, headers=["时间戳", "日志级别", "详细信息"])
        # 若只有表头或空，直接 append
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A:C",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[ts, level, message]]},
        ).execute()


class PublicDownloader:
    def prepare_name(self, url: str):
        base = os.path.basename(urlparse(url).path)
        return sanitize_path_part(base) if base else "file.jpg"

    def download(self, url: str, target_path: str):
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 batch-downloader"})
        with urlopen(request, timeout=60) as response:
            data = response.read()
            remote_name = filename_from_content_disposition(response.headers.get("Content-Disposition", ""))

        if remote_name:
            folder = os.path.dirname(target_path)
            target_path = os.path.join(folder, sanitize_path_part(remote_name))

        if not extension_from_name(target_path):
            target_path += ".jpg"

        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "wb") as f:
            f.write(data)
        return target_path


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1040x760")
        self.minsize(960, 700)
        self.configure(bg="#eef2f7")

        self.log_queue = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.google_client = None
        self.sheet_infos = {}

        base_dir = os.path.dirname(os.path.abspath(__file__))
        default_service_account = os.path.join(base_dir, "谷歌服务账号.json")
        default_credentials = default_service_account if os.path.exists(default_service_account) else os.path.join(base_dir, "credentials.json")
        self.credentials_path = tk.StringVar(value=default_credentials)
        self.token_path = tk.StringVar(value=os.path.join(base_dir, "token.json"))
        self.output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "批量下载"))
        self.spreadsheet_id = tk.StringVar()
        self.sheet_name = tk.StringVar()
        self.name_col = tk.StringVar(value="A")
        self.link_col = tk.StringVar(value="P")
        self.start_row = tk.IntVar(value=2)
        self.end_row = tk.IntVar(value=100)
        self.scan_all = tk.BooleanVar(value=True)
        self.skip_existing = tk.BooleanVar(value=True)
        self.keyword = tk.StringVar()
        self.group_mode = tk.StringVar(value="person")

        self._setup_style()
        self._build_ui()
        self.after(100, self._drain_log_queue)

    def _setup_style(self):
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.style.configure("Card.TFrame", background="#ffffff", relief="flat")
        self.style.configure("App.TLabel", background="#ffffff", foreground="#1f2937")
        self.style.configure("Muted.TLabel", background="#ffffff", foreground="#64748b")
        self.style.configure("Title.TLabel", background="#eef2f7", foreground="#0f172a", font=("Microsoft YaHei UI", 20, "bold"))
        self.style.configure("Primary.TButton", padding=(14, 8), font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("Danger.TButton", padding=(14, 8), font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("TEntry", padding=6)
        self.style.configure("TCombobox", padding=6)

    def card(self, parent, title=None):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=16)
        if title:
            ttk.Label(frame, text=title, style="App.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 10))
        return frame

    def _build_ui(self):
        root = ttk.Frame(self, padding=18)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(root, text="DIY下载器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(root, text="从 Google 表格读取 P 列真实链接，按 A 列名称分文件夹，文件使用源文件名保存。", foreground="#64748b", background="#eef2f7").pack(anchor="w", pady=(4, 14))

        top = ttk.Frame(root)
        top.pack(fill=tk.X)
        left = self.card(top, "连接与目录")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        right = self.card(top, "读取规则")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.form_row(left, "凭据 JSON", self.credentials_path, button=("选择", self.choose_credentials))
        self.form_row(left, "本地下载目录", self.output_dir, button=("选择", self.choose_output_dir))
        self.form_row(left, "表格 ID", self.spreadsheet_id, button=("加载 Sheet", self.load_sheets))

        grid = ttk.Frame(right, style="Card.TFrame")
        grid.pack(fill=tk.X)
        self.small_entry(grid, "Sheet", self.sheet_name, 0, combo=True)
        self.small_entry(grid, "名称列", self.name_col, 1)
        self.small_entry(grid, "链接列", self.link_col, 2)
        self.small_entry(grid, "起始行", self.start_row, 3, spin=True)
        self.small_entry(grid, "结束行", self.end_row, 4, spin=True)

        opts = ttk.Frame(right, style="Card.TFrame")
        opts.pack(fill=tk.X, pady=(12, 0))
        ttk.Checkbutton(opts, text="扫描整个 Sheet", variable=self.scan_all).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(opts, text="已下载过则跳过", variable=self.skip_existing).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Label(opts, text="包含名称", style="App.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(opts, textvariable=self.keyword, width=18).pack(side=tk.LEFT)

        mode = ttk.Frame(right, style="Card.TFrame")
        mode.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(mode, text="文件夹命名", style="App.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Combobox(
            mode,
            textvariable=self.group_mode,
            width=22,
            state="readonly",
            values=["person", "prefix", "full"],
        ).pack(side=tk.LEFT)
        ttk.Label(mode, text="person=张三；prefix=12-ZB-张三；full=整行 A 列", style="Muted.TLabel").pack(side=tk.LEFT, padx=10)

        actions = ttk.Frame(root, padding=(0, 14, 0, 8))
        actions.pack(fill=tk.X)
        self.preview_button = ttk.Button(actions, text="预览读取", command=self.preview_items, style="Primary.TButton")
        self.preview_button.pack(side=tk.LEFT)
        self.start_button = ttk.Button(actions, text="开始下载", command=self.start_download, style="Primary.TButton")
        self.start_button.pack(side=tk.LEFT, padx=8)
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop_download, style="Danger.TButton", state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="清空日志", command=lambda: self.log.delete("1.0", tk.END)).pack(side=tk.LEFT, padx=8)

        bottom = ttk.Frame(root)
        bottom.pack(fill=tk.BOTH, expand=True)
        preview_card = self.card(bottom, "预览")
        preview_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        log_card = self.card(bottom, "日志")
        log_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.preview = tk.Text(preview_card, height=16, wrap="none", bg="#f8fafc", fg="#0f172a", relief="flat", padx=10, pady=10)
        self.preview.pack(fill=tk.BOTH, expand=True)
        self.log = tk.Text(log_card, height=16, wrap="word", bg="#111827", fg="#86efac", insertbackground="#86efac", relief="flat", padx=10, pady=10)
        self.log.pack(fill=tk.BOTH, expand=True)

    def form_row(self, parent, label, variable, button=None):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill=tk.X, pady=5)
        ttk.Label(row, text=label, style="App.TLabel", width=12).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=variable).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        if button:
            ttk.Button(row, text=button[0], command=button[1]).pack(side=tk.LEFT)

    def small_entry(self, parent, label, variable, column, combo=False, spin=False):
        box = ttk.Frame(parent, style="Card.TFrame")
        box.grid(row=0, column=column, sticky="ew", padx=(0, 8))
        parent.columnconfigure(column, weight=1)
        ttk.Label(box, text=label, style="App.TLabel").pack(anchor="w")
        if combo:
            self.sheet_combo = ttk.Combobox(box, textvariable=variable, state="readonly", width=18)
            self.sheet_combo.pack(fill=tk.X, pady=(4, 0))
        elif spin:
            ttk.Spinbox(box, from_=1, to=999999, textvariable=variable, width=8).pack(fill=tk.X, pady=(4, 0))
        else:
            ttk.Entry(box, textvariable=variable, width=8).pack(fill=tk.X, pady=(4, 0))

    def choose_credentials(self):
        path = filedialog.askopenfilename(title="选择凭据 JSON", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.credentials_path.set(path)
            self.google_client = None

    def choose_output_dir(self):
        path = filedialog.askdirectory(initialdir=self.output_dir.get() or os.path.expanduser("~"))
        if path:
            self.output_dir.set(path)

    def get_google_client(self):
        if not self.google_client:
            self.google_client = GoogleClient(self.credentials_path.get(), self.token_path.get())
            self.log_queue.put(f"已使用凭据：{self.google_client.account_label}")
        return self.google_client

    def load_sheets(self):
        def worker():
            try:
                infos = self.get_google_client().list_sheets(self.spreadsheet_id.get().strip())
                self.log_queue.put(("sheets", infos))
                self.log_queue.put(f"已加载 {len(infos)} 个 sheet。")
            except Exception as exc:
                self.log_queue.put(f"加载 Sheet 失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def current_end_row(self):
        if self.scan_all.get():
            info = self.sheet_infos.get(self.sheet_name.get())
            if info and info.row_count:
                return info.row_count
        return int(self.end_row.get())

    def read_items_from_sheet(self):
        return self.get_google_client().read_items(
            spreadsheet_id=self.spreadsheet_id.get().strip(),
            sheet_name=self.sheet_name.get().strip(),
            start_row=int(self.start_row.get()),
            end_row=self.current_end_row(),
            name_col=self.name_col.get().strip(),
            link_col=self.link_col.get().strip(),
            group_mode=self.group_mode.get(),
            keyword=self.keyword.get().strip(),
        )

    def preview_items(self):
        def worker():
            try:
                items = self.read_items_from_sheet()
                self.log_queue.put(("preview", items[:200]))
                link_count = sum(1 for item in items if item.url.startswith("http"))
                self.log_queue.put(f"预览完成：匹配 {len(items)} 行，其中 {link_count} 行有链接。")
            except Exception as exc:
                self.log_queue.put(f"预览失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def start_download(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "任务正在运行。")
            return
        output_dir = self.output_dir.get().strip()
        if not output_dir:
            messagebox.showwarning(APP_TITLE, "请先选择下载目录。")
            return

        self.stop_event.clear()
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.worker = threading.Thread(target=self.run_downloads, args=(output_dir,), daemon=True)
        self.worker.start()

    def stop_download(self):
        self.stop_event.set()
        self.log_queue.put("正在停止，当前文件处理完后会退出...")

    def build_target_path(self, output_dir, item, source_name):
        safe_source_name = sanitize_path_part(source_name or "file.jpg")
        if not extension_from_name(safe_source_name):
            safe_source_name += ".jpg"
        return os.path.join(output_dir, item.group_name, safe_source_name)

    def run_downloads(self, output_dir):
        try:
            client = self.get_google_client()
            public_downloader = PublicDownloader()
            items = self.read_items_from_sheet()
            self.log_queue.put(f"准备下载 {len(items)} 行。")

            success = skipped = failed = 0
            for item in items:
                if self.stop_event.is_set():
                    self.log_queue.put("任务已停止。")
                    break

                if not item.url or not item.url.startswith("http"):
                    skipped += 1
                    self.log_queue.put(f"第 {item.row_number} 行跳过：没有链接")
                    continue
                if re.search(r"drive\.google\.com/(?:drive/(?:u/\d+/)?folders|folderview)", item.url, re.I):
                    skipped += 1
                    self.log_queue.put(f"第 {item.row_number} 行跳过：文件夹链接")
                    continue

                try:
                    file_id, _ = extract_drive_file_info(item.url)
                    if file_id:
                        source_name = client.get_drive_file_name(file_id)
                        target_path = self.build_target_path(output_dir, item, source_name)
                        if self.skip_existing.get() and os.path.exists(target_path):
                            skipped += 1
                            self.log_queue.put(f"已存在，跳过：{target_path}")
                            continue
                        saved_path = client.download_drive_file(file_id, unique_path(target_path), self.stop_event)
                    else:
                        source_name = public_downloader.prepare_name(item.url)
                        target_path = self.build_target_path(output_dir, item, source_name)
                        if self.skip_existing.get() and os.path.exists(target_path):
                            skipped += 1
                            self.log_queue.put(f"已存在，跳过：{target_path}")
                            continue
                        saved_path = public_downloader.download(item.url, unique_path(target_path))

                    success += 1
                    self.log_queue.put(f"成功：{saved_path}")
                    time.sleep(0.1)
                except Exception as exc:
                    if self.stop_event.is_set():
                        self.log_queue.put("任务已停止。")
                        break
                    failed += 1
                    self.log_queue.put(f"第 {item.row_number} 行失败：{exc}")

            self.log_queue.put(f"完成：成功 {success}，跳过 {skipped}，失败 {failed}。")
        except Exception as exc:
            self.log_queue.put(f"任务失败：{exc}")
        finally:
            self.log_queue.put("__DONE__")

    def _drain_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                if message == "__DONE__":
                    self.start_button.config(state=tk.NORMAL)
                    self.stop_button.config(state=tk.DISABLED)
                elif isinstance(message, tuple) and message[0] == "sheets":
                    infos = message[1]
                    self.sheet_infos = {info.title: info for info in infos}
                    titles = [info.title for info in infos]
                    self.sheet_combo["values"] = titles
                    if titles and not self.sheet_name.get():
                        self.sheet_name.set(titles[0])
                        self.end_row.set(self.sheet_infos[titles[0]].row_count or self.end_row.get())
                elif isinstance(message, tuple) and message[0] == "preview":
                    self.preview.delete("1.0", tk.END)
                    self.preview.insert(tk.END, "行号\t文件夹\tA列名称\t链接\n")
                    for item in message[1]:
                        self.preview.insert(tk.END, f"{item.row_number}\t{item.group_name}\t{item.title}\t{item.url}\n")
                else:
                    now = time.strftime("%H:%M:%S")
                    self.log.insert(tk.END, f"[{now}] {message}\n")
                    self.log.see(tk.END)
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)


if __name__ == "__main__":
    App().mainloop()
