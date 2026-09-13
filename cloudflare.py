import httpx
class Cloudflare:
    def __init__(self,cfg):
        self.b="https://api.cloudflare.com/client/v4"; self.t=cfg.cf_token; self.a=cfg.cf_account
        if not self.t or not self.a: raise RuntimeError("Cloudflare credentials missing")
    async def req(self,m,p,**kw):
        async with httpx.AsyncClient(timeout=45) as c:r=await c.request(m,self.b+p,headers={"Authorization":f"Bearer {self.t}","Content-Type":"application/json"},**kw)
        if r.status_code>=400:raise RuntimeError(f"Cloudflare {r.status_code}: {r.text[:500]}")
        j=r.json()
        if not j.get("success"):raise RuntimeError(str(j.get("errors")))
        return j["result"]
    async def zone_create(self,d):return await self.req("POST","/zones",json={"name":d,"account":{"id":self.a}})
    async def zone_get(self,z):return await self.req("GET",f"/zones/{z}")
    async def projects(self):return await self.req("GET",f"/accounts/{self.a}/pages/projects")
    async def project_create(self,n):return await self.req("POST",f"/accounts/{self.a}/pages/projects",json={"name":n,"production_branch":"main"})
    async def project_domain(self,p,d):return await self.req("POST",f"/accounts/{self.a}/pages/projects/{p}/domains",json={"name":d})
    async def email_destination(self,e):return await self.req("POST",f"/accounts/{self.a}/email/routing/addresses",json={"email":e})
    async def email_dns(self,z):return await self.req("POST",f"/zones/{z}/email/routing/dns")
    async def email_rule(self,z,addr,dest):return await self.req("POST",f"/zones/{z}/email/routing/rules",json={"actions":[{"type":"forward","value":[dest]}],"matchers":[{"type":"literal","field":"to","value":addr}],"enabled":True})
