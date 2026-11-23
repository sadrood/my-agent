import json, os, sys, urllib.request, urllib.error, time
from dotenv import load_dotenv

load_dotenv()

# 令牌一律从 .env 读（绝不写进仓库）：NOVELFLOW_TOKEN。
# 端点与项目 id 也允许用环境变量覆盖，默认值仅为本机常用值。
BASE = os.getenv("NOVELFLOW_BASE", "http://127.0.0.1:8090").rstrip("/")
T = os.getenv("NOVELFLOW_TOKEN", "").strip()
PID = int(os.getenv("NOVELFLOW_PROJECT_ID", "19"))

if not T:
    sys.exit("缺少 NOVELFLOW_TOKEN：在 .env 里加一行 NOVELFLOW_TOKEN=<你的令牌> 再运行。")

def req(method, path, body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(BASE+path,data=data,method=method,
        headers={"Authorization":"Bearer "+T,"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(r,timeout=600) as f:
            data2=f.read().decode()
            try:
                return json.loads(data2)
            except Exception:
                return {"raw":data2}
    except urllib.error.HTTPError as e:
        try:
            return {"error":e.code,"body":e.read().decode()[:500]}
        except Exception:
            return {"error":e.code}
    except Exception as e:
        return {"error":type(e).__name__,"body":str(e)[:300]}

def run_status(run_id):
    d=req("GET",f"/api/runs/{run_id}")
    if isinstance(d,dict):
        if "run" in d and isinstance(d["run"],dict):
            return d["run"].get("status"), d["run"]
        return d.get("status"), d
    return None,None

def wait_run(run_id,label,max_wait=1800):
    t0=time.time()
    while time.time()-t0<max_wait:
        st,info=run_status(run_id)
        if st in ("done","completed","failed","error"):
            print(label,"run",run_id,"status",st,flush=True)
            return st,info
        time.sleep(5)
    print(label,"timeout",run_id,flush=True)
    return "timeout",None

# batch generate chapters 2-3
print("start batch 2-3",flush=True)
r=req("POST",f"/api/projects/{PID}/batch",{"from_idx":2,"to_idx":3})
print("batch response",json.dumps(r,ensure_ascii=False)[:500],flush=True)
run_id=None
if isinstance(r,dict):
    run_id=r.get("run_id") or (r.get("run") or {}).get("id") or (r.get("batch") or {}).get("run_id")
if run_id:
    st,info=wait_run(run_id,"batch")
    if info:
        print("run info",json.dumps(info,ensure_ascii=False)[:1000],flush=True)
else:
    # fallback: generate each chapter
    chapters=req("GET",f"/api/projects/{PID}/chapters")
    items=[]
    if isinstance(chapters,dict):
        items=chapters.get("chapters",[])
    for c in items:
        if c.get("idx") in (2,3) and c.get("word_count",0)==0:
            cid=c["id"]
            r=req("POST",f"/api/chapters/{cid}/generate",{})
            rid=r.get("run_id") if isinstance(r,dict) else None
            print("chapter",cid,"generate",json.dumps(r,ensure_ascii=False)[:200],flush=True)
            if rid:
                st,info=wait_run(rid,"chapter")
                if info:
                    print("run info",json.dumps(info,ensure_ascii=False)[:1000],flush=True)

# fetch chapters
chapters=req("GET",f"/api/projects/{PID}/chapters")
items=chapters.get("chapters",[]) if isinstance(chapters,dict) else []
for c in items:
    print("chapter idx",c.get("idx"),"id",c.get("id"),"status",c.get("status"),"wc",c.get("word_count"),"verdict",c.get("verdict"),flush=True)

# consistency check
r=req("POST",f"/api/projects/{PID}/consistency-check",{})
print("consistency response",json.dumps(r,ensure_ascii=False)[:1000],flush=True)
rid=r.get("run_id") if isinstance(r,dict) else None
if rid:
    st,info=wait_run(rid,"consistency")
    if info:
        print("consistency run",json.dumps(info,ensure_ascii=False)[:1000],flush=True)

# export
r=req("GET",f"/api/projects/{PID}/export")
if isinstance(r,dict) and "raw" in r:
    out=r["raw"]
else:
    out=json.dumps(r,ensure_ascii=False)
with open("/tmp/novel_export.txt","w",encoding="utf-8") as f:
    f.write(out)
print("export saved len",len(out),flush=True)

# also save full chapter content
with open("/tmp/novel_full.txt","w",encoding="utf-8") as f:
    for c in items:
        f.write(f"\n\n===== 第{c.get('idx')}章 {c.get('title')} =====\n\n")
        content=req("GET",f"/api/chapters/{c.get('id')}")
        if isinstance(content,dict):
            c2=content.get("chapter") or content
            f.write(c2.get("content",""))
print("full saved",flush=True)
