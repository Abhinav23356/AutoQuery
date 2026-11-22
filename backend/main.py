import hashlib, json, logging, os, secrets, time, uuid
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from openai import APIStatusError, APITimeoutError
import psycopg
from pwdlib import PasswordHash
from agent.mcp_client import MCPConnections
from agent.orchestrator import Agent
from datasets import UploadedDataset, MAX_UPLOAD_BYTES

# --- Logging and application configuration ---
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format='%(asctime)s %(levelname)s %(name)s %(message)s')
# AutoGen logs full prompts and tool results at INFO; keep user data out of logs.
logging.getLogger("autogen_core.events").setLevel(logging.WARNING)
log=logging.getLogger("detective.api")
app=FastAPI(title="AutoQuery API",version="1.0.0")
app.add_middleware(CORSMiddleware,allow_origins=os.getenv("CORS_ORIGINS","http://localhost:5173").split(","),allow_methods=["*"],allow_headers=["*"],allow_credentials=True)
agent=None
password_hash=PasswordHash.recommended()
SESSION_COOKIE="dd_session"
SESSION_DAYS=14


# --- Database access and session helpers ---
def app_db(): return psycopg.connect(os.environ["APP_DATABASE_URL"])
def _secure_cookie(): return os.getenv("SESSION_COOKIE_SECURE","false").lower() in {"1","true","yes"}
def _token_hash(token:str): return hashlib.sha256(token.encode("utf-8")).hexdigest()
def _new_session(user_id:str,response:Response):
 token=secrets.token_urlsafe(32); expires=datetime.now(timezone.utc)+timedelta(days=SESSION_DAYS)
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("DELETE FROM sessions WHERE expires_at<=now()")
  cur.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES (%s,%s,%s)",(_token_hash(token),user_id,expires))
 response.set_cookie(SESSION_COOKIE,token,max_age=SESSION_DAYS*24*60*60,httponly=True,secure=_secure_cookie(),samesite="lax",path="/api")

def current_user(request:Request):
 token=request.cookies.get(SESSION_COOKIE)
 if not token: raise HTTPException(401,"Sign in to continue.")
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("""SELECT u.id::text,u.email FROM sessions s JOIN users u ON u.id=s.user_id
    WHERE s.token_hash=%s AND s.expires_at>now()""",(_token_hash(token),))
  row=cur.fetchone()
 if not row: raise HTTPException(401,"Your session has expired. Sign in again.")
 return {"id":row[0],"email":row[1]}

def owned_conversation(conversation_id:str,user:dict):
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("SELECT id::text,owner_id::text,title,messages,updated_at FROM conversations WHERE id=%s AND owner_id=%s",(conversation_id,user["id"]))
  row=cur.fetchone()
 if not row: raise HTTPException(404,"Investigation not found.")
 return {"id":row[0],"owner_id":row[1],"title":row[2],"messages":row[3],"updated_at":row[4].isoformat()}

def persist_conversation(convo):
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("""INSERT INTO conversations(id,owner_id,title,messages,updated_at) VALUES (%s,%s,%s,%s::jsonb,%s)
    ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title,messages=EXCLUDED.messages,updated_at=EXCLUDED.updated_at
    WHERE conversations.owner_id=EXCLUDED.owner_id""",
    (convo["id"],convo["owner_id"],convo["title"],json.dumps(convo["messages"]),convo["updated_at"]))
  if cur.rowcount!=1: raise HTTPException(404,"Investigation not found.")

def load_dataset(conversation_id):
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("SELECT filename,csv_content FROM uploaded_datasets WHERE conversation_id=%s",(conversation_id,))
  row=cur.fetchone()
 if not row: return None
 return UploadedDataset.from_csv(row[0],bytes(row[1]))

