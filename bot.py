import os
import re
import asyncio
from dotenv import load_dotenv

# --- PATCH PYROGRAM 64-BIT CHANNEL ID BUG ---
import pyrogram.utils

def patched_get_peer_type(peer_id: int) -> str:
    if peer_id < 0:
        # Standard channel IDs start with -100 and can exceed -1999999999999
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

API_ID = int(os.getenv("APP_ID"))
API_HASH = os.getenv("API_HASH")

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


async def sync_dialog_cache():
    """Iterates recent dialogs to ensure peer access_hashes are cached in SQLite."""
    print("[*] Synchronizing chat dialogs...")
    async for _ in app.get_dialogs(limit=50):
        pass


async def verify_destination_permissions(app: Client, dest_chat) -> bool:
    try:
        chat = await app.get_chat(dest_chat)
    except Exception as e:
        print(f"[x] Cannot resolve destination {dest_chat}: {e}")
        return False

    if chat.type in (ChatType.PRIVATE, ChatType.BOT):
        return True

    try:
        member = await app.get_chat_member(chat.id, "me")
    except UserNotParticipant:
        print(f"[x] Error: Your account is not in destination '{chat.title}' ({chat.id}).")
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
        print(f"[x] Error: You are only a subscriber in '{chat.title}'. Channel posting requires admin rights.")
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

        # Sync dialogs so peers are loaded into SQLite storage
        await sync_dialog_cache()

        src_input = input("Enter Source Channel ID or @username: ")
        dst_input = input("Enter Destination Channel/User ID or @username: ")

        source_chat = parse_chat_target(src_input)
        dest_chat = parse_chat_target(dst_input)

        print("[+] Validating destination permissions...")
        if not await verify_destination_permissions(app, dest_chat):
            print("[-] Aborting process due to insufficient permissions.")
            return

        print(f"[+] Permissions verified. Scanning {source_chat} for videos...")

        video_messages = []
        async for message in app.get_chat_history(source_chat):
            if message.video:
                video_messages.append(message)

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
                file_label = msg.video.file_name or "video.mp4"
                file_mb = round(msg.video.file_size / (1024 * 1024), 2)
                print(f" -> Downloading: {file_label} ({file_mb} MB)...")
                downloaded_path = await app.download_media(msg)

                while True:
                    try:
                        print(" -> Uploading with thumbnail stripped...")
                        await app.send_video(
                            chat_id=dest_chat,
                            video=downloaded_path,
                            caption=new_caption,
                            duration=msg.video.duration,
                            width=msg.video.width,
                            height=msg.video.height,
                            thumb=None,
                            supports_streaming=True
                        )
                        print(f"[✓] Uploaded successfully (Msg ID: {msg.id})")
                        await asyncio.sleep(2)
                        break

                    except FloodWait as e:
                        print(f"[!] FloodWait received: Sleeping {e.value}s...")
                        await asyncio.sleep(e.value)

                    except ChatWriteForbidden:
                        print(f"[x] Fatal: ChatWriteForbidden. You cannot post in {dest_chat}.")
                        return

            except Exception as e:
                print(f"[x] Error on message {msg.id}: {e}")

            finally:
                if downloaded_path and os.path.exists(downloaded_path):
                    try:
                        os.remove(downloaded_path)
                        print(" -> Cleaned up local file.")
                    except OSError as err:
                        print(f"[!] File deletion failed: {err}")

        print("\n[+] All videos processed successfully!")


if __name__ == "__main__":
    app.run(main())