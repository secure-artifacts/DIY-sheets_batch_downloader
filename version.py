"""应用版本与发布仓库信息。发版时同步修改 APP_VERSION。"""

APP_NAME = "DIY下载器"
APP_VERSION = "1.2.0"
GITHUB_OWNER = "secure-artifacts"
GITHUB_REPO = "DIY-sheets_batch_downloader"
GITHUB_REPO_SLUG = f"{GITHUB_OWNER}/{GITHUB_REPO}"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO_SLUG}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO_SLUG}/releases"