def persist_execution(execution,create=False):
 with app_db() as conn, conn.cursor() as cur:
  if create:
   cur.execute("""INSERT INTO executions(id,conversation_id,owner_id,events,status)
     VALUES (%s,%s,%s,%s::jsonb,%s)""",(execution["id"],execution["conversation_id"],execution["owner_id"],json.dumps(execution["events"]),execution["status"]))
  else:
   cur.execute("""UPDATE executions SET events=%s::jsonb,status=%s,error=%s,duration_ms=%s
     WHERE id=%s AND owner_id=%s""",(json.dumps(execution["events"]),execution["status"],execution.get("error"),execution.get("duration_ms"),execution["id"],execution["owner_id"]))


# --- API request models ---
class ChatRequest(BaseModel): question:str=Field(min_length=1,max_length=4000); conversation_id:str|None=None
class Credentials(BaseModel): email:EmailStr; password:str=Field(min_length=12,max_length=128)


# --- Account registration and authentication routes ---
@app.post("/api/auth/register",status_code=201)
def register(payload:Credentials,response:Response):
 email=str(payload.email).strip().lower(); uid=str(uuid.uuid4()); encoded=password_hash.hash(payload.password)
 try:
  with app_db() as conn, conn.cursor() as cur:
   cur.execute("SELECT pg_advisory_xact_lock(781244)")
   cur.execute("SELECT count(*) FROM users")
   first_account=cur.fetchone()[0]==0
   cur.execute("INSERT INTO users(id,email,password_hash) VALUES (%s,%s,%s)",(uid,email,encoded))
   if first_account: cur.execute("UPDATE conversations SET owner_id=%s WHERE owner_id IS NULL",(uid,))
 except psycopg.errors.UniqueViolation:
  raise HTTPException(409,"An account with this email already exists.") from None
 _new_session(uid,response)
 return {"id":uid,"email":email}

@app.post("/api/auth/login")
def login(payload:Credentials,response:Response):
 email=str(payload.email).strip().lower()
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("SELECT id::text,password_hash FROM users WHERE email=%s",(email,)); row=cur.fetchone()
 valid=False
 if row:
  try: valid=password_hash.verify(payload.password,row[1])
  except Exception: valid=False
 if not valid: raise HTTPException(401,"Email or password is incorrect.")
 _new_session(row[0],response)
 return {"id":row[0],"email":email}

@app.get("/api/auth/me")
def auth_me(user:dict=Depends(current_user)): return user

@app.post("/api/auth/logout")
def logout(request:Request,response:Response):
 token=request.cookies.get(SESSION_COOKIE)
 if token:
  with app_db() as conn, conn.cursor() as cur: cur.execute("DELETE FROM sessions WHERE token_hash=%s",(_token_hash(token),))
 response.delete_cookie(SESSION_COOKIE,path="/api",httponly=True,secure=_secure_cookie(),samesite="lax")
 return {"ok":True}


# --- Application startup and health check ---
@app.on_event("startup")
def startup():
 global agent
 if os.getenv("GEMINI_API_KEY"): agent=Agent()

@app.get("/api/health")
def health(): return {"status":"ok","agent_ready":agent is not None,"mcp_servers":["chart-mcp-server"]}


# --- Conversation and execution routes ---
@app.get("/api/conversations")
def list_conversations(user:dict=Depends(current_user)):
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("""SELECT c.id::text,c.title,c.messages,c.updated_at,d.filename
    FROM conversations c LEFT JOIN uploaded_datasets d ON d.conversation_id=c.id
    WHERE c.owner_id=%s ORDER BY c.updated_at DESC""",(user["id"],))
  rows=cur.fetchall()
 return [{"id":cid,"title":title,"updated_at":updated_at.isoformat(),"dataset_name":filename,
   "recent_queries":[m["content"] for m in messages if m.get("role")=="user"][-5:]}
   for cid,title,messages,updated_at,filename in rows]

@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id:str,user:dict=Depends(current_user)):
 return owned_conversation(conversation_id,user)

@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id:str,user:dict=Depends(current_user)):
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("DELETE FROM conversations WHERE id=%s AND owner_id=%s",(conversation_id,user["id"]))
  if cur.rowcount!=1: raise HTTPException(404,"Investigation not found.")
 return {"deleted":True}

