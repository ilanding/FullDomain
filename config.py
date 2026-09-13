import os
from dataclasses import dataclass

def csv(name, default=""):
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())

@dataclass(frozen=True)
class Config:
    bot_token:str; admins:tuple; cf_token:str; cf_account:str
    sp_key:str; sp_secret:str; sp_base:str
    gh_token:str; gh_repo:str; gh_branch:str; data_file:str
    tlds:tuple; poll_seconds:int

def load_config():
    ids=tuple(int(x) for x in csv("ADMIN_USER_IDS"))
    if not os.getenv("BOT_TOKEN") or not ids: raise RuntimeError("BOT_TOKEN and ADMIN_USER_IDS are required")
    return Config(os.getenv("BOT_TOKEN"),ids,os.getenv("CLOUDFLARE_API_TOKEN",""),os.getenv("CLOUDFLARE_ACCOUNT_ID",""),
                  os.getenv("SPACESHIP_API_KEY",""),os.getenv("SPACESHIP_API_SECRET",""),
                  os.getenv("SPACESHIP_API_BASE_URL","https://spaceship.dev/api/v1").rstrip("/"),
                  os.getenv("GITHUB_TOKEN",""),os.getenv("GITHUB_REPO",""),os.getenv("GITHUB_BRANCH","main"),
                  os.getenv("DATA_FILE","data.json"),csv("ALLOWED_PURCHASE_TLDS",".sbs,.rest,.buzz,.click"),
                  int(os.getenv("CF_POLL_SECONDS","600")))
