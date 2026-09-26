import os
import re
import sys
import json
import time
import shutil
import asyncio
import traceback
import subprocess
from datetime import datetime
from dotenv import load_dotenv

import pymupdf
import pyrogram.utils

# --- PATCH PYROGRAM 64-BIT CHANNEL ID BUG ---
def patched_get_peer_type(peer_id: int) -> str:
    if peer_id < 0:
        if str(peer_id).startswith("-100"):
            return "channel"
        return "chat"
    return "user"

pyrogram.utils.get_peer_type = patched_get_peer_type
# ---------------------------------------------

from pyrogram import Client
from pyrogram.enums import ChatType, ChatMemberStatus
from pyrogram.errors import FloodWait, ChatWriteForbidden, UserNotParticipant
from pyrogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Message
)

load_dotenv()

# --- Strict Environment Variable Validation ---
_app_id_str = os.getenv("APP_ID")
API_HASH = os.getenv("API_HASH")
SOURCE_CHAT_ENV = os.getenv("SOURCE_CHAT")
DEST_CHAT_ENV = os.getenv("DEST_CHAT")
SCAN_LIMIT_ENV = os.getenv("SCAN_LIMIT")

if not all([_app_id_str, API_HASH, SOURCE_CHAT_ENV, DEST_CHAT_ENV]):
    print("[x] FATAL ERROR: Missing required environment variables.")
    print("    Check your .env file and ensure APP_ID, API_HASH, SOURCE_CHAT, and DEST_CHAT are set.")
    sys.exit(1)

try:
    API_ID = int(_app_id_str)
except ValueError:
    print(f"[x] FATAL ERROR: APP_ID must be a number. You provided: '{_app_id_str}'")
    sys.exit(1)

app = Client("my_account", api_id=API_ID, api_hash=API_HASH)

CHECKPOINT_FILE = "checkpoint.json"
FAILED_LOG_FILE = "failed_message.txt"
TEMP_DIR = os.path.join(os.getcwd(), "temp_downloads")
MAX_RETRIES = 3
DELAY_BETWEEN_MESSAGES = 2.5

PROMO_KEYWORDS = [
    r"\bjoin(?:\s+us)?\b",
    r"\bfollow(?:\s+us)?\b",
    r"\bcredit[s]?\b",
    r"\bchannel\b",
    r"\bsource\b",
    r"\bby\b",
    r"\bfrom\b"
]

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv", ".m4v"}

def ensure_temp_dir():
    """Ensures temp directory exists and clears stale files on startup."""
    os.makedirs(TEMP_DIR, exist_ok=True)

def parse_chat_target(target: str):
    target = target.strip()
    try:
        return int(target)
    except ValueError:
        return target

# --- Checkpoint Management ---
def load_checkpoint(source_chat, dest_chat) -> int:
    """Returns the last processed message ID for the given source and dest channels."""
    if not os.path.exists(CHECKPOINT_FILE):
        return 0
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if str(data.get("source_chat")) == str(source_chat) and str(data.get("dest_chat")) == str(dest_chat):
                return int(data.get("last_processed_id", 0))
    except Exception as e:
        print(f"[!] Warning: Could not read checkpoint file: {e}")
    return 0

def save_checkpoint(source_chat, dest_chat, last_id: int):
    """Saves the last processed message ID atomically."""
    try:
        data = {
            "source_chat": str(source_chat),
            "dest_chat": str(dest_chat),
            "last_processed_id": last_id,
            "last_updated": datetime.now().isoformat()
        }
        temp_file = f"{CHECKPOINT_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, CHECKPOINT_FILE)
    except Exception as e:
        print(f"[!] Warning: Could not save checkpoint: {e}")

# --- Text & Filename Sanitization ---
def clean_text_content(text: str | None) -> str | None:
    """Removes @course_guy, promotional links, and orphan promo preambles."""
    if not text:
        return None
    cleaned = text
    # Remove Telegram channel links referencing course_guy
    cleaned = re.sub(r"https?://(?:t(?:elegram)?\.me)/course_guy\S*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:t(?:elegram)?\.me)/course_guy\S*", "", cleaned, flags=re.IGNORECASE)
    # Remove mentions and username handles
    cleaned = re.sub(r"@course_guy\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bcourse_guy\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[\s*\]|\(\s*\)", "", cleaned)
    
    # Process line by line to remove orphan promo remnants
    cleaned_lines = []
    for line in cleaned.splitlines():
        line_str = re.sub(r"[ \t]+", " ", line).strip(" :-–—|•*")
        is_promo_remnant = any(re.fullmatch(kw, line_str, flags=re.IGNORECASE) for kw in PROMO_KEYWORDS)
        if not is_promo_remnant and line_str:
            cleaned_lines.append(line_str)
            
    final_text = "\n".join(cleaned_lines).strip()
    return final_text if final_text else None