@app.get("/api/executions/{execution_id}")
def get_execution(execution_id:str,user:dict=Depends(current_user)):
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("""SELECT id::text,conversation_id::text,owner_id::text,events,status,error,duration_ms
    FROM executions WHERE id=%s AND owner_id=%s""",(execution_id,user["id"]))
  row=cur.fetchone()
 if not row: raise HTTPException(404,"Execution not found.")
 return {"id":row[0],"conversation_id":row[1],"owner_id":row[2],"events":row[3],"status":row[4],"error":row[5],"duration_ms":row[6]}


# --- Dataset upload and management routes ---
@app.post("/api/conversations/{conversation_id}/dataset")
async def upload_dataset(conversation_id:str,file:UploadFile=File(...),user:dict=Depends(current_user)):
 if not file.filename or not file.filename.lower().endswith(".csv"):
  raise HTTPException(415,"Upload a CSV file. Excel files are not supported yet.")
 content=await file.read(MAX_UPLOAD_BYTES+1)
 try:
  dataset=UploadedDataset.from_csv(file.filename,content)
  columns,row_count=dataset.columns,dataset.row_count
  dataset.connection.close()
 except ValueError as exc: raise HTTPException(400,str(exc)) from exc
 try:
  with app_db() as conn, conn.cursor() as cur:
   cur.execute("SELECT owner_id::text,messages FROM conversations WHERE id=%s FOR UPDATE",(conversation_id,))
   existing=cur.fetchone()
   if existing and existing[0]!=user["id"]: raise HTTPException(404,"Investigation not found.")
   if existing and existing[1]: raise HTTPException(409,"Upload this dataset to a new investigation so earlier data context is not reused.")
   if not existing:
    cur.execute("INSERT INTO conversations(id,owner_id,title,messages,updated_at) VALUES (%s,%s,%s,'[]'::jsonb,now())",(conversation_id,user["id"],file.filename))
   cur.execute("""INSERT INTO uploaded_datasets(conversation_id,filename,csv_content,columns,row_count,updated_at)
    VALUES (%s,%s,%s,%s::jsonb,%s,now()) ON CONFLICT(conversation_id) DO UPDATE SET
    filename=EXCLUDED.filename,csv_content=EXCLUDED.csv_content,columns=EXCLUDED.columns,row_count=EXCLUDED.row_count,updated_at=now()""",
    (conversation_id,file.filename,content,json.dumps(columns),row_count))
 except HTTPException: raise
 except Exception:
  log.exception("Could not persist uploaded dataset")
  raise HTTPException(503,"Could not store this dataset in PostgreSQL. Check the database connection.")
 return {"name":file.filename,"columns":columns,"row_count":row_count,"conversation_id":conversation_id}

@app.get("/api/conversations/{conversation_id}/dataset")
def get_dataset(conversation_id:str,user:dict=Depends(current_user)):
 owned_conversation(conversation_id,user)
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("SELECT filename,columns,row_count FROM uploaded_datasets WHERE conversation_id=%s",(conversation_id,)); row=cur.fetchone()
 if not row: raise HTTPException(404,"This chat has no uploaded dataset.")
 return {"name":row[0],"columns":row[1],"row_count":row[2],"conversation_id":conversation_id}

@app.delete("/api/conversations/{conversation_id}/dataset")
def remove_dataset(conversation_id:str,user:dict=Depends(current_user)):
 owned_conversation(conversation_id,user)
 with app_db() as conn, conn.cursor() as cur:
  cur.execute("DELETE FROM uploaded_datasets WHERE conversation_id=%s",(conversation_id,))
  removed=cur.rowcount>0
 return {"removed":removed}


