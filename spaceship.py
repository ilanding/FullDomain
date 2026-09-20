import httpx,asyncio,re,html
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

    async def regular_tld_price(self,tld):
        """Fetch the current regular first-year registration price from Spaceship's public TLD page.
        Premium/domain-specific prices continue to come from the availability API.
        """
        tld = str(tld or "").strip().lower().lstrip(".")
        if not tld:
            return None
        cache = getattr(self, "_tld_price_cache", None)
        if cache is None:
            cache = self._tld_price_cache = {}
        if tld in cache:
            return cache[tld]
        url = f"https://www.spaceship.com/domains/gtld/{tld}/"
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(url, headers={"Accept": "text/html"})
            if r.status_code >= 400:
                cache[tld] = None
                return None
            text = html.unescape(re.sub(r"<[^>]+>", " ", r.text))
            text = re.sub(r"\s+", " ", text)
            # Spaceship's public page uses either:
            #   Register Sale $28.46 $0.98 /yr  -> current sale price is $0.98
            #   Register $11.20 /yr             -> regular price is $11.20
            # Prefer the final price when a sale price is shown.
            m = re.search(
                r"\bRegister\s+Sale\s+(?:US\$|\$)\s*[0-9]+(?:\.[0-9]+)?\s*(?:US\$|\$)\s*([0-9]+(?:\.[0-9]+)?)\s*/yr",
                text, re.I,
            )
            if not m:
                m = re.search(
                    r"\bRegister\s+(?:US\$|\$)\s*([0-9]+(?:\.[0-9]+)?)\s*/yr",
                    text, re.I,
                )
            if not m:
                m = re.search(
                    r"\bRegister\s+Sale\s+([0-9]+(?:\.[0-9]+)?)\s+([0-9]+(?:\.[0-9]+)?)\s*/yr",
                    text, re.I,
                )
            price = float(m.group(1) if m and m.lastindex == 1 else m.group(2)) if m else None
            cache[tld] = price
            return price
        except (httpx.HTTPError, ValueError):
            cache[tld] = None
            return None
    async def register(self,domain,contacts,confirmation_token=None):
        body={"autoRenew":False,"years":1,"privacyProtection":{"level":"high","userConsent":True},"contacts":contacts}
        if confirmation_token:
            body["confirmationToken"]=confirmation_token; body["confirmationResponse"]="accept"
        return await self.req("POST",f"/domains/{domain}",json=body)
    async def operation(self,op): return (await self.req("GET",f"/async-operations/{op}")).json()
    async def nameservers(self,domain,hosts):
        return (await self.req("PUT",f"/domains/{domain}/nameservers",json={"provider":"custom","hosts":hosts})).json()
