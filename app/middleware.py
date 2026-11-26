"""Bounded bodies, including requests without Content-Length."""
from starlette.responses import JSONResponse

class BodyLimitMiddleware:
    def __init__(self,app,max_bytes=1_000_000):
        self.app=app; self.max_bytes=max_bytes
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method') not in ('POST','PUT','PATCH'):
            return await self.app(scope,receive,send)
        buf=bytearray()
        while True:
            msg=await receive()
            if msg['type']=='http.disconnect': return
            chunk=msg.get('body',b'')
            if len(buf)+len(chunk)>self.max_bytes:
                return await JSONResponse({'detail':'요청 크기 제한 초과'},status_code=413)(scope,receive,send)
            buf.extend(chunk)
            if not msg.get('more_body',False): break
        body=bytes(buf); del buf; sent=False
        async def replay():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':body,'more_body':False}
            return await receive()
        await self.app(scope,replay,send)
