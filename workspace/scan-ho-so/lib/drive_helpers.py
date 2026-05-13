"""Drive helpers with folder caching to minimize API calls."""
from __future__ import annotations
import io
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from .google_clients import drive

# In-process cache: key = (parent_id, folder_name) → folder_id
_FOLDER_CACHE: dict[tuple[str, str], str] = {}
_LIST_CACHE: dict[str, dict[str, str]] = {}  # parent_id → {filename: file_id}


def get_or_create_folder(name: str, parent_id: str, drive_id: str | None = None) -> str:
    """Return folder ID, create if missing. Cached per process."""
    key = (parent_id, name)
    if key in _FOLDER_CACHE:
        return _FOLDER_CACHE[key]

    q = f"name = '{name.replace(chr(39), chr(92)+chr(39))}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    list_kwargs = dict(q=q, fields="files(id, name)", pageSize=10)
    if drive_id:
        list_kwargs.update(corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True, supportsAllDrives=True)
    resp = drive().files().list(**list_kwargs).execute()
    files = resp.get("files", [])
    if files:
        fid = files[0]["id"]
    else:
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
        create_kwargs = dict(body=meta, fields="id")
        if drive_id:
            create_kwargs["supportsAllDrives"] = True
        fid = drive().files().create(**create_kwargs).execute()["id"]
    _FOLDER_CACHE[key] = fid
    return fid


