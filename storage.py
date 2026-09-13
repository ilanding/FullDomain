import asyncio
from github_storage import GitHubJSON
class Store:
    def __init__(self,cfg):
        self.db=GitHubJSON(cfg.gh_token,cfg.gh_repo,cfg.gh_branch,cfg.data_file)
        self.data={"projects":{},"domains":{},"websites":{},"deployments":{},"email_routing":{},"settings":{}}
        self.lock=asyncio.Lock()
    async def load(self):
        d=await asyncio.to_thread(self.db.load)
        if d:self.data.update(d)
    async def save(self,msg="Update"):
        async with self.lock: await asyncio.to_thread(self.db.save,self.data,msg)
