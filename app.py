from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from uuid import uuid4
from datetime import datetime, timezone
import os, json, urllib.request, urllib.error, psycopg
from psycopg.rows import dict_row

app=FastAPI(title='UNG-DOCS',version='1.0.0')
DB=os.getenv('DATABASE_URL','')
JANUS_BASE_URL=os.getenv('JANUS_BASE_URL','https://ung-iam-production.up.railway.app').rstrip('/')
def conn(): return psycopg.connect(DB,row_factory=dict_row)
def auth(permission,authorization):
    if not authorization or not authorization.lower().startswith('bearer '): raise HTTPException(401,'JANUS bearer token required')
    req=urllib.request.Request(JANUS_BASE_URL+'/v1/auth/introspect',data=b'',method='POST',headers={'Authorization':authorization})
    try:
        with urllib.request.urlopen(req,timeout=5) as r:data=json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401,403): raise HTTPException(401,'JANUS token invalid or expired')
        raise HTTPException(503,'JANUS authorization unavailable')
    except Exception: raise HTTPException(503,'JANUS authorization unavailable')
    principal=data.get('principal') or {}; perms=set(principal.get('permissions') or [])
    if permission not in perms and 'ung.admin' not in perms: raise HTTPException(403,f'Missing JANUS permission: {permission}')
    return principal

@app.on_event('startup')
def init():
    if DB:
        with conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS documents(id UUID PRIMARY KEY,title TEXT,document_type TEXT,classification TEXT,status TEXT,current_version INTEGER,owner TEXT,created_at TIMESTAMPTZ,updated_at TIMESTAMPTZ)')
            c.execute('CREATE TABLE IF NOT EXISTS document_versions(id UUID PRIMARY KEY,document_id UUID,version INTEGER,content TEXT,change_note TEXT,created_by TEXT,created_at TIMESTAMPTZ)')
            c.execute('CREATE TABLE IF NOT EXISTS document_approvals(id UUID PRIMARY KEY,document_id UUID,approver TEXT,status TEXT,comment TEXT,created_at TIMESTAMPTZ,decided_at TIMESTAMPTZ)')

class DocumentIn(BaseModel): title:str; document_type:str='general'; classification:str='internal'; owner:str='UNG-DOCS'; content:str=''
class VersionIn(BaseModel): content:str; change_note:str=''
class ApprovalIn(BaseModel): approver:str
class DecisionIn(BaseModel): comment:str=''

@app.get('/health')
def health(): return {'status':'ok','service':'UNG-DOCS','version':'1.0.0'}
@app.get('/ready')
def ready():
    try:
        with conn() as c:c.execute('SELECT 1')
        return {'status':'ready','database':'connected','janus':JANUS_BASE_URL}
    except Exception:return {'status':'degraded','database':'unavailable','janus':JANUS_BASE_URL}
@app.get('/v1/system')
def system(): return {'system_id':'UNG-DOCS','domain':'document-records-management','capabilities':['documents','version-control','classification','approvals','records-lifecycle','janus-auth']}
@app.get('/v1/documents')
def list_documents(authorization:str|None=Header(None)):
    auth('docs.documents.read',authorization)
    with conn() as c:return c.execute('SELECT * FROM documents ORDER BY updated_at DESC').fetchall()
@app.post('/v1/documents',status_code=201)
def create_document(b:DocumentIn,authorization:str|None=Header(None)):
    p=auth('docs.documents.write',authorization); now=datetime.now(timezone.utc); did=str(uuid4()); vid=str(uuid4()); actor=p.get('subject') or p.get('id') or b.owner
    with conn() as c:
        doc=c.execute('INSERT INTO documents VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',(did,b.title,b.document_type,b.classification,'draft',1,b.owner,now,now)).fetchone()
        c.execute('INSERT INTO document_versions VALUES(%s,%s,%s,%s,%s,%s,%s)',(vid,did,1,b.content,'Initial version',actor,now))
        return doc
