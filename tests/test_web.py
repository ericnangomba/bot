import concurrent.futures
import http.client
import json
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from botteam.api import Server
from botteam.engine import Engine
from botteam.settings import read_config
from botteam.store import encode, event
from botteam.telegram import Telegram, Scope
from botteam.web import WebAccess

ROOT = Path(__file__).resolve().parents[1]
class Transport:
    def __init__(self): self.messages=[]
    def call(self, method, payload, upload=None, timeout=25):
        self.messages.append((method,payload,upload))
        return {"message_id":len(self.messages)}

class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.engine=Engine(read_config(ROOT/'bots.yaml',offline=True),self.tmp.name)
        self.client=Transport();self.telegram=Telegram(self.engine,self.client,[123,456])
        self.password=secrets.token_urlsafe(32)
        with patch.dict('os.environ',{},clear=True): self.server=Server(('127.0.0.1',0),secrets.token_urlsafe(32),self.engine,self.telegram)
        self.server.web=WebAccess(self.password,owner=123)
        self.port=self.server.server_address[1];self.origin=f'http://127.0.0.1:{self.port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.cleanup)
        self.cookie='';self.csrf=''
    def cleanup(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def req(self,path,body=None,headers=None,auth=True):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        h={}
        if auth: h.update({'Cookie':self.cookie,'X-CSRF-Token':self.csrf})
        if body is not None: h.update({'Origin':self.origin,'Content-Type':'application/json'})
        h.update(headers or {})
        conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,h)
        response=conn.getresponse();raw=response.read();status=response.status;rh=dict(response.getheaders());conn.close()
        try:data=json.loads(raw)
        except ValueError:data=raw.decode()
        return status,data,rh
    def login(self):
        status,_,headers=self.req('/ui/login',{'password':self.password});self.assertEqual(status,200)
        self.cookie=headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        status,data,_=self.req('/ui/session');self.assertEqual(status,200);self.csrf=data['csrf']
    def create(self,key=None,goal='Example safe goal'):
        return self.req('/ui/api/tasks',{'goal':goal,'attachments':{}},{'Idempotency-Key':key or secrets.token_hex(16)})
    def gate(self,task,tool='publish'):
        approval=secrets.token_hex(16)
        action={'tool':tool,'args':{'question':'Which date?'} if tool=='clarify' else {'source':'output.txt','destination':'reports'},'sha256':'f'*64}
        with self.engine.store.tx() as db:
            db.execute("INSERT INTO steps(task,id,capability,instruction,deps,state) VALUES(?,?,?,?,?,'blocked')",(task,'review','ops','Review','[]'))
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,'pending',NULL,100)",(approval,task,'review','digest',encode(action)))
            db.execute("UPDATE tasks SET status='needs_review' WHERE id=?",(task,))
            event(db,task,'approval_requested',{'approval':approval},'review')
        return approval
    def test_auth_csrf_host_and_no_secret_in_assets(self):
        self.assertEqual(self.req('/ui/api/tasks')[0],401)
        self.assertEqual(self.req('/ui/login',{'password':self.password},{'Origin':'https://evil.invalid'})[0],403)
        self.assertEqual(self.req('/ui/',headers={'Host':'evil.invalid'})[0],403)
        for path in ['/ui/','/ui/app.js','/ui/style.css']:
            status,data,headers=self.req(path);self.assertEqual(status,200);self.assertNotIn(self.password,data);self.assertNotIn(self.server.token,data)
            self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.login()
        self.assertEqual(self.req('/ui/api/tasks',{'goal':'x'},{'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(self.req('/ui/logout',{})[0],200)
        self.assertEqual(self.req('/ui/api/tasks')[0],401)
    def test_login_rate_limit_and_expiry(self):
        for _ in range(8):self.assertEqual(self.req('/ui/login',{'password':'wrong'})[0],401)
        self.assertEqual(self.req('/ui/login',{'password':self.password})[0],401)
        self.server.web.failures.clear();self.login()
        key=self.cookie.split('=',1)[1];self.server.web.sessions[key]=(0,self.csrf)
        self.assertEqual(self.req('/ui/session')[0],401)
    def test_durable_dedupe_and_changed_payload_conflict(self):
        self.login();key=secrets.token_hex(16)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.create(key),range(4)))
        ids={r[1]['id'] for r in results};self.assertEqual(len(ids),1)
        self.assertEqual(self.create(key,goal='Different content')[0],409)
        restarted=Engine(read_config(ROOT/'bots.yaml',offline=True),self.tmp.name)
        self.server.engine=restarted
        self.assertEqual(self.create(key)[1]['id'],ids.pop())
    def test_sessions_not_inherited_after_restart_and_workbench_optional(self):
        self.login()
        self.server.web=WebAccess(self.password,owner=123)
        self.assertEqual(self.req('/ui/session')[0],401)
        self.server.web=None
        self.assertEqual(self.req('/ui/')[0],404)

    def test_uploads_downloads_and_html_not_executed(self):
        import base64
        self.login()
        payload={'goal':'Review an untrusted file','attachments':{'note.html':base64.b64encode(b'<script>alert(1)</script>').decode()}}
        status,data,_=self.req('/ui/api/tasks',payload,{'Idempotency-Key':secrets.token_hex(16)})
        self.assertEqual(status,201)
        status,body,headers=self.req('/ui/api/tasks/'+data['id']+'/artifacts/attachments/note.html')
        self.assertEqual(status,200);self.assertEqual(headers['Content-Type'],'application/octet-stream')
        self.assertEqual(headers['Content-Disposition'],'attachment')

    def test_telegram_to_dashboard_and_dashboard_to_telegram(self):
        self.login()
        update={'update_id':77,'message':{'from':{'id':123},'chat':{'id':123,'type':'private'},'message_id':77,'text':'Goal from chat'}}
        self.telegram.handle(update);self.telegram.handle(update)
        rows=self.req('/ui/api/tasks')[1];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['goal'],'Goal from chat')
        task=self.create()[1]['id'];self.assertTrue(any(r['id']==task for r in self.telegram.lookup(Scope(123,123))))
        approval=self.gate(task);self.telegram.deliver_events()
        self.assertTrue(any('needs input' in p.get('text','') for _,p,_ in self.client.messages))
        self.assertEqual(self.req(f'/ui/api/approvals/{approval}/approve',{})[0],200)
        self.telegram.deliver_events()
        self.assertTrue(any('Decision recorded' in p.get('text','') for _,p,_ in self.client.messages))
        self.telegram.handle({'update_id':88,'callback_query':{'id':'stale','from':{'id':123},'data':'a:'+approval,'message':{'chat':{'id':123,'type':'private'}}}})
        self.assertTrue(any('no longer pending' in p.get('text','') for _,p,_ in self.client.messages))
        self.assertEqual(self.req(f'/ui/api/approvals/{approval}/reject',{})[0],409)
        view=self.req('/ui/api/tasks/'+task)[1];self.assertEqual(view['approvals'][0]['state'],'approved')
    def test_reject_from_chat_and_unauthorized_peer(self):
        self.login();task=self.create()[1]['id'];approval=self.gate(task)
        def cb(user):self.telegram.handle({'update_id':88,'callback_query':{'id':'cb','from':{'id':user},'data':'r:'+approval,'message':{'chat':{'id':user,'type':'private'}}}})
        cb(456);self.assertEqual(self.engine.task_view(task)['approvals'][0]['state'],'pending')
        cb(123);view=self.req('/ui/api/tasks/'+task)[1]
        self.assertEqual((view['status'],view['approvals'][0]['state']),('failed','rejected'))
        self.assertEqual(self.req(f'/ui/api/approvals/{approval}/approve',{})[0],409)
    def test_clarification_and_artifact_path_guard(self):
        self.login();task=self.create()[1]['id'];approval=self.gate(task,'clarify')
        self.assertEqual(self.req(f'/ui/api/approvals/{approval}/approve',{})[0],400)
        self.assertEqual(self.req(f'/ui/api/approvals/{approval}/approve',{'response':'Friday'})[0],200)
        self.assertEqual(self.req(f'/ui/api/tasks/{task}/artifacts/%2E%2E/%2E%2E/state.sqlite3')[0],400)
    def test_full_lifecycle_shared_gate_and_result_without_external_calls(self):
        class Model:
            offline = True
            def configured(self): return True
            def plan(self, *args):
                return {"steps": [
                    {"id":"write", "capability":"write", "instruction":"Write a safe report", "depends_on":[]},
                    {"id":"save", "capability":"ops", "instruction":"Save after approval", "depends_on":["write"]}]}
            def worker(self, task, profile, context):
                if context['step']=='write':
                    if not context['observations']:
                        return {"tool":"write", "args":{"path":"report.md", "content":"Fictional report checked."}}
                    return {"summary":"Report prepared", "artifacts":["steps/write/report.md"]}
                if not context['observations']:
                    return {"tool":"publish", "args":{"path":"steps/write/report.md"}}
                return {"summary":"Report saved after approval"}
            def finish(self, *args): return {"summary":"Fictional report complete", "passed":True}
        self.login();self.engine.model=Model()
        with patch.object(self.engine.tools,'sandbox_cleanup'):
            self.engine.start()
        self.addCleanup(self.engine.close)
        task=self.create()[1]['id']
        def wait(status):
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                view=self.req('/ui/api/tasks/'+task)[1]
                if view['status']==status:return view
                if view['status']=='failed':self.fail(str(view))
                time.sleep(.03)
            self.fail('Timed out waiting for '+status)
        view=wait('needs_review');approval=view['approvals'][0]['id']
        self.telegram.deliver_events()
        self.telegram.handle({'update_id':99,'callback_query':{'id':'ok','from':{'id':123},'data':'a:'+approval,'message':{'chat':{'id':123,'type':'private'}}}})
        view=wait('completed');self.assertEqual(view['summary'],'Fictional report complete')
        self.telegram.deliver_events()
        self.assertTrue(any('completed' in p.get('text','') for _,p,_ in self.client.messages))
        published=next(p for p in view['artifacts'] if p.startswith('published/'))
        status,content,_=self.req('/ui/api/tasks/'+task+'/artifacts/'+published)
        self.assertEqual((status,content),(200,'Fictional report checked.'))
        self.assertEqual(self.req('/ui/api/approvals/'+approval+'/approve',{})[0],409)

    def test_owner_validation_and_secure_cookie_configuration(self):
        with patch.dict('os.environ',{'BOTTEAM_WEB_PASSWORD':self.password,'BOTTEAM_WEB_ORIGIN':'https://example.test','BOTTEAM_TELEGRAM_OWNER_ID':'123'},clear=True):
            access=WebAccess.from_env(self.telegram);self.assertTrue(access.secure);self.assertEqual(access.owner,123)
        with patch.dict('os.environ',{'BOTTEAM_WEB_PASSWORD':self.password,'BOTTEAM_TELEGRAM_OWNER_ID':'999'},clear=True):
            self.assertRaises(ValueError,WebAccess.from_env,self.telegram)
        with patch.dict('os.environ',{'BOTTEAM_WEB_PASSWORD':self.password,'BOTTEAM_WEB_ORIGIN':'http://example.test'},clear=True):
            self.assertRaises(ValueError,WebAccess.from_env,self.telegram)

if __name__=='__main__':unittest.main()
