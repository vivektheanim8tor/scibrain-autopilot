"""
Sci.Brain - Cloud Daily Publisher
---------------------------------
Executes 100% autonomously in GitHub Actions.
Pulls today's scheduled science fact kit from Google Drive and publishes to:
- Instagram (@sci.brain via Meta Graph API: Reels for video shorts, Carousel containers for multi-slide carousels)
- Facebook Page (Sci.Brain via Meta Graph API: Video Reels for shorts, Multi-photo feed posts for carousels)
- YouTube Shorts (Sci.Brain via official YouTube Data API v3 for video shorts; skipped for carousels)
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

# ----------------- Meta Graph API: Video Reels -----------------
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

# ----------------- Meta Graph API: Carousels -----------------
def publish_to_facebook_carousel(meta_cfg, slide_paths, caption, pinned_comment=None):
    """
    Publishes a multi-photo carousel post to Facebook Page.
    1. Uploads each photo as published=false to obtain photo FBIDs.
    2. Queries each photo to retrieve its Meta CDN URL (for Instagram carousel ingestion).
    3. Creates multi-photo feed post attached_media=[{"media_fbid": id}, ...].
    4. Posts pinned conversation-starter comment.
    Returns: (post_id, cdn_urls)
    """
    page_id = meta_cfg["facebook_page_id"]
    token = meta_cfg["facebook_page_access_token"]
    photo_ids = []
    cdn_urls = []

    log(f"Uploading {len(slide_paths)} carousel slides to Facebook Page...")
    upload_url = f"https://graph.facebook.com/v21.0/{page_id}/photos"

    for idx, spath in enumerate(slide_paths):
        log(f"Uploading slide {idx+1}/{len(slide_paths)}: {os.path.basename(spath)}...")
        with open(spath, "rb") as sf:
            r = requests.post(
                upload_url,
                data={"published": "false", "access_token": token},
                files={"source": sf}
            )
        if r.status_code != 200:
            log(f"[ERROR] FB photo upload failed for slide {idx+1}: {r.text}")
            return None, []
        pid = r.json().get("id")
        photo_ids.append(pid)

        # Retrieve CDN URL for Instagram ingestion
        r_img = requests.get(
            f"https://graph.facebook.com/v21.0/{pid}",
            params={"fields": "images", "access_token": token}
        )
        if r_img.status_code == 200 and "images" in r_img.json():
            imgs = r_img.json()["images"]
            cdn_url = imgs[0]["source"]
            cdn_urls.append(cdn_url)
        else:
            log(f"[WARNING] Could not fetch CDN URL for photo {pid}: {r_img.text}")

    # Create multi-photo feed post
    log("Creating Facebook Page multi-photo carousel post...")
    feed_url = f"https://graph.facebook.com/v21.0/{page_id}/feed"
    attached = [{"media_fbid": pid} for pid in photo_ids]
    r_post = requests.post(
        feed_url,
        json={
            "message": caption,
            "attached_media": attached,
            "access_token": token
        }
    )
    if r_post.status_code == 200 and "id" in r_post.json():
        post_id = r_post.json()["id"]
        log(f"[SUCCESS] Facebook multi-photo carousel published! Post ID: {post_id}")

        # Post pinned comment if available
        if pinned_comment:
            try:
                c_url = f"https://graph.facebook.com/v21.0/{post_id}/comments"
                r_c = requests.post(c_url, data={"message": pinned_comment, "access_token": token})
                if r_c.status_code == 200:
                    log("Pinned comment posted to Facebook carousel post!")
            except Exception as e_c:
                log(f"[NOTE] FB pinned comment note: {e_c}")

        return post_id, cdn_urls
    else:
        log(f"[ERROR] Facebook feed post creation failed: {r_post.text}")
        return None, cdn_urls

def publish_to_instagram_carousel(meta_cfg, cdn_urls, caption, pinned_comment=None):
    """
    Publishes a multi-slide carousel to Instagram:
    1. Creates child image item containers with is_carousel_item=true.
    2. Creates parent carousel container with media_type=CAROUSEL and children=[ids].
    3. Publishes container via media_publish.
    4. Posts pinned conversation-starter comment.
    """
    ig_id = meta_cfg["instagram_account_id"]
    token = meta_cfg["facebook_page_access_token"]
    child_container_ids = []

    log(f"Creating {len(cdn_urls)} Instagram carousel item containers...")
    item_url = f"https://graph.facebook.com/v21.0/{ig_id}/media"

    for idx, curl in enumerate(cdn_urls):
        r_item = requests.post(
            item_url,
            data={
                "image_url": curl,
                "is_carousel_item": "true",
                "access_token": token
            }
        )
        if r_item.status_code != 200:
            log(f"[ERROR] IG carousel child {idx+1} creation failed: {r_item.text}")
            return False
        child_id = r_item.json().get("id")
        child_container_ids.append(child_id)

    log(f"Creating Instagram carousel parent container for {len(child_container_ids)} slides...")
    r_parent = requests.post(
        item_url,
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_container_ids),
            "caption": caption,
            "access_token": token
        }
    )
    if r_parent.status_code != 200:
        log(f"[ERROR] IG carousel parent container creation failed: {r_parent.text}")
        return False
    carousel_container_id = r_parent.json().get("id")

    # Small sleep to let Meta finish bundling the container
    time.sleep(10)

    log("Publishing carousel container to Instagram profile...")
    pub_url = f"https://graph.facebook.com/v21.0/{ig_id}/media_publish"
    r_pub = requests.post(pub_url, data={"creation_id": carousel_container_id, "access_token": token})
    if r_pub.status_code == 200 and "id" in r_pub.json():
        media_id = r_pub.json().get("id")
        log(f"[SUCCESS] Instagram Carousel published! Media ID: {media_id}")

        if pinned_comment:
            try:
                c_url = f"https://graph.facebook.com/v21.0/{media_id}/comments"
                r_c = requests.post(c_url, data={"message": pinned_comment, "access_token": token})
                if r_c.status_code == 200:
                    log("Pinned comment posted to Instagram carousel!")
            except Exception as e_c:
                log(f"[NOTE] IG pinned comment note: {e_c}")

        return True
    else:
        log(f"[ERROR] IG carousel publish failed: {r_pub.text}")
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
        log("[NOTICE] All items in queue are already posted. Nothing to publish.")
        return

    next_item = pending[0]
    now_ist = datetime.now(IST)

    # 3. Schedule target check
    target_str = next_item.get("scheduled_for")
    target_dt = None
    if target_str:
        try:
            target_dt = datetime.fromisoformat(target_str)
        except Exception:
            target_dt = None

    if not target_dt or target_dt.date() < now_ist.date():
        target_dt = get_random_target_for_day(now_ist)
        next_item["scheduled_for"] = target_dt.isoformat()
        log(f"Generated new organic target for Item #{next_item['id']}: {target_dt.strftime('%I:%M:%S %p')} IST")
        save_queue_to_drive(service, q_file_id, folder_id, queue)

    if now_ist < target_dt:
        diff_mins = int((target_dt - now_ist).total_seconds() // 60)
        log(f"Today's upload (Day #{next_item['id']} - {next_item['title']}) is organically scheduled for {target_dt.strftime('%I:%M:%S %p')} IST (~{diff_mins} mins remaining). Standing by.")
        return

    content_type = next_item.get("content_type", "10s_short")
    log(f"Scheduled target time ({target_dt.strftime('%I:%M:%S %p')} IST) reached! Publishing Day {next_item['id']} [{content_type.upper()}] - {next_item['title']}...")

    # Load Meta config
    meta_json = os.environ.get("META_CONFIG_JSON")
    meta_cfg = json.loads(meta_json) if meta_json else None
    if not meta_cfg and os.path.exists("scibrain_meta_config.json"):
        with open("scibrain_meta_config.json") as f:
            meta_cfg = json.load(f)

    caption = next_item.get("caption", "")
    pinned_comment = next_item.get("pinned_comment", "")

    # ==================== CASE A: MULTI-SLIDE CAROUSEL ====================
    if content_type == "carousel":
        slides = next_item.get("slides", [])
        if not slides:
            log(f"[ERROR] Carousel item {next_item['id']} has no slides configured.")
            sys.exit(1)

        log(f"Downloading {len(slides)} slides for carousel from Google Drive...")
        downloaded_slides = []
        for s in slides:
            s_name = s["name"]
            local_s_path = os.path.join(".", s_name)
            download_file_by_name(service, folder_id, s_name, local_s_path, file_id=s.get("file_id"))
            downloaded_slides.append(local_s_path)
            log(f"  - Downloaded {s_name} ({os.path.getsize(local_s_path) // 1024} KB)")

        # 1. Publish to Facebook Page multi-photo post and obtain Meta CDN URLs
        fb_post_id, cdn_urls = None, []
        if meta_cfg and not next_item.get("facebook_posted_at"):
            fb_post_id, cdn_urls = publish_to_facebook_carousel(meta_cfg, downloaded_slides, caption, pinned_comment=pinned_comment)
        else:
            log("Facebook Carousel already published for this day.")

        # 2. Publish to Instagram Carousel using Meta CDN URLs
        ig_ok = False
        if meta_cfg and not next_item.get("instagram_posted_at"):
            if cdn_urls:
                ig_ok = publish_to_instagram_carousel(meta_cfg, cdn_urls, caption, pinned_comment=pinned_comment)
            else:
                log("[ERROR] Cannot publish Instagram Carousel: no CDN URLs retrieved from Facebook.")
        else:
            log("Instagram Carousel already published for this day.")

        # YouTube does not support image carousels
        next_item["youtube_posted_at"] = "not_applicable"

        # Update queue state
        now_iso = now_ist.isoformat()
        updated = False
        if ig_ok:
            next_item["instagram_posted_at"] = now_iso
            updated = True
        if fb_post_id:
            next_item["facebook_posted_at"] = now_iso
            updated = True

        if next_item.get("instagram_posted_at") or next_item.get("facebook_posted_at"):
            next_item["status"] = "posted"
            next_item["posted_at"] = now_iso

            tomorrow_ist = now_ist + timedelta(days=1)
            remaining = [r for r in queue if r.get("status") == "pending"]
            if remaining:
                tomorrow_target = get_random_target_for_day(tomorrow_ist)
                remaining[0]["scheduled_for"] = tomorrow_target.isoformat()
                log(f"Pre-scheduled tomorrow's Day #{remaining[0]['id']} for: {tomorrow_target.strftime('%Y-%m-%d %I:%M:%S %p')} IST")

            log("Saving updated queue.json back to Google Drive...")
            save_queue_to_drive(service, q_file_id, folder_id, queue)
            log(f"[SUCCESS] Day {next_item['id']} Carousel published successfully across Meta and cloud queue updated!")
        else:
            log("[ERROR] Carousel publishing failed. Queue not updated.")
            sys.exit(1)

        # Cleanup local slide files
        for sp in downloaded_slides:
            if os.path.exists(sp):
                try:
                    os.remove(sp)
                except Exception:
                    pass

    # ==================== CASE B: VIDEO SHORTS (10s or 20-30s) ====================
    else:
        # Download video file from Drive
        raw_filename = next_item.get("video_filename", "reel.mp4").replace('\\', '/').split('/')[-1]
        local_video_path = os.path.join(".", raw_filename)
        log(f"Downloading {raw_filename} from Google Drive...")
        download_file_by_name(service, folder_id, raw_filename, local_video_path, file_id=next_item.get("video_file_id"))
        log(f"Downloaded {raw_filename} ({os.path.getsize(local_video_path) // (1024*1024)} MB)")

        # Download custom thumbnail if available
        local_thumb_path = None
        if next_item.get("thumb_filename"):
            thumb_name = next_item["thumb_filename"].replace('\\', '/').split('/')[-1]
            local_thumb_path = os.path.join(".", thumb_name)
            try:
                download_file_by_name(service, folder_id, thumb_name, local_thumb_path, file_id=next_item.get("thumb_file_id"))
                log(f"Downloaded custom thumbnail: {thumb_name}")
            except Exception as thumb_err:
                log(f"[WARNING] Could not download thumbnail {thumb_name}: {thumb_err}")
                local_thumb_path = None

        hook = next_item.get("hook", next_item.get("title", ""))
        yt_title = next_item.get("title", hook)
        if "#Shorts" not in yt_title:
            yt_title = f"{yt_title} #Shorts"
        if len(yt_title) > 95:
            yt_title = yt_title[:95]
            
        ig_caption = next_item.get("instagram_caption", caption)
        yt_desc = next_item.get("youtube_description", caption)
        yt_tags_list = next_item.get("youtube_tags")
        if yt_tags_list:
            yt_tags = ", ".join(yt_tags_list)
        else:
            yt_tags = "science, scibrain, human body, did you know, neuroscience, interesting facts, biology, brain facts, science facts, shorts"

        # Publish to Instagram Reels (Meta Graph API)
        ig_ok = False
        if meta_cfg and not next_item.get("posted_at") and not next_item.get("instagram_posted_at"):
            ig_ok = publish_to_instagram_reels(meta_cfg, local_video_path, ig_caption, thumb_offset_ms=2000)
        else:
            log("Instagram Reel already published for this day.")

        # Publish to Facebook Page Reels (Meta Graph API)
        fb_ok = False
        if meta_cfg and not next_item.get("facebook_posted_at"):
            fb_ok = publish_to_facebook_page(meta_cfg, local_video_path, ig_caption, thumb_path=local_thumb_path)
        else:
            log("Facebook Reel already published for this day.")

        # Publish to YouTube Shorts (YouTube Data API v3)
        yt_id = None
        if not next_item.get("youtube_posted_at"):
            try:
                yt_token_json = os.environ.get("YOUTUBE_TOKEN_JSON") or os.environ.get("GOOGLE_TOKEN_JSON")
                if yt_token_json:
                    creds_info = json.loads(yt_token_json)
                    creds = Credentials.from_authorized_user_info(creds_info, scopes=['https://www.googleapis.com/auth/youtube.upload'])
                    if creds and creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                    yt_id = publish_to_youtube_shorts(creds, local_video_path, yt_title, yt_desc, yt_tags, local_thumb_path)
            except Exception as yt_err:
                log(f"[WARNING] YouTube publishing error: {yt_err}")
        else:
            log(f"YouTube Short already published for this day ({next_item.get('youtube_posted_at')}).")

        # Update queue
        now_iso = now_ist.isoformat()
        updated = False
        if ig_ok:
            next_item["status"] = "posted"
            next_item["posted_at"] = now_iso
            next_item["instagram_posted_at"] = now_iso
            updated = True
        if fb_ok:
            next_item["facebook_posted_at"] = now_iso
            updated = True
        if yt_id:
            next_item["youtube_posted_at"] = now_iso
            updated = True

        if next_item.get("posted_at") or (next_item.get("facebook_posted_at") and next_item.get("youtube_posted_at")):
            next_item["status"] = "posted"

        if updated or next_item.get("status") == "posted":
            tomorrow_ist = now_ist + timedelta(days=1)
            remaining = [r for r in queue if r.get("status") == "pending"]
            if remaining:
                tomorrow_target = get_random_target_for_day(tomorrow_ist)
                remaining[0]["scheduled_for"] = tomorrow_target.isoformat()
                log(f"Pre-scheduled tomorrow's Day #{remaining[0]['id']} for: {tomorrow_target.strftime('%Y-%m-%d %I:%M:%S %p')} IST")

            log("Saving updated queue.json back to Google Drive...")
            save_queue_to_drive(service, q_file_id, folder_id, queue)
            log(f"[SUCCESS] Day {next_item['id']} published successfully across platforms and cloud queue updated!")
        else:
            log("[ERROR] Publishing failed for pending platforms. Queue not updated.")
            sys.exit(1)

        # Cleanup local video files
        if os.path.exists(local_video_path):
            os.remove(local_video_path)
        if local_thumb_path and os.path.exists(local_thumb_path):
            os.remove(local_thumb_path)

if __name__ == "__main__":
    main()
