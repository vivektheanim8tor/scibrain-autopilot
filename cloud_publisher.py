"""
Sci.Brain - Cloud Daily Publisher
---------------------------------
Executes 100% autonomously in GitHub Actions.
Pulls today's scheduled science fact reel from Google Drive, publishes to:
- Instagram Reels (@sci.brain via Meta Graph API)
- Facebook Page Reels (Sci.Brain via Meta Graph API)
- YouTube Shorts (Sci.Brain via official YouTube Data API v3)
Updates and saves master queue.json back to Google Drive.
"""

import os
import sys
import io
import json
import time
import random
import requests
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive']
IST = timezone(timedelta(hours=5, minutes=30))

def log(msg):
    ts = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    print(f"[{ts} IST] {msg}")

def get_random_target_for_day(date_obj):
    # Organic slot: 75% evening curiosity peak (7:15 PM - 9:15 PM IST), 25% afternoon (1:00 PM - 2:30 PM IST)
    slot = random.choices(["evening", "afternoon"], weights=[0.75, 0.25])[0]
    if slot == "evening":
        hour = random.randint(19, 21)
        minute = random.randint(10, 45)
    else:
        hour = random.randint(13, 14)
        minute = random.randint(10, 40)
    second = random.randint(10, 50)
    return date_obj.replace(hour=hour, minute=minute, second=second, microsecond=0)

