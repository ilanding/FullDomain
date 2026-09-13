import tempfile,subprocess,os,shutil
from pathlib import Path
class Pages:
    def __init__(self,cf):self.cf=cf
    async def free_name(self,base):
        used={x.get("name") for x in await self.cf.projects()}
        if base not in used:return base
        i=1
        while f"{base}{i}" in used:i+=1
        return f"{base}{i}"
    async def deploy(self,name,html):
        d=Path(tempfile.mkdtemp(prefix="site-"))
        try:
            (d/"index.html").write_bytes(html)
            env=os.environ.copy();env["CLOUDFLARE_ACCOUNT_ID"]=self.cf.a;env["CLOUDFLARE_API_TOKEN"]=self.cf.t
            p=await __import__("asyncio").to_thread(subprocess.run,["npx","wrangler","pages","deploy",str(d),"--project-name",name],env=env,capture_output=True,text=True,timeout=180)
            if p.returncode:raise RuntimeError(p.stderr[-2000:])
            return p.stdout[-3000:]
        finally:shutil.rmtree(d,ignore_errors=True)