# --- Chat analysis and execution lifecycle ---
@app.post("/api/chat")
async def chat(req:ChatRequest,user:dict=Depends(current_user)):
 if req.conversation_id:
  cid=req.conversation_id; convo=owned_conversation(cid,user)
 else:
  cid=str(uuid.uuid4()); convo={"id":cid,"owner_id":user["id"],"title":req.question[:60],"messages":[],"updated_at":datetime.now(timezone.utc).isoformat()}
 convo["updated_at"]=datetime.now(timezone.utc).isoformat()
 convo["messages"].append({"role":"user","content":req.question})
 if len(convo["messages"])==1: convo["title"]=req.question[:60]
 history=[{"role":m["role"],"content":m["content"]} for m in convo["messages"][:-1]]
 persist_conversation(convo)
 eid=str(uuid.uuid4()); events=[]
 execution={"id":eid,"conversation_id":cid,"owner_id":user["id"],"events":events,"status":"running"}
 persist_execution(execution,create=True)
 started=time.monotonic(); dataset=None
 async def event(e):
  events.append({**e,"timestamp":datetime.now(timezone.utc).isoformat()})
  persist_execution(execution)
 try:
  dataset=load_dataset(cid)
  if dataset is None:
   answer="Upload a CSV dataset to this investigation first. Each investigation can analyze only its own uploaded dataset."
   convo["messages"].append({"role":"assistant","content":answer,"charts":[]}); convo["updated_at"]=datetime.now(timezone.utc).isoformat()
   persist_conversation(convo)
   execution.update(status="completed",duration_ms=round((time.monotonic()-started)*1000)); persist_execution(execution)
   return {"conversation_id":cid,"execution_id":eid,"answer":answer,"charts":[],"events":events}
  if agent is None: raise HTTPException(503,"Set GEMINI_API_KEY from Google AI Studio in the backend environment to enable the agent.")
  async with MCPConnections() as connections: result=await agent.run(req.question,history,connections,event,dataset)
  convo["messages"].append({"role":"assistant","content":result["answer"],"charts":result["charts"],"query_results":result["query_results"],"execution_id":eid}); convo["updated_at"]=datetime.now(timezone.utc).isoformat()
  persist_conversation(convo)
  execution.update(status="completed",duration_ms=round((time.monotonic()-started)*1000)); persist_execution(execution)
  return {"conversation_id":cid,"execution_id":eid,**result,"events":events}
 except APITimeoutError as exc:
  detail="Gemini timed out after retries. Try again shortly."
  log.warning("Gemini request timed out: %s",exc)
  execution.update(status="failed",error=detail,duration_ms=round((time.monotonic()-started)*1000)); persist_execution(execution)
  await event({"tool":"agent","server":"Google Gemini","arguments":{},"status":"failed","summary":detail})
  raise HTTPException(504,detail)
 except APIStatusError as exc:
  status=503 if exc.status_code==503 else 429 if exc.status_code==429 else 502
  if status==503: detail="Gemini is temporarily overloaded. Wait a minute and retry."
  elif status==429: detail="Gemini quota or rate limit reached. Check the project limits in Google AI Studio."
  else: detail=f"Gemini rejected the request (HTTP {exc.status_code}). Check the API key, project, and model settings."
  log.warning("Gemini API returned HTTP %d",exc.status_code)
  execution.update(status="failed",error=detail,duration_ms=round((time.monotonic()-started)*1000)); persist_execution(execution)
  await event({"tool":"agent","server":"Google Gemini","arguments":{},"status":"failed","summary":detail})
  raise HTTPException(status,detail)
 except HTTPException as exc:
  execution.update(status="failed",error=str(exc.detail),duration_ms=round((time.monotonic()-started)*1000)); persist_execution(execution)
  raise
 except Exception as exc:
  log.exception("Chat execution failed")
  execution.update(status="failed",error=str(exc),duration_ms=round((time.monotonic()-started)*1000)); persist_execution(execution)
  await event({"tool":"agent","server":"backend","arguments":{},"status":"failed","summary":str(exc)[:300]})
  raise HTTPException(502,"The analysis could not be completed. Check API logs and MCP server availability.")
 finally:
  if dataset is not None: dataset.connection.close()