def get_drive_service():
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        creds_info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(creds_info, scopes=SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build('drive', 'v3', credentials=creds)
    elif os.path.exists("scibrain_token.json"):
        creds = Credentials.from_authorized_user_file("scibrain_token.json", scopes=SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build('drive', 'v3', credentials=creds)
    else:
        raise ValueError("GOOGLE_TOKEN_JSON not found in environment.")

def find_folder(service, name="SciBrain_Reels"):
    q = f"mimeType = 'application/vnd.google-apps.folder' and name = '{name}' and trashed = false"
    res = service.files().list(q=q, fields="files(id, name)").execute()
    files = res.get('files', [])
    if files:
        return files[0]['id']
    return None

def get_file_content(service, folder_id, filename):
    q = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    res = service.files().list(q=q, fields="files(id, name)").execute()
    files = res.get('files', [])
    if not files:
        return None, None
    file_id = files[0]['id']
    req = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    return file_id, fh.read()

def download_file_by_name(service, folder_id, filename, dest_path, file_id=None):
    clean_name = filename.replace('\\', '/').split('/')[-1]
    if not file_id:
        q = f"name = '{clean_name}' and trashed = false"
        res = service.files().list(q=q, fields="files(id, name)").execute()
        files = res.get('files', [])
        if not files:
            raise FileNotFoundError(f"File {clean_name} not found in Google Drive")
        file_id = files[0]['id']
        
    req = service.files().get_media(fileId=file_id)
    with io.FileIO(dest_path, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest_path

def save_queue_to_drive(service, queue_file_id, folder_id, queue_data):
    temp_file = "temp_scibrain_queue.json"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(queue_data, f, indent=2)
    media = MediaFileUpload(temp_file, mimetype='application/json', resumable=True)
    if queue_file_id:
        service.files().update(fileId=queue_file_id, media_body=media).execute()
    else:
        metadata = {'name': 'queue.json', 'parents': [folder_id]}
        service.files().create(body=metadata, media_body=media).execute()
    if os.path.exists(temp_file):
        try:
            os.remove(temp_file)
        except Exception:
            pass

# ----------------- Meta Graph API Publishing -----------------
def publish_to_facebook_page(meta_cfg, video_path, caption, thumb_path=None):
    page_id = meta_cfg["facebook_page_id"]
    token = meta_cfg["facebook_page_access_token"]
    file_size = os.path.getsize(video_path)

    log(f"Publishing to Facebook Page: {meta_cfg.get('facebook_page_name', 'Sci.Brain')} (ID: {page_id})...")
    start_url = f"https://graph.facebook.com/v21.0/{page_id}/video_reels"
    r_start = requests.post(start_url, json={"upload_phase": "start", "access_token": token})
    if r_start.status_code != 200:
        log(f"[ERROR] FB start failed: {r_start.text}")
        return False
    data = r_start.json()
    video_id = data.get("video_id")
    upload_url = data.get("upload_url")

    log(f"Uploading {file_size} bytes to Facebook...")
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream"
    }
    with open(video_path, "rb") as f:
        r_up = requests.post(upload_url, headers=headers, data=f)
    if r_up.status_code != 200:
        log(f"[ERROR] FB upload failed: {r_up.text}")
        return False

    log("Finishing and publishing Facebook Reel...")
    r_fin = requests.post(start_url, json={
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
        "description": caption,
        "access_token": token
    })
    if r_fin.status_code == 200 and r_fin.json().get("success"):
        post_id = r_fin.json().get("post_id", video_id)
        log(f"[SUCCESS] Facebook Reel published! Post ID: {post_id}")

        if thumb_path and os.path.exists(thumb_path):
            try:
                th_url = f"https://graph.facebook.com/v21.0/{video_id}/thumbnails"
                with open(thumb_path, "rb") as tf:
                    r_th = requests.post(th_url, params={"access_token": token, "is_preferred": "true"}, files={"source": tf})
                    if r_th.status_code == 200:
                        log("Custom thumbnail attached to Facebook Reel!")
            except Exception as e_th:
                log(f"[NOTE] FB thumbnail attach note: {e_th}")

        return True
    else:
        log(f"[ERROR] FB finish failed: {r_fin.text}")
        return False

def publish_to_instagram_reels(meta_cfg, video_path, caption, thumb_offset_ms=2000):
    ig_id = meta_cfg["instagram_account_id"]
    token = meta_cfg["facebook_page_access_token"]
    file_size = os.path.getsize(video_path)

    log(f"Publishing to Instagram Reels (@{meta_cfg.get('instagram_username', 'sci.brain')}) with thumb_offset={thumb_offset_ms}ms...")
    init_url = f"https://graph.facebook.com/v21.0/{ig_id}/media"
    r_init = requests.post(init_url, data={
        "upload_type": "resumable",
        "media_type": "REELS",
        "caption": caption,
        "thumb_offset": str(thumb_offset_ms),
        "access_token": token
    })
    if r_init.status_code != 200:
        log(f"[ERROR] IG init failed: {r_init.text}")
        return False
    init_data = r_init.json()
    container_id = init_data.get("id")
    upload_uri = init_data.get("uri")

    log(f"Uploading {file_size} bytes to rupload.facebook.com...")
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream"
    }
    with open(video_path, "rb") as f:
        r_up = requests.post(upload_uri, headers=headers, data=f)
    if r_up.status_code != 200:
        log(f"[ERROR] IG upload failed: {r_up.text}")
        return False

    log("Waiting for Instagram video processing...")
    status_url = f"https://graph.facebook.com/v21.0/{container_id}"
    ready = False
    for attempt in range(25):
        time.sleep(5)
        r_stat = requests.get(status_url, params={"fields": "status_code,status", "access_token": token})
        if r_stat.status_code == 200:
            s_code = r_stat.json().get("status_code")
            log(f"Status poll: {s_code} ({attempt+1}/25)")
            if s_code == "FINISHED":
                ready = True
                break
            elif s_code == "ERROR":
                log(f"[ERROR] IG processing error: {r_stat.text}")
                return False
        else:
            log(f"[WARNING] Status poll response: {r_stat.text}")

    if not ready:
        log("[ERROR] Timed out waiting for Instagram processing.")
        return False

    log("Publishing container to Instagram profile...")
    pub_url = f"https://graph.facebook.com/v21.0/{ig_id}/media_publish"
    r_pub = requests.post(pub_url, data={"creation_id": container_id, "access_token": token})
    if r_pub.status_code == 200 and "id" in r_pub.json():
        media_id = r_pub.json().get("id")
        log(f"[SUCCESS] Instagram Reel published! Media ID: {media_id}")
        return True
    else:
        log(f"[ERROR] IG publish failed: {r_pub.text}")
        return False

# ----------------- YouTube Data API v3 Publishing -----------------
def publish_to_youtube_shorts(creds, video_path, title, description, tags, thumb_path=None):
    try:
        log("Publishing to YouTube Shorts via official YouTube Data API v3...")
        youtube = build('youtube', 'v3', credentials=creds)
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": [t.strip() for t in tags.split(",") if t.strip()],
                "categoryId": "28" # Science & Technology
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False
            }
        }
        media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        res = None
        while res is None:
            status, res = req.next_chunk()
            if status:
                log(f"YouTube upload progress: {int(status.progress() * 100)}%")

        video_id = res.get("id")
        log(f"[SUCCESS] YouTube Short published! Video ID: {video_id}")

        if thumb_path and os.path.exists(thumb_path):
            try:
                thumb_media = MediaFileUpload(thumb_path, mimetype="image/png")
                youtube.thumbnails().set(videoId=video_id, media_body=thumb_media).execute()
                log("Custom thumbnail attached to YouTube Short!")
            except Exception as te:
                log(f"[NOTE] YouTube thumbnail note: {te}")

        return video_id
    except Exception as e:
        log(f"[ERROR] YouTube Shorts upload failed: {e}")
        return None

