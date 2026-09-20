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
        headers = {
            "Accept": "text/html",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(url, headers=headers)
            if r.status_code >= 400:
                cache[tld] = None
                return None
            text = html.unescape(re.sub(r"<[^>]+>", " ", r.text))
            text = re.sub(r"\s+", " ", text)
            # The page shows a crossed-out regular price followed by the
            # actual current price inside the "Register ... Renew" block,
            # e.g. "Register $15.53/yr $0.77/yr Renew $15.53/yr". Take the
            # LAST $/yr figure in that block so we get the price a user
            # would actually pay today, not the struck-through one.
            m = re.search(r"\bRegister\b(.*?)\bRenew\b", text, re.I | re.S)
            segment = m.group(1) if m else text
            prices = re.findall(r"(?:US\$|\$)\s*([0-9]+(?:\.[0-9]+)?)\s*/yr", segment, re.I)
            price = float(prices[-1]) if prices else None
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