@app.get('/v1/documents/{document_id}')
def get_document(document_id:str,authorization:str|None=Header(None)):
    auth('docs.documents.read',authorization)
    with conn() as c:
        doc=c.execute('SELECT * FROM documents WHERE id=%s',(document_id,)).fetchone()
        if not doc: raise HTTPException(404,'document_not_found')
        versions=c.execute('SELECT * FROM document_versions WHERE document_id=%s ORDER BY version DESC',(document_id,)).fetchall()
        approvals=c.execute('SELECT * FROM document_approvals WHERE document_id=%s ORDER BY created_at DESC',(document_id,)).fetchall()
        return {'document':doc,'versions':versions,'approvals':approvals}
@app.post('/v1/documents/{document_id}/versions',status_code=201)
def new_version(document_id:str,b:VersionIn,authorization:str|None=Header(None)):
    p=auth('docs.documents.write',authorization); now=datetime.now(timezone.utc); actor=p.get('subject') or p.get('id') or 'unknown'
    with conn() as c:
        doc=c.execute('SELECT * FROM documents WHERE id=%s',(document_id,)).fetchone()
        if not doc: raise HTTPException(404,'document_not_found')
        v=doc['current_version']+1
        row=c.execute('INSERT INTO document_versions VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',(str(uuid4()),document_id,v,b.content,b.change_note,actor,now)).fetchone()
        c.execute("UPDATE documents SET current_version=%s,status='draft',updated_at=%s WHERE id=%s",(v,now,document_id)); return row
@app.post('/v1/documents/{document_id}/submit')
def submit(document_id:str,b:ApprovalIn,authorization:str|None=Header(None)):
    auth('docs.documents.write',authorization); now=datetime.now(timezone.utc)
    with conn() as c:
        if not c.execute('SELECT id FROM documents WHERE id=%s',(document_id,)).fetchone(): raise HTTPException(404,'document_not_found')
        approval=c.execute('INSERT INTO document_approvals VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',(str(uuid4()),document_id,b.approver,'pending','',now,None)).fetchone()
        c.execute("UPDATE documents SET status='pending_approval',updated_at=%s WHERE id=%s",(now,document_id)); return approval
@app.post('/v1/approvals/{approval_id}/approve')
def approve(approval_id:str,b:DecisionIn,authorization:str|None=Header(None)):
    auth('docs.approvals.write',authorization); now=datetime.now(timezone.utc)
    with conn() as c:
        row=c.execute("UPDATE document_approvals SET status='approved',comment=%s,decided_at=%s WHERE id=%s RETURNING *",(b.comment,now,approval_id)).fetchone()
        if not row: raise HTTPException(404,'approval_not_found')
        c.execute("UPDATE documents SET status='approved',updated_at=%s WHERE id=%s",(now,row['document_id'])); return row
@app.post('/v1/approvals/{approval_id}/reject')
def reject(approval_id:str,b:DecisionIn,authorization:str|None=Header(None)):
    auth('docs.approvals.write',authorization); now=datetime.now(timezone.utc)
    with conn() as c:
        row=c.execute("UPDATE document_approvals SET status='rejected',comment=%s,decided_at=%s WHERE id=%s RETURNING *",(b.comment,now,approval_id)).fetchone()
        if not row: raise HTTPException(404,'approval_not_found')
        c.execute("UPDATE documents SET status='rejected',updated_at=%s WHERE id=%s",(now,row['document_id'])); return row
@app.get('/v1/summary')
def summary(authorization:str|None=Header(None)):
    auth('docs.documents.read',authorization)
    with conn() as c:return {'documents':c.execute('SELECT COUNT(*) n FROM documents').fetchone()['n'],'approved':c.execute("SELECT COUNT(*) n FROM documents WHERE status='approved'").fetchone()['n'],'pending':c.execute("SELECT COUNT(*) n FROM documents WHERE status='pending_approval'").fetchone()['n']}