# ----------------- Main Automation Pipeline -----------------
def main():
    log("==========================================")
    log("Starting Sci.Brain Cloud Daily Publisher")
    log("==========================================")

    # 1. Connect to Drive
    service = get_drive_service()
    folder_id = find_folder(service, "SciBrain_Reels")
    if not folder_id:
        log("[ERROR] SciBrain_Reels folder not found on Drive.")
        sys.exit(1)
    log(f"Connected to Google Drive folder: {folder_id}")

    # 2. Load queue.json
    q_file_id, q_content = get_file_content(service, folder_id, "queue.json")
    if not q_content:
        log("[ERROR] queue.json not found in SciBrain_Reels.")
        sys.exit(1)

    queue = json.loads(q_content.decode("utf-8"))
    pending = [r for r in queue if r.get("status") == "pending"]
    if not pending:
        log("[NOTICE] All reels in queue are already posted. Nothing to publish.")
        return

    next_reel = pending[0]
    now_ist = datetime.now(IST)

    # 3. Schedule target check
    target_str = next_reel.get("scheduled_for")
    target_dt = None
    if target_str:
        try:
            target_dt = datetime.fromisoformat(target_str)
        except Exception:
            target_dt = None

    if not target_dt or target_dt.date() < now_ist.date():
        target_dt = get_random_target_for_day(now_ist)
        next_reel["scheduled_for"] = target_dt.isoformat()
        log(f"Generated new organic target for Reel #{next_reel['id']}: {target_dt.strftime('%I:%M:%S %p')} IST")
        save_queue_to_drive(service, q_file_id, folder_id, queue)

    if now_ist < target_dt:
        diff_mins = int((target_dt - now_ist).total_seconds() // 60)
        log(f"Today's upload (Reel #{next_reel['id']}) is organically scheduled for {target_dt.strftime('%I:%M:%S %p')} IST (~{diff_mins} mins remaining). Standing by.")
        return

    log(f"Scheduled target time ({target_dt.strftime('%I:%M:%S %p')} IST) reached! Publishing Day {next_reel['id']} - {next_reel['title']}...")

    # 4. Download video file from Drive
    raw_filename = next_reel.get("video_filename", "reel.mp4").replace('\\', '/').split('/')[-1]
    local_video_path = os.path.join(".", raw_filename)
    log(f"Downloading {raw_filename} from Google Drive...")
    download_file_by_name(service, folder_id, raw_filename, local_video_path, file_id=next_reel.get("video_file_id"))
    log(f"Downloaded {raw_filename} ({os.path.getsize(local_video_path) // (1024*1024)} MB)")

    # 5. Download custom thumbnail if available
    local_thumb_path = None
    if next_reel.get("thumb_filename"):
        thumb_name = next_reel["thumb_filename"].replace('\\', '/').split('/')[-1]
        local_thumb_path = os.path.join(".", thumb_name)
        try:
            download_file_by_name(service, folder_id, thumb_name, local_thumb_path, file_id=next_reel.get("thumb_file_id"))
            log(f"Downloaded custom thumbnail: {thumb_name}")
        except Exception as thumb_err:
            log(f"[WARNING] Could not download thumbnail {thumb_name}: {thumb_err}")
            local_thumb_path = None

    # 6. Captions & Titles
    caption = next_reel.get("caption", "")
    hook = next_reel.get("hook", next_reel.get("title", ""))
    
    yt_title = next_reel.get("title", hook)
    if "#Shorts" not in yt_title:
        yt_title = f"{yt_title} #Shorts"
    if len(yt_title) > 95:
        yt_title = yt_title[:95]
        
    yt_tags = "science, scibrain, human body, did you know, neuroscience, interesting facts, biology, brain facts, science facts, shorts"

    # Load Meta config
    meta_json = os.environ.get("META_CONFIG_JSON")
    meta_cfg = json.loads(meta_json) if meta_json else None
    if not meta_cfg and os.path.exists("scibrain_meta_config.json"):
        with open("scibrain_meta_config.json") as f:
            meta_cfg = json.load(f)

    # 7. Publish to Instagram Reels (Meta Graph API)
    ig_ok = False
    if meta_cfg and not next_reel.get("posted_at") and not next_reel.get("instagram_posted_at"):
        ig_ok = publish_to_instagram_reels(meta_cfg, local_video_path, caption, thumb_offset_ms=2000)
    else:
        log("Instagram Reel already published for this day.")

    # 8. Publish to Facebook Page Reels (Meta Graph API)
    fb_ok = False
    if meta_cfg and not next_reel.get("facebook_posted_at"):
        fb_ok = publish_to_facebook_page(meta_cfg, local_video_path, caption, thumb_path=local_thumb_path)
    else:
        log("Facebook Reel already published for this day.")

    # 9. Publish to YouTube Shorts (YouTube Data API v3)
    yt_id = None
    if not next_reel.get("youtube_posted_at"):
        try:
            token_json = os.environ.get("GOOGLE_TOKEN_JSON")
            if token_json:
                creds_info = json.loads(token_json)
                creds = Credentials.from_authorized_user_info(creds_info, scopes=SCOPES + ['https://www.googleapis.com/auth/youtube.upload'])
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                yt_id = publish_to_youtube_shorts(creds, local_video_path, yt_title, caption, yt_tags, local_thumb_path)
        except Exception as yt_err:
            log(f"[WARNING] YouTube publishing error: {yt_err}")
    else:
        log(f"YouTube Short already published for this day ({next_reel.get('youtube_posted_at')}).")

    # 10. Update queue
    now_iso = now_ist.isoformat()
    updated = False
    if ig_ok:
        next_reel["status"] = "posted"
        next_reel["posted_at"] = now_iso
        next_reel["instagram_posted_at"] = now_iso
        updated = True
    if fb_ok:
        next_reel["facebook_posted_at"] = now_iso
        updated = True
    if yt_id:
        next_reel["youtube_posted_at"] = now_iso
        updated = True

    if next_reel.get("posted_at") or (next_reel.get("facebook_posted_at") and next_reel.get("youtube_posted_at")):
        next_reel["status"] = "posted"

    if updated or next_reel.get("status") == "posted":
        # Pre-schedule tomorrow's organic target time
        tomorrow_ist = now_ist + timedelta(days=1)
        remaining = [r for r in queue if r.get("status") == "pending"]
        if remaining:
            tomorrow_target = get_random_target_for_day(tomorrow_ist)
            remaining[0]["scheduled_for"] = tomorrow_target.isoformat()
            log(f"Pre-scheduled tomorrow's Reel #{remaining[0]['id']} for: {tomorrow_target.strftime('%Y-%m-%d %I:%M:%S %p')} IST (organic randomized time)")

        log("Saving updated queue.json back to Google Drive...")
        save_queue_to_drive(service, q_file_id, folder_id, queue)
        log(f"[SUCCESS] Day {next_reel['id']} published successfully across platforms and cloud queue updated!")
    else:
        log("[ERROR] Publishing failed for pending platforms. Queue not updated.")
        sys.exit(1)

    # Clean up local temporary files
    if os.path.exists(local_video_path):
        os.remove(local_video_path)
    if local_thumb_path and os.path.exists(local_thumb_path):
        os.remove(local_thumb_path)

if __name__ == "__main__":
    main()
