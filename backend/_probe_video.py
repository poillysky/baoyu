import asyncio, json, sys
sys.path.insert(0,'/Volumes/vol1T/project/cams/baoyu/baoyu-py/backend')
import client as CL, stream, config as C

HTML = r"""
<!DOCTYPE html><html><head>
<script src="https://download.agora.io/sdk/release/AgoraRTC_N-4.20.0.js"></script>
</head><body><video id="v" autoplay muted playsinline></video>
<script>
window.__R={state:'init',video:0,audio:0,res:''};
(async()=>{
  const P=new URLSearchParams(location.search);
  const appid=P.get('appid'),ch=P.get('ch'),tok=P.get('tok'),uid=P.get('uid');
  const client=AgoraRTC.createClient({mode:'live',codec:'vp8'});
  client.on('user-published',async(u,mt)=>{
    try{await client.subscribe(u,mt);}catch(e){}
    if(mt==='video'){window.__R.video=1;window.__R.res=u.videoTrack.getMediaStreamTrack?('v'):'';u.videoTrack.play('v');}
    if(mt==='audio'){window.__R.audio=1;u.audioTrack.play();}
    window.__R.state='published-'+mt;
  });
  client.on('user-unpublished',(u,mt)=>{window.__R.state='unpub-'+mt;});
  try{await client.join(appid,ch,tok,uid===null?null:parseInt(uid));window.__R.state='joined';}
  catch(e){window.__R.state='join-fail:'+e;}
})();
</script></body></html>
"""
open('/Volumes/vol1T/project/cams/baoyu/baoyu-py/backend/_probe_video.html','w').write(HTML)

async def probe(channel,uid,seconds=10):
    from playwright.async_api import async_playwright
    appid="84437b2ceb75422ab1f7df9c2689241d"
    cred=stream.get_token(channel, user_id=uid)
    tok=cred["token"]
    url=f"file:///Volumes/vol1T/project/cams/baoyu/baoyu-py/backend/_probe_video.html?appid={appid}&ch={channel}&tok={tok}&uid={uid}"
    async with async_playwright() as p:
        br=await p.chromium.launch(executable_path=C.CHROMIUM,args=["--no-sandbox","--disable-gpu"])
        pg=await br.new_page()
        await pg.goto(url)
        last=None
        for i in range(seconds):
            await asyncio.sleep(1)
            r=await pg.evaluate("window.__R")
            last=r
            if r["video"]: break
        await br.close()
        return last

async def main():
    # 批量扫在线房
    targets=[("73677160","715903696","18柚子"),("91199373","715903696","嗯31996"),("10003764","715903696","气质雅雅4876")]
    for ch,uid,nm in targets:
        try:
            r=await probe(ch,uid,10)
            print(f"{nm} ch={ch}: state={r['state']} video={r['video']} audio={r['audio']}")
        except Exception as e:
            print(f"{nm} ch={ch}: ERR {e!r}")

asyncio.run(main())
