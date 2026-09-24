import os
import re
import asyncio
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import FloodWait

load_dotenv()

raw_api_id = os.getenv("APP_ID")
API_HASH = os.getenv("API_HASH")

if raw_api_id is None or API_HASH is None:
    raise RuntimeError(
        "Missing required environment variables: set APP_ID/API_ID and API_HASH in your .env file."
    )

try:
    API_ID = int(raw_api_id)
except ValueError as exc:
    raise RuntimeError("APP_ID/API_ID must be an integer value.") from exc

app = Client(
    name="my_account",
    api_id=API_ID,
    api_hash=API_HASH
)


def parse_chat_target(target: str):
    """Converts input string into an integer ID if numeric, otherwise keeps username string."""
    target = target.strip()
    try:
        return int(target)
    except ValueError:
        return target


def clean_caption(caption: str | None) -> str | None:
    """Removes the handle '@course_guy' (case-insensitive) and cleans up leftover spaces."""
    if not caption:
        return None

    # Remove @course_guy (case-insensitive)
    cleaned = re.sub(r"@course_guy\b", "", caption, flags=re.IGNORECASE)

    # Clean up double/trailing spaces that might remain
    cleaned = re.sub(r" +", " ", cleaned).strip()

    return cleaned if cleaned else None


async def main():
    async with app:
        me = await app.get_me()
        print(f"[+] Logged in as: {me.first_name} (@{me.username}) | ID: {me.id}\n")

        src_input = input("Enter Source Channel ID or @username: ")
        dst_input = input("Enter Destination User ID or @username: ")

        source_chat = parse_chat_target(src_input)
        dest_chat = parse_chat_target(dst_input)

        print(f"\n[+] Scanning {source_chat} for videos...")

        # Collect only video messages
        video_messages = []
        async for message in app.get_chat_history(source_chat):
            if message.video:
                video_messages.append(message)

        # Reverse list to process oldest (top of channel) to newest
        video_messages.reverse()
        total = len(video_messages)
        print(f"[+] Found {total} video(s) to process.")

        if total == 0:
            print("[-] No videos found.")
            return

        for index, msg in enumerate(video_messages, start=1):
            print(f"\n[{index}/{total}] Processing video (Msg ID: {msg.id})...")
            downloaded_path = None

            # Clean original caption
            new_caption = clean_caption(msg.caption)

            try:
                # 1. Download video
                file_label = msg.video.file_name or "video.mp4"
                file_mb = round(msg.video.file_size / (1024 * 1024), 2)
                print(f" -> Downloading: {file_label} ({file_mb} MB)...")
                downloaded_path = await app.download_media(msg)

                # 2. Upload without thumbnail (thumb=None) and with modified caption
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
                            thumb=None,  # Strips custom thumbnail
                            supports_streaming=True
                        )
                        print(f"[✓] Uploaded successfully (Msg ID: {msg.id})")
                        await asyncio.sleep(2)  # Prevent rapid flood issues
                        break

                    except FloodWait as e:
                        print(f"[!] FloodWait received: Sleeping {e.value}s...")
                        await asyncio.sleep(e.value)

            except Exception as e:
                print(f"[x] Error on message {msg.id}: {e}")

            finally:
                # 3. Always remove disk file before moving to the next video
                if downloaded_path and os.path.exists(downloaded_path):
                    try:
                        os.remove(downloaded_path)
                        print(" -> Cleaned up local file.")
                    except OSError as err:
                        print(f"[!] File deletion failed: {err}")

        print("\n[+] All videos processed successfully!")


if __name__ == "__main__":
    app.run(main())