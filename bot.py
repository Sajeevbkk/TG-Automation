import os
import re
import asyncio
from dotenv import load_dotenv

# --- PATCH PYROGRAM 64-BIT CHANNEL ID BUG ---
import pyrogram.utils

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
    exit(1)

try:
    API_ID = int(_app_id_str)
except ValueError:
    print(f"[x] FATAL ERROR: APP_ID must be a number. You provided: '{_app_id_str}'")
    exit(1)

app = Client("my_account", api_id=API_ID, api_hash=API_HASH)

def parse_chat_target(target: str):
    target = target.strip()
    try:
        return int(target)
    except ValueError:
        return target

def clean_caption(caption: str | None) -> str | None:
    if not caption:
        return None
    cleaned = re.sub(r"@course_guy\b", "", caption, flags=re.IGNORECASE)
    cleaned = re.sub(r" +", " ", cleaned).strip()
    return cleaned if cleaned else None

async def progress_bar(current, total, action_prefix):
    """Displays a real-time progress bar in the console."""
    if total > 0:
        percent = (current / total) * 100
        print(f"\r {action_prefix}: {percent:.1f}% ({current // (1024*1024)}MB / {total // (1024*1024)}MB)", end="")

async def ensure_peer_cached(app: Client, chat_target):
    """Scans dialogs to cache the access_hash for private channels you are subscribed to."""
    try:
        chat = await app.get_chat(chat_target)
        return chat
    except Exception:
        print(f"[*] Chat {chat_target} is missing from local cache. Scanning your chat list to find it...")
        
        # Prepare variants of the ID (in case the -100 prefix is missing or wrong)
        target_variants = [chat_target]
        if isinstance(chat_target, int):
            target_str = str(chat_target)
            if target_str.startswith("-100"):
                target_variants.append(int(target_str[4:]))      # e.g., 2622933496
                target_variants.append(int(f"-{target_str[4:]}")) # e.g., -2622933496
            else:
                clean_id = target_str.lstrip('-')
                target_variants.append(int(f"-100{clean_id}"))    # e.g., -1002622933496
        
        found_chats = []

        async for dialog in app.get_dialogs():
            # Save all groups/channels for debugging just in case
            if dialog.chat.type in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
                found_chats.append(f"{dialog.chat.title} | ID: {dialog.chat.id}")

            # Check if this chat matches any ID variant or username
            if dialog.chat.id in target_variants or dialog.chat.username == chat_target:
                print(f"[✓] Found and cached: {dialog.chat.title}")
                return dialog.chat
        
        # If we get here, the channel wasn't found at all. Dump the list to a file.
        try:
            with open("my_channels.txt", "w", encoding="utf-8") as f:
                f.write("=== YOUR GROUPS & CHANNELS ===\n\n")
                f.write("\n".join(found_chats))
            print("\n[!] FATAL ERROR: Cannot access source channel.")
            print("    I have saved a list of ALL your joined groups/channels to 'my_channels.txt'.")
            print("    Please open that file, search for your channel's name, and update your .env file with the exact ID shown there.")
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

async def main():
    async with app:
        me = await app.get_me()
        print(f"[+] Logged in as: {me.first_name} (@{me.username}) | ID: {me.id}\n")

        source_chat = parse_chat_target(SOURCE_CHAT_ENV)
        dest_chat = parse_chat_target(DEST_CHAT_ENV)
        
        scan_limit = None
        if SCAN_LIMIT_ENV and SCAN_LIMIT_ENV.strip().isdigit():
            scan_limit = int(SCAN_LIMIT_ENV.strip())

        # Validate Source Channel (Read-only as Subscriber)
        print(f"[+] Resolving Source Channel: {source_chat}...")
        src_chat_info = await ensure_peer_cached(app, source_chat)
        if not src_chat_info:
            print(f"[x] FATAL ERROR: Cannot access source channel {source_chat}.")
            print("    Fix: Make sure you have actually joined this channel with your Telegram account.")
            return
        print(f" -> Source verified: '{src_chat_info.title}'")

        # Validate Destination Channel (Requires Admin/Write rights)
        print("\n[+] Validating destination permissions...")
        if not await verify_destination_permissions(app, dest_chat):
            print("[-] Aborting process due to insufficient permissions.")
            return
        print(f" -> Permissions verified.")

        print(f"\n[+] Scanning {src_chat_info.title} for videos...")
        video_messages = []
        try:
            async for message in app.get_chat_history(source_chat, limit=scan_limit):
                if message.video:
                    video_messages.append(message)
        except Exception as e:
            print(f"[x] Failed to fetch chat history: {e}")
            return

        video_messages.reverse()
        total = len(video_messages)
        print(f"[+] Found {total} video(s) to process.")

        if total == 0:
            print("[-] No videos found.")
            return

        for index, msg in enumerate(video_messages, start=1):
            print(f"\n[{index}/{total}] Processing video (Msg ID: {msg.id})...")
            downloaded_path = None
            new_caption = clean_caption(msg.caption)

            try:
                file_label = msg.video.file_name or f"video_{msg.id}.mp4"
                file_mb = round(msg.video.file_size / (1024 * 1024), 2)
                print(f" -> Queued: {file_label} ({file_mb} MB)")
                
                downloaded_path = await app.download_media(
                    msg, 
                    progress=progress_bar, 
                    progress_args=("-> Downloading",)
                )
                print() 

                while True:
                    try:
                        await app.send_video(
                            chat_id=dest_chat,
                            video=downloaded_path,
                            caption=new_caption,
                            duration=msg.video.duration,
                            width=msg.video.width,
                            height=msg.video.height,
                            thumbnail=None, # Removed thumbnail as requested
                            supports_streaming=True,
                            progress=progress_bar,
                            progress_args=("-> Uploading",)
                        )
                        print(f"\n[✓] Uploaded successfully (Msg ID: {msg.id})")
                        await asyncio.sleep(2.5) # Slight delay to avoid Telegram flood limits
                        break

                    except FloodWait as e:
                        print(f"\n[!] FloodWait received: Sleeping for {e.value} seconds...")
                        await asyncio.sleep(e.value)

                    except ChatWriteForbidden:
                        print(f"\n[x] Fatal: ChatWriteForbidden. You cannot post in {dest_chat}.")
                        return

            except Exception as e:
                print(f"\n[x] Error on message {msg.id}: {e}")

            finally:
                if downloaded_path and os.path.exists(downloaded_path):
                    try:
                        os.remove(downloaded_path)
                        print(" -> Cleaned up local file.")
                    except OSError as err:
                        print(f"[!] File deletion failed: {err}")

        print("\n[+] All videos processed successfully!")

if __name__ == "__main__":
    try:
        app.run(main())
    except KeyboardInterrupt:
        print("\n\n[-] Script terminated by user.")