def clean_filename(filename: str | None, default_id: int = 0) -> str:
    """Removes @course_guy and dangerous filesystem characters from file names."""
    if not filename:
        return f"file_{default_id}"
    base, ext = os.path.splitext(filename)
    cleaned_base = re.sub(r"https?://(?:t(?:elegram)?\.me)/course_guy\S*", "", base, flags=re.IGNORECASE)
    cleaned_base = re.sub(r"(?:t(?:elegram)?\.me)/course_guy\S*", "", cleaned_base, flags=re.IGNORECASE)
    cleaned_base = re.sub(r"@course_guy\b", "", cleaned_base, flags=re.IGNORECASE)
    cleaned_base = re.sub(r"\bcourse_guy\b", "", cleaned_base, flags=re.IGNORECASE)
    cleaned_base = re.sub(r"\[\s*\]|\(\s*\)", "", cleaned_base)
    cleaned_base = re.sub(r"[\r\n\t]+", " ", cleaned_base)
    cleaned_base = re.sub(r'[<>:"/\\|?*]', "_", cleaned_base)
    cleaned_base = re.sub(r"[-_\s]+", " ", cleaned_base).strip(" .-_")
    if not cleaned_base:
        cleaned_base = f"file_{default_id}" if default_id else "file"
    return f"{cleaned_base}{ext}"