def list_folder(parent_id: str, drive_id: str | None = None) -> dict[str, str]:
    """List all non-folder files in parent → {name: file_id}. Cached per process."""
    if parent_id in _LIST_CACHE:
        return _LIST_CACHE[parent_id]
    items: dict[str, str] = {}
    page_token = None
    while True:
        list_kwargs = dict(
            q=f"'{parent_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'",
            fields="nextPageToken, files(id, name)",
            pageSize=200,
        )
        if drive_id:
            list_kwargs.update(corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True, supportsAllDrives=True)
        if page_token:
            list_kwargs["pageToken"] = page_token
        resp = drive().files().list(**list_kwargs).execute()
        for f in resp.get("files", []):
            items[f["name"]] = f["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    _LIST_CACHE[parent_id] = items
    return items


def upload_file(local_path: str, dest_name: str, parent_id: str, drive_id: str | None = None, mime: str | None = None) -> dict:
    """Upload (or skip if same name exists). Returns {id, name, link, skipped}."""
    existing = list_folder(parent_id, drive_id)
    if dest_name in existing:
        fid = existing[dest_name]
        return {
            "id": fid,
            "name": dest_name,
            "link": f"https://drive.google.com/file/d/{fid}/view?usp=drivesdk",
            "skipped": True,
        }
    media = MediaFileUpload(local_path, mimetype=mime, resumable=False)
    body = {"name": dest_name, "parents": [parent_id]}
    create_kwargs = dict(body=body, media_body=media, fields="id, name, webViewLink")
    if drive_id:
        create_kwargs["supportsAllDrives"] = True
    f = drive().files().create(**create_kwargs).execute()
    # update cache
    _LIST_CACHE.setdefault(parent_id, {})[f["name"]] = f["id"]
    return {
        "id": f["id"],
        "name": f["name"],
        "link": f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view?usp=drivesdk",
        "skipped": False,
    }


def delete_file(file_id: str, drive_id: str | None = None) -> None:
    """Trash a file (recoverable)."""
    kwargs = dict(fileId=file_id, body={"trashed": True})
    if drive_id:
        kwargs["supportsAllDrives"] = True
    drive().files().update(**kwargs).execute()


def rename_file(file_id: str, new_name: str, drive_id: str | None = None) -> None:
    """Đổi tên một file/folder trên Drive (id giữ nguyên → link không đổi)."""
    kwargs = dict(fileId=file_id, body={"name": new_name})
    if drive_id:
        kwargs["supportsAllDrives"] = True
    drive().files().update(**kwargs).execute()


def move_file(file_id: str, new_parent_id: str, drive_id: str | None = None) -> None:
    """Move 1 file sang folder cha mới (Drive cho phép 1 file nhiều parent — ta xoá hết
    parent cũ + add parent mới ⇒ tương đương 'move'). File id giữ nguyên, link không đổi."""
    svc = drive()
    g_kwargs = dict(fileId=file_id, fields="parents")
    u_kwargs = dict(fileId=file_id, addParents=new_parent_id, fields="id,parents")
    if drive_id:
        g_kwargs["supportsAllDrives"] = True
        u_kwargs["supportsAllDrives"] = True
    cur = svc.files().get(**g_kwargs).execute()
    old_parents = cur.get("parents") or []
    if old_parents:
        u_kwargs["removeParents"] = ",".join(old_parents)
    svc.files().update(**u_kwargs).execute()


def download_file_text(file_id: str, drive_id: str | None = None, encoding: str = "utf-8") -> str:
    """Download a (small, text) file's bytes and decode → str. Read-only."""
    return download_file_bytes(file_id, drive_id).decode(encoding, errors="replace")


def download_file_bytes(file_id: str, drive_id: str | None = None) -> bytes:
    """Download a file's raw bytes (any type). Read-only."""
    kwargs = dict(fileId=file_id)
    if drive_id:
        kwargs["supportsAllDrives"] = True
    req = drive().files().get_media(**kwargs)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def replace_file(local_path: str, dest_name: str, parent_id: str, drive_id: str | None = None, mime: str | None = None) -> dict:
    """Like upload_file but OVERWRITE: if dest_name already exists in parent_id, trash the
    old one (and evict it from the list cache) so the fresh content actually lands.
    Returns {id, name, link, replaced}."""
    existing = list_folder(parent_id, drive_id)
    replaced = False
    if dest_name in existing:
        old_id = existing[dest_name]
        try:
            delete_file(old_id, drive_id)
        except Exception:
            pass
        # evict so upload_file below doesn't think it still exists and skip
        _LIST_CACHE.get(parent_id, {}).pop(dest_name, None)
        replaced = True
    up = upload_file(local_path, dest_name, parent_id, drive_id=drive_id, mime=mime)
    up["replaced"] = replaced
    return up


def find_file_by_name(name: str, parent_id: str, drive_id: str | None = None,
                      mime_type: str | None = None) -> str | None:
    """Return the id of the first non-trashed file named `name` in `parent_id` (optionally
    filtered by mimeType), or None. Not cached (we want a fresh look)."""
    q = (f"name = '{name.replace(chr(39), chr(92)+chr(39))}' and '{parent_id}' in parents "
         f"and trashed = false")
    if mime_type:
        q += f" and mimeType = '{mime_type}'"
    list_kwargs = dict(q=q, fields="files(id, name)", pageSize=10)
    if drive_id:
        list_kwargs.update(corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True, supportsAllDrives=True)
    resp = drive().files().list(**list_kwargs).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def copy_file(src_file_id: str, dest_name: str, parent_id: str, drive_id: str | None = None) -> str | None:
    """Copy an existing Drive file into parent_id under dest_name. Returns new id, or None on failure."""
    try:
        body = {"name": dest_name, "parents": [parent_id]}
        kwargs = dict(fileId=src_file_id, body=body, fields="id")
        if drive_id:
            kwargs["supportsAllDrives"] = True
        return drive().files().copy(**kwargs).execute()["id"]
    except Exception:
        return None


def find_or_create_report_sheet(case_folder_id: str, sheet_name: str, drive_id: str | None = None,
                                template_id: str | None = None) -> str:
    """Return the id of the case's report spreadsheet, creating it if missing.

    - If a spreadsheet named `sheet_name` already exists in `case_folder_id` → return it.
    - Else, if `template_id` is given, try copying the template into the folder (keeps formatting).
    - Else (or if the copy fails) → create a blank spreadsheet.
    Caller is responsible for (re)writing the values.
    """
    SHEET_MIME = "application/vnd.google-apps.spreadsheet"
    existing = find_file_by_name(sheet_name, case_folder_id, drive_id, mime_type=SHEET_MIME)
    if existing:
        return existing
    if template_id:
        new_id = copy_file(template_id, sheet_name, case_folder_id, drive_id)
        if new_id:
            return new_id
    body = {"name": sheet_name, "mimeType": SHEET_MIME, "parents": [case_folder_id]}
    kwargs = dict(body=body, fields="id")
    if drive_id:
        kwargs["supportsAllDrives"] = True
    return drive().files().create(**kwargs).execute()["id"]
