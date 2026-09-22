"""Discord 送信を止めるフラグ。"""
import os

MUTE_FLAG = "/home/azureuser/antigravity/data/DISCORD_MUTE.flag"


def discord_muted() -> bool:
    return os.path.exists(MUTE_FLAG)
