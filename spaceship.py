import httpx,asyncio
class Spaceship:
    def __init__(self,cfg):
        self.base=cfg.sp_base; self.key=cfg.sp_key; self.secret=cfg.sp_secret
        if not self.key or not self.secret: raise RuntimeError("Spaceship API credentials missing")
    def h(self): return {"X-API-Key":self.key,"X-API-Secret":self.secret,"Content-Type":"application/json"}
    async def req(self,m,path,**kw):
        for n in range(4):
            try:
                async with httpx.AsyncClient(timeout=45) as c:r=await c.request(m,self.base+path,headers=self.h(),**kw)
                if r.status_code==429 or r.status_code>=500: await asyncio.sleep(2**n); continue
                if r.status_code>=400: raise RuntimeError(f"Spaceship {r.status_code}: {r.text[:500]}")
                return r
            except httpx.HTTPError:
                if n==3: raise
                await asyncio.sleep(2**n)
        raise RuntimeError("Spaceship request failed")
    async def availability(self,ds): return (await self.req("POST","/domains/available",json={"domains":ds})).json()
    async def register(self,domain,contacts,confirmation_token=None):
        body={"autoRenew":False,"years":1,"privacyProtection":{"level":"high","userConsent":True},"contacts":contacts}
        if confirmation_token:
            body["confirmationToken"]=confirmation_token; body["confirmationResponse"]="accept"
        return await self.req("POST",f"/domains/{domain}",json=body)
    async def operation(self,op): return (await self.req("GET",f"/async-operations/{op}")).json()
    async def nameservers(self,domain,hosts):
        return (await self.req("PUT",f"/domains/{domain}/nameservers",json={"provider":"custom","hosts":hosts})).json()
