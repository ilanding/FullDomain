import base64,json,httpx
class GitHubJSON:
    def __init__(self,t,r,b,p): self.t=t; self.r=r; self.b=b; self.p=p; self.url=f"https://api.github.com/repos/{r}/contents/data.json"
    def h(self): return {"Authorization":f"Bearer {self.t}","Accept":"application/vnd.github+json"}
    def load(self):
        if not(self.t and self.r):
            try:
                with open(self.p,encoding="utf8") as f:return json.load(f)
            except FileNotFoundError:return {}
        x=httpx.get(self.url,headers=self.h(),params={"ref":self.b},timeout=30)
        if x.status_code==404:return {}
        x.raise_for_status(); return json.loads(base64.b64decode(x.json()["content"]).decode())
    def save(self,d,msg):
        if not(self.t and self.r):
            with open(self.p,"w",encoding="utf8") as f:json.dump(d,f,indent=2,ensure_ascii=False)
            return
        old=httpx.get(self.url,headers=self.h(),params={"ref":self.b},timeout=30)
        body={"message":msg,"content":base64.b64encode(json.dumps(d,indent=2,ensure_ascii=False).encode()).decode(),"branch":self.b}
        if old.status_code==200:body["sha"]=old.json()["sha"]
        x=httpx.put(self.url,headers=self.h(),json=body,timeout=30)
        if x.status_code==409:
            old=httpx.get(self.url,headers=self.h(),params={"ref":self.b},timeout=30); body["sha"]=old.json()["sha"]
            x=httpx.put(self.url,headers=self.h(),json=body,timeout=30)
        x.raise_for_status()