# --- Dynamic Thumbnail Generation ---
def extract_video_thumbnail(video_path: str, duration: int = 0) -> str | None:
    """Extracts a frame from video using FFmpeg as a 320px thumbnail."""
    try:
        thumb_path = os.path.join(TEMP_DIR, f"thumb_vid_{int(time.time() * 1000)}.jpg")
        target_ss = min(5, max(1, int(duration * 0.1))) if duration and duration > 1 else 1

        cmd = [
            "ffmpeg", "-y", "-ss", str(target_ss),
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale='min(320,iw)':-1",
            "-q:v", "2",
            thumb_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path

        # Fallback to frame 0
        cmd_fallback = [
            "ffmpeg", "-y", "-ss", "0",
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale='min(320,iw)':-1",
            "-q:v", "2",
            thumb_path
        ]
        result2 = subprocess.run(cmd_fallback, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result2.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
        return None
    except Exception as e:
        print(f"    [!] Warning: Video thumbnail generation skipped: {e}")
        return None

def extract_pdf_thumbnail(pdf_path: str) -> str | None:
    """Extracts the first page of a PDF document as a JPEG thumbnail."""
    try:
        thumb_path = os.path.join(TEMP_DIR, f"thumb_pdf_{int(time.time() * 1000)}.jpg")
        doc = pymupdf.open(pdf_path)
        if len(doc) == 0:
            doc.close()
            return None
        page = doc[0]
        pix = page.get_pixmap(dpi=150)
        pix.save(thumb_path)
        doc.close()
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
        return None
    except Exception as e:
        print(f"    [!] Warning: PDF thumbnail generation skipped: {e}")
        return None

def generate_thumbnail_for_file(file_path: str, duration: int = 0) -> str | None:
    """Chooses the correct thumbnail extraction strategy based on file extension."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return extract_pdf_thumbnail(file_path)
    elif ext in VIDEO_EXTENSIONS:
        return extract_video_thumbnail(file_path, duration)
    return None

def get_message_content_type(msg: Message) -> str:
    """Returns a string description of the message media type."""
    if msg.text:
        return "text"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    if msg.photo:
        return "photo"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.animation:
        return "animation"
    if msg.sticker:
        return "sticker"
    if msg.video_note:
        return "video_note"
    return "unknown"

def get_message_filename(msg: Message) -> str | None:
    if msg.document:
        return msg.document.file_name
    if msg.video:
        return msg.video.file_name
    if msg.audio:
        return msg.audio.file_name
    return None

async def progress_bar(current, total, action_prefix):
    """Displays a real-time progress bar in the console."""
    if total > 0:
        percent = (current / total) * 100
        print(f"\r {action_prefix}: {percent:.1f}% ({current // (1024*1024)}MB / {total // (1024*1024)}MB)", end="")

# --- Failure Logging ---
def record_failure_and_exit(batch: Message | list[Message], error: Exception):
    """Writes detailed failure report to failed_message.txt and stops execution."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(FAILED_LOG_FILE, "w", encoding="utf-8") as f:
        f.write("====================================================\n")
        f.write("      TELEGRAM FORWARDER FAILURE REPORT\n")
        f.write("====================================================\n\n")
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Source Channel: {SOURCE_CHAT_ENV}\n")
        f.write(f"Destination Channel: {DEST_CHAT_ENV}\n")
        f.write(f"Retry Attempts: {MAX_RETRIES} (All failed)\n\n")

        if isinstance(batch, list):
            ids = [m.id for m in batch]
            f.write(f"Batch Type: Media Album ({len(batch)} items)\n")
            f.write(f"Failed Message IDs: {ids}\n\n")
            f.write("Items in Album:\n")
            for i, m in enumerate(batch, start=1):
                f.write(f"  [{i}] ID: {m.id} | Type: {get_message_content_type(m)} | File: {get_message_filename(m)} | Caption: {repr(m.caption)}\n")
        else:
            f.write(f"Failed Message ID: {batch.id}\n")
            f.write(f"Message Type: {get_message_content_type(batch)}\n")
            f.write(f"File Name: {get_message_filename(batch)}\n")
            if batch.text:
                f.write(f"Text Content: {repr(batch.text)}\n")
            if batch.caption:
                f.write(f"Caption: {repr(batch.caption)}\n")

        f.write(f"\nError Details:\n{error}\n\n")
        f.write("Traceback:\n")
        f.write(traceback.format_exc() + "\n")
        f.write("====================================================\n")
        f.write("HOW TO RESUME:\n")
        f.write("1. Check the error above (e.g. file size > 2GB, permissions, or network issue).\n")
        f.write("2. Once resolved, simply run the bot again.\n")
        f.write(f"3. The bot will automatically resume from this exact point using '{CHECKPOINT_FILE}'.\n")
        f.write("====================================================\n")

    print(f"\n\n[x] FATAL: Message forwarding failed after {MAX_RETRIES} attempts.")
    print(f"[!] Full details written to '{FAILED_LOG_FILE}'.")
    print(f"[!] Last successful checkpoint is preserved. Terminating script.")

# --- Peer Caching & Permissions ---
async def ensure_peer_cached(app: Client, chat_target):
    """Scans dialogs to cache the access_hash for private channels you are subscribed to."""
    try:
        chat = await app.get_chat(chat_target)
        return chat
    except Exception:
        print(f"[*] Chat {chat_target} is missing from local cache. Scanning your chat list to find it...")
        
        target_variants = [chat_target]
        if isinstance(chat_target, int):
            target_str = str(chat_target)
            if target_str.startswith("-100"):
                target_variants.append(int(target_str[4:]))
                target_variants.append(int(f"-{target_str[4:]}"))
            else:
                clean_id = target_str.lstrip("-")
                target_variants.append(int(f"-100{clean_id}"))
        
        found_chats = []

        async for dialog in app.get_dialogs():
            if dialog.chat.type in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
                found_chats.append(f"{dialog.chat.title} | ID: {dialog.chat.id}")

            if dialog.chat.id in target_variants or dialog.chat.username == chat_target:
                print(f"[✓] Found and cached: {dialog.chat.title}")
                return dialog.chat
        
        try:
            with open("my_channels.txt", "w", encoding="utf-8") as f:
                f.write("=== YOUR GROUPS & CHANNELS ===\n\n")
                f.write("\n".join(found_chats))
            print("\n[!] FATAL ERROR: Cannot access chat.")
            print("    I have saved a list of ALL your joined groups/channels to 'my_channels.txt'.")
            print("    Please check that file and verify your chat ID.")
        except Exception as e:
            print(f"    (Could not save my_channels.txt: {e})")

        return None

async def verify_destination_permissions(app: Client, dest_chat) -> bool:
    chat = await ensure_peer_cached(app, dest_chat)
    if not chat:
        print(f"[x] Cannot resolve destination {dest_chat}. Are you a member/admin?")
        return False

    if chat.type in (ChatType.PRIVATE, ChatType.BOT):
        return True

    try:
        member = await app.get_chat_member(chat.id, "me")
    except UserNotParticipant:
        print(f"[x] Error: Your account is not in destination '{chat.title}'.")
        return False
    except Exception as e:
        print(f"[x] Could not inspect membership in '{chat.title}': {e}")
        return False

    if chat.type == ChatType.CHANNEL:
        if member.status == ChatMemberStatus.OWNER:
            return True
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            if member.privileges and member.privileges.can_post_messages:
                return True
            print(f"[x] Error: Admin in '{chat.title}', but missing 'Post Messages' rights.")
            return False
        print(f"[x] Error: You are only a subscriber in '{chat.title}'. You must be an admin to post.")
        return False

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            return True
        if member.permissions and not member.permissions.can_send_media_messages:
            print(f"[x] Error: Sending media is restricted for members in '{chat.title}'.")
            return False

    return True

# --- Individual Message Processor ---
async def send_single_message(app: Client, dest_chat, msg: Message):
    """Handles sending a single message cleanly without forward tags."""
    temp_files = []
    try:
        # 1. Text Message
        if msg.text:
            cleaned_text = clean_text_content(msg.text.markdown if hasattr(msg.text, "markdown") else msg.text)
            if cleaned_text:
                await app.send_message(chat_id=dest_chat, text=cleaned_text)
                print(f" -> Sent Text Message (Msg ID: {msg.id})")
            else:
                print(f" -> Skipped promotional-only text message (Msg ID: {msg.id})")
            return

        cleaned_caption = clean_text_content(msg.caption.markdown if msg.caption and hasattr(msg.caption, "markdown") else (msg.caption or ""))

        # Handle caption splitting if caption > 1024 characters
        follow_up_caption = None
        if cleaned_caption and len(cleaned_caption) > 1024:
            follow_up_caption = cleaned_caption[1024:]
            cleaned_caption = cleaned_caption[:1024]

        # 2. Video Message
        if msg.video:
            orig_name = msg.video.file_name or f"video_{msg.id}.mp4"
            sanitized_name = clean_filename(orig_name, msg.id)
            target_download_path = os.path.join(TEMP_DIR, f"{msg.id}_{sanitized_name}")
            temp_files.append(target_download_path)

            print(f" -> Queued Video: {sanitized_name} ({round(msg.video.file_size / (1024*1024), 2)} MB)")
            downloaded = await app.download_media(msg, file_name=target_download_path, progress=progress_bar, progress_args=("-> Downloading",))
            print()

            thumb_path = extract_video_thumbnail(downloaded, duration=msg.video.duration)
            if thumb_path:
                temp_files.append(thumb_path)

            await app.send_video(
                chat_id=dest_chat,
                video=downloaded,
                caption=cleaned_caption,
                duration=msg.video.duration,
                width=msg.video.width,
                height=msg.video.height,
                thumb=thumb_path,
                supports_streaming=True,
                progress=progress_bar,
                progress_args=("-> Uploading",)
            )
            print(f"\n[✓] Uploaded Video (Msg ID: {msg.id})")

        # 3. Document Message
        elif msg.document:
            orig_name = msg.document.file_name or f"document_{msg.id}"
            sanitized_name = clean_filename(orig_name, msg.id)
            target_download_path = os.path.join(TEMP_DIR, f"{msg.id}_{sanitized_name}")
            temp_files.append(target_download_path)

            print(f" -> Queued Document: {sanitized_name} ({round(msg.document.file_size / (1024*1024), 2)} MB)")
            downloaded = await app.download_media(msg, file_name=target_download_path, progress=progress_bar, progress_args=("-> Downloading",))
            print()

            thumb_path = generate_thumbnail_for_file(downloaded)
            if thumb_path:
                temp_files.append(thumb_path)

            await app.send_document(
                chat_id=dest_chat,
                document=downloaded,
                file_name=sanitized_name,
                caption=cleaned_caption,
                thumb=thumb_path,
                progress=progress_bar,
                progress_args=("-> Uploading",)
            )
            print(f"\n[✓] Uploaded Document (Msg ID: {msg.id})")

        # 4. Photo Message
        elif msg.photo:
            target_download_path = os.path.join(TEMP_DIR, f"photo_{msg.id}.jpg")
            temp_files.append(target_download_path)

            print(f" -> Queued Photo (Msg ID: {msg.id})")
            downloaded = await app.download_media(msg, file_name=target_download_path, progress=progress_bar, progress_args=("-> Downloading",))
            print()

            await app.send_photo(
                chat_id=dest_chat,
                photo=downloaded,
                caption=cleaned_caption,
                progress=progress_bar,
                progress_args=("-> Uploading",)
            )
            print(f"\n[✓] Uploaded Photo (Msg ID: {msg.id})")

        # 5. Audio Message
        elif msg.audio:
            orig_name = msg.audio.file_name or f"audio_{msg.id}.mp3"
            sanitized_name = clean_filename(orig_name, msg.id)
            target_download_path = os.path.join(TEMP_DIR, f"{msg.id}_{sanitized_name}")
            temp_files.append(target_download_path)

            print(f" -> Queued Audio: {sanitized_name}")
            downloaded = await app.download_media(msg, file_name=target_download_path, progress=progress_bar, progress_args=("-> Downloading",))
            print()

            clean_performer = clean_text_content(msg.audio.performer)
            clean_title = clean_text_content(msg.audio.title)

            await app.send_audio(
                chat_id=dest_chat,
                audio=downloaded,
                file_name=sanitized_name,
                caption=cleaned_caption,
                duration=msg.audio.duration,
                performer=clean_performer,
                title=clean_title,
                progress=progress_bar,
                progress_args=("-> Uploading",)
            )
            print(f"\n[✓] Uploaded Audio (Msg ID: {msg.id})")

        # 6. Voice Message
        elif msg.voice:
            target_download_path = os.path.join(TEMP_DIR, f"voice_{msg.id}.ogg")
            temp_files.append(target_download_path)

            print(f" -> Queued Voice Note (Msg ID: {msg.id})")
            downloaded = await app.download_media(msg, file_name=target_download_path, progress=progress_bar, progress_args=("-> Downloading",))
            print()

            await app.send_voice(
                chat_id=dest_chat,
                voice=downloaded,
                caption=cleaned_caption,
                duration=msg.voice.duration,
                progress=progress_bar,
                progress_args=("-> Uploading",)
            )
            print(f"\n[✓] Uploaded Voice Note (Msg ID: {msg.id})")

        # 7. Animation / GIF
        elif msg.animation:
            orig_name = msg.animation.file_name or f"animation_{msg.id}.mp4"
            sanitized_name = clean_filename(orig_name, msg.id)
            target_download_path = os.path.join(TEMP_DIR, f"{msg.id}_{sanitized_name}")
            temp_files.append(target_download_path)

            print(f" -> Queued Animation (Msg ID: {msg.id})")
            downloaded = await app.download_media(msg, file_name=target_download_path, progress=progress_bar, progress_args=("-> Downloading",))
            print()

            await app.send_animation(
                chat_id=dest_chat,
                animation=downloaded,
                caption=cleaned_caption,
                duration=msg.animation.duration,
                width=msg.animation.width,
                height=msg.animation.height,
                progress=progress_bar,
                progress_args=("-> Uploading",)
            )
            print(f"\n[✓] Uploaded Animation (Msg ID: {msg.id})")

        # 8. Sticker
        elif msg.sticker:
            target_download_path = os.path.join(TEMP_DIR, f"sticker_{msg.id}.webp")
            temp_files.append(target_download_path)

            print(f" -> Queued Sticker (Msg ID: {msg.id})")
            downloaded = await app.download_media(msg, file_name=target_download_path)

            await app.send_sticker(chat_id=dest_chat, sticker=downloaded)
            print(f"\n[✓] Uploaded Sticker (Msg ID: {msg.id})")

        # Send follow-up message if caption was longer than 1024 characters
        if follow_up_caption:
            await app.send_message(chat_id=dest_chat, text=follow_up_caption)
            print(f" -> Sent follow-up caption portion (Msg ID: {msg.id})")

    finally:
        # Clean up all temporary files created for this message
        for fpath in temp_files:
            if fpath and os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

# --- Media Group / Album Processor ---
async def send_media_album(app: Client, dest_chat, album_msgs: list[Message]):
    """Handles sending multiple grouped media items as an album."""
    temp_files = []
    print(f"\n[*] Processing Media Album ({len(album_msgs)} items, Group: {album_msgs[0].media_group_id})...")
    try:
        # Find primary caption from the first message in the album that has one
        primary_caption = None
        for m in album_msgs:
            if m.caption:
                primary_caption = clean_text_content(m.caption.markdown if hasattr(m.caption, "markdown") else m.caption)
                if primary_caption:
                    break

        input_media_list = []
        is_first = True

        for m in album_msgs:
            cap = primary_caption if is_first else None
            is_first = False

            if m.photo:
                dl_path = os.path.join(TEMP_DIR, f"album_photo_{m.id}.jpg")
                temp_files.append(dl_path)
                downloaded = await app.download_media(m, file_name=dl_path)
                input_media_list.append(InputMediaPhoto(media=downloaded, caption=cap))

            elif m.video:
                orig_name = m.video.file_name or f"video_{m.id}.mp4"
                sanitized_name = clean_filename(orig_name, m.id)
                dl_path = os.path.join(TEMP_DIR, f"album_{m.id}_{sanitized_name}")
                temp_files.append(dl_path)
                downloaded = await app.download_media(m, file_name=dl_path)
                
                thumb_path = extract_video_thumbnail(downloaded, duration=m.video.duration)
                if thumb_path:
                    temp_files.append(thumb_path)

                input_media_list.append(
                    InputMediaVideo(
                        media=downloaded,
                        caption=cap,
                        duration=m.video.duration,
                        width=m.video.width,
                        height=m.video.height,
                        thumb=thumb_path,
                        supports_streaming=True
                    )
                )

            elif m.document:
                orig_name = m.document.file_name or f"doc_{m.id}"
                sanitized_name = clean_filename(orig_name, m.id)
                dl_path = os.path.join(TEMP_DIR, f"album_{m.id}_{sanitized_name}")
                temp_files.append(dl_path)
                downloaded = await app.download_media(m, file_name=dl_path)

                thumb_path = generate_thumbnail_for_file(downloaded)
                if thumb_path:
                    temp_files.append(thumb_path)

                input_media_list.append(InputMediaDocument(media=downloaded, caption=cap, thumb=thumb_path))

            elif m.audio:
                orig_name = m.audio.file_name or f"audio_{m.id}.mp3"
                sanitized_name = clean_filename(orig_name, m.id)
                dl_path = os.path.join(TEMP_DIR, f"album_{m.id}_{sanitized_name}")
                temp_files.append(dl_path)
                downloaded = await app.download_media(m, file_name=dl_path)

                input_media_list.append(
                    InputMediaAudio(
                        media=downloaded,
                        caption=cap,
                        duration=m.audio.duration,
                        performer=clean_text_content(m.audio.performer),
                        title=clean_text_content(m.audio.title)
                    )
                )

        if input_media_list:
            # Telegram albums allow up to 10 items per group
            for i in range(0, len(input_media_list), 10):
                chunk = input_media_list[i:i + 10]
                await app.send_media_group(chat_id=dest_chat, media=chunk)
            print(f"[✓] Uploaded Album ({len(album_msgs)} items) successfully!")

    finally:
        for fpath in temp_files:
            if fpath and os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

# --- Robust Dispatcher with 3-Try Retry Mechanism ---
async def process_batch_with_retry(app: Client, dest_chat, batch: Message | list[Message]) -> bool:
    """Executes forwarding for a single message or album with up to 3 retries."""
    attempt = 0
    last_exception = None

    while attempt < MAX_RETRIES:
        attempt += 1
        try:
            if isinstance(batch, list):
                await send_media_album(app, dest_chat, batch)
            else:
                await send_single_message(app, dest_chat, batch)
            return True

        except FloodWait as e:
            print(f"\n[!] FloodWait received: Sleeping for {e.value} seconds...")
            await asyncio.sleep(e.value)
            # FloodWait does not count against failure retry attempts
            attempt -= 1

        except ChatWriteForbidden:
            print(f"\n[x] Fatal: ChatWriteForbidden. You cannot post in {dest_chat}.")
            record_failure_and_exit(batch, ChatWriteForbidden("You do not have permission to post."))
            return False

        except Exception as e:
            last_exception = e
            msg_ids = [m.id for m in batch] if isinstance(batch, list) else batch.id
            print(f"\n[!] [Attempt {attempt}/{MAX_RETRIES}] Error processing Msg ID {msg_ids}: {e}")
            if attempt < MAX_RETRIES:
                wait_time = attempt * 3
                print(f"    Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)

    # 3 retries exhausted: Log and stop program
    record_failure_and_exit(batch, last_exception)
    return False

# --- Main Forwarder Loop ---
async def main():
    ensure_temp_dir()

    async with app:
        me = await app.get_me()
        print(f"[+] Logged in as: {me.first_name} (@{me.username}) | ID: {me.id}\n")

        source_chat = parse_chat_target(SOURCE_CHAT_ENV)
        dest_chat = parse_chat_target(DEST_CHAT_ENV)

        scan_limit = None
        if SCAN_LIMIT_ENV and SCAN_LIMIT_ENV.strip().isdigit():
            scan_limit = int(SCAN_LIMIT_ENV.strip())

        # Validate Source Channel
        print(f"[+] Resolving Source Channel: {source_chat}...")
        src_chat_info = await ensure_peer_cached(app, source_chat)
        if not src_chat_info:
            print(f"[x] FATAL ERROR: Cannot access source channel {source_chat}.")
            return
        print(f" -> Source verified: '{src_chat_info.title}'")

        # Validate Destination Channel
        print("\n[+] Validating destination permissions...")
        if not await verify_destination_permissions(app, dest_chat):
            print("[-] Aborting process due to insufficient permissions.")
            return
        print(" -> Destination permissions verified.")

        # Check Checkpoint
        last_processed_id = load_checkpoint(source_chat, dest_chat)
        if last_processed_id > 0:
            print(f"\n[+] Checkpoint found: Resuming forwarding after Msg ID: {last_processed_id}")
            if os.path.exists(FAILED_LOG_FILE):
                print(f"[*] Note: A previous failure report exists in '{FAILED_LOG_FILE}'.")
        else:
            print("\n[+] No previous checkpoint found. Starting from the beginning of scan.")

        # Scan History
        print(f"\n[+] Scanning {src_chat_info.title} for messages...")
        raw_messages = []
        try:
            async for message in app.get_chat_history(source_chat, limit=scan_limit):
                if message.empty or message.service:
                    continue
                # If we've reached messages that were already processed in a previous run, stop scanning older messages
                if last_processed_id > 0 and message.id <= last_processed_id:
                    break
                raw_messages.append(message)
        except Exception as e:
            print(f"[x] Failed to fetch chat history: {e}")
            return

        # Reverse to ensure strictly chronological order (oldest -> newest)
        raw_messages.reverse()
        total_msgs = len(raw_messages)
        print(f"[+] Found {total_msgs} new message(s) to process.")

        if total_msgs == 0:
            print("[-] No new messages to forward. Everything is up to date!")
            return

        # Group consecutive media items that share media_group_id into albums
        batches = []
        current_album = []
        current_group_id = None

        for msg in raw_messages:
            if msg.media_group_id:
                if current_group_id is None:
                    current_group_id = msg.media_group_id
                    current_album = [msg]
                elif current_group_id == msg.media_group_id:
                    current_album.append(msg)
                else:
                    batches.append(current_album)
                    current_group_id = msg.media_group_id
                    current_album = [msg]
            else:
                if current_album:
                    batches.append(current_album)
                    current_album = []
                    current_group_id = None
                batches.append(msg)

        if current_album:
            batches.append(current_album)

        total_batches = len(batches)
        print(f"[+] Formed {total_batches} batch(es) (individual messages + media albums).")

        # Process each batch in strict chronological order
        for idx, batch in enumerate(batches, start=1):
            if isinstance(batch, list):
                batch_ids = [m.id for m in batch]
                print(f"\n[{idx}/{total_batches}] Processing Album with Msg IDs {batch_ids}...")
                highest_id = max(batch_ids)
            else:
                print(f"\n[{idx}/{total_batches}] Processing Msg ID {batch.id} ({get_message_content_type(batch)})...")
                highest_id = batch.id

            success = await process_batch_with_retry(app, dest_chat, batch)
            if not success:
                # Execution stops here upon failure after 3 tries (failure report already written)
                return

            # Update checkpoint immediately upon success
            save_checkpoint(source_chat, dest_chat, highest_id)
            await asyncio.sleep(DELAY_BETWEEN_MESSAGES)

        # Clear failed log if whole queue finished successfully
        if os.path.exists(FAILED_LOG_FILE):
            try:
                os.remove(FAILED_LOG_FILE)
            except OSError:
                pass

        print("\n====================================================")
        print("[✓] ALL MESSAGES FORWARDED SUCCESSFULLY IN ORDER!")
        print("====================================================")

if __name__ == "__main__":
    try:
        app.run(main())
    except KeyboardInterrupt:
        print("\n\n[-] Script terminated by user.